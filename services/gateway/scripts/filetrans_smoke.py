"""Explicit, PAID FileTrans end-to-end smoke test. Disabled by default.

The free connectivity probe lives behind ``POST /v1/llm/test`` (kind=audio)
and never creates a task. This script is the only path that submits a real,
billable transcription task, and it refuses to run unless explicitly armed.

Required environment variables:

    MEMECHO_FILETRANS_SMOKE=1                          # arm the script
    MEMECHO_FILETRANS_SMOKE_AUDIO_URL=https://.../a.wav  # public audio URL
    MEMECHO_FILETRANS_SMOKE_API_KEY=sk-...             # or BAILIAN_AUDIO_API_KEY

Optional:

    MEMECHO_FILETRANS_SMOKE_BASE_URL=https://dashscope.aliyuncs.com

Credentials are read from environment variables only; this script never
loads ``.env``. Output is limited to sanitized stats (task reference, poll
attempts, elapsed ms, sentence count, language, duration) — API keys,
signed URLs, and transcript text are never printed.

Run from ``services/gateway`` with the dev dependencies installed, e.g.:

    uv run python scripts/filetrans_smoke.py

Exit codes: 0 = ok / skipped-by-default, 1 = smoke failure, 2 = bad setup.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_PHASE_STATS = {
    "poll_attempts",
    "next_poll_after_ms",
    "elapsed_ms",
    "sentence_count",
    "language",
    "audio_duration_ms",
    "task_reference",
    "error_code",
}


async def main() -> int:
    if os.environ.get("MEMECHO_FILETRANS_SMOKE") != "1":
        print(
            "FileTrans real smoke test is disabled by default. "
            "Set MEMECHO_FILETRANS_SMOKE=1 plus MEMECHO_FILETRANS_SMOKE_AUDIO_URL "
            "and an API key to run a PAID end-to-end transcription."
        )
        return 0

    audio_url = os.environ.get("MEMECHO_FILETRANS_SMOKE_AUDIO_URL", "").strip()
    api_key = (
        os.environ.get("MEMECHO_FILETRANS_SMOKE_API_KEY")
        or os.environ.get("BAILIAN_AUDIO_API_KEY")
        or ""
    ).strip()
    base_url = os.environ.get(
        "MEMECHO_FILETRANS_SMOKE_BASE_URL", "https://dashscope.aliyuncs.com"
    ).strip()
    if not audio_url or not api_key:
        print(
            "Refusing to run: missing MEMECHO_FILETRANS_SMOKE_AUDIO_URL or API key.",
            file=sys.stderr,
        )
        return 2

    from memecho_gateway.config import Settings
    from memecho_gateway.processing_details import safe_error_code
    from memecho_gateway.providers.transcription import TranscriptionDownloader

    # _env_file=None guarantees no .env is read: credentials come from the
    # explicit environment variables checked above.
    settings = Settings(_env_file=None)
    settings.bailian_audio_api_key = api_key
    settings.bailian_audio_base_url = base_url

    downloader = TranscriptionDownloader(settings, mock=False)

    def on_phase(phase: str, **kwargs: object) -> None:
        stats = {key: value for key, value in kwargs.items() if key in _PHASE_STATS}
        print(f"[smoke] phase={phase} {stats}")

    try:
        result = await downloader.download_with_phase(audio_url, on_phase=on_phase)
    except Exception as exc:  # noqa: BLE001 - smoke test reports stable code only
        # Never print str(exc): vendor messages can embed signed URLs.
        print(f"[smoke] FAILED error_code={safe_error_code(exc)}", file=sys.stderr)
        return 1

    segments = result.get("transcript", [])
    print(
        "[smoke] OK sentence_count=%d language=%s duration_ms=%s"
        % (len(segments), result.get("language"), result.get("duration_ms"))
    )
    if not segments:
        print("[smoke] FAILED no usable sentences", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
