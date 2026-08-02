from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf

from memecho_gateway.config import get_settings
from memecho_gateway.models import SessionCreate
from memecho_gateway.orchestrator import Orchestrator
from memecho_gateway.providers.mock import MockProvider
from memecho_gateway.providers.oss import AliyunOSSClient
from memecho_gateway.store import MemoryStore, UploadRecord


async def test_real_file_upload_delegates_to_resumable_without_read_bytes(
    tmp_path, monkeypatch
):
    import oss2

    settings = get_settings()
    settings.oss_bucket = "bucket"
    settings.oss_multipart_threshold_bytes = 4
    settings.oss_part_size_bytes = 4
    client = AliyunOSSClient(settings, mock=False)
    source = tmp_path / "audio.wav"
    source.write_bytes(b"0123456789")
    bucket = object()
    calls = []

    def fake_resumable_upload(received_bucket, key, filename, **kwargs):
        calls.append((received_bucket, key, filename, kwargs))

    def forbidden_read_bytes(_self):
        raise AssertionError("whole-file read is forbidden")

    monkeypatch.setattr(client, "_bucket", lambda: bucket)
    monkeypatch.setattr(oss2, "resumable_upload", fake_resumable_upload)
    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)

    uri = await client.upload_file("prefix/audio.wav", source, "audio/wav")

    assert uri == "oss://bucket/prefix/audio.wav"
    assert calls[0][0] is bucket
    assert calls[0][1:3] == ("prefix/audio.wav", str(source))
    assert calls[0][3]["multipart_threshold"] == 4
    assert calls[0][3]["part_size"] == 4


async def test_orchestrator_uses_file_upload_instead_of_bytes(tmp_path):
    store = MemoryStore(tmp_path)
    session = await store.create_session(
        SessionCreate(
            title="streaming",
            context="work",
            occurred_at=datetime.now(UTC),
            source_mode="import",
        )
    )
    wav_path = tmp_path / session.id / "audio.wav"
    sf.write(wav_path, np.zeros(1600, dtype=np.float32), 16000)
    upload = UploadRecord(
        "upl_stream", session.id, "import", "audio.wav", "audio/wav",
        wav_path.stat().st_size, "abc", wav_path.parent, completed_path=wav_path,
    )
    session.uploads[upload.id] = upload

    class FileOnlyOSS:
        settings = SimpleNamespace(oss_prefix="memecho-test")

        def __init__(self):
            self.paths = []

        async def upload(self, *_args, **_kwargs):
            raise AssertionError("orchestrator must not use bytes upload")

        async def upload_file(self, key, path, content_type):
            self.paths.append((key, path, content_type))
            return f"oss://bucket/{key}"

        async def signed_url(self, key):
            return f"https://example.invalid/{key}"

        async def delete(self, _key):
            return None

    oss = FileOnlyOSS()
    orchestrator = Orchestrator(store, MockProvider(), oss)
    job = await store.create_job(session.id, "req_stream")
    await orchestrator.run(job.id, session.id, {"request_id": "req_stream"})

    assert oss.paths == [
        (f"memecho-test/{session.id}/upl_stream/audio.wav", wav_path, "audio/wav")
    ]
    assert store.jobs[job.id].status.value == "complete"


async def test_fun_asr_and_emotion_start_concurrently(tmp_path):
    started: set[str] = set()
    both_started = asyncio.Event()

    class ConcurrentDashScope:
        async def _call(self, name):
            started.add(name)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.5)
            return {"output": {"results": []}}

        async def submit_fun_asr(self, _url):
            return await self._call("fun_asr")

        async def submit_emotion(self, _url):
            return await self._call("emotion")

    orchestrator = Orchestrator(
        MemoryStore(tmp_path), object(), dashscope_client=ConcurrentDashScope()
    )
    result = await orchestrator._collect_remote_observations("https://example.invalid/audio")
    assert started == {"fun_asr", "emotion"}
    assert result["errors"] == []
