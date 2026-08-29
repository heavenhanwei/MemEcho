"""Credential reference resolution for Provider Profiles.

Secrets are never stored in SQLite, config files, or API payloads. A profile
only carries a ``credential_ref``; the gateway resolves the actual secret at
the moment a provider request is issued.

Supported reference schemes:

- ``env:<VAR_NAME>`` (or a bare name): environment variable lookup. This is
  the headless / development compatibility path (``.env`` stays the only
  secret source outside the OS credential store).
- ``wincred:<target>`` (or a bare name on Windows): Windows Credential
  Manager lookup. The desktop native layer writes entries under the
  ``memecho:`` target prefix, so refs like ``wincred:memecho:profile:...``
  resolve directly.

Bare refs are tried against every resolver in chain order.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Protocol

log = logging.getLogger(__name__)


class CredentialResolver(Protocol):
    def resolve(self, ref: str) -> str | None: ...


class EnvCredentialResolver:
    """Resolve ``env:<NAME>`` refs (or bare names) from the environment."""

    def resolve(self, ref: str) -> str | None:
        name = ref.removeprefix("env:")
        value = os.environ.get(name)
        return value or None


class WindowsCredentialResolver:
    """Resolve ``wincred:<target>`` refs from Windows Credential Manager.

    Reads generic credentials via Advapi32 so the native desktop layer keeps
    ownership of secrets: nothing is cached, logged, or returned except the
    secret string handed straight to the outbound provider request.
    """

    scheme = "wincred:"

    def resolve(self, ref: str) -> str | None:
        target = ref.removeprefix(self.scheme)
        if not target:
            return None
        try:
            return _read_windows_credential(target)
        except Exception:
            log.warning("Windows Credential Manager lookup failed", exc_info=True)
            return None


def _read_windows_credential(target: str) -> str | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class _Filetime(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class _CredentialW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", _Filetime),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.windll.advapi32  # type: ignore[attr-defined]
    cred = ctypes.POINTER(_CredentialW)()
    # CRED_TYPE_GENERIC = 1. The desktop layer writes secrets as UTF-8 blobs.
    if not advapi32.CredReadW(target, 1, 0, ctypes.byref(cred)):
        return None
    try:
        entry = cred.contents
        blob = ctypes.string_at(entry.CredentialBlob, entry.CredentialBlobSize)
        return blob.decode("utf-8")
    finally:
        advapi32.CredFree(cred)


class ChainedCredentialResolver:
    """Try each resolver in order; first non-empty secret wins."""

    def __init__(self, resolvers: list[CredentialResolver]):
        self.resolvers = resolvers

    def resolve(self, ref: str) -> str | None:
        if not ref:
            return None
        candidates = [ref]
        if not ref.startswith(("env:", "wincred:")):
            candidates = [f"env:{ref}", f"wincred:{ref}"]
        for candidate in candidates:
            for resolver in self.resolvers:
                if isinstance(resolver, EnvCredentialResolver) and candidate.startswith(
                    "wincred:"
                ):
                    continue
                if isinstance(
                    resolver, WindowsCredentialResolver
                ) and candidate.startswith("env:"):
                    continue
                value = resolver.resolve(candidate)
                if value:
                    return value
        return None


def build_default_credential_resolver() -> CredentialResolver:
    resolvers: list[CredentialResolver] = [EnvCredentialResolver()]
    if sys.platform == "win32":
        resolvers.append(WindowsCredentialResolver())
    return ChainedCredentialResolver(resolvers)
