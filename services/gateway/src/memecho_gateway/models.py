from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContractModel(BaseModel):
    """Forward-compatible memEcho contract output."""

    model_config = ConfigDict(extra="ignore")


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
    # Optional Provider Profile binding. When set, every task of this session
    # (analyze, retry, identity resume, live captions) resolves providers and
    # credentials from this profile instead of the global env configuration.
    provider_profile_id: str | None = Field(default=None, max_length=64)


class SessionCreated(BaseModel):
    id: str
    request_id: str
    status: JobStatus
    provider_profile_id: str | None = None


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
    self_participant_id: str | None
    identity_basis: Literal["user_confirmed", "auto_single_speaker", "unknown"]


SourceType = Literal["text", "transcript", "audio", "video"]
FocusModule = Literal["minutes", "content_analysis", "vad", "self_echo", "coaching"]
IdentityBasis = Literal["user_confirmed", "auto_single_speaker", "unknown"]
AnalysisMode = Literal["connected_full", "local_enhanced", "text_only", "insufficient"]


class AnalysisSource(StrictRequest):
    type: SourceType
    text: str | None = None
    path: str | None = None
    mime_type: str | None = None

    @model_validator(mode="after")
    def has_exactly_one_locator(self) -> "AnalysisSource":
        if self.type in {"text", "transcript"}:
            if self.text is None or not self.text.strip():
                raise ValueError("text and transcript sources require non-empty source.text")
            if len(self.text.encode("utf-8")) > 5 * 1024 * 1024:
                raise ValueError("source.text exceeds the 5 MiB UTF-8 limit")
        elif self.path is None or not self.path.strip():
            raise ValueError("audio and video sources require non-empty source.path")

        if bool(self.text) == bool(self.path):
            raise ValueError("exactly one of source.text or source.path is required")
        return self


class AnalysisSession(StrictRequest):
    title: str = Field(min_length=1, max_length=120)
    occurred_at: datetime | None = None
    context: str = Field(default="工作", max_length=80)


class Participant(ContractModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    is_self: bool = False


class CoachingOptions(StrictRequest):
    enabled: bool = False
    max_scenes: int = Field(default=1, ge=1, le=10)


class AnalysisMark(StrictRequest):
    at_ms: int = Field(ge=0)
    label: str = Field(min_length=1, max_length=120)


class MemoryOptions(StrictRequest):
    mode: Literal["off", "ask", "on"] = "off"
    scope: list[str] = Field(default_factory=list)


class AnalysisRequest(StrictRequest):
    """Portable memEcho 1.1 request with gateway-compatible defaults.

    Desktop recordings carry source/session metadata in the session and upload
    endpoints, so those portable fields remain optional at this route.
    """

    request_id: str = Field(min_length=1)
    schema_version: Literal["1.1"] = "1.1"
    source: AnalysisSource | None = None
    session: AnalysisSession | None = None
    participants: list[Participant] = Field(default_factory=list)
    self_identity_basis: IdentityBasis = "unknown"
    target_participant_ids: list[str] = Field(default_factory=list)
    language: str = "zh-CN"
    focus: list[FocusModule] = Field(
        default_factory=lambda: ["minutes", "content_analysis", "vad", "self_echo"]
    )
    coaching: CoachingOptions = Field(default_factory=CoachingOptions)
    marks: list[AnalysisMark] = Field(default_factory=list)
    memory: MemoryOptions = Field(default_factory=MemoryOptions)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_memory_mode(cls, value: Any) -> Any:
        if isinstance(value, dict) and "memory_mode" in value:
            migrated = dict(value)
            legacy_mode = migrated.pop("memory_mode")
            migrated.setdefault("memory", {"mode": legacy_mode, "scope": []})
            return migrated
        return value


# Backwards-compatible import used by earlier gateway code and tests.
AnalyzeRequest = AnalysisRequest


class AnalysisScope(ContractModel):
    single_session: Literal[True]
    signals_used: list[str]
    signals_missing: list[str]
    quality: float = Field(ge=0, le=1)
    target_participant_ids: list[str]
    self_participant_id: str | None
    self_identity_basis: IdentityBasis


class ActionItem(ContractModel):
    text: str = Field(min_length=1)
    owner: str | None
    due_at: datetime | None
    origin: Literal["discussed", "suggested"]
    status: Literal["confirmed", "proposed"]
    evidence_refs: list[str]


class Minutes(ContractModel):
    summary: str
    focus: list[str]
    consensus: list[str]
    disagreements: list[str]
    explicit_actions: list[ActionItem]
    recommendations: list[ActionItem]


class ParticipantContentAnalysis(ContractModel):
    participant_id: str
    fact_claims: list[str]
    opinions: list[str]
    attitudes: list[str]
    influence_summary: list[str]


class VadPoint(ContractModel):
    participant_id: str
    segment_id: str
    v: float = Field(ge=-1, le=1)
    a: float = Field(ge=-1, le=1)
    d: float = Field(ge=-1, le=1)
    scale: Literal["-1..1"]
    confidence: float = Field(ge=0, le=1)
    linguistic_weight: float = Field(ge=0, le=1)
    acoustic_weight: float = Field(ge=0, le=1)
    evidence_refs: list[str]


class FlexibleContractObject(RootModel[dict[str, Any]]):
    pass


class SelfEchoEffect(ContractModel):
    wording: str
    observed_followup: str
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str]


