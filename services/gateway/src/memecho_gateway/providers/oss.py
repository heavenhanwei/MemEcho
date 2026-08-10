from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Protocol

from ..config import Settings

log = logging.getLogger(__name__)


class OSSClient(Protocol):
    async def upload(self, key: str, data: bytes, content_type: str) -> str: ...
    async def upload_file(
        self, key: str, path: Path, content_type: str
    ) -> str: ...
    async def signed_url(self, key: str, expires: int = 7200) -> str: ...
    async def delete(self, key: str) -> None: ...


class AliyunOSSClient:
    def __init__(self, settings: Settings, mock: bool = False):
        self.settings = settings
        self.mock = mock
        self._mock_store: dict[str, bytes] = {}

    async def upload(self, key: str, data: bytes, content_type: str = "audio/wav") -> str:
        if self.mock:
            self._mock_store[key] = data
            log.info("OSS mock upload key=%s size=%d", key, len(data))
            return f"oss://{self.settings.oss_bucket}/{key}"
        import oss2

        bucket = self._bucket()
        bucket.put_object(key, data, headers={"Content-Type": content_type})
        return f"oss://{self.settings.oss_bucket}/{key}"


    async def upload_file(
        self, key: str, path: Path, content_type: str = "audio/wav"
    ) -> str:
        """Upload a path without materializing the complete file as bytes."""
        if self.mock:
            chunks: list[bytes] = []
            with path.open("rb") as source:
                while chunk := source.read(self.settings.oss_part_size_bytes):
                    chunks.append(chunk)
            self._mock_store[key] = b"".join(chunks)
            log.info("OSS mock file upload key=%s size=%d", key, path.stat().st_size)
            return f"oss://{self.settings.oss_bucket}/{key}"

        await asyncio.to_thread(self._resumable_upload, key, path, content_type)
        return f"oss://{self.settings.oss_bucket}/{key}"

    def _resumable_upload(self, key: str, path: Path, content_type: str) -> None:
        import oss2

        oss2.resumable_upload(
            self._bucket(),
            key,
            str(path),
            multipart_threshold=self.settings.oss_multipart_threshold_bytes,
            part_size=self.settings.oss_part_size_bytes,
            num_threads=1,
            headers={"Content-Type": content_type},
        )
    async def signed_url(self, key: str, expires: int = 7200) -> str:
        if self.mock:
            return f"https://mock-oss.example.com/{key}?expires={int(time.time()) + expires}"
        import oss2

        bucket = self._bucket()
        # DashScope rejects signed URLs whose object path separators are
        # percent-encoded as ``%2F``. Keep the OSS key as a normal URL path.
        return bucket.sign_url("GET", key, expires, slash_safe=True)

    async def delete(self, key: str) -> None:
        if self.mock:
            self._mock_store.pop(key, None)
            log.info("OSS mock delete key=%s", key)
            return
        import oss2

        bucket = self._bucket()
        try:
            bucket.delete_object(key)
        except Exception:
            log.warning("OSS delete failed for key=%s", key, exc_info=True)

    def _bucket(self) -> Any:
        import oss2

        auth = oss2.Auth(self.settings.oss_access_key_id, self.settings.oss_access_key_secret)
        return oss2.Bucket(auth, self.settings.oss_endpoint, self.settings.oss_bucket)


def make_oss_key(prefix: str, session_id: str, upload_id: str, filename: str) -> str:
    safe_name = filename.replace("/", "_").replace("\\", "_")
    return f"{prefix}/{session_id}/{upload_id}/{safe_name}"
