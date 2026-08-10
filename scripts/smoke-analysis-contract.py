"""Run a real text analysis and print contract diagnostics without content/secrets."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "gateway" / "src"))

from memecho_gateway.config import Settings  # noqa: E402
from memecho_gateway.contracts import validate_result  # noqa: E402
from memecho_gateway.providers.bailian import BailianProvider  # noqa: E402
from memecho_gateway.text_only import (  # noqa: E402
    build_text_segments,
    enforce_text_only_metadata,
)


def load_local_env(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--env-file",
        type=Path,
        default=ROOT / "services" / "gateway" / ".env",
    )
    args = parser.parse_args()
    load_local_env(args.env_file)
    settings = Settings(_env_file=None)
    if not settings.bailian_text_api_key or not settings.bailian_text_base_url:
        print("configuration=missing")
        return 2

    text = "项目当前存在一个接口问题。\n\n我建议先复现错误，再决定是否发布。"
    segments = build_text_segments(text)
    weights = {
        "quality_tier": "text_only",
        "linguistic_weight": 1.0,
        "acoustic_weight": 0.0,
        "aggregation": "text_only",
    }
    request = {
        "request_id": "req_contract_smoke",
        "source": {"type": "text", "text": text},
    }
    result = await BailianProvider(settings).analyze(
        session={
            "id": "ses_contract_smoke",
            "title": "Contract smoke test",
            "context": "work",
            "occurred_at": "2026-08-10T00:00:00+00:00",
            "participant_resolution": None,
            "observations": {
                "text_segments": segments,
                "aligned_segments": [],
                "acoustic_metrics": [],
                "model_errors": [],
                "evidence_weights": weights,
            },
        },
        tracks=[],
        request=request,
    )
    enforce_text_only_metadata(result)
    errors = validate_result(result, text_segments=segments)
    print(f"contract_valid={str(not errors).lower()}")
    print(f"validation_error_count={len(errors)}")
    for index, error in enumerate(errors[:20], start=1):
        print(f"error_{index}={error}")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
