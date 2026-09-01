"""Provider Profile registry: capability manifests, credential resolution,
provider selection, and bill-free verification probes.

Business code resolves providers through profiles and capabilities instead of
branching on a single global ``MEMECHO_PROVIDER`` value; the env-based global
provider remains only as the headless/development fallback.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Settings
from .credentials import CredentialResolver
from .models import (
    CapabilityProbe,
    ProfileVerification,
    ProviderCapability,
    ProviderKind,
    ProviderKindManifest,
    ProviderProfileView,
)

log = logging.getLogger(__name__)

# Stable verification error codes surfaced to clients.
ERR_PROVIDER_AUTH_FAILED = "provider_auth_failed"
ERR_ENDPOINT_UNREACHABLE = "endpoint_unreachable"
ERR_CREDENTIAL_UNRESOLVED = "credential_unresolved"
ERR_NOT_CONFIGURED = "profile_not_configured"
ERR_UPSTREAM = "upstream_error"

_AUDIO_CAPABILITIES = (
    ProviderCapability.realtime_asr,
    ProviderCapability.file_transcription,
    ProviderCapability.diarization,
    ProviderCapability.audio_emotion,
)

PROVIDER_MANIFESTS: dict[ProviderKind, ProviderKindManifest] = {
    "bailian": ProviderKindManifest(
        id="bailian",
        display_name="Alibaba Cloud Model Studio",
        capabilities=[*_AUDIO_CAPABILITIES, ProviderCapability.text_analysis],
        auth_fields=["api_key"],
        media_inputs=["public_url", "binary_upload"],
    ),
    "openai_compatible": ProviderKindManifest(
        id="openai_compatible",
        display_name="OpenAI-compatible text endpoint",
        capabilities=[ProviderCapability.text_analysis],
        auth_fields=["api_key"],
        media_inputs=[],
    ),
    "mock": ProviderKindManifest(
        id="mock",
        display_name="Local mock provider (development)",
        capabilities=[*_AUDIO_CAPABILITIES, ProviderCapability.text_analysis],
        auth_fields=[],
        media_inputs=["local_path"],
    ),
}

# Synthetic DashScope task id for the bill-free credentials probe; it can
# never exist, so only auth/endpoint behavior is observed.
_PROBE_TASK_ID = "00000000-0000-0000-0000-000000000000"


def capabilities_for(kind: ProviderKind) -> list[ProviderCapability]:
    return list(PROVIDER_MANIFESTS[kind].capabilities)


def kind_supports(kind: ProviderKind, capability: ProviderCapability) -> bool:
    return capability in PROVIDER_MANIFESTS[kind].capabilities


def build_profile_view(profile_id: str, data: dict[str, Any]) -> ProviderProfileView:
    return ProviderProfileView(
        id=profile_id,
        name=data["name"],
        provider=data["provider"],
        credential_ref=data.get("credential_ref"),
        text_base_url=data.get("text_base_url", ""),
        text_model=data.get("text_model", ""),
        audio_base_url=data.get("audio_base_url", ""),
        transcription_model=data.get("transcription_model", ""),
        diarization_model=data.get("diarization_model", ""),
        emotion_model=data.get("emotion_model", ""),
        realtime_ws_url=data.get("realtime_ws_url", ""),
        realtime_model=data.get("realtime_model", ""),
        workspace_id=data.get("workspace_id", ""),
        capabilities=capabilities_for(data["provider"]),
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )


class ProfileCredentialError(RuntimeError):
    """A bound profile's secret could not be resolved from the OS store."""


def resolve_profile_secret(
    profile: ProviderProfileView, resolver: CredentialResolver
) -> str:
    """Resolve the API key for a profile that requires one.

    Raises ``ProfileCredentialError`` (stable code ``credential_unresolved``)
    when the ref is missing or cannot be read, so callers never silently fall
    back to another account's global key.
    """
    if profile.provider == "mock":
        return ""
    secret = resolver.resolve(profile.credential_ref or "")
    if not secret:
        raise ProfileCredentialError(ERR_CREDENTIAL_UNRESOLVED)
    return secret


def build_profile_overrides(
    profile: ProviderProfileView, api_key: str, settings: Settings
) -> dict[str, Any]:
    """Effective provider kwargs for a bound profile.

    Returns a dict consumed by the orchestrator: text/audio kwargs plus the
    capability gates. A bound profile is authoritative and never borrows
    provider configuration from the process environment.
    """
    if profile.provider == "mock":
        return {"text_kwargs": {}, "audio_kwargs": {}, "supports_audio": False}

    if profile.provider == "openai_compatible":
        text_kwargs: dict[str, str] = {}
        if api_key:
            text_kwargs["api_key"] = api_key
        if profile.text_base_url:
            text_kwargs["base_url"] = profile.text_base_url
        if profile.text_model:
            text_kwargs["model"] = profile.text_model
        return {"text_kwargs": text_kwargs, "audio_kwargs": {}, "supports_audio": False}

    # bailian
    text_kwargs = {}
    if api_key:
        text_kwargs["api_key"] = api_key
    base_url = profile.text_base_url
    if base_url:
        text_kwargs["base_url"] = base_url
    model = profile.text_model
    if model:
        text_kwargs["model"] = model

    audio_kwargs: dict[str, str] = {}
    if api_key:
        audio_kwargs["api_key"] = api_key
    audio_base = profile.audio_base_url
    if audio_base:
        audio_kwargs["base_url"] = audio_base
    workspace = profile.workspace_id
    if workspace:
        audio_kwargs["workspace_id"] = workspace
    return {
        "text_kwargs": text_kwargs,
        "audio_kwargs": audio_kwargs,
        "transcription_model": profile.transcription_model,
        "diarization_model": profile.diarization_model,
        "emotion_model": profile.emotion_model,
        "supports_audio": True,
    }


