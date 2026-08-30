from __future__ import annotations

import pytest

from memecho_gateway.__main__ import runtime_bind


def test_sidecar_bind_defaults_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMECHO_GATEWAY_HOST", raising=False)
    monkeypatch.delenv("MEMECHO_GATEWAY_PORT", raising=False)
    assert runtime_bind() == ("127.0.0.1", 8787)


def test_sidecar_bind_accepts_supervisor_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMECHO_GATEWAY_HOST", "127.0.0.1")
    monkeypatch.setenv("MEMECHO_GATEWAY_PORT", "43127")
    assert runtime_bind() == ("127.0.0.1", 43127)


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "::1"])
def test_sidecar_rejects_non_contract_hosts(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setenv("MEMECHO_GATEWAY_HOST", host)
    with pytest.raises(SystemExit, match="must be 127.0.0.1"):
        runtime_bind()


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_sidecar_rejects_invalid_ports(
    monkeypatch: pytest.MonkeyPatch, port: str
) -> None:
    monkeypatch.setenv("MEMECHO_GATEWAY_PORT", port)
    with pytest.raises(SystemExit, match="MEMECHO_GATEWAY_PORT"):
        runtime_bind()
