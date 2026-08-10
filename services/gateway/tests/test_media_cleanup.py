"""P1 lifecycle tests for gateway-local upload copies."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from memecho_gateway import media_cleanup
from memecho_gateway.models import JobStatus, SessionCreate
from memecho_gateway.orchestrator import Orchestrator
from memecho_gateway.providers.mock import MockProvider
from memecho_gateway.store import MemoryStore, UploadRecord


async def _session_with_upload(tmp_path: Path) -> tuple[MemoryStore, object, object, Path]:
    store = MemoryStore(tmp_path)
    session = await store.create_session(
        SessionCreate(
            title="cleanup",
            context="work",
            occurred_at=datetime.now(UTC),
            source_mode="mixed",
        )
    )
    directory = tmp_path / session.id / "upl_clean"
    directory.mkdir(parents=True, exist_ok=True)
    media = directory / "audio.webm"
    media.write_bytes(b"payload")
    upload = UploadRecord(
        "upl_clean",
        session.id,
        "mixed",
        "audio.webm",
        "audio/webm",
        media.stat().st_size,
        "sha",
        directory,
        completed_path=media,
    )
    session.uploads[upload.id] = upload
    return store, session, upload, directory


async def test_completed_session_media_is_removed(tmp_path):
    store, session, _upload, directory = await _session_with_upload(tmp_path)
    job = await store.create_job(session.id, "req_done")
    await store.update_job(job.id, JobStatus.complete, 100, "done")

    removed = media_cleanup.remove_session_media(store, session.id, 3600)

    assert removed == 1
    assert not directory.exists()


async def test_failed_job_media_is_retained_for_retry_window(tmp_path):
    store, session, _upload, directory = await _session_with_upload(tmp_path)
    job = await store.create_job(session.id, "req_fail")
    await store.update_job(job.id, JobStatus.failed, 40, "failed")

    assert media_cleanup.remove_session_media(store, session.id, 3600) == 0
    assert directory.exists()

    after_window = store.jobs[job.id].updated_at + timedelta(seconds=3601)
    assert media_cleanup.remove_session_media(store, session.id, 3600, now=after_window) == 1
    assert not directory.exists()


async def test_active_job_media_is_never_removed(tmp_path):
    store, session, _upload, directory = await _session_with_upload(tmp_path)
    job = await store.create_job(session.id, "req_active")
    await store.update_job(job.id, JobStatus.transcribing, 20, "working")

    far_future = store.jobs[job.id].updated_at + timedelta(days=30)
    assert (
        media_cleanup.remove_session_media(store, session.id, 60, now=far_future) == 0
    )
    assert directory.exists()


async def test_sweep_only_removes_expired_unknown_sessions(tmp_path):
    store = MemoryStore(tmp_path)
    now = datetime.now(UTC)

    live = await store.create_session(
        SessionCreate(
            title="live",
            context="work",
            occurred_at=now,
            source_mode="mixed",
        )
    )
    (tmp_path / live.id / "upl").mkdir(parents=True, exist_ok=True)

    stale = tmp_path / "ses_stale"
    (stale / "upl").mkdir(parents=True)
    fresh = tmp_path / "ses_fresh"
    (fresh / "upl").mkdir(parents=True)

    stale_stamp = (now - timedelta(hours=2)).timestamp()
    os.utime(stale, (stale_stamp, stale_stamp))
    fresh_stamp = (now - timedelta(minutes=5)).timestamp()
    os.utime(fresh, (fresh_stamp, fresh_stamp))

    removed = media_cleanup.sweep_expired_media(store, 3600, now=now)

    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()
    assert (tmp_path / live.id).exists()


async def test_orchestrator_cleans_local_media_after_success(tmp_path):
    store, session, _upload, directory = await _session_with_upload(tmp_path)
    session.participant_resolution = {
        "participants": [{"id": "speaker_self", "name": "我", "is_self": True}],
        "self_participant_id": "speaker_self",
        "identity_basis": "user_confirmed",
    }
    orchestrator = Orchestrator(
        store, MockProvider(), media_retention_seconds=3600
    )
    job = await store.create_job(session.id, "req_orch")

    await orchestrator.run(job.id, session.id, {"request_id": "req_orch"})

    assert store.jobs[job.id].status.value == "complete"
    assert not directory.exists()


async def test_orchestrator_keeps_media_for_failed_job(tmp_path):
    store, session, _upload, directory = await _session_with_upload(tmp_path)

    class BrokenProvider:
        async def analyze(self, session, tracks, request):
            raise RuntimeError("provider unavailable")

        async def chat(self, question, context):
            return ""

    orchestrator = Orchestrator(
        store, BrokenProvider(), media_retention_seconds=3600
    )
    job = await store.create_job(session.id, "req_broken")

    await orchestrator.run(job.id, session.id, {"request_id": "req_broken"})

    assert store.jobs[job.id].status.value == "failed"
    assert directory.exists()