def select_provider(kind: ProviderKind, settings: Settings) -> Any:
    """Pick the adapter implementation for a profile kind."""
    from .providers import BailianProvider, MockProvider

    if kind == "mock":
        return MockProvider()
    return BailianProvider(settings)


def realtime_settings_for(
    profile: ProviderProfileView, api_key: str, settings: Settings
) -> Settings:
    """Settings copy for the realtime ASR client bound to a profile."""
    workspace_id = profile.workspace_id
    realtime_url = profile.realtime_ws_url
    if realtime_url and workspace_id:
        for placeholder in ("{WorkspaceId}", "{workspace_id}", "{workspaceId}"):
            realtime_url = realtime_url.replace(placeholder, workspace_id)
    return settings.model_copy(
        update={
            "bailian_audio_api_key": api_key,
            "bailian_realtime_ws_url": realtime_url,
            "bailian_realtime_model": profile.realtime_model,
            "bailian_workspace_id": workspace_id,
        }
    )


# ── Bill-free verification probes ────────────────────────────────────────────


async def _probe_chat_completions(
    base_url: str, api_key: str, model: str
) -> tuple[str, str | None]:
    if not base_url:
        return "failed", ERR_NOT_CONFIGURED
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model or "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                },
            )
    except httpx.HTTPError:
        return "failed", ERR_ENDPOINT_UNREACHABLE
    if resp.status_code == 200:
        return "ok", None
    if resp.status_code in (401, 403):
        return "failed", ERR_PROVIDER_AUTH_FAILED
    return "failed", ERR_UPSTREAM


async def _probe_dashscope_auth(
    base_url: str, api_key: str
) -> tuple[str, str | None]:
    """Bill-free DashScope probe: synthetic task lookup validates credentials."""
    if not base_url:
        return "failed", ERR_NOT_CONFIGURED
    base = base_url.rstrip("/")
    if base.endswith("/transcription"):
        from urllib.parse import urlparse

        parsed = urlparse(base)
        url = f"{parsed.scheme}://{parsed.netloc}/api/v1/tasks/{_PROBE_TASK_ID}"
    else:
        url = f"{base}/api/v1/tasks/{_PROBE_TASK_ID}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError:
        return "failed", ERR_ENDPOINT_UNREACHABLE
    if resp.status_code in (401, 403):
        return "failed", ERR_PROVIDER_AUTH_FAILED
    if resp.status_code >= 500:
        return "failed", ERR_UPSTREAM
    # Any other status (e.g. 404 task-not-found) means credentials were
    # accepted by the endpoint.
    return "ok", None


async def verify_profile(
    profile: ProviderProfileView,
    resolver: CredentialResolver,
    settings: Settings,
) -> ProfileVerification:
    """Probe a profile's capabilities without creating billable work."""
    manifest = PROVIDER_MANIFESTS[profile.provider]

    if profile.provider == "mock":
        return ProfileVerification(
            profile_id=profile.id,
            ok=True,
            capabilities=[
                CapabilityProbe(capability=capability, status="ok")
                for capability in manifest.capabilities
            ],
        )

    try:
        api_key = resolve_profile_secret(profile, resolver)
    except ProfileCredentialError:
        return ProfileVerification(
            profile_id=profile.id,
            ok=False,
            error_code=ERR_CREDENTIAL_UNRESOLVED,
            capabilities=[
                CapabilityProbe(
                    capability=capability,
                    status="failed",
                    error_code=ERR_CREDENTIAL_UNRESOLVED,
                )
                for capability in manifest.capabilities
            ],
        )

    probes: list[CapabilityProbe] = []

    text_status, text_error = await _probe_chat_completions(
        profile.text_base_url,
        api_key,
        profile.text_model,
    )
    probes.append(
        CapabilityProbe(
            capability=ProviderCapability.text_analysis,
            status=text_status,  # type: ignore[arg-type]
            error_code=text_error,
        )
    )

    audio_result: tuple[str, str | None] | None = None
    for capability in _AUDIO_CAPABILITIES:
        if not kind_supports(profile.provider, capability):
            probes.append(CapabilityProbe(capability=capability, status="unavailable"))
            continue
        if audio_result is None:
            # All bailian audio capabilities share one auth surface, so a
            # single bill-free probe covers them.
            audio_result = await _probe_dashscope_auth(
                profile.audio_base_url, api_key
            )
        audio_status, audio_error = audio_result
        probes.append(
            CapabilityProbe(
                capability=capability,
                status=audio_status,  # type: ignore[arg-type]
                error_code=audio_error,
            )
        )

    failed = [probe for probe in probes if probe.status == "failed"]
    return ProfileVerification(
        profile_id=profile.id,
        ok=not failed,
        error_code=failed[0].error_code if failed else None,
        capabilities=probes,
    )
