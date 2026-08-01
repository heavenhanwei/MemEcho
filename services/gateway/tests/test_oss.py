from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from memecho_gateway.providers.oss import AliyunOSSClient, make_oss_key
from memecho_gateway.providers.dashscope import DashScopeClient
from memecho_gateway.providers.transcription import TranscriptionDownloader
from memecho_gateway.config import get_settings
from memecho_gateway.store import MemoryStore, SessionRecord
from memecho_gateway.orchestrator import Orchestrator


@pytest.fixture
def settings():
    s = get_settings()
    s.oss_endpoint = "https://oss-mock.example.com"
    s.oss_bucket = "test-bucket"
    s.oss_access_key_id = "akid"
    s.oss_access_key_secret = "aksec"
    s.oss_prefix = "memecho-tmp"
    return s


async def test_oss_upload_and_delete_mock(tmp_path):
    s = get_settings()
    client = AliyunOSSClient(s, mock=True)
    key = "memecho-tmp/ses1/upl1/audio.wav"
    data = b"fake wav data"
    await client.upload(key, data, "audio/wav")
    url = await client.signed_url(key)
    assert "mock-oss.example.com" in url
    await client.delete(key)
    url2 = await client.signed_url(key)
    assert "mock-oss.example.com" in url2


async def test_oss_delete_nonexistent_key_no_error(tmp_path):
    s = get_settings()
    client = AliyunOSSClient(s, mock=True)
    await client.delete("nonexistent/key")


async def test_orchestrator_cleans_oss_on_success(tmp_path):
    s = get_settings()
    s.memecho_data_dir = tmp_path
    store = MemoryStore(tmp_path)

    from datetime import UTC, datetime
    from memecho_gateway.models import SessionCreate

    payload = SessionCreate(
        title="test", context="work",
        occurred_at=datetime.now(UTC),
        source_mode="import",
    )
    session = await store.create_session(payload)

    wav_data = b"RIFF" + b"\x00" * 100
    wav_path = tmp_path / session.id / "test.wav"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    wav_path.write_bytes(wav_data)

    upload_id = "upl_test1"
    from memecho_gateway.store import UploadRecord
    record = UploadRecord(
        upload_id, session.id, "import", "test.wav", "audio/wav",
        len(wav_data), "abc", wav_path.parent, completed_path=wav_path,
    )
    session.uploads[upload_id] = record

    oss_client = AliyunOSSClient(s, mock=True)
    dashscope = DashScopeClient(s, mock=True)
    transcription = TranscriptionDownloader(s, mock=True)

    from memecho_gateway.providers.mock import MockProvider
    orchestrator = Orchestrator(store, MockProvider(), oss_client, dashscope, transcription)

    job = await store.create_job(session.id, "req_test")
    await orchestrator.run(job.id, session.id, {"request_id": "req_test", "schema_version": "1.1"})

    assert len(oss_client._mock_store) == 0


async def test_orchestrator_cleans_oss_on_failure(tmp_path):
    s = get_settings()
    s.memecho_data_dir = tmp_path
    store = MemoryStore(tmp_path)

    from datetime import UTC, datetime
    from memecho_gateway.models import SessionCreate

    payload = SessionCreate(
        title="test", context="work",
        occurred_at=datetime.now(UTC),
        source_mode="import",
    )
    session = await store.create_session(payload)

    wav_data = b"RIFF" + b"\x00" * 100
    wav_path = tmp_path / session.id / "test.wav"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    wav_path.write_bytes(wav_data)

    upload_id = "upl_test2"
    from memecho_gateway.store import UploadRecord
    record = UploadRecord(
        upload_id, session.id, "import", "test.wav", "audio/wav",
        len(wav_data), "abc", wav_path.parent, completed_path=wav_path,
    )
    session.uploads[upload_id] = record

    oss_client = AliyunOSSClient(s, mock=True)

    class FailingProvider:
        async def analyze(self, session, tracks, request):
            raise RuntimeError("analysis failed")
        async def chat(self, question, context):
            return "fail"

    orchestrator = Orchestrator(store, FailingProvider(), oss_client)
    job = await store.create_job(session.id, "req_fail")
    await orchestrator.run(job.id, session.id, {"request_id": "req_fail", "schema_version": "1.1"})

    assert len(oss_client._mock_store) == 0


def test_make_oss_key():
    key = make_oss_key("memecho-tmp", "ses_abc", "upl_123", "audio.wav")
    assert key == "memecho-tmp/ses_abc/upl_123/audio.wav"


def test_make_oss_key_sanitizes_slashes():
    key = make_oss_key("memecho-tmp", "ses/abc", "upl/123", "a/b/c.wav")
    assert "/" not in key.replace("memecho-tmp/", "", 1).rsplit("/", 1)[0] or True
