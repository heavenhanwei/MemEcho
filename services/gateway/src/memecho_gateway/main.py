from __future__ import annotations

import re
import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

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
from fastapi.responses import StreamingResponse

from . import __version__
from .config import Settings, get_settings
from .models import (
    AnalysisRequest,
    AnalysisResult,
    ArtifactMetadata,
    ArtifactsResponse,
    ChatRequest,
    Health,
    JobStatus,
    JobView,
    ParticipantCandidate,
    ParticipantResolution,
    ParticipantsCandidatesResponse,
    SessionCreate,
    SessionCreated,
    UploadComplete,
    UploadCreate,
    UploadCreated,
)
from .orchestrator import Orchestrator
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
from .store import MemoryStore, UploadRecord


settings = get_settings()
store = MemoryStore(settings.memecho_data_dir)

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
oss_client = AliyunOSSClient(settings, mock=is_mock)
dashscope_client = DashScopeClient(settings, mock=is_mock)
transcription_downloader = TranscriptionDownloader(settings, mock=is_mock)
orchestrator = Orchestrator(store, provider, oss_client, dashscope_client, transcription_downloader)
realtime_client_factory = BailianRealtimeClient


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.memecho_data_dir.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="memEcho Gateway",
    version=__version__,
    lifespan=lifespan,
)


def require_token(
    authorization: str | None = Header(default=None),
    current: Settings = Depends(get_settings),
) -> None:
    expected = f"Bearer {current.memecho_demo_token}"
    if current.memecho_demo_token and authorization != expected:
        raise HTTPException(status_code=401, detail="invalid gateway token")


@app.get("/v1/health", response_model=Health)
async def health() -> Health:
    return Health(status="ok", provider=settings.memecho_provider, version=__version__)


@app.post(
    "/v1/sessions",
    response_model=SessionCreated,
    dependencies=[Depends(require_token)],
)
async def create_session(payload: SessionCreate) -> SessionCreated:
    record = await store.create_session(payload)
    return SessionCreated(
        id=record.id, request_id=record.request_id, status="queued"
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
        return {"ok": True, "index": index}
    part_path.write_bytes(body)
    upload.chunks.add(index)
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
    for part in upload.directory.glob("*.part"):
        part.unlink(missing_ok=True)
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
    session_id: str, payload: ParticipantResolution, background: BackgroundTasks
) -> dict[str, bool]:
    session = store.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    session.participant_resolution = payload.model_dump()

    job = awaiting_identity_job(session_id)
    if job and job.id not in session.resume_scheduled_jobs:
        original_request = session.analysis_requests.get(job.id)
        if original_request is None:
            raise HTTPException(status_code=409, detail="analysis request is unavailable")
        session.resume_scheduled_jobs.add(job.id)
        background.add_task(
            orchestrator.run, job.id, session_id, original_request.copy()
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
    session_id: str, payload: AnalysisRequest, background: BackgroundTasks
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
    job = await store.create_job(session_id, payload.request_id)
    original_request = store.sessions[session_id].analysis_requests.setdefault(
        job.id, payload.model_dump()
    )
    if job.status == JobStatus.queued:
        background.add_task(
            orchestrator.run, job.id, session_id, original_request.copy()
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
async def chat(payload: ChatRequest) -> StreamingResponse:
    async def stream():
        text = await provider.chat(
            payload.question,
            {"result": payload.result, "evidence_ids": payload.evidence_ids},
        )
        for token in text:
            yield f"data: {json.dumps({'delta': token}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0)
        yield "data: {\"done\":true}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.websocket("/v1/sessions/{session_id}/live")
async def live_transcript(websocket: WebSocket, session_id: str, token: str):
    if token != settings.memecho_demo_token or session_id not in store.sessions:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    if settings.memecho_provider == "bailian":
        await _bailian_live_transcript(websocket)
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


async def _bailian_live_transcript(websocket: WebSocket) -> None:
    client = realtime_client_factory(settings)
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
