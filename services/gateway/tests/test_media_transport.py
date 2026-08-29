"""Media Transport and async upstream task recovery tests.

Covers the Qoder C scope:
- transport selection / base64 cap / object store cleanup (unit)
- no-OSS direct binary upload end-to-end
- URL-only provider with no matching transport -> media_input_unsupported
- restart recovery resumes polling without resubmitting (idempotent submit)
- timeout keeps the upstream reference resumable and continues polling
- temporary media cleanup after completion
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from memecho_gateway import media, persistence, processing_details
from memecho_gateway.media import (
    BASE64_INLINE_MAX_BYTES,
    Base64InlineTransport,
    MediaInput,
    MediaInputUnsupportedError,
    MediaRequest,
    ObjectStoreTransport,
    compatible_media_inputs,
    default_transports,
    select_transport,
)
from memecho_gateway.models import (
    FILETRANS_CANONICAL_STEPS,
    FileTransPhase,
    JobStatus,
    PHASE_TO_CANONICAL_STEP,
    ProcessingStage,
    RECOVERABLE_FILETRANS_PHASES,
    SessionCreate,
)
from memecho_gateway.orchestrator import Orchestrator
from memecho_gateway.providers.mock import MockProvider
from memecho_gateway.store import MemoryStore, PersistentStore, UploadRecord


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class StubOSS:
    """Object store stub recording uploads and deletions."""

    def __init__(self):
        self.keys: list[str] = []
        self.deleted: list[str] = []

    async def upload_file(self, key, path, content_type):
        self.keys.append(key)
        return f"oss://bucket/{key}"

    async def signed_url(self, key, expires: int = 3600):
        return f"https://signed.example.invalid/{key}"

    async def delete(self, key):
        self.deleted.append(key)


def _fake_result() -> dict:
    return {
        "transcript": [
            {
                "speaker_id": "speaker_self",
                "start_ms": 0,
                "end_ms": 8000,
                "text": "第一句",
                "confidence": 0.9,
            },
            {
                "speaker_id": "speaker_2",
                "start_ms": 8000,
                "end_ms": 16000,
                "text": "第二句",
                "confidence": 0.9,
            },
        ],
        "language": "zh",
        "duration_ms": 16000,
    }


class BinaryUploadTranscription:
    """Provider that accepts a direct binary upload (no object storage)."""

    provider_id = "fake-direct"
    capability = "file_transcription"
    media_inputs = (MediaInput.binary_upload,)

    def __init__(self):
        self.submissions = 0
        self.resumes = 0
        self.received_bytes = b""

    async def download_with_media(self, prepared, *, on_phase=None, on_submitted=None):
        self.submissions += 1
        self.received_bytes += b"".join(prepared.data_loader())
        if on_submitted is not None:
            on_submitted("fake-task-direct-001")
        if on_phase:
            on_phase("submitting")
            on_phase("queued", task_reference="ft_***001", task_id="fake-task-direct-001")
            on_phase("polling", poll_attempts=1, next_poll_after_ms=100, elapsed_ms=50)
            on_phase("downloading", elapsed_ms=80)
            on_phase("normalizing", elapsed_ms=90)
            on_phase(
                "succeeded", elapsed_ms=100, sentence_count=2,
                language="zh", audio_duration_ms=16000,
            )
        return _fake_result()

    async def resume_with_phase(self, task_id, *, on_phase=None):
        self.resumes += 1
        if on_phase:
            on_phase("succeeded", elapsed_ms=10, sentence_count=2)
        return _fake_result()


class UrlOnlyTranscription:
    """Provider that only accepts a public URL."""

    provider_id = "fake-url-only"
    capability = "file_transcription"
    media_inputs = (MediaInput.public_url,)

    async def download_with_phase(self, url, *, on_phase=None, on_submitted=None):
        raise AssertionError("download_with_phase must not run without a URL transport")

    async def resume_with_phase(self, task_id, *, on_phase=None):
        raise AssertionError("resume must not run without a URL transport")


class UrlPhasedTranscription:
    """Happy-path URL provider with full phase emission."""

    provider_id = "fake-url"
    capability = "file_transcription"
    media_inputs = (MediaInput.public_url,)

    def __init__(self):
        self.submissions = 0

    async def download_with_phase(self, url, *, on_phase=None, on_submitted=None):
        self.submissions += 1
        if on_submitted is not None:
            on_submitted("fake-task-url-001")
        if on_phase:
            on_phase("submitting")
            on_phase("queued", task_reference="ft_***001", task_id="fake-task-url-001")
            on_phase("polling", poll_attempts=1, next_poll_after_ms=100, elapsed_ms=50)
            on_phase("downloading", elapsed_ms=80)
            on_phase("normalizing", elapsed_ms=90)
            on_phase(
                "succeeded", elapsed_ms=100, sentence_count=2,
                language="zh", audio_duration_ms=16000,
            )
        return _fake_result()


class InterruptibleTranscription:
    """Submits once, then dies; a resume path completes without resubmitting."""

    provider_id = "fake-interrupt"
    capability = "file_transcription"
    media_inputs = (MediaInput.public_url,)

    def __init__(self):
        self.submissions = 0
        self.resumes = 0

    async def download_with_phase(self, url, *, on_phase=None, on_submitted=None):
        self.submissions += 1
        if on_submitted is not None:
            on_submitted("task-int-001")
        if on_phase:
            on_phase("submitting")
            on_phase("queued", task_reference="ft_***001", task_id="task-int-001")
            on_phase("polling", poll_attempts=1, next_poll_after_ms=2000, elapsed_ms=200)
        raise KeyboardInterrupt("simulated gateway shutdown")

    async def resume_with_phase(self, task_id, *, on_phase=None):
        assert task_id == "task-int-001"
        self.resumes += 1
        if on_phase:
            on_phase("polling", poll_attempts=1, next_poll_after_ms=100, elapsed_ms=300)
            on_phase("downloading", elapsed_ms=400)
            on_phase("normalizing", elapsed_ms=450)
            on_phase(
                "succeeded", elapsed_ms=500, sentence_count=2,
                language="zh", audio_duration_ms=16000,
            )
        return _fake_result()


class TimingOutTranscription:
    """Submits once, times out while polling; later resumes the same task."""

    provider_id = "fake-timeout"
    capability = "file_transcription"
    media_inputs = (MediaInput.public_url,)

    def __init__(self):
        self.submissions = 0
        self.resumes = 0

    async def download_with_phase(self, url, *, on_phase=None, on_submitted=None):
        self.submissions += 1
        if on_submitted is not None:
            on_submitted("task-to-001")
        if on_phase:
            on_phase("submitting")
            on_phase("queued", task_reference="ft_***001", task_id="task-to-001")
            on_phase("polling", poll_attempts=1, next_poll_after_ms=100, elapsed_ms=50)
            on_phase("polling", poll_attempts=2, next_poll_after_ms=100, elapsed_ms=120)
            on_phase("timed_out", error_code="upstream_timeout", retryable=True)
        raise TimeoutError("polling window exhausted")

    async def resume_with_phase(self, task_id, *, on_phase=None):
        assert task_id == "task-to-001"
        self.resumes += 1
        if on_phase:
            on_phase("polling", poll_attempts=1, next_poll_after_ms=100, elapsed_ms=150)
            on_phase("downloading", elapsed_ms=200)
            on_phase("normalizing", elapsed_ms=210)
            on_phase(
                "succeeded", elapsed_ms=220, sentence_count=2,
                language="zh", audio_duration_ms=16000,
            )
        return _fake_result()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _session_with_wav(store, tmp_path: Path, upload_id: str = "upl_media"):
    session = await store.create_session(
        SessionCreate(
            title="transport",
            context="work",
            occurred_at=datetime.now(UTC),
            source_mode="mixed",
        )
    )
    wav_path = tmp_path / session.id / upload_id / "audio.wav"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    signal = 0.5 * np.sin(2 * np.pi * 220 * np.linspace(0, 2, 32000, endpoint=False))
    sf.write(wav_path, signal.astype(np.float32), 16000)
    upload = UploadRecord(
        upload_id,
        session.id,
        "mixed",
        "audio.wav",
        "audio/wav",
        wav_path.stat().st_size,
        "sha-media",
        wav_path.parent,
        completed_path=wav_path,
    )
    session.uploads[upload.id] = upload
    processing_details.mark_upload_completed(session, upload)
    if hasattr(store, "save_upload"):
        store.save_upload(upload)
        store.mark_upload_completed(upload)
    return session, wav_path


def _request(request_id: str) -> dict:
    return {"request_id": request_id, "schema_version": "1.1"}


# ---------------------------------------------------------------------------
# Unit tests: transport layer
# ---------------------------------------------------------------------------


def test_select_transport_prefers_provider_order():
    transports = default_transports(None)

    class PrefersBinary:
        media_inputs = (MediaInput.binary_upload, MediaInput.local_path)

    class PrefersUrl:
        media_inputs = (MediaInput.public_url, MediaInput.binary_upload)

    chosen = select_transport(PrefersBinary.media_inputs, transports)
    assert chosen.transport_id == "binary_upload"
    # public_url is unavailable without object storage -> falls through.
    chosen = select_transport(PrefersUrl.media_inputs, transports)
    assert chosen.transport_id == "binary_upload"
    assert select_transport((MediaInput.public_url,), transports) is None


def test_default_transports_without_oss_excludes_object_store():
    capabilities = {item.capability for item in default_transports(None)}
    assert capabilities == {
        MediaInput.binary_upload,
        MediaInput.local_path,
        MediaInput.base64_inline,
    }
    with_oss = {item.capability for item in default_transports(StubOSS())}
    assert MediaInput.public_url in with_oss


async def test_base64_inline_transport_enforces_cap(tmp_path):
    path = tmp_path / "small.bin"
    path.write_bytes(b"abc")
    transport = Base64InlineTransport(max_bytes=8)
    prepared = await transport.prepare(
        MediaRequest("ses_1", "upl_1", path, "small.bin", "application/octet-stream", 3)
    )
    assert prepared.base64_payload is not None

    oversized = MediaRequest(
        "ses_1", "upl_1", path, "small.bin", "application/octet-stream",
        BASE64_INLINE_MAX_BYTES + 1,
    )
    with pytest.raises(ValueError):
        await Base64InlineTransport().prepare(oversized)


def test_compatible_media_inputs_intersection_preserves_order():
    class First:
        media_inputs = (MediaInput.binary_upload, MediaInput.public_url)

    class Second:
        media_inputs = (MediaInput.public_url,)

    fallback = (MediaInput.public_url,)
    assert compatible_media_inputs([First(), Second()], fallback) == (
        MediaInput.public_url,
    )
    # Legacy clients without declarations use the fallback.
    assert compatible_media_inputs([object()], fallback) == fallback
    assert compatible_media_inputs([], fallback) == ()


def test_safe_error_code_maps_media_input_unsupported():
    exc = MediaInputUnsupportedError(
        "audio-pipeline", "file_transcription",
        (MediaInput.public_url,), (MediaInput.binary_upload,),
    )
    assert processing_details.safe_error_code(exc) == "media_input_unsupported"
    assert exc.error_code == "media_input_unsupported"


def test_canonical_steps_cover_ui_phases_without_renaming():
    assert PHASE_TO_CANONICAL_STEP[FileTransPhase.submitting] == "submitted"
    assert PHASE_TO_CANONICAL_STEP[FileTransPhase.queued] == "submitted"
    assert PHASE_TO_CANONICAL_STEP[FileTransPhase.normalizing] == "parsing"
    assert PHASE_TO_CANONICAL_STEP[FileTransPhase.succeeded] == "completed"
    assert RECOVERABLE_FILETRANS_PHASES == frozenset(
        {FileTransPhase.polling, FileTransPhase.timed_out}
    )
    assert set(FILETRANS_CANONICAL_STEPS) == {
        "submitted", "polling", "downloading", "parsing", "completed",
    }


async def test_object_store_transport_cleanup_deletes_key(tmp_path):
    stub = StubOSS()
    transport = ObjectStoreTransport(stub, prefix="memecho-tmp")
    path = tmp_path / "audio.wav"
    path.write_bytes(b"RIFF" + b"\x00" * 8)
    prepared = await transport.prepare(
        MediaRequest("ses_1", "upl_1", path, "audio.wav", "audio/wav", path.stat().st_size)
    )
    assert prepared.input_type == MediaInput.public_url
    assert prepared.url.startswith("https://signed.example.invalid/")
    assert stub.keys
    await transport.cleanup(prepared)
    assert stub.deleted == stub.keys


def test_upstream_task_store_round_trip_and_upsert(tmp_path):
    store = MemoryStore(tmp_path)
    record = {
        "job_id": "job_1",
        "capability": "file_transcription",
        "upload_id": "upl_1",
        "session_id": "ses_1",
        "provider": "bailian",
        "media_input": "public_url",
        "upstream_task_id": "task_a",
        "status": persistence.UPSTREAM_STATUS_SUBMITTED,
        "poll_count": 0,
        "next_poll_at": None,
        "last_error_code": None,
    }
    store.save_upstream_task(record)
    # A second submission callback for the same key must not duplicate.
    store.save_upstream_task({**record, "upstream_task_id": "task_a"})
    assert len(store.upstream_tasks) == 1

    store.update_upstream_task(
        "job_1", "file_transcription", "upl_1",
        {"status": persistence.UPSTREAM_STATUS_POLLING, "poll_count": 3},
    )
    loaded = store.get_upstream_task("job_1", "file_transcription", "upl_1")
    assert loaded["status"] == persistence.UPSTREAM_STATUS_POLLING
    assert loaded["poll_count"] == 3
    # None values are ignored, missing records are a no-op.
    store.update_upstream_task("job_1", "file_transcription", "upl_1", {"poll_count": None})
    assert store.get_upstream_task("job_1", "file_transcription", "upl_1")["poll_count"] == 3
    store.update_upstream_task("missing", "file_transcription", "upl_1", {"poll_count": 9})

    assert [task["upstream_task_id"] for task in store.resumable_upstream_tasks()] == ["task_a"]
    store.update_upstream_task(
        "job_1", "file_transcription", "upl_1",
        {"status": persistence.UPSTREAM_STATUS_COMPLETED},
    )
    assert store.resumable_upstream_tasks() == []


def test_upstream_task_persistence_round_trip(tmp_path):
    path = tmp_path / "upstream.db"
    persistence.init_db(path)
    # upstream_tasks.job_id is a real FK to jobs (cascade delete), so the
    # parent session/job must exist before an upstream task can be saved.
    persistence.save_session(
        path,
        "ses_p",
        "req_p",
        {
            "title": "Upstream Task Test",
            "context": "工作",
            "occurred_at": "2026-08-30T10:00:00+08:00",
            "source_mode": "microphone",
            "marks": [],
        },
    )
    persistence.save_job(
        path,
        "job_p",
        "ses_p",
        "req_p",
        JobStatus.queued,
        0,
        "等待处理",
    )
    record = {
        "job_id": "job_p",
        "capability": "file_transcription",
        "upload_id": "upl_p",
        "session_id": "ses_p",
        "provider": "bailian",
        "media_input": "public_url",
        "upstream_task_id": "task_p",
        "status": persistence.UPSTREAM_STATUS_SUBMITTED,
        "poll_count": 0,
        "next_poll_at": None,
        "last_error_code": None,
    }
    persistence.save_upstream_task(path, record)
    loaded = persistence.load_all_upstream_tasks(path)
    assert len(loaded) == 1
    assert loaded[0]["upstream_task_id"] == "task_p"

    persistence.update_upstream_task(
        path, "job_p", "file_transcription", "upl_p",
        {
            "status": persistence.UPSTREAM_STATUS_TIMEOUT,
            "poll_count": 5,
            "last_error_code": "upstream_timeout",
        },
    )
    reloaded = persistence.load_all_upstream_tasks(path)
    assert reloaded[0]["status"] == persistence.UPSTREAM_STATUS_TIMEOUT
    assert reloaded[0]["poll_count"] == 5
    assert reloaded[0]["last_error_code"] == "upstream_timeout"


# ---------------------------------------------------------------------------
# Integration: no-OSS direct binary upload
# ---------------------------------------------------------------------------


async def test_no_oss_direct_binary_upload(tmp_path):
    store = MemoryStore(tmp_path)
    session, wav_path = await _session_with_wav(store, tmp_path)
    wav_bytes = wav_path.read_bytes()
    fake = BinaryUploadTranscription()
    orchestrator = Orchestrator(store, MockProvider(), None, None, fake)

    job = await store.create_job(session.id, "req_direct")
    await orchestrator.run(job.id, session.id, _request("req_direct"))

    assert store.jobs[job.id].status == JobStatus.complete
    assert fake.submissions == 1
    assert fake.received_bytes == wav_bytes

    record = store.get_upstream_task(job.id, Orchestrator.FILETRANS_CAPABILITY, "upl_media")
    assert record["status"] == persistence.UPSTREAM_STATUS_COMPLETED
    assert record["provider"] == "fake-direct"
    assert record["media_input"] == "binary_upload"
    assert record["upstream_task_id"] == "fake-task-direct-001"

    details = processing_details.build_response(session)
    track = details.tracks[0]
    assert track.oss_status == ProcessingStage.skipped
    assert track.filetrans.status.value == "succeeded"
    assert track.filetrans.sentence_count == 2
    assert details.transcript_segments


# ---------------------------------------------------------------------------
# Integration: URL required but no transport available
# ---------------------------------------------------------------------------


async def test_url_only_provider_without_transport_fails_cleanly(tmp_path):
    store = MemoryStore(tmp_path)
    session, wav_path = await _session_with_wav(store, tmp_path)
    orchestrator = Orchestrator(store, MockProvider(), None, None, UrlOnlyTranscription())

    job = await store.create_job(session.id, "req_nourl")
    await orchestrator.run(job.id, session.id, _request("req_nourl"))

    finished = store.jobs[job.id]
    assert finished.status == JobStatus.failed
    assert finished.error_code == "media_input_unsupported"
    assert finished.retryable is True

    details = processing_details.build_response(session)
    track = details.tracks[0]
    for module in track.modules.values():
        assert module.status.value == "failed"
        assert module.error_code == "media_input_unsupported"
    # Must never masquerade as an upstream task failure.
    assert "upstream_task_failed" not in details.model_dump_json()


# ---------------------------------------------------------------------------
# Integration: restart recovery resumes polling without resubmitting
# ---------------------------------------------------------------------------


async def test_restart_recovers_polling_without_resubmit(tmp_path):
    db_path = tmp_path / "gateway.db"
    store1 = PersistentStore(tmp_path, db_path)
    await store1.initialize()
    session, wav_path = await _session_with_wav(store1, tmp_path, upload_id="upl_recover")
    fake = InterruptibleTranscription()
    orchestrator1 = Orchestrator(store1, MockProvider(), StubOSS(), None, fake)

    job = await store1.create_job(session.id, "req_recover")
    await orchestrator1.run(job.id, session.id, _request("req_recover"))
    assert fake.submissions == 1

    interrupted = store1.get_upstream_task(
        job.id, Orchestrator.FILETRANS_CAPABILITY, "upl_recover"
    )
    assert interrupted["upstream_task_id"] == "task-int-001"
    assert interrupted["status"] == persistence.UPSTREAM_STATUS_POLLING
    assert interrupted["poll_count"] == 1

    # Simulate a gateway shutdown while the job is still transcribing.
    await store1.update_job(job.id, JobStatus.transcribing, 20, "in flight")

    # "Restart": a fresh store over the same database.
    store2 = PersistentStore(tmp_path, db_path)
    await store2.initialize()
    assert any(info["id"] == job.id for info in store2.get_unfinished_jobs())
    tasks = store2.upstream_tasks_for_job(job.id)
    assert any(
        task["upstream_task_id"]
        and task["status"] in persistence.UPSTREAM_RESUMABLE_STATUSES
        for task in tasks
    )

    # The completed first run removed local media; a live interrupted job
    # would still have it, so restore the file for the resumed attempt.
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    signal = 0.5 * np.sin(2 * np.pi * 220 * np.linspace(0, 2, 32000, endpoint=False))
    sf.write(wav_path, signal.astype(np.float32), 16000)

    orchestrator2 = Orchestrator(store2, MockProvider(), StubOSS(), None, fake)
    await orchestrator2.run(job.id, session.id, _request("req_recover"))

    assert store2.jobs[job.id].status == JobStatus.complete
    assert fake.submissions == 1  # no duplicate billable submission
    assert fake.resumes == 1
    resumed = store2.get_upstream_task(
        job.id, Orchestrator.FILETRANS_CAPABILITY, "upl_recover"
    )
    assert resumed["status"] == persistence.UPSTREAM_STATUS_COMPLETED


# ---------------------------------------------------------------------------
# Integration: timeout keeps the reference resumable, retry continues polling
# ---------------------------------------------------------------------------


async def test_timeout_then_retry_continues_polling_same_task(tmp_path):
    store = MemoryStore(tmp_path)
    session, wav_path = await _session_with_wav(store, tmp_path, upload_id="upl_timeout")
    fake = TimingOutTranscription()
    orchestrator = Orchestrator(store, MockProvider(), StubOSS(), None, fake)

    job = await store.create_job(session.id, "req_timeout")
    await orchestrator.run(job.id, session.id, _request("req_timeout"))

    timed_out = store.get_upstream_task(
        job.id, Orchestrator.FILETRANS_CAPABILITY, "upl_timeout"
    )
    assert timed_out["status"] == persistence.UPSTREAM_STATUS_TIMEOUT
    assert timed_out["last_error_code"] == "upstream_timeout"
    details = processing_details.build_response(session)
    filetrans = details.tracks[0].filetrans
    assert filetrans.phase == FileTransPhase.timed_out
    assert filetrans.retryable is True
    assert filetrans.error_code == "upstream_timeout"

    # The media file survives a retryable failure (retention window); the
    # retry resumes polling the same upstream task id.
    if not wav_path.exists():
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        signal = 0.5 * np.sin(2 * np.pi * 220 * np.linspace(0, 2, 32000, endpoint=False))
        sf.write(wav_path, signal.astype(np.float32), 16000)
    await orchestrator.run(job.id, session.id, _request("req_timeout"))

    assert fake.submissions == 1
    assert fake.resumes == 1
    final = store.get_upstream_task(
        job.id, Orchestrator.FILETRANS_CAPABILITY, "upl_timeout"
    )
    assert final["status"] == persistence.UPSTREAM_STATUS_COMPLETED
    # The resumed attempt continues the persisted poll counter.
    assert final["poll_count"] > 2
    details = processing_details.build_response(session)
    assert details.tracks[0].filetrans.status.value == "succeeded"


# ---------------------------------------------------------------------------
# Integration: temporary media cleanup after completion
# ---------------------------------------------------------------------------


async def test_temp_media_cleanup_after_completion(tmp_path):
    store = MemoryStore(tmp_path)
    session, wav_path = await _session_with_wav(store, tmp_path, upload_id="upl_cleanup")
    stub = StubOSS()
    fake = UrlPhasedTranscription()
    orchestrator = Orchestrator(store, MockProvider(), stub, None, fake)

    job = await store.create_job(session.id, "req_cleanup")
    await orchestrator.run(job.id, session.id, _request("req_cleanup"))

    assert store.jobs[job.id].status == JobStatus.complete
    assert fake.submissions == 1
    # Every temporary object-store key is deleted after the job finishes.
    assert len(stub.keys) == 1
    assert stub.deleted == stub.keys
    # The local upload copy is removed once the session's jobs are complete.
    assert not wav_path.parent.exists()
