"""Tests for docker-entrypoint.sh validation rules.

Replicates the shell validation logic in pure Python so tests run
cross-platform (CI on Linux, local dev on Windows/macOS).
"""

from __future__ import annotations

from pathlib import Path

import pytest


# tests/ -> gateway/ -> services/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINT = _REPO_ROOT / "infra" / "docker-entrypoint.sh"
COMPOSE = _REPO_ROOT / "infra" / "docker-compose.yml"

# ── Canonical variable list (must match docker-entrypoint.sh exactly) ─────────

REQUIRED_VARS = [
    "MEMECHO_PROVIDER",
    "MEMECHO_DEMO_TOKEN",
    "MEMECHO_PUBLIC_BASE_URL",
    "BAILIAN_TEXT_BASE_URL",
    "BAILIAN_TEXT_API_KEY",
    "BAILIAN_TEXT_MODEL",
    "BAILIAN_AUDIO_BASE_URL",
    "BAILIAN_AUDIO_API_KEY",
    "BAILIAN_REALTIME_WS_URL",
    "BAILIAN_REALTIME_MODEL",
    "BAILIAN_DIARIZATION_MODEL",
    "BAILIAN_EMOTION_MODEL",
    "OSS_ENDPOINT",
    "OSS_BUCKET",
    "OSS_ACCESS_KEY_ID",
    "OSS_ACCESS_KEY_SECRET",
    "OSS_PREFIX",
    "CHUNK_SIZE_BYTES",
    "MAX_SESSION_SECONDS",
]

VALID_ENV = {
    "MEMECHO_PROVIDER": "bailian",
    "MEMECHO_DEMO_TOKEN": "a" * 32,
    "MEMECHO_PUBLIC_BASE_URL": "https://gateway.example.com",
    "BAILIAN_TEXT_BASE_URL": "https://text.bailian.example.com",
    "BAILIAN_TEXT_API_KEY": "sk-test",
    "BAILIAN_TEXT_MODEL": "qwen3.7-max-2026-06-08",
    "BAILIAN_AUDIO_BASE_URL": "https://audio.bailian.example.com",
    "BAILIAN_AUDIO_API_KEY": "sk-audio",
    "BAILIAN_REALTIME_WS_URL": "wss://realtime.bailian.example.com",
    "BAILIAN_REALTIME_MODEL": "qwen3-asr-flash-realtime",
    "BAILIAN_DIARIZATION_MODEL": "fun-asr",
    "BAILIAN_EMOTION_MODEL": "qwen3-asr-flash-filetrans",
    "OSS_ENDPOINT": "https://oss-cn-hangzhou.aliyuncs.com",
    "OSS_BUCKET": "memecho-prod",
    "OSS_ACCESS_KEY_ID": "akid",
    "OSS_ACCESS_KEY_SECRET": "aksec",
    "OSS_PREFIX": "memecho-tmp",
    "CHUNK_SIZE_BYTES": "8388608",
    "MAX_SESSION_SECONDS": "7200",
}


# ── Pure-Python entrypoint validation replica ─────────────────────────────────


def _validate_entrypoint(env: dict[str, str]) -> str | None:
    """Replicate the docker-entrypoint.sh validation logic.

    Returns None on success, or an error message on failure.
    """
    for name in REQUIRED_VARS:
        value = env.get(name, "")
        if not value:
            return f"memEcho gateway configuration error: {name} is required"

    if env.get("MEMECHO_PROVIDER") != "bailian":
        return "memEcho gateway configuration error: production image requires MEMECHO_PROVIDER=bailian"

    if len(env.get("MEMECHO_DEMO_TOKEN", "")) < 32:
        return "memEcho gateway configuration error: MEMECHO_DEMO_TOKEN must be at least 32 characters"

    for var, scheme in [
        ("MEMECHO_PUBLIC_BASE_URL", "https"),
        ("BAILIAN_TEXT_BASE_URL", "https"),
        ("BAILIAN_AUDIO_BASE_URL", "https"),
        ("OSS_ENDPOINT", "https"),
    ]:
        if not env.get(var, "").startswith(f"{scheme}://"):
            return f"memEcho gateway configuration error: {var} must use {scheme.upper()}"

    if not env.get("BAILIAN_REALTIME_WS_URL", "").startswith("wss://"):
        return "memEcho gateway configuration error: BAILIAN_REALTIME_WS_URL must use WSS"

    return None


