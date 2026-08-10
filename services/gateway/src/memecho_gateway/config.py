from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    memecho_provider: str = "mock"
    memecho_demo_token: str = "change-me"
    memecho_data_dir: Path = Path("./tmp")
    memecho_public_base_url: str = "http://127.0.0.1:8787"
    # Comma-separated browser origins. Keep this explicit so production does
    # not accidentally become an open CORS proxy.
    memecho_allowed_origins: str = (
        "http://localhost:1420,http://127.0.0.1:1420,"
        "http://tauri.localhost,tauri://localhost"
    )

    bailian_text_base_url: str = ""
    bailian_text_api_key: str = ""
    bailian_text_model: str = "qwen3.7-max-2026-06-08"
    bailian_audio_base_url: str = ""
    bailian_audio_api_key: str = ""
    bailian_realtime_ws_url: str = ""
    bailian_realtime_model: str = "qwen3-asr-flash-realtime"
    bailian_workspace_id: str = ""
    bailian_realtime_language: str = "zh"
    bailian_realtime_sample_rate: int = 16000
    bailian_realtime_vad_threshold: float = 0.2
    bailian_realtime_silence_duration_ms: int = 800
    bailian_realtime_heartbeat_seconds: float = 20.0
    bailian_realtime_heartbeat_timeout_seconds: float = 20.0
    bailian_realtime_close_timeout_seconds: float = 5.0
    bailian_realtime_finish_timeout_seconds: float = 10.0
    bailian_realtime_max_frame_bytes: int = 1024 * 1024
    bailian_diarization_model: str = "fun-asr"
    bailian_emotion_model: str = "qwen3-asr-flash-filetrans"
    bailian_transcription_model: str = "qwen3-asr-flash-filetrans"

    oss_endpoint: str = ""
    oss_bucket: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    oss_prefix: str = "memecho-tmp"
    oss_multipart_threshold_bytes: int = 8 * 1024 * 1024
    oss_part_size_bytes: int = 8 * 1024 * 1024

    chunk_size_bytes: int = 8 * 1024 * 1024
    max_session_seconds: int = 2 * 60 * 60
    # Failed jobs keep their local upload copies for this long so retries
    # remain possible; completed sessions are cleaned immediately.
    memecho_media_retention_seconds: int = 24 * 60 * 60


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.memecho_data_dir.mkdir(parents=True, exist_ok=True)
    return settings

