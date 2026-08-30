from __future__ import annotations

import logging
import re
import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import __version__, media_cleanup, persistence, processing_details, profile_config
from . import profiles as profile_registry
from .config import Settings, get_settings
from .credentials import build_default_credential_resolver
from .models import (
    AnalysisRequest,
    AnalysisResult,
    ArtifactMetadata,
    ArtifactsResponse,
    CapabilitiesResponse,
    ChatRequest,
    Health,
    JobStatus,
    JobView,
    ParticipantCandidate,
    ParticipantResolution,
    ParticipantsCandidatesResponse,
    ProcessingDetailsResponse,
    ProcessingStage,
    ProfileVerification,
    ProviderCapability,
    ProviderProfileCreate,
    ProviderProfileConfigStatus,
    ProviderProfileList,
    ProviderProfileUpdate,
    ProviderProfileView,
    SessionCreate,
    SessionCreated,
    UploadComplete,
    UploadCreate,
    UploadCreated,
)
from .orchestrator import Orchestrator, ProviderOverrides
from .providers import (
    AliyunOSSClient,
    BailianProvider,
    DashScopeClient,
    MockProvider,
    TranscriptionDownloader,
)
from .realtime import (
    BailianRealtimeClient,
    RealtimeConfigurationError,
    RealtimeUpstreamDisconnected,
)
from .store import MemoryStore, PersistentStore, UploadRecord


settings = get_settings()
store = PersistentStore(settings.memecho_data_dir, settings.memecho_db_path)

SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._-]")
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def safe_filename(name: str) -> str:
    """Return a cross-platform leaf name that is also safe on Windows."""
    leaf = name.replace("\\", "/").rsplit("/", 1)[-1]
    if not leaf or leaf in {".", ".."}:
        return "upload"
    leaf = SAFE_FILENAME_RE.sub("_", leaf.strip().replace(" ", "_"))
    leaf = leaf.replace("..", "_").rstrip(" .")[:255].rstrip(" .")
    if not leaf or leaf in {".", ".."}:
        return "upload"
    if leaf.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        leaf = f"_{leaf}"
    return leaf[:255].rstrip(" .") or "upload"


def awaiting_identity_job(session_id: str) -> JobView | None:
    jobs = (
        job
        for job in store.jobs.values()
        if job.session_id == session_id and job.status == JobStatus.awaiting_identity
    )
    return max(jobs, key=lambda job: job.updated_at, default=None)


is_mock = settings.memecho_provider != "bailian"
provider = BailianProvider(settings) if not is_mock else MockProvider()
# Object storage is an optional Media Transport, not a core dependency.
# Demo/mock mode keeps the in-memory store; real mode only enables it when
# fully configured, otherwise providers must accept direct transports.
_oss_configured = bool(
    settings.oss_endpoint
    and settings.oss_bucket
    and settings.oss_access_key_id
    and settings.oss_access_key_secret
)
oss_client = AliyunOSSClient(settings, mock=True) if is_mock else (
    AliyunOSSClient(settings) if _oss_configured else None
)
dashscope_client = DashScopeClient(settings, mock=is_mock)
transcription_downloader = TranscriptionDownloader(settings, mock=is_mock)
orchestrator = Orchestrator(
    store,
    provider,
    oss_client,
    dashscope_client,
    transcription_downloader,
    media_retention_seconds=settings.memecho_media_retention_seconds,
)
realtime_client_factory = BailianRealtimeClient
credential_resolver = build_default_credential_resolver()


