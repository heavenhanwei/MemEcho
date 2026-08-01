from __future__ import annotations

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
    AnalyzeRequest,
    ChatRequest,
    Health,
    JobView,
    ParticipantResolution,
    SessionCreate,
    SessionCreated,
    UploadComplete,
    UploadCreate,
    UploadCreated,
)
from .orchestrator import Orchestrator
from .providers import BailianProvider, MockProvider
from .store import MemoryStore, UploadRecord


settings = get_settings()
store = MemoryStore(settings.memecho_data_dir)
provider = BailianProvider(settings) if settings.memecho_provider == "bailian" else MockProvider()
orchestrator = Orchestrator(store, provider)


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
    record = UploadRecord(
        upload_id,
        session_id,
        payload.track,
        payload.file_name,
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
    body = await request.body()
    if len(body) > settings.chunk_size_bytes:
        raise HTTPException(status_code=413, detail="chunk too large")
    (upload.directory / f"{index:08d}.part").write_bytes(body)
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
    for part in upload.directory.glob("*.part"):
        part.unlink(missing_ok=True)
    return {"upload_id": upload_id, "path": f"asset://{upload_id}", "size": size}


@app.post(
    "/v1/sessions/{session_id}/participants/resolve",
    dependencies=[Depends(require_token)],
)
async def resolve_participants(
    session_id: str, payload: ParticipantResolution
) -> dict[str, bool]:
    session = store.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    session.participant_resolution = payload.model_dump()
    return {"ok": True}


@app.post(
    "/v1/sessions/{session_id}/analyze",
    response_model=JobView,
    dependencies=[Depends(require_token)],
)
async def analyze(
    session_id: str, payload: AnalyzeRequest, background: BackgroundTasks
) -> JobView:
    if session_id not in store.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    job = await store.create_job(session_id, payload.request_id)
    if job.status == "queued":
        background.add_task(
            orchestrator.run, job.id, session_id, payload.model_dump()
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
    dependencies=[Depends(require_token)],
)
async def get_result(session_id: str) -> dict:
    session = store.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    if not session.result:
        raise HTTPException(status_code=409, detail="result not ready")
    return session.result


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

