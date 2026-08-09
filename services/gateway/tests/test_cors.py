from __future__ import annotations

import httpx
import pytest

from memecho_gateway.main import app


@pytest.mark.asyncio
async def test_local_frontend_origin_can_read_health() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        response = await client.get(
            "/v1/health",
            headers={"Origin": "http://localhost:1420"},
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:1420"


@pytest.mark.asyncio
async def test_unknown_origin_is_not_allowed() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        response = await client.get(
            "/v1/health",
            headers={"Origin": "https://untrusted.example"},
        )

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