log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.memecho_data_dir.mkdir(parents=True, exist_ok=True)
    # Initialize persistent store and load persisted state
    await store.initialize()
    profile_path = profile_config.config_path(settings.memecho_data_dir)
    try:
        if profile_path.exists():
            configured_profiles = profile_config.load(profile_path)
            configured_ids = {profile.id for profile in configured_profiles}
            for existing_id in list(store.profiles):
                if existing_id not in configured_ids and store.sessions_using_profile(existing_id) == 0:
                    store.delete_profile(existing_id)
            for profile in configured_profiles:
                store.save_profile(
                    profile.model_copy(
                        update={
                            "capabilities": profile_registry.capabilities_for(profile.provider)
                        }
                    )
                )
        else:
            profile_config.save(profile_path, list(store.profiles.values()))
    except Exception:
        log.exception("Failed to load editable provider profile configuration")
    # Recover interrupted jobs. Jobs holding a resumable upstream async task
    # reference continue polling the same upstream task id (a restart must
    # never resubmit a billable task); everything else is marked failed so it
    # can be retried.
    for job_info in store.get_unfinished_jobs():
        job_id = job_info["id"]
        session_id = job_info["session_id"]
        session = store.sessions.get(session_id)
        original_request = (
            session.analysis_requests.get(job_id) if session else None
        )
        resumable = session is not None and any(
            task.get("upstream_task_id")
            and task.get("status") in persistence.UPSTREAM_RESUMABLE_STATUSES
            for task in store.upstream_tasks_for_job(job_id)
        )
        if (
            resumable
            and original_request is not None
            and job_id not in session.resume_scheduled_jobs
        ):
            session.resume_scheduled_jobs.add(job_id)
            store.save_resume_scheduled_job(session_id, job_id)
            try:
                await store.update_job(
                    job_id,
                    JobStatus.transcribing,
                    job_info["progress"],
                    "Gateway 重启，恢复上游任务轮询",
                )
            except Exception:
                log.warning("Failed to mark job %s for upstream resume", job_id)
            asyncio.create_task(
                orchestrator.run(job_id, session_id, original_request.copy())
            )
            continue
        if session_id in store.sessions:
            try:
                await store.update_job(
                    job_id,
                    JobStatus.failed,
                    job_info["progress"],
                    "Gateway 重启，任务中断",
                    retryable=True,
                    error_code="gateway_restart",
                )
            except Exception:
                log.warning("Failed to mark job %s as restart-interrupted", job_id)
    try:
        media_cleanup.sweep_expired_media(
            store, settings.memecho_media_retention_seconds
        )
    except Exception:
        pass
    yield


app = FastAPI(
    title="memEcho Gateway",
    version=__version__,
    lifespan=lifespan,
)

allowed_origins = [
    origin.strip()
    for origin in settings.memecho_allowed_origins.split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type",
        "X-LLM-Text-Api-Key", "X-LLM-Text-Endpoint", "X-LLM-Text-Model",
        "X-LLM-Audio-Api-Key", "X-LLM-Audio-Endpoint", "X-LLM-Workspace-Id",
    ],
)


@app.middleware("http")
async def llm_config_middleware(request: Request, call_next):
    """Attach per-request LLM config from X-LLM-* headers."""
    request.state.llm_text_api_key = request.headers.get("X-LLM-Text-Api-Key", "")
    request.state.llm_text_endpoint = request.headers.get("X-LLM-Text-Endpoint", "")
    request.state.llm_text_model = request.headers.get("X-LLM-Text-Model", "")
    request.state.llm_audio_api_key = request.headers.get("X-LLM-Audio-Api-Key", "")
    request.state.llm_audio_endpoint = request.headers.get("X-LLM-Audio-Endpoint", "")
    request.state.llm_workspace_id = request.headers.get("X-LLM-Workspace-Id", "")
    response = await call_next(request)
    return response


def resolve_text_settings(request: Request) -> dict:
    """Effective text LLM settings: request headers > .env defaults."""
    return {
        "api_key": getattr(request.state, "llm_text_api_key", "") or settings.bailian_text_api_key,
        "base_url": getattr(request.state, "llm_text_endpoint", "") or settings.bailian_text_base_url,
        "model": getattr(request.state, "llm_text_model", "") or settings.bailian_text_model,
    }


def resolve_audio_settings(request: Request) -> dict:
    """Effective audio ASR settings: request headers > .env defaults."""
    return {
        "api_key": getattr(request.state, "llm_audio_api_key", "") or settings.bailian_audio_api_key,
        "base_url": getattr(request.state, "llm_audio_endpoint", "") or settings.bailian_audio_base_url,
        "workspace_id": getattr(request.state, "llm_workspace_id", "") or settings.bailian_workspace_id,
    }


def resolve_session_runtime(session) -> tuple[ProviderOverrides | None, object | None]:
    """Resolve provider overrides/adapter for a session.

    A session bound to a Provider Profile always resolves from that profile —
    it never falls back to the global env key, so realtime captions and the
    formal report share one account. Unbound sessions keep the compatibility
    path (X-LLM headers resolved by the caller, then env defaults).
    """
    profile_id = session.create.provider_profile_id
    if not profile_id:
        return None, None
    profile = store.profiles.get(profile_id)
    if profile is None:
        raise HTTPException(status_code=409, detail="profile_not_found")
    if profile.provider == "mock":
        return (
            ProviderOverrides(profile_id=profile_id, supports_audio=False),
            profile_registry.select_provider("mock", settings),
        )
    try:
        api_key = profile_registry.resolve_profile_secret(profile, credential_resolver)
    except profile_registry.ProfileCredentialError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    resolved = profile_registry.build_profile_overrides(profile, api_key, settings)
    overrides = ProviderOverrides(
        text_api_key=resolved["text_kwargs"].get("api_key", ""),
        text_endpoint=resolved["text_kwargs"].get("base_url", ""),
        text_model=resolved["text_kwargs"].get("model", ""),
        audio_api_key=resolved["audio_kwargs"].get("api_key", ""),
        audio_endpoint=resolved["audio_kwargs"].get("base_url", ""),
        workspace_id=resolved["audio_kwargs"].get("workspace_id", ""),
        profile_id=profile_id,
        supports_audio=resolved["supports_audio"],
    )
    return overrides, profile_registry.select_provider(profile.provider, settings)