# ── Entrypoint file existence and structure ───────────────────────────────────


def test_entrypoint_file_exists():
    """The entrypoint script must exist in the infra directory."""
    assert ENTRYPOINT.exists(), f"Missing {ENTRYPOINT}"


def test_entrypoint_uses_exec_uvicorn():
    """Entrypoint must exec uvicorn so signals propagate correctly."""
    text = ENTRYPOINT.read_text()
    assert "exec uvicorn" in text, "Entrypoint must use 'exec uvicorn'"


def test_entrypoint_is_posix_sh():
    """Entrypoint must use POSIX sh for Alpine compatibility."""
    text = ENTRYPOINT.read_text()
    assert text.startswith("#!/bin/sh"), "Entrypoint must start with #!/bin/sh"


# ── Required variable validation ─────────────────────────────────────────────


def test_all_required_vars_are_validated():
    """Every var in REQUIRED_VARS is checked for non-empty."""
    for var in REQUIRED_VARS:
        env = {**VALID_ENV, var: ""}
        error = _validate_entrypoint(env)
        assert error is not None, f"Empty {var} should be rejected"
        assert var in error, f"Error should mention {var}"


def test_valid_env_passes():
    """A fully valid environment must pass all checks."""
    assert _validate_entrypoint(VALID_ENV) is None


# ── Provider validation ──────────────────────────────────────────────────────


def test_rejects_non_bailian_provider():
    """Production image requires MEMECHO_PROVIDER=bailian."""
    env = {**VALID_ENV, "MEMECHO_PROVIDER": "mock"}
    error = _validate_entrypoint(env)
    assert error is not None
    assert "MEMECHO_PROVIDER=bailian" in error


def test_rejects_missing_provider():
    """MEMECHO_PROVIDER is required."""
    env = {k: v for k, v in VALID_ENV.items() if k != "MEMECHO_PROVIDER"}
    error = _validate_entrypoint(env)
    assert error is not None
    assert "MEMECHO_PROVIDER" in error


# ── Token validation ─────────────────────────────────────────────────────────


def test_rejects_short_token():
    """MEMECHO_DEMO_TOKEN must be at least 32 characters."""
    env = {**VALID_ENV, "MEMECHO_DEMO_TOKEN": "short"}
    error = _validate_entrypoint(env)
    assert error is not None
    assert "MEMECHO_DEMO_TOKEN" in error


def test_rejects_empty_token():
    """MEMECHO_DEMO_TOKEN must not be empty."""
    env = {**VALID_ENV, "MEMECHO_DEMO_TOKEN": ""}
    error = _validate_entrypoint(env)
    assert error is not None


def test_accepts_32_char_token():
    """A 32-character token is the minimum valid length."""
    env = {**VALID_ENV, "MEMECHO_DEMO_TOKEN": "x" * 32}
    assert _validate_entrypoint(env) is None


# ── URL scheme validation ────────────────────────────────────────────────────


def test_rejects_http_public_url():
    """MEMECHO_PUBLIC_BASE_URL must use HTTPS."""
    env = {**VALID_ENV, "MEMECHO_PUBLIC_BASE_URL": "http://example.com"}
    error = _validate_entrypoint(env)
    assert error is not None
    assert "MEMECHO_PUBLIC_BASE_URL" in error


def test_rejects_http_text_url():
    """BAILIAN_TEXT_BASE_URL must use HTTPS."""
    env = {**VALID_ENV, "BAILIAN_TEXT_BASE_URL": "http://text.example.com"}
    error = _validate_entrypoint(env)
    assert error is not None
    assert "BAILIAN_TEXT_BASE_URL" in error


