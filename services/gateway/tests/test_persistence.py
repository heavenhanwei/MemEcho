"""Tests for persistent store with SQLite backend.

Tests cover:
- Initialization and schema creation
- Session persistence and recovery
- Job persistence and recovery
- Upload persistence and recovery
- Idempotency persistence
- Analysis request persistence
- Job intermediate persistence
- Processing state persistence
- Restart recovery (unfinished jobs)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from memecho_gateway.models import JobStatus, SessionCreate
from memecho_gateway.persistence import (
    delete_session,
    get_unfinished_jobs,
    init_db,
    load_all_jobs,
    load_all_sessions,
    load_all_uploads,
    load_analysis_requests,
    load_idempotency,
    load_job_intermediates,
    load_resume_scheduled_jobs,
    save_analysis_request,
    save_idempotency,
    save_job,
    save_job_intermediate,
    save_resume_scheduled_job,
    save_session,
    save_upload,
    update_job_status,
    update_session_participant_resolution,
    update_session_processing,
    update_session_result,
    update_upload_chunks,
    update_upload_completed,
)
from memecho_gateway.store import PersistentStore, UploadRecord


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def store(tmp_path):
    return PersistentStore(tmp_path, tmp_path / "gateway.db")


def test_init_db_creates_schema(db_path):
    init_db(db_path)
    assert db_path.exists()
    # Verify tables exist by trying to query them
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}
    conn.close()
    assert "sessions" in tables
    assert "jobs" in tables
    assert "uploads" in tables
    assert "idempotency" in tables
    assert "analysis_requests" in tables
    assert "job_intermediates" in tables
    assert "resume_scheduled_jobs" in tables
    assert "schema_version" in tables


def test_save_and_load_session(db_path):
    init_db(db_path)
    session_id = "ses_test123"
    request_id = "req_test456"
    create_data = {
        "title": "Test Session",
        "context": "work",
        "occurred_at": "2026-07-30T10:00:00+08:00",
        "source_mode": "microphone",
        "marks": [],
    }
    save_session(db_path, session_id, request_id, create_data)
    sessions = load_all_sessions(db_path)
    assert session_id in sessions
    assert sessions[session_id]["title"] == "Test Session"
    assert sessions[session_id]["request_id"] == request_id


def test_save_and_load_job(db_path):
    init_db(db_path)
    # First save a session
    save_session(
        db_path,
        "ses_test",
        "req_test",
        {
            "title": "Test",
            "context": "work",
            "occurred_at": "2026-07-30T10:00:00+08:00",
            "source_mode": "import",
            "marks": [],
        },
    )
    # Then save a job
    now = datetime.now(UTC)
    save_job(
        db_path,
        "job_test",
        "ses_test",
        "req_test",
        JobStatus.queued,
        2,
        "等待处理",
        created_at=now,
        updated_at=now,
    )
    jobs = load_all_jobs(db_path)
    assert "job_test" in jobs
    assert jobs["job_test"]["status"] == "queued"
    assert jobs["job_test"]["progress"] == 2


def test_save_and_load_upload(db_path):
    init_db(db_path)
    save_session(
        db_path,
        "ses_test",
        "req_test",
        {
            "title": "Test",
            "context": "work",
            "occurred_at": "2026-07-30T10:00:00+08:00",
            "source_mode": "import",
            "marks": [],
        },
    )
    save_upload(
        db_path,
        "upl_test",
        "ses_test",
        "microphone",
        "test.wav",
        "audio/wav",
        1024,
        "abc123",
        "/tmp/test",
        {0, 1, 2},
    )
    uploads = load_all_uploads(db_path)
    assert "upl_test" in uploads
    assert uploads["upl_test"]["session_id"] == "ses_test"
    assert uploads["upl_test"]["chunks"] == {0, 1, 2}


def test_update_upload_chunks(db_path):
    init_db(db_path)
    save_session(
        db_path,
        "ses_test",
        "req_test",
        {
            "title": "Test",
            "context": "work",
            "occurred_at": "2026-07-30T10:00:00+08:00",
            "source_mode": "import",
            "marks": [],
        },
    )
    save_upload(
        db_path,
        "upl_test",
        "ses_test",
        "microphone",
        "test.wav",
        "audio/wav",
        1024,
        "abc123",
        "/tmp/test",
    )
    update_upload_chunks(db_path, "upl_test", {0, 1})
    uploads = load_all_uploads(db_path)
    assert uploads["upl_test"]["chunks"] == {0, 1}


def test_update_upload_completed(db_path):
    init_db(db_path)
    save_session(
        db_path,
        "ses_test",
        "req_test",
        {
            "title": "Test",
            "context": "work",
            "occurred_at": "2026-07-30T10:00:00+08:00",
            "source_mode": "import",
            "marks": [],
        },
    )
    save_upload(
        db_path,
        "upl_test",
        "ses_test",
        "microphone",
        "test.wav",
        "audio/wav",
        1024,
        "abc123",
        "/tmp/test",
    )
    update_upload_completed(db_path, "upl_test", "/tmp/test/test.wav")
    uploads = load_all_uploads(db_path)
    assert uploads["upl_test"]["completed_path"] == "/tmp/test/test.wav"


def test_update_job_status(db_path):
    init_db(db_path)
    save_session(
        db_path,
        "ses_test",
        "req_test",
        {
            "title": "Test",
            "context": "work",
            "occurred_at": "2026-07-30T10:00:00+08:00",
            "source_mode": "import",
            "marks": [],
        },
    )
    now = datetime.now(UTC)
    save_job(
        db_path,
        "job_test",
        "ses_test",
        "req_test",
        JobStatus.queued,
        2,
        "等待处理",
        created_at=now,
        updated_at=now,
    )
    update_job_status(
        db_path,
        "job_test",
        JobStatus.transcribing,
        20,
        "正式转写与说话人分离",
    )
    jobs = load_all_jobs(db_path)
    assert jobs["job_test"]["status"] == "transcribing"
    assert jobs["job_test"]["progress"] == 20


def test_save_and_load_idempotency(db_path):
    init_db(db_path)
    save_session(
        db_path,
        "ses_test",
        "req_test",
        {
            "title": "Test",
            "context": "work",
            "occurred_at": "2026-07-30T10:00:00+08:00",
            "source_mode": "import",
            "marks": [],
        },
    )
    now = datetime.now(UTC)
    save_job(
        db_path,
        "job_test",
        "ses_test",
        "req_test",
        JobStatus.queued,
        2,
        "等待处理",
        created_at=now,
        updated_at=now,
    )
    save_idempotency(db_path, "req_test", "job_test")
    idempotency = load_idempotency(db_path)
    assert idempotency["req_test"] == "job_test"


def test_save_and_load_analysis_request(db_path):
    init_db(db_path)
    save_session(
        db_path,
        "ses_test",
        "req_test",
        {
            "title": "Test",
            "context": "work",
            "occurred_at": "2026-07-30T10:00:00+08:00",
            "source_mode": "import",
            "marks": [],
        },
    )
    now = datetime.now(UTC)
    save_job(
        db_path,
        "job_test",
        "ses_test",
        "req_test",
        JobStatus.queued,
        2,
        "等待处理",
        created_at=now,
        updated_at=now,
    )
    request_data = {"request_id": "req_test", "schema_version": "1.1"}
    save_analysis_request(db_path, "job_test", request_data)
    requests = load_analysis_requests(db_path)
    assert "job_test" in requests
    assert requests["job_test"]["request_id"] == "req_test"


def test_save_and_load_job_intermediate(db_path):
    init_db(db_path)
    save_session(
        db_path,
        "ses_test",
        "req_test",
        {
            "title": "Test",
            "context": "work",
            "occurred_at": "2026-07-30T10:00:00+08:00",
            "source_mode": "import",
            "marks": [],
        },
    )
    now = datetime.now(UTC)
    save_job(
        db_path,
        "job_test",
        "ses_test",
        "req_test",
        JobStatus.queued,
        2,
        "等待处理",
        created_at=now,
        updated_at=now,
    )
    intermediate_data = {"aligned": [], "quality_metrics": []}
    save_job_intermediate(db_path, "job_test", intermediate_data)
    intermediates = load_job_intermediates(db_path)
    assert "job_test" in intermediates
    assert intermediates["job_test"]["aligned"] == []


def test_update_session_result(db_path):
    init_db(db_path)
    save_session(
        db_path,
        "ses_test",
        "req_test",
        {
            "title": "Test",
            "context": "work",
            "occurred_at": "2026-07-30T10:00:00+08:00",
            "source_mode": "import",
            "marks": [],
        },
    )
    result = {"schema_version": "1.1", "request_id": "req_test"}
    update_session_result(db_path, "ses_test", result)
    sessions = load_all_sessions(db_path)
    assert sessions["ses_test"]["result"]["schema_version"] == "1.1"


def test_update_session_processing(db_path):
    init_db(db_path)
    save_session(
        db_path,
        "ses_test",
        "req_test",
        {
            "title": "Test",
            "context": "work",
            "occurred_at": "2026-07-30T10:00:00+08:00",
            "source_mode": "import",
            "marks": [],
        },
    )
    processing = {"tracks": {}, "transcript": [], "aligned_segment_count": 0}
    update_session_processing(db_path, "ses_test", processing)
    sessions = load_all_sessions(db_path)
    assert sessions["ses_test"]["processing"]["aligned_segment_count"] == 0


def test_update_session_participant_resolution(db_path):
    init_db(db_path)
    save_session(
        db_path,
        "ses_test",
        "req_test",
        {
            "title": "Test",
            "context": "work",
            "occurred_at": "2026-07-30T10:00:00+08:00",
            "source_mode": "import",
            "marks": [],
        },
    )
    resolution = {"participants": [], "self_participant_id": None}
    update_session_participant_resolution(db_path, "ses_test", resolution)
    sessions = load_all_sessions(db_path)
    assert sessions["ses_test"]["participant_resolution"]["self_participant_id"] is None


def test_save_and_load_resume_scheduled_jobs(db_path):
    init_db(db_path)
    save_session(
        db_path,
        "ses_test",
        "req_test",
        {
            "title": "Test",
            "context": "work",
            "occurred_at": "2026-07-30T10:00:00+08:00",
            "source_mode": "import",
            "marks": [],
        },
    )
    now = datetime.now(UTC)
    save_job(
        db_path,
        "job_test",
        "ses_test",
        "req_test",
        JobStatus.queued,
        2,
        "等待处理",
        created_at=now,
        updated_at=now,
    )
    save_resume_scheduled_job(db_path, "ses_test", "job_test")
    resume_jobs = load_resume_scheduled_jobs(db_path)
    assert "ses_test" in resume_jobs
    assert "job_test" in resume_jobs["ses_test"]


def test_get_unfinished_jobs(db_path):
    init_db(db_path)
    save_session(
        db_path,
        "ses_test",
        "req_test",
        {
            "title": "Test",
            "context": "work",
            "occurred_at": "2026-07-30T10:00:00+08:00",
            "source_mode": "import",
            "marks": [],
        },
    )
    now = datetime.now(UTC)
    # Queued job - should be returned
    save_job(
        db_path,
        "job_queued",
        "ses_test",
        "req_test",
        JobStatus.queued,
        2,
        "等待处理",
        created_at=now,
        updated_at=now,
    )
    # In-progress job - should be returned
    save_job(
        db_path,
        "job_running",
        "ses_test",
        "req_test",
        JobStatus.transcribing,
        20,
        "正式转写",
        created_at=now,
        updated_at=now,
    )
    # Completed job - should NOT be returned
    save_job(
        db_path,
        "job_done",
        "ses_test",
        "req_test",
        JobStatus.complete,
        100,
        "完成",
        created_at=now,
        updated_at=now,
    )
    # Failed job - should NOT be returned
    save_job(
        db_path,
        "job_failed",
        "ses_test",
        "req_test",
        JobStatus.failed,
        50,
        "失败",
        created_at=now,
        updated_at=now,
    )
    unfinished = get_unfinished_jobs(db_path)
    unfinished_ids = {j["id"] for j in unfinished}
    assert "job_queued" in unfinished_ids
    assert "job_running" in unfinished_ids
    assert "job_done" not in unfinished_ids
    assert "job_failed" not in unfinished_ids


def test_delete_session_cascades(db_path):
    init_db(db_path)
    save_session(
        db_path,
        "ses_test",
        "req_test",
        {
            "title": "Test",
            "context": "work",
            "occurred_at": "2026-07-30T10:00:00+08:00",
            "source_mode": "import",
            "marks": [],
        },
    )
    now = datetime.now(UTC)
    save_job(
        db_path,
        "job_test",
        "ses_test",
        "req_test",
        JobStatus.queued,
        2,
        "等待处理",
        created_at=now,
        updated_at=now,
    )
    save_upload(
        db_path,
        "upl_test",
        "ses_test",
        "microphone",
        "test.wav",
        "audio/wav",
        1024,
        "abc123",
        "/tmp/test",
    )
    delete_session(db_path, "ses_test")
    sessions = load_all_sessions(db_path)
    jobs = load_all_jobs(db_path)
    uploads = load_all_uploads(db_path)
    assert "ses_test" not in sessions
    assert "job_test" not in jobs
    assert "upl_test" not in uploads


@pytest.mark.asyncio
async def test_persistent_store_initialize(store):
    await store.initialize()
    assert store._initialized


@pytest.mark.asyncio
async def test_persistent_store_create_session(store):
    await store.initialize()
    create = SessionCreate(
        title="Test Session",
        context="work",
        occurred_at="2026-07-30T10:00:00+08:00",
        source_mode="microphone",
    )
    record = await store.create_session(create)
    assert record.id.startswith("ses_")
    assert record.request_id.startswith("req_")
    # Verify persisted
    sessions = load_all_sessions(store.db_path)
    assert record.id in sessions


@pytest.mark.asyncio
async def test_persistent_store_create_job(store):
    await store.initialize()
    create = SessionCreate(
        title="Test Session",
        context="work",
        occurred_at="2026-07-30T10:00:00+08:00",
        source_mode="microphone",
    )
    record = await store.create_session(create)
    job = await store.create_job(record.id, record.request_id)
    assert job.id.startswith("job_")
    assert job.status == JobStatus.queued
    # Verify persisted
    jobs = load_all_jobs(store.db_path)
    assert job.id in jobs
    # Verify idempotency
    idempotency = load_idempotency(store.db_path)
    assert record.request_id in idempotency


@pytest.mark.asyncio
async def test_persistent_store_update_job(store):
    await store.initialize()
    create = SessionCreate(
        title="Test Session",
        context="work",
        occurred_at="2026-07-30T10:00:00+08:00",
        source_mode="microphone",
    )
    record = await store.create_session(create)
    job = await store.create_job(record.id, record.request_id)
    updated = await store.update_job(
        job.id,
        JobStatus.transcribing,
        20,
        "正式转写与说话人分离",
    )
    assert updated.status == JobStatus.transcribing
    assert updated.progress == 20
    # Verify persisted
    jobs = load_all_jobs(store.db_path)
    assert jobs[job.id]["status"] == "transcribing"
    assert jobs[job.id]["progress"] == 20


@pytest.mark.asyncio
async def test_persistent_store_idempotency(store):
    await store.initialize()
    create = SessionCreate(
        title="Test Session",
        context="work",
        occurred_at="2026-07-30T10:00:00+08:00",
        source_mode="microphone",
    )
    record = await store.create_session(create)
    job1 = await store.create_job(record.id, record.request_id)
    job2 = await store.create_job(record.id, record.request_id)
    assert job1.id == job2.id


@pytest.mark.asyncio
async def test_persistent_store_recovery(store):
    """Test that state survives store recreation (simulates restart)."""
    await store.initialize()
    create = SessionCreate(
        title="Recovery Test",
        context="work",
        occurred_at="2026-07-30T10:00:00+08:00",
        source_mode="microphone",
    )
    record = await store.create_session(create)
    job = await store.create_job(record.id, record.request_id)
    await store.update_job(
        job.id,
        JobStatus.transcribing,
        20,
        "正式转写",
    )

    # Simulate restart by creating new store with same db_path
    new_store = PersistentStore(store.data_dir, store.db_path)
    await new_store.initialize()

    # Verify sessions loaded
    assert record.id in new_store.sessions
    assert new_store.sessions[record.id].request_id == record.request_id

    # Verify jobs loaded
    assert job.id in new_store.jobs
    assert new_store.jobs[job.id].status == JobStatus.transcribing
    assert new_store.jobs[job.id].progress == 20

    # Verify idempotency loaded
    assert record.request_id in new_store.idempotency
    assert new_store.idempotency[record.request_id] == job.id


@pytest.mark.asyncio
async def test_persistent_store_recovery_with_upload(store):
    """Test that upload state survives restart."""
    await store.initialize()
    create = SessionCreate(
        title="Upload Recovery Test",
        context="work",
        occurred_at="2026-07-30T10:00:00+08:00",
        source_mode="microphone",
    )
    record = await store.create_session(create)
    upload = UploadRecord(
        id="upl_test",
        session_id=record.id,
        track="microphone",
        file_name="test.wav",
        mime_type="audio/wav",
        size=1024,
        sha256="abc123",
        directory=store.data_dir / record.id / "upl_test",
        chunks={0, 1},
    )
    store.save_upload(upload)

    # Simulate restart
    new_store = PersistentStore(store.data_dir, store.db_path)
    await new_store.initialize()

    assert record.id in new_store.sessions
    assert "upl_test" in new_store.sessions[record.id].uploads
    assert new_store.sessions[record.id].uploads["upl_test"].chunks == {0, 1}


@pytest.mark.asyncio
async def test_persistent_store_recovery_with_analysis_request(store):
    """Test that analysis requests survive restart."""
    await store.initialize()
    create = SessionCreate(
        title="Analysis Recovery Test",
        context="work",
        occurred_at="2026-07-30T10:00:00+08:00",
        source_mode="microphone",
    )
    record = await store.create_session(create)
    job = await store.create_job(record.id, record.request_id)
    request_data = {"request_id": record.request_id, "schema_version": "1.1"}
    store.save_analysis_request(job.id, request_data)

    # Simulate restart
    new_store = PersistentStore(store.data_dir, store.db_path)
    await new_store.initialize()

    assert record.id in new_store.sessions
    assert job.id in new_store.sessions[record.id].analysis_requests
    assert new_store.sessions[record.id].analysis_requests[job.id]["schema_version"] == "1.1"


@pytest.mark.asyncio
async def test_persistent_store_recovery_with_result(store):
    """Test that session results survive restart."""
    await store.initialize()
    create = SessionCreate(
        title="Result Recovery Test",
        context="work",
        occurred_at="2026-07-30T10:00:00+08:00",
        source_mode="microphone",
    )
    record = await store.create_session(create)
    result = {"schema_version": "1.1", "request_id": record.request_id}
    store.save_session_result(record.id, result)

    # Simulate restart
    new_store = PersistentStore(store.data_dir, store.db_path)
    await new_store.initialize()

    assert record.id in new_store.sessions
    assert new_store.sessions[record.id].result is not None
    assert new_store.sessions[record.id].result["schema_version"] == "1.1"


@pytest.mark.asyncio
async def test_persistent_store_get_unfinished_jobs(store):
    """Test getting unfinished jobs for restart recovery."""
    await store.initialize()
    create = SessionCreate(
        title="Unfinished Test",
        context="work",
        occurred_at="2026-07-30T10:00:00+08:00",
        source_mode="microphone",
    )
    record = await store.create_session(create)
    job = await store.create_job(record.id, record.request_id)
    await store.update_job(
        job.id,
        JobStatus.transcribing,
        20,
        "正式转写",
    )

    unfinished = store.get_unfinished_jobs()
    assert len(unfinished) == 1
    assert unfinished[0]["id"] == job.id
    assert unfinished[0]["status"] == "transcribing"