def require_token(
    authorization: str | None = Header(default=None),
    current: Settings = Depends(get_settings),
) -> None:
    expected = f"Bearer {current.memecho_demo_token}"
    if current.memecho_demo_token and authorization != expected:
        raise HTTPException(status_code=401, detail="invalid gateway token")


@app.get("/v1/health", response_model=Health)
async def health() -> Health:
    return Health(
        status="ok",
        provider=settings.memecho_provider,
        version=__version__,
        protocol_version=1,
    )


class LlmTestRequest(BaseModel):
    kind: Literal["text", "audio"]


class LlmTestResponse(BaseModel):
    ok: bool
    error: str | None = None


@app.post("/v1/llm/test", response_model=LlmTestResponse, dependencies=[Depends(require_token)])
async def test_llm_connection(request: Request, payload: LlmTestRequest) -> LlmTestResponse:
    """Verify user-supplied LLM credentials with a minimal request."""
    if payload.kind == "text":
        resolved = resolve_text_settings(request)
        if not resolved["api_key"] or not resolved["base_url"]:
            return LlmTestResponse(ok=False, error="缺少 API Key 或 Endpoint")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{resolved['base_url'].rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {resolved['api_key']}"},
                    json={
                        "model": resolved["model"],
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                    },
                )
                if resp.status_code == 200:
                    return LlmTestResponse(ok=True)
                return LlmTestResponse(ok=False, error=f"上游返回 HTTP {resp.status_code}")
        except Exception as e:
            return LlmTestResponse(ok=False, error=f"连接失败: {type(e).__name__}")

    elif payload.kind == "audio":
        resolved = resolve_audio_settings(request)
        if not resolved["api_key"] or not resolved["base_url"]:
            return LlmTestResponse(ok=False, error="缺少 API Key 或 Endpoint")
        # Bill-free capability probe: querying the async task-status endpoint
        # with a synthetic task id validates credentials and endpoint
        # reachability without creating a paid transcription task. The
        # explicit paid end-to-end check lives in
        # scripts/filetrans_smoke.py and must be opted into manually.
        ok, error = await dashscope_client.probe_credentials(
            api_key=resolved["api_key"], base_url=resolved["base_url"],
        )
        return LlmTestResponse(ok=ok, error=error)

    return LlmTestResponse(ok=False, error=f"未知的 kind: {payload.kind}")


@app.get(
    "/v1/capabilities",
    response_model=CapabilitiesResponse,
    dependencies=[Depends(require_token)],
)
async def get_capabilities() -> CapabilitiesResponse:
    """Gateway and provider capability manifests."""
    return CapabilitiesResponse(
        provider=settings.memecho_provider,
        provider_kinds=list(profile_registry.PROVIDER_MANIFESTS.values()),
    )


@app.get(
    "/v1/provider-profiles",
    response_model=ProviderProfileList,
    dependencies=[Depends(require_token)],
)
async def list_provider_profiles() -> ProviderProfileList:
    return ProviderProfileList(
        profiles=sorted(store.profiles.values(), key=lambda item: item.created_at)
    )


@app.post(
    "/v1/provider-profiles",
    response_model=ProviderProfileView,
    status_code=201,
    dependencies=[Depends(require_token)],
)
async def create_provider_profile(payload: ProviderProfileCreate) -> ProviderProfileView:
    now = datetime.now(UTC)
    view = ProviderProfileView(
        id=f"prof_{uuid4().hex[:16]}",
        name=payload.name,
        provider=payload.provider,
        credential_ref=payload.credential_ref,
        text_base_url=payload.text_base_url,
        text_model=payload.text_model,
        audio_base_url=payload.audio_base_url,
        realtime_ws_url=payload.realtime_ws_url,
        realtime_model=payload.realtime_model,
        workspace_id=payload.workspace_id,
        capabilities=profile_registry.capabilities_for(payload.provider),
        created_at=now,
        updated_at=now,
    )
    store.save_profile(view)
    profile_config.save(
        profile_config.config_path(settings.memecho_data_dir),
        list(store.profiles.values()),
    )
    log.info(
        "Provider profile created profile_id=%s provider=%s",
        view.id,
        view.provider,
    )
    return view