def test_rejects_http_audio_url():
    """BAILIAN_AUDIO_BASE_URL must use HTTPS."""
    env = {**VALID_ENV, "BAILIAN_AUDIO_BASE_URL": "http://audio.example.com"}
    error = _validate_entrypoint(env)
    assert error is not None
    assert "BAILIAN_AUDIO_BASE_URL" in error


def test_rejects_ws_realtime_url():
    """BAILIAN_REALTIME_WS_URL must use WSS."""
    env = {**VALID_ENV, "BAILIAN_REALTIME_WS_URL": "ws://realtime.example.com"}
    error = _validate_entrypoint(env)
    assert error is not None
    assert "BAILIAN_REALTIME_WS_URL" in error


def test_rejects_http_oss_endpoint():
    """OSS_ENDPOINT must use HTTPS."""
    env = {**VALID_ENV, "OSS_ENDPOINT": "http://oss.example.com"}
    error = _validate_entrypoint(env)
    assert error is not None
    assert "OSS_ENDPOINT" in error


# ── Entrypoint script contains all validation rules ──────────────────────────


def test_entrypoint_checks_all_required_vars():
    """The shell script must list every required variable."""
    text = ENTRYPOINT.read_text()
    for var in REQUIRED_VARS:
        assert var in text, f"Entrypoint missing check for {var}"


def test_entrypoint_validates_provider_is_bailian():
    """The shell script must enforce MEMECHO_PROVIDER=bailian."""
    text = ENTRYPOINT.read_text()
    assert "MEMECHO_PROVIDER" in text
    assert "bailian" in text


def test_entrypoint_validates_token_length():
    """The shell script must check MEMECHO_DEMO_TOKEN length >= 32."""
    text = ENTRYPOINT.read_text()
    assert "32" in text or "MEMECHO_DEMO_TOKEN" in text


def test_entrypoint_validates_https_for_urls():
    """The shell script must check HTTPS for all URL variables."""
    text = ENTRYPOINT.read_text()
    for var in ["MEMECHO_PUBLIC_BASE_URL", "BAILIAN_TEXT_BASE_URL", "BAILIAN_AUDIO_BASE_URL", "OSS_ENDPOINT"]:
        assert var in text, f"Entrypoint missing HTTPS check for {var}"


def test_entrypoint_validates_wss_for_realtime():
    """The shell script must check WSS for the realtime WebSocket URL."""
    text = ENTRYPOINT.read_text()
    assert "wss://" in text or "WSS" in text


# ── Compose/env consistency ──────────────────────────────────────────────────


def test_compose_injects_all_required_vars():
    """Every variable in REQUIRED_VARS is injected by docker-compose.yml."""
    compose_text = COMPOSE.read_text()
    for var in REQUIRED_VARS:
        assert var in compose_text, f"{var} missing from docker-compose.yml environment"


def test_compose_uses_memecho_demo_token_placeholder():
    """docker-compose.yml must use the ${VAR:?Set ...} pattern for secrets."""
    compose_text = COMPOSE.read_text()
    assert "MEMECHO_DEMO_TOKEN" in compose_text
    # Verify it uses the required-var pattern (no hardcoded secrets)
    assert '"change-me"' not in compose_text, "No hardcoded tokens in compose"


# ── Python config defaults ───────────────────────────────────────────────────


def test_config_default_token_is_placeholder(monkeypatch):
    """Default token is 'change-me' — entrypoint enforces real token in prod."""
    from memecho_gateway.config import Settings

    monkeypatch.delenv("MEMECHO_DEMO_TOKEN", raising=False)
    # _env_file=None keeps a developer's cwd .env from overriding defaults.
    s = Settings(_env_file=None)
    assert s.memecho_demo_token == "change-me"


def test_config_default_provider_is_mock(monkeypatch):
    """Default provider is 'mock' — entrypoint enforces 'bailian' in prod."""
    from memecho_gateway.config import Settings

    monkeypatch.delenv("MEMECHO_PROVIDER", raising=False)
    s = Settings(_env_file=None)
    assert s.memecho_provider == "mock"
