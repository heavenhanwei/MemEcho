"""Editable, non-secret provider profile configuration file."""

from __future__ import annotations

import json
from pathlib import Path

from .models import ProviderProfileConfigFile, ProviderProfileView


CONFIG_FILE_NAME = "provider_profiles.json"


def config_path(data_dir: Path) -> Path:
    return data_dir / CONFIG_FILE_NAME


def load(path: Path) -> list[ProviderProfileView]:
    payload = ProviderProfileConfigFile.model_validate_json(path.read_text(encoding="utf-8"))
    identifiers = [profile.id for profile in payload.profiles]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("duplicate provider profile id")
    return payload.profiles


def save(path: Path, profiles: list[ProviderProfileView]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = ProviderProfileConfigFile(profiles=profiles)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