@app.get(
    "/v1/provider-profiles/config",
    response_model=ProviderProfileConfigStatus,
    dependencies=[Depends(require_token)],
)
async def provider_profile_config_status() -> ProviderProfileConfigStatus:
    path = profile_config.config_path(settings.memecho_data_dir)
    if not path.exists():
        profile_config.save(path, list(store.profiles.values()))
    return ProviderProfileConfigStatus(path=str(path), profiles=len(store.profiles))


@app.post(
    "/v1/provider-profiles/config/reload",
    response_model=ProviderProfileConfigStatus,
    dependencies=[Depends(require_token)],
)
async def reload_provider_profile_config() -> ProviderProfileConfigStatus:
    path = profile_config.config_path(settings.memecho_data_dir)
    try:
        configured_profiles = profile_config.load(path)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="provider_profile_config_invalid") from exc
    configured_ids = {profile.id for profile in configured_profiles}
    blocked = [
        profile_id
        for profile_id in store.profiles
        if profile_id not in configured_ids and store.sessions_using_profile(profile_id) > 0
    ]
    if blocked:
        raise HTTPException(status_code=409, detail="provider_profile_in_use")
    for existing_id in list(store.profiles):
        if existing_id not in configured_ids:
            store.delete_profile(existing_id)
    for profile in configured_profiles:
        store.save_profile(
            profile.model_copy(
                update={"capabilities": profile_registry.capabilities_for(profile.provider)}
            )
        )
    return ProviderProfileConfigStatus(path=str(path), profiles=len(store.profiles))


