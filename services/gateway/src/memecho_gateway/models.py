from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobStatus(StrEnum):
    queued = "queued"
    uploading = "uploading"
    transcribing = "transcribing"
    awaiting_identity = "awaiting_identity"
    aligning = "aligning"
    analyzing = "analyzing"
    rendering = "rendering"
    complete = "complete"
    failed = "failed"


class ParticipantCandidate(BaseModel):
    participant_id: str
    display_name: str
    source: Literal["diarization", "user_provided", "imported"]
    speaking_time_ms: int
    segment_count: int


class ParticipantsCandidatesResponse(BaseModel):
    candidates: list[ParticipantCandidate]


class ArtifactMetadata(BaseModel):
    type: Literal["json", "markdown", "html"]
    content_type: str
    size_bytes: int
    sha256: str


class ArtifactsResponse(BaseModel):
    artifacts: dict[str, ArtifactMetadata]
    contents: dict[str, str]


class SessionCreate(StrictRequest):
    title: str = Field(min_length=1, max_length=120)
    context: str = Field(default="工作", max_length=80)
    occurred_at: datetime
    source_mode: Literal["microphone", "system", "mixed", "import"]
    marks: list[dict[str, Any]] = Field(default_factory=list)


class SessionCreated(BaseModel):
    id: str
    request_id: str
    status: JobStatus


class UploadCreate(StrictRequest):
    track: Literal["microphone", "system", "mixed", "import"]
    file_name: str
    mime_type: str
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class UploadCreated(BaseModel):
    upload_id: str
    chunk_size: int
    received_chunks: list[int]


class UploadComplete(StrictRequest):
    upload_id: str
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class ParticipantResolution(StrictRequest):
    participants: list[dict[str, Any]]
    self_participant_id: str | None = None
    identity_basis: Literal["user_confirmed", "auto_single_speaker", "unknown"]


class AnalyzeRequest(StrictRequest):
    request_id: str
    schema_version: Literal["1.1"] = "1.1"
    focus: list[str] = Field(
        default_factory=lambda: ["minutes", "content_analysis", "vad", "self_echo"]
    )
    memory_mode: Literal["off", "ask", "on"] = "off"
    language: str = "zh-CN"


class JobView(BaseModel):
    id: str
    session_id: str
    request_id: str
    status: JobStatus
    progress: int = Field(ge=0, le=100)
    stage_label: str
    retryable: bool = False
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class ChatRequest(StrictRequest):
    question: str = Field(min_length=1, max_length=2000)
    result: dict[str, Any]
    evidence_ids: list[str] = Field(default_factory=list)


class Health(BaseModel):
    status: Literal["ok"]
    provider: str
    version: str