class SelfEchoAlternative(ContractModel):
    source: str
    rewrite: str


class SelfEcho(ContractModel):
    participant_id: str | None
    identity_basis: IdentityBasis
    effects: list[SelfEchoEffect | FlexibleContractObject]
    alternatives: list[SelfEchoAlternative | FlexibleContractObject]


class CoachingResult(ContractModel):
    enabled: bool
    status: Literal["not_requested", "awaiting_user", "scored", "complete"]
    scenes: list[FlexibleContractObject]


class Insight(ContractModel):
    id: str
    claim: str
    claim_level: Literal["observed", "computed", "interpreted"]
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str]
    alternatives: list[str]


class Evidence(ContractModel):
    id: str
    source_type: Literal["transcript", "acoustic", "user_mark"]
    speaker_id: str | None
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    segment_id: str
    excerpt: str
    quality_flags: list[str]
    track: str | None = None

    @model_validator(mode="after")
    def has_ordered_range(self) -> "Evidence":
        if self.end_ms < self.start_ms:
            raise ValueError("evidence.end_ms must be greater than or equal to start_ms")
        return self


class ModelManifestEntry(ContractModel):
    provider: str
    model: str


class Provenance(ContractModel):
    skill_version: str
    service_version: str | None
    model_manifest: list[ModelManifestEntry]


class MemoryResult(ContractModel):
    written: bool
    consent_basis: str | None


class AnalysisResult(ContractModel):
    schema_version: Literal["1.1"]
    request_id: str
    analysis_mode: AnalysisMode
    scope: AnalysisScope
    minutes: Minutes
    content_analysis: list[ParticipantContentAnalysis]
    participants: list[Participant]
    vad_series: list[VadPoint]
    interaction_events: list[FlexibleContractObject]
    self_echo: SelfEcho
    coaching: CoachingResult
    insights: list[Insight]
    evidence: list[Evidence]
    uncertainties: list[str]
    provenance: Provenance
    memory: MemoryResult


class JobView(BaseModel):
    id: str
    session_id: str
    request_id: str
    status: JobStatus
    progress: int = Field(ge=0, le=100)
    stage_label: str
    retryable: bool = False
    error_code: str | None = None
    # Safe, bounded diagnostics for the authenticated client. Never include
    # provider payloads, transcript text, credentials, or model output here.
    error_detail: str | None = Field(default=None, max_length=2000)
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