@app.get(
    "/v1/provider-profiles/{profile_id}",
    response_model=ProviderProfileView,
    dependencies=[Depends(require_token)],
)
async def get_provider_profile(profile_id: str) -> ProviderProfileView:
    profile = store.profiles.get(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return profile


@app.patch(
    "/v1/provider-profiles/{profile_id}",
    response_model=ProviderProfileView,
    dependencies=[Depends(require_token)],
)
async def update_provider_profile(
    profile_id: str, payload: ProviderProfileUpdate
) -> ProviderProfileView:
    existing = store.profiles.get(profile_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="profile not found")
    data = existing.model_dump()
    data.update(payload.model_dump(exclude_unset=True))
    data["updated_at"] = datetime.now(UTC)
    data["capabilities"] = profile_registry.capabilities_for(existing.provider)
    view = ProviderProfileView(**data)
    store.save_profile(view)
    profile_config.save(
        profile_config.config_path(settings.memecho_data_dir),
        list(store.profiles.values()),
    )
    log.info("Provider profile updated profile_id=%s", profile_id)
    return view


@app.delete(
    "/v1/provider-profiles/{profile_id}",
    dependencies=[Depends(require_token)],
)
async def delete_provider_profile(profile_id: str) -> dict[str, bool]:
    if profile_id not in store.profiles:
        raise HTTPException(status_code=404, detail="profile not found")
    if store.sessions_using_profile(profile_id) > 0:
        raise HTTPException(status_code=409, detail="profile_in_use")
    store.delete_profile(profile_id)
    profile_config.save(
        profile_config.config_path(settings.memecho_data_dir),
        list(store.profiles.values()),
    )
    log.info("Provider profile deleted profile_id=%s", profile_id)
    return {"ok": True}


@app.post(
    "/v1/provider-profiles/{profile_id}/verify",
    response_model=ProfileVerification,
    dependencies=[Depends(require_token)],
)
async def verify_provider_profile(profile_id: str) -> ProfileVerification:
    """Bill-free credential and capability probe with stable error codes."""
    profile = store.profiles.get(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return await profile_registry.verify_profile(profile, credential_resolver, settings)


@app.post(
    "/v1/sessions",
    response_model=SessionCreated,
    dependencies=[Depends(require_token)],
)
async def create_session(payload: SessionCreate) -> SessionCreated:
    if payload.provider_profile_id and payload.provider_profile_id not in store.profiles:
        raise HTTPException(status_code=404, detail="provider profile not found")
    record = await store.create_session(payload)
    return SessionCreated(
        id=record.id,
        request_id=record.request_id,
        status="queued",
        provider_profile_id=payload.provider_profile_id,
    )


@app.post(
    "/v1/sessions/{session_id}/uploads",
    response_model=UploadCreated,
    dependencies=[Depends(require_token)],
)
async def create_upload(session_id: str, payload: UploadCreate) -> UploadCreated:
    session = store.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    upload_id = f"upl_{uuid4().hex[:16]}"
    directory = settings.memecho_data_dir / session_id / upload_id
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = safe_filename(payload.file_name)
    record = UploadRecord(
        upload_id,
        session_id,
        payload.track,
        safe_name,
        payload.mime_type,
        payload.size,
        payload.sha256.lower(),
        directory,
    )
    session.uploads[upload_id] = record
    store.save_upload(record)
    expected_chunks = (payload.size + settings.chunk_size_bytes - 1) // settings.chunk_size_bytes
    processing_details.set_upload(
        session, record, ProcessingStage.queued, expected_chunks
    )
    return UploadCreated(
        upload_id=upload_id,
        chunk_size=settings.chunk_size_bytes,
        received_chunks=[],
    )


@app.put(
    "/v1/sessions/{session_id}/uploads/{upload_id}/chunks/{index}",
    dependencies=[Depends(require_token)],
)
async def put_chunk(
    session_id: str, upload_id: str, index: int, request: Request
) -> dict[str, int | bool]:
    session = store.sessions.get(session_id)
    upload = session.uploads.get(upload_id) if session else None
    if not upload:
        raise HTTPException(status_code=404, detail="upload not found")
    if index < 0:
        raise HTTPException(status_code=422, detail="chunk index must be non-negative")
    expected_chunks = (upload.size + settings.chunk_size_bytes - 1) // settings.chunk_size_bytes
    if index >= expected_chunks:
        raise HTTPException(status_code=422, detail="chunk index exceeds expected upload size")

    chunk = bytearray()
    async for piece in request.stream():
        if len(chunk) + len(piece) > settings.chunk_size_bytes:
            raise HTTPException(status_code=413, detail="chunk too large")
        chunk.extend(piece)
    body = bytes(chunk)
    if not body:
        raise HTTPException(status_code=422, detail="chunk must not be empty")

    part_path = upload.directory / f"{index:08d}.part"
    if part_path.exists():
        # Idempotent: only identical bytes are accepted for a duplicate index
        if part_path.read_bytes() != body:
            raise HTTPException(status_code=409, detail="chunk content mismatch")
        upload.chunks.add(index)
        store.update_upload_chunks(upload)
        processing_details.upsert_track(session, upload)
        return {"ok": True, "index": index}
    part_path.write_bytes(body)
    upload.chunks.add(index)
    store.update_upload_chunks(upload)
    processing_details.upsert_track(session, upload)
    return {"ok": True, "index": index}


@app.post(
    "/v1/sessions/{session_id}/uploads/{upload_id}/complete",
    dependencies=[Depends(require_token)],
)
async def complete_upload(
    session_id: str, upload_id: str, payload: UploadComplete
) -> dict[str, str | int]:
    session = store.sessions.get(session_id)
    upload = session.uploads.get(upload_id) if session else None
    if not upload:
        raise HTTPException(status_code=404, detail="upload not found")
    if payload.upload_id != upload_id:
        raise HTTPException(status_code=422, detail="payload upload_id does not match url")
    if payload.sha256.lower() != upload.sha256:
        raise HTTPException(status_code=422, detail="payload sha256 does not match upload declaration")

    # Idempotent: if already completed, return existing result with sha256
    if upload.completed_path and upload.completed_path.exists():
        processing_details.mark_upload_completed(session, upload)
        return {
            "upload_id": upload_id,
            "path": f"asset://{upload_id}",
            "size": upload.completed_path.stat().st_size,
            "sha256": upload.sha256,
        }

    expected_chunks = (upload.size + settings.chunk_size_bytes - 1) // settings.chunk_size_bytes
    if upload.chunks != set(range(expected_chunks)):
        raise HTTPException(status_code=409, detail="upload is incomplete")

    target = upload.directory / upload.file_name
    digest = hashlib.sha256()
    size = 0
    with target.open("wb") as output:
        for part in sorted(upload.directory.glob("*.part")):
            data = part.read_bytes()
            output.write(data)
            digest.update(data)
            size += len(data)
    if digest.hexdigest() != payload.sha256.lower() or size != upload.size:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="upload checksum mismatch")
    upload.completed_path = target
    upload.sha256 = digest.hexdigest()
    store.mark_upload_completed(upload)
    for part in upload.directory.glob("*.part"):
        part.unlink(missing_ok=True)
    processing_details.mark_upload_completed(session, upload)
    return {
        "upload_id": upload_id,
        "path": f"asset://{upload_id}",
        "size": size,
        "sha256": digest.hexdigest(),
    }


@app.post(
    "/v1/sessions/{session_id}/participants/resolve",
    dependencies=[Depends(require_token)],
)
async def resolve_participants(
    session_id: str,
    payload: ParticipantResolution,
    background: BackgroundTasks,
    request: Request,
) -> dict[str, bool]:
    session = store.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    session.participant_resolution = payload.model_dump()
    store.save_participant_resolution(session_id, session.participant_resolution)

    job = awaiting_identity_job(session_id)
    if job and job.id not in session.resume_scheduled_jobs:
        original_request = session.analysis_requests.get(job.id)
        if original_request is None:
            raise HTTPException(status_code=409, detail="analysis request is unavailable")
        # The resumed job keeps the session's Profile binding; unbound
        # sessions keep the X-LLM header compatibility path.
        overrides, chosen_provider = resolve_session_runtime(session)
        if overrides is None:
            overrides = ProviderOverrides(
                text_api_key=getattr(request.state, "llm_text_api_key", ""),
                text_endpoint=getattr(request.state, "llm_text_endpoint", ""),
                text_model=getattr(request.state, "llm_text_model", ""),
                audio_api_key=getattr(request.state, "llm_audio_api_key", ""),
                audio_endpoint=getattr(request.state, "llm_audio_endpoint", ""),
                workspace_id=getattr(request.state, "llm_workspace_id", ""),
            )
        session.resume_scheduled_jobs.add(job.id)
        store.save_resume_scheduled_job(session_id, job.id)
        background.add_task(
            orchestrator.run,
            job.id,
            session_id,
            original_request.copy(),
            overrides,
            chosen_provider,
        )

    return {"ok": True}


@app.get(
    "/v1/sessions/{session_id}/participants/candidates",
    response_model=ParticipantsCandidatesResponse,
    dependencies=[Depends(require_token)],
)
async def get_participant_candidates(session_id: str) -> ParticipantsCandidatesResponse:
    session = store.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")

    candidates: list[ParticipantCandidate] = []
    job = awaiting_identity_job(session_id)
    aligned = session.job_intermediates.get(job.id, {}).get("aligned", []) if job else []
    speaker_stats: dict[str, dict[str, int]] = {}
    for segment in aligned:
        speaker_id = str(segment.get("speaker_id", "unknown"))
        stats = speaker_stats.setdefault(speaker_id, {"time_ms": 0, "count": 0})
        stats["time_ms"] += max(
            0, int(segment.get("end_ms", 0)) - int(segment.get("start_ms", 0))
        )
        stats["count"] += 1

    for speaker_id, stats in speaker_stats.items():
        candidates.append(
            ParticipantCandidate(
                participant_id=speaker_id,
                display_name=f"Speaker {speaker_id}",
                source="diarization",
                speaking_time_ms=stats["time_ms"],
                segment_count=stats["count"],
            )
        )

    return ParticipantsCandidatesResponse(candidates=candidates)


@app.post(
    "/v1/sessions/{session_id}/analyze",
    response_model=JobView,
    dependencies=[Depends(require_token)],
)
async def analyze(
    session_id: str, payload: AnalysisRequest, request: Request, background: BackgroundTasks
) -> JobView:
    if session_id not in store.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    session = store.sessions[session_id]
    if (
        payload.source is not None
        and payload.source.type in {"text", "transcript"}
        and session.uploads
    ):
        raise HTTPException(
            status_code=422,
            detail="text source cannot be combined with media uploads",
        )
    profile_overrides, profile_provider = resolve_session_runtime(session)
    if profile_overrides is not None:
        overrides = profile_overrides
        chosen_provider = profile_provider
    else:
        overrides = ProviderOverrides(
            text_api_key=getattr(request.state, "llm_text_api_key", ""),
            text_endpoint=getattr(request.state, "llm_text_endpoint", ""),
            text_model=getattr(request.state, "llm_text_model", ""),
            audio_api_key=getattr(request.state, "llm_audio_api_key", ""),
            audio_endpoint=getattr(request.state, "llm_audio_endpoint", ""),
            workspace_id=getattr(request.state, "llm_workspace_id", ""),
        )
        chosen_provider = None
    job = await store.create_job(session_id, payload.request_id)
    original_request = store.sessions[session_id].analysis_requests.setdefault(
        job.id, payload.model_dump()
    )
    store.save_analysis_request(job.id, original_request)
    if job.status == JobStatus.queued:
        background.add_task(
            orchestrator.run,
            job.id,
            session_id,
            original_request.copy(),
            overrides,
            chosen_provider,
        )
    return job


@app.get(
    "/v1/jobs/{job_id}",
    response_model=JobView,
    dependencies=[Depends(require_token)],
)
async def get_job(job_id: str) -> JobView:
    if job_id not in store.jobs:
        raise HTTPException(status_code=404, detail="job not found")
    return store.jobs[job_id]


@app.get("/v1/jobs/{job_id}/events", dependencies=[Depends(require_token)])
async def job_events(job_id: str) -> StreamingResponse:
    if job_id not in store.events:
        raise HTTPException(status_code=404, detail="job not found")

    async def stream():
        job = store.jobs[job_id]
        yield f"data: {json.dumps(job.model_dump(mode='json'), ensure_ascii=False)}\n\n"
        while job.status not in {"complete", "failed"}:
            event = await store.events[job_id].get()
            job = store.jobs[job_id]
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get(
    "/v1/sessions/{session_id}/result",
    response_model=AnalysisResult,
    dependencies=[Depends(require_token)],
)
async def get_result(session_id: str) -> AnalysisResult:
    session = store.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    if not session.result:
        raise HTTPException(status_code=409, detail="result not ready")
    return session.result


@app.get(
    "/v1/sessions/{session_id}/processing-details",
    response_model=ProcessingDetailsResponse,
    dependencies=[Depends(require_token)],
)
async def get_processing_details(session_id: str) -> ProcessingDetailsResponse:
    """Sanitized pipeline observability: no keys, signed URLs, vendor bodies,
    or absolute paths are ever included."""
    session = store.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    return processing_details.build_response(session)


@app.get(
    "/v1/sessions/{session_id}/artifacts",
    response_model=ArtifactsResponse,
    dependencies=[Depends(require_token)],
)
async def get_artifacts(session_id: str) -> ArtifactsResponse:
    session = store.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    if not session.result:
        raise HTTPException(status_code=409, detail="result not ready")

    result = session.result
    artifacts: dict[str, ArtifactMetadata] = {}
    contents: dict[str, str] = {}

    # JSON artifact (bare AnalysisResult without rendered fields)
    json_result = {
        key: value
        for key, value in result.items()
        if key not in {"rendered_markdown", "rendered_html"}
    }
    json_content = json.dumps(json_result, ensure_ascii=False)
    json_bytes = json_content.encode("utf-8")
    artifacts["json"] = ArtifactMetadata(
        type="json",
        content_type="application/json",
        size_bytes=len(json_bytes),
        sha256=hashlib.sha256(json_bytes).hexdigest(),
    )
    contents["json"] = json_content

    # Markdown artifact
    md_content = result.get("rendered_markdown", "")
    md_bytes = md_content.encode("utf-8")
    artifacts["markdown"] = ArtifactMetadata(
        type="markdown",
        content_type="text/markdown",
        size_bytes=len(md_bytes),
        sha256=hashlib.sha256(md_bytes).hexdigest(),
    )
    contents["markdown"] = md_content

    # HTML artifact
    html_content = result.get("rendered_html", "")
    html_bytes = html_content.encode("utf-8")
    artifacts["html"] = ArtifactMetadata(
        type="html",
        content_type="text/html",
        size_bytes=len(html_bytes),
        sha256=hashlib.sha256(html_bytes).hexdigest(),
    )
    contents["html"] = html_content

    return ArtifactsResponse(artifacts=artifacts, contents=contents)


@app.post("/v1/chat/stream", dependencies=[Depends(require_token)])
async def chat(payload: ChatRequest, request: Request) -> StreamingResponse:
    text_kwargs: dict[str, str] = {}
    api_key = getattr(request.state, "llm_text_api_key", "")
    endpoint = getattr(request.state, "llm_text_endpoint", "")
    model = getattr(request.state, "llm_text_model", "")
    if api_key:
        text_kwargs["api_key"] = api_key
    if endpoint:
        text_kwargs["base_url"] = endpoint
    if model:
        text_kwargs["model"] = model

    async def stream():
        text = await provider.chat(
            payload.question,
            {"result": payload.result, "evidence_ids": payload.evidence_ids},
            **text_kwargs,
        )
        for token in text:
            yield f"data: {json.dumps({'delta': token}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0)
        yield "data: {\"done\":true}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.websocket("/v1/sessions/{session_id}/live")
async def live_transcript(websocket: WebSocket, session_id: str, token: str):
    if (settings.memecho_demo_token and token != settings.memecho_demo_token) or session_id not in store.sessions:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    session = store.sessions[session_id]
    profile = (
        store.profiles.get(session.create.provider_profile_id)
        if session.create.provider_profile_id
        else None
    )
    if profile is not None:
        if not profile_registry.kind_supports(
            profile.provider, ProviderCapability.realtime_asr
        ):
            await _send_live_failure(
                websocket,
                code="realtime_configuration_error",
                message="Profile does not support realtime ASR.",
                retryable=False,
            )
            return
        if profile.provider == "bailian":
            try:
                api_key = profile_registry.resolve_profile_secret(
                    profile, credential_resolver
                )
            except profile_registry.ProfileCredentialError:
                await _send_live_failure(
                    websocket,
                    code="realtime_configuration_error",
                    message="credential_unresolved",
                    retryable=False,
                )
                return
            realtime_settings = profile_registry.realtime_settings_for(
                profile, api_key, settings
            )
            await _bailian_live_transcript(websocket, realtime_settings)
            return
        # mock profile: fall through to the mock caption loop below.
    elif settings.memecho_provider == "bailian":
        await _bailian_live_transcript(websocket, settings)
        return
    await websocket.send_json({"type": "connection.state", "state": "connected"})
    bytes_seen = 0
    try:
        while True:
            message = await websocket.receive()
            if chunk := message.get("bytes"):
                bytes_seen += len(chunk)
                if settings.memecho_provider == "mock" and bytes_seen // 64000 > (
                    bytes_seen - len(chunk)
                ) // 64000:
                    await websocket.send_json(
                        {
                            "type": "transcript.partial",
                            "text": "临时字幕正在形成……",
                            "at_ms": int(bytes_seen / 32),
                        }
                    )
            elif message.get("text") == "end":
                await websocket.send_json(
                    {"type": "connection.state", "state": "offline"}
                )
                await websocket.close()
                return
    except WebSocketDisconnect:
        return


