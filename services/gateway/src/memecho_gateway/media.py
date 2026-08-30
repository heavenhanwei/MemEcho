"""Media Transport layer: capability-driven media delivery to providers.

Providers declare which ``MediaInput`` kinds they accept; the pipeline picks
a compatible transport instead of hard-coding object storage. Object storage
(OSS/S3-style) is one optional transport among four:

1. ``local_path``    - provider reads a gateway-local file directly;
2. ``binary_upload`` - gateway streams the file to the provider API;
3. ``base64_inline`` - small files only, bounded by a strict size cap;
4. ``public_url``    - temporary object storage with short-lived signed URLs.

When no available transport matches the provider's declared inputs the
pipeline raises ``MediaInputUnsupportedError`` (stable code
``media_input_unsupported``) — it must never masquerade as
``upstream_task_failed``.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

# Hard cap for base64_inline: providers that accept inline payloads only do
# so for small files. Anything larger must use another transport.
BASE64_INLINE_MAX_BYTES = 1 * 1024 * 1024


class MediaInput(StrEnum):
    local_path = "local_path"
    binary_upload = "binary_upload"
    base64_inline = "base64_inline"
    public_url = "public_url"


ALL_MEDIA_INPUTS: tuple[MediaInput, ...] = (
    MediaInput.local_path,
    MediaInput.binary_upload,
    MediaInput.base64_inline,
    MediaInput.public_url,
)


class MediaInputUnsupportedError(RuntimeError):
    """No available transport satisfies the provider's declared media inputs.

    Carries only provider/capability identifiers — never paths or URLs.
    """

    error_code = "media_input_unsupported"

    def __init__(
        self,
        provider: str,
        capability: str,
        accepted: tuple[MediaInput, ...],
        available: tuple[MediaInput, ...],
    ):
        self.provider = provider
        self.capability = capability
        self.accepted = accepted
        self.available = available
        super().__init__(
            f"provider {provider!r} capability {capability!r} accepts "
            f"{[item.value for item in accepted]} but available transports are "
            f"{[item.value for item in available]}"
        )


@dataclass
class MediaRequest:
    """A gateway-local media asset that must reach a provider."""

    session_id: str
    upload_id: str
    path: Path
    file_name: str
    mime_type: str
    size_bytes: int


@dataclass
class PreparedMedia:
    """The provider-facing form of a media asset plus its cleanup handle."""

    input_type: MediaInput
    transport_id: str
    local_path: Path | None = None
    data_loader: Any | None = None
    base64_payload: str | None = None
    url: str | None = None
    cleanup_ref: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    def audio_reference(self) -> str:
        """Best URL-like reference for clients that still expect a URL.

        Only meaningful for ``public_url``; other transports raise so misuse
        is caught loudly instead of silently uploading to the wrong place.
        """
        if self.input_type != MediaInput.public_url or not self.url:
            raise MediaInputUnsupportedError(
                self.transport_id,
                "public_url",
                (MediaInput.public_url,),
                (self.input_type,),
            )
        return self.url


class MediaTransport(Protocol):
    capability: MediaInput
    transport_id: str

    async def prepare(self, request: MediaRequest) -> PreparedMedia: ...

    async def cleanup(self, prepared: PreparedMedia) -> None: ...


class LocalPathTransport:
    capability = MediaInput.local_path
    transport_id = "local_path"

    async def prepare(self, request: MediaRequest) -> PreparedMedia:
        return PreparedMedia(
            input_type=self.capability,
            transport_id=self.transport_id,
            local_path=request.path,
        )

    async def cleanup(self, prepared: PreparedMedia) -> None:
        return None


class BinaryUploadTransport:
    """Streams file bytes so the provider API can receive them directly."""

    capability = MediaInput.binary_upload
    transport_id = "binary_upload"

    def __init__(self, chunk_size: int = 1024 * 1024):
        self.chunk_size = chunk_size

    async def prepare(self, request: MediaRequest) -> PreparedMedia:
        def iter_chunks() -> Any:
            with request.path.open("rb") as source:
                while chunk := source.read(self.chunk_size):
                    yield chunk

        return PreparedMedia(
            input_type=self.capability,
            transport_id=self.transport_id,
            local_path=request.path,
            data_loader=iter_chunks,
        )

    async def cleanup(self, prepared: PreparedMedia) -> None:
        return None


class Base64InlineTransport:
    capability = MediaInput.base64_inline
    transport_id = "base64_inline"

    def __init__(self, max_bytes: int = BASE64_INLINE_MAX_BYTES):
        self.max_bytes = max_bytes

    async def prepare(self, request: MediaRequest) -> PreparedMedia:
        if request.size_bytes > self.max_bytes:
            raise ValueError(
                "media exceeds the base64_inline size limit"
            )
        payload = base64.b64encode(request.path.read_bytes()).decode("ascii")
        return PreparedMedia(
            input_type=self.capability,
            transport_id=self.transport_id,
            base64_payload=payload,
        )

    async def cleanup(self, prepared: PreparedMedia) -> None:
        return None


class ObjectStoreTransport:
    """Temporary object storage adapter (OSS/S3-style) for public_url.

    Randomized keys, short-lived signed URLs, and best-effort deletion after
    the job finishes, fails, or is cancelled.
    """

    capability = MediaInput.public_url
    transport_id = "object_store"

    def __init__(self, oss_client: Any, prefix: str | None = None):
        self.oss = oss_client
        self.prefix = prefix

    def resolved_prefix(self) -> str:
        if self.prefix:
            return self.prefix
        # Defer the settings lookup until media is actually prepared so
        # building the transport registry never touches the OSS client.
        return getattr(
            getattr(self.oss, "settings", None), "oss_prefix", "memecho-tmp"
        )

    async def prepare(self, request: MediaRequest) -> PreparedMedia:
        from .providers.oss import make_oss_key

        key = make_oss_key(
            self.resolved_prefix(), request.session_id, request.upload_id, request.file_name
        )
        await self.oss.upload_file(key, request.path, request.mime_type)
        url = await self.oss.signed_url(key)
        return PreparedMedia(
            input_type=self.capability,
            transport_id=self.transport_id,
            url=url,
            cleanup_ref=key,
        )

    async def cleanup(self, prepared: PreparedMedia) -> None:
        if prepared.cleanup_ref is None:
            return
        try:
            await self.oss.delete(prepared.cleanup_ref)
        except Exception:
            log.warning(
                "object store cleanup failed transport=%s", self.transport_id,
                exc_info=True,
            )


def select_transport(
    accepted: tuple[MediaInput, ...] | list[MediaInput],
    available: list[MediaTransport],
) -> MediaTransport | None:
    """Pick the first available transport in the provider's preference order."""
    by_capability = {transport.capability: transport for transport in available}
    for wanted in accepted:
        transport = by_capability.get(MediaInput(wanted))
        if transport is not None:
            return transport
    return None


