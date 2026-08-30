"""Standalone entry point for the bundled memEcho Gateway sidecar."""

from __future__ import annotations

import os

import uvicorn

from memecho_gateway.main import app


LOOPBACK_HOST = "127.0.0.1"


def runtime_bind() -> tuple[str, int]:
    """Read and validate the desktop supervisor's sidecar bind contract."""
    host = os.environ.get("MEMECHO_GATEWAY_HOST", LOOPBACK_HOST).strip()
    if host != LOOPBACK_HOST:
        raise SystemExit("MEMECHO_GATEWAY_HOST must be 127.0.0.1")
    raw_port = os.environ.get("MEMECHO_GATEWAY_PORT", "8787").strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise SystemExit("MEMECHO_GATEWAY_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise SystemExit("MEMECHO_GATEWAY_PORT must be between 1 and 65535")
    return host, port


def main() -> None:
    host, port = runtime_bind()
    uvicorn.run(
        app,
        host=host,
        port=port,
        workers=1,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
