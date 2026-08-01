from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    memecho_provider: str = "mock"
    memecho_demo_token: str = "change-me"
    memecho_data_dir: Path = Path("./tmp")
    memecho_public_base_url: str = "http://127.0.0.1:8787"

    bailian_text_base_url: str = ""
    bailian_text_api_key: str = ""
    bailian_text_model: str = "qwen3.7-max-2026-06-08"
    bailian_audio_base_url: str = ""
    bailian_audio_api_key: str = ""
    bailian_realtime_ws_url: str = ""
    bailian_realtime_model: str = "qwen3-asr-flash-realtime"
    bailian_diarization_model: str = "fun-asr"
    bailian_emotion_model: str = "qwen3-asr-flash-filetrans"

    oss_endpoint: str = ""
    oss_bucket: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    oss_prefix: str = "memecho-tmp"

    chunk_size_bytes: int = 8 * 1024 * 1024
    max_session_seconds: int = 2 * 60 * 60


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.memecho_data_dir.mkdir(parents=True, exist_ok=True)
    return settings

