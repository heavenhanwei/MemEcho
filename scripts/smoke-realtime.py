from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
import websockets

from memecho_gateway.config import Settings


async def smoke(base_url: str, env_file: Path, token_override: str | None) -> int:
    settings = Settings(_env_file=env_file)
    token = token_override or settings.memecho_demo_token
    if not token:
        raise RuntimeError("Gateway token is empty; pass --token for the local smoke test")
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "title": "Realtime smoke",
        "context": "work",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "source_mode": "microphone",
        "marks": [],
    }
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(f"{base_url}/v1/sessions", headers=headers, json=payload)
        response.raise_for_status()
        session_id = response.json()["id"]

    websocket_base = base_url.replace("http://", "ws://").replace("https://", "wss://")
    uri = f"{websocket_base}/v1/sessions/{session_id}/live?token={quote(token)}"
    async with websockets.connect(uri, max_size=1024 * 1024) as socket:
        first = json.loads(await asyncio.wait_for(socket.recv(), timeout=20))
        print(
            json.dumps(
                {
                    "first_type": first.get("type"),
                    "first_state": first.get("state"),
                    "first_code": first.get("code"),
                },
                ensure_ascii=False,
            )
        )
        if first.get("type") == "error":
            return 1

        # One second of PCM16 LE mono silence at 16 kHz validates transport and
        # upstream session lifecycle without uploading user content.
        await socket.send(bytes(32_000))
        await socket.send("end")
        events: list[dict[str, object]] = []
        for _ in range(4):
            try:
                event = json.loads(await asyncio.wait_for(socket.recv(), timeout=8))
            except (TimeoutError, websockets.ConnectionClosed):
                break
            events.append(
                {
                    "type": event.get("type"),
                    "state": event.get("state"),
                    "code": event.get("code"),
                }
            )
        print(json.dumps(events, ensure_ascii=False))
        return 0 if any(event.get("state") == "offline" for event in events) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the memEcho realtime ASR bridge")
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--env-file", type=Path, default=Path("services/gateway/.env"))
    parser.add_argument("--token", help="Local Gateway token; never printed")
    args = parser.parse_args()
    return asyncio.run(smoke(args.base_url.rstrip("/"), args.env_file, args.token))


if __name__ == "__main__":
    raise SystemExit(main())