class ProcessingStage(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"


class FileTransPhase(StrEnum):
    """Fine-grained async phases for the FileTrans lifecycle.

    Backward-compatible: the ``status`` field still uses ``ProcessingStage``;
    ``phase`` provides the detailed sub-state.
    """

    not_started = "not_started"
    submitting = "submitting"
    queued = "queued"
    polling = "polling"
    downloading = "downloading"
    normalizing = "normalizing"
    succeeded = "succeeded"
    failed = "failed"
    timed_out = "timed_out"


# Mapping from fine-grained phase to the legacy ``ProcessingStage`` so that
# older clients that only understand ``status`` keep working.
PHASE_TO_STATUS: dict[FileTransPhase, ProcessingStage] = {
    FileTransPhase.not_started: ProcessingStage.queued,
    FileTransPhase.submitting: ProcessingStage.running,
    FileTransPhase.queued: ProcessingStage.running,
    FileTransPhase.polling: ProcessingStage.running,
    FileTransPhase.downloading: ProcessingStage.running,
    FileTransPhase.normalizing: ProcessingStage.running,
    FileTransPhase.succeeded: ProcessingStage.succeeded,
    FileTransPhase.failed: ProcessingStage.failed,
    FileTransPhase.timed_out: ProcessingStage.failed,
}


class ModuleDetails(BaseModel):
    """Sanitized per-module status. Only stable error codes, never raw text."""

    status: ProcessingStage
    error_code: str | None = None
    elapsed_ms: int | None = Field(default=None, ge=0)


class FileTransDetails(ModuleDetails):
    phase: FileTransPhase = FileTransPhase.not_started
    poll_attempts: int = Field(default=0, ge=0)
    next_poll_after_ms: int | None = Field(default=None, ge=0)
    last_polled_at: datetime | None = None
    retryable: bool = False
    task_reference: str | None = None
    sentence_count: int | None = Field(default=None, ge=0)
    language: str | None = None
    audio_duration_ms: int | None = Field(default=None, ge=0)


class TranscriptSnippet(BaseModel):
    speaker_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str = Field(max_length=600)


class TrackProcessingDetails(BaseModel):
    upload_id: str
    file_name: str
    track: str
    mime_type: str
    size_bytes: int = Field(ge=0)
    upload_status: ProcessingStage
    received_chunks: int = Field(ge=0)
    expected_chunks: int = Field(ge=0)
    oss_status: ProcessingStage
    # Keys are module names: fun_asr, emotion, transcription.
    modules: dict[str, ModuleDetails]
    filetrans: FileTransDetails


class ProcessingDetailsResponse(BaseModel):
    """Desensitized pipeline observability contract.

    Must never contain API keys, signed URLs, vendor response bodies, or
    absolute local paths.
    """

    session_id: str
    updated_at: datetime
    tracks: list[TrackProcessingDetails]
    aligned_segment_count: int = Field(ge=0)
    submitted_to_qwen: bool
    qwen_status: ProcessingStage
    qwen_error_code: str | None = None
    transcript_segments: list[TranscriptSnippet]
    transcript_truncated: bool


# ── Provider Profile contracts (BYOK) ────────────────────────────────────────


class ProviderCapability(StrEnum):
    realtime_asr = "realtime_asr"
    file_transcription = "file_transcription"
    diarization = "diarization"
    audio_emotion = "audio_emotion"
    text_analysis = "text_analysis"


ProviderKind = Literal["bailian", "openai_compatible", "mock"]


class ProviderProfileCreate(StrictRequest):
    """Non-sensitive profile configuration.

    Secrets are never accepted here: the API key lives in the OS credential
    store and is referenced only via ``credential_ref``. Extra fields such as
    ``api_key`` are rejected by the strict schema.
    """

    name: str = Field(min_length=1, max_length=120)
    provider: ProviderKind
    credential_ref: str | None = Field(default=None, max_length=255)
    text_base_url: str = Field(default="", max_length=512)
    text_model: str = Field(default="", max_length=120)
    audio_base_url: str = Field(default="", max_length=512)
    realtime_ws_url: str = Field(default="", max_length=512)
    realtime_model: str = Field(default="", max_length=120)
    workspace_id: str = Field(default="", max_length=120)


class ProviderProfileUpdate(StrictRequest):
    """Partial update. ``None`` means "unchanged"; empty string clears a field."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    credential_ref: str | None = Field(default=None, max_length=255)
    text_base_url: str | None = Field(default=None, max_length=512)
    text_model: str | None = Field(default=None, max_length=120)
    audio_base_url: str | None = Field(default=None, max_length=512)
    realtime_ws_url: str | None = Field(default=None, max_length=512)
    realtime_model: str | None = Field(default=None, max_length=120)
    workspace_id: str | None = Field(default=None, max_length=120)


class ProviderProfileView(BaseModel):
    """Profile as exposed by the API. Never contains secrets."""

    id: str
    name: str
    provider: ProviderKind
    credential_ref: str | None = None
    text_base_url: str = ""
    text_model: str = ""
    audio_base_url: str = ""
    realtime_ws_url: str = ""
    realtime_model: str = ""
    workspace_id: str = ""
    capabilities: list[ProviderCapability] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ProviderProfileList(BaseModel):
    profiles: list[ProviderProfileView]


class CapabilityProbe(BaseModel):
    capability: ProviderCapability
    status: Literal["ok", "failed", "unavailable"]
    error_code: str | None = None


class ProfileVerification(BaseModel):
    """Result of a bill-free profile probe. Only stable error codes, never keys."""

    profile_id: str
    ok: bool
    error_code: str | None = None
    capabilities: list[CapabilityProbe] = Field(default_factory=list)


class ProviderKindManifest(BaseModel):
    id: ProviderKind
    display_name: str
    capabilities: list[ProviderCapability]
    auth_fields: list[str]
    media_inputs: list[str]


class CapabilitiesResponse(BaseModel):
    provider: str
    provider_kinds: list[ProviderKindManifest]