def default_transports(
    oss_client: Any | None,
    *,
    oss_prefix: str | None = None,
    base64_max_bytes: int = BASE64_INLINE_MAX_BYTES,
) -> list[MediaTransport]:
    """The gateway's transport registry. Object storage is optional."""
    transports: list[MediaTransport] = [
        BinaryUploadTransport(),
        LocalPathTransport(),
        Base64InlineTransport(max_bytes=base64_max_bytes),
    ]
    if oss_client is not None:
        transports.append(ObjectStoreTransport(oss_client, prefix=oss_prefix))
    return transports


def accepted_media_inputs(client: Any, fallback: tuple[MediaInput, ...]) -> tuple[MediaInput, ...]:
    """Read a provider's declared media inputs, tolerating legacy clients."""
    declared = getattr(client, "media_inputs", None)
    if not declared:
        return fallback
    return tuple(MediaInput(item) for item in declared)


def compatible_media_inputs(
    clients: list[Any], fallback: tuple[MediaInput, ...]
) -> tuple[MediaInput, ...]:
    """Intersection of declared inputs across every configured provider.

    Preserves the first client's preference order so the pipeline can honor
    provider-declared transport preferences.
    """
    configured = [client for client in clients if client is not None]
    if not configured:
        return ()
    sets = [set(accepted_media_inputs(client, fallback)) for client in configured]
    intersection = set.intersection(*sets)
    ordered = [
        item for item in accepted_media_inputs(configured[0], fallback)
        if item in intersection
    ]
    return tuple(ordered)