async def _send_live_failure(
    websocket: WebSocket,
    *,
    code: str,
    message: str,
    retryable: bool,
) -> None:
    await websocket.send_json(
        {"type": "error", "code": code, "message": message, "retryable": retryable}
    )
    await websocket.send_json({"type": "connection.state", "state": "offline"})


async def _bailian_live_transcript(
    websocket: WebSocket, realtime_settings: Settings | None = None
) -> None:
    client = realtime_client_factory(realtime_settings or settings)
    desktop_task: asyncio.Task | None = None
    upstream_task: asyncio.Task | None = None
    try:
        try:
            await client.start()
        except RealtimeConfigurationError as exc:
            await _send_live_failure(
                websocket,
                code="realtime_configuration_error",
                message=str(exc),
                retryable=False,
            )
            return
        except Exception:
            await _send_live_failure(
                websocket,
                code="upstream_connect_failed",
                message="Realtime transcription service is unavailable.",
                retryable=True,
            )
            return

        desktop_task = asyncio.create_task(websocket.receive())
        upstream_task = asyncio.create_task(client.receive_event())
        finishing = False

        while True:
            pending = {task for task in (desktop_task, upstream_task) if task}
            timeout = (
                settings.bailian_realtime_finish_timeout_seconds if finishing else None
            )
            done, _ = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
                timeout=timeout,
            )
            if not done:
                await _send_live_failure(
                    websocket,
                    code="upstream_finish_timeout",
                    message="Realtime transcription did not finish in time.",
                    retryable=True,
                )
                return

            if upstream_task in done:
                event = upstream_task.result()
                if event is not None:
                    await websocket.send_json(event)
                    if (
                        event.get("type") == "connection.state"
                        and event.get("state") == "offline"
                    ):
                        return
                    if (
                        event.get("type") == "error"
                        and event.get("code") == "upstream_disconnected"
                    ):
                        await websocket.send_json(
                            {"type": "connection.state", "state": "offline"}
                        )
                        return
                upstream_task = asyncio.create_task(client.receive_event())

            if desktop_task in done:
                message = desktop_task.result()
                if message.get("type") == "websocket.disconnect":
                    return
                chunk = message.get("bytes")
                if chunk is not None:
                    if len(chunk) > settings.bailian_realtime_max_frame_bytes:
                        await _send_live_failure(
                            websocket,
                            code="audio_frame_too_large",
                            message="The audio frame exceeds the gateway limit.",
                            retryable=False,
                        )
                        return
                    try:
                        await client.send_audio(chunk)
                    except RealtimeUpstreamDisconnected:
                        await _send_live_failure(
                            websocket,
                            code="upstream_disconnected",
                            message="Realtime transcription disconnected; retry it.",
                            retryable=True,
                        )
                        return
                    desktop_task = asyncio.create_task(websocket.receive())
                elif message.get("text") == "end":
                    try:
                        await client.finish()
                    except RealtimeUpstreamDisconnected:
                        await _send_live_failure(
                            websocket,
                            code="upstream_disconnected",
                            message="Realtime transcription disconnected; retry it.",
                            retryable=True,
                        )
                        return
                    finishing = True
                    desktop_task = None
                else:
                    desktop_task = asyncio.create_task(websocket.receive())
    except WebSocketDisconnect:
        return
    finally:
        tasks = tuple(task for task in (desktop_task, upstream_task) if task)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()
