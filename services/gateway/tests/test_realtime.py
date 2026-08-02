import asyncio
import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from memecho_gateway import main
from memecho_gateway.config import Settings, get_settings
from memecho_gateway.realtime import (
    BailianRealtimeClient,
    RealtimeConfigurationError,
    build_realtime_url,
)


class FakeUpstreamSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue[object] = asyncio.Queue()
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        item = await self.incoming.get()
        if isinstance(item, Exception):
            raise item
        return str(item)

    async def close(self) -> None:
        self.closed = True


def realtime_settings(**updates) -> Settings:
    values = {
        "bailian_audio_api_key": "sk-test",
        "bailian_realtime_ws_url": (
            "wss://workspace.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime"
        ),
    }
    values.update(updates)
    return Settings(**values)


def test_build_realtime_url_keeps_configuration_explicit():
    url = build_realtime_url(
        "wss://workspace.example/realtime?trace=on&model=old",
        "qwen3-asr-flash-realtime-2026-02-10",
    )

    assert "model=qwen3-asr-flash-realtime-2026-02-10" in url
    assert "heartbeat=true" in url
    assert "trace=on" in url


async def test_client_forwards_pcm_and_maps_official_events():
    socket = FakeUpstreamSocket()
    connection: dict = {}

    async def connector(url: str, **kwargs):
        connection.update(url=url, **kwargs)
        return socket

    client = BailianRealtimeClient(realtime_settings(), connector=connector)
    await client.start()

    assert connection["url"].endswith(
        "model=qwen3-asr-flash-realtime&heartbeat=true"
    )
    assert connection["additional_headers"]["Authorization"] == "Bearer sk-test"
    assert connection["ping_interval"] == 20.0
    update = socket.sent[0]
    assert update["type"] == "session.update"
    assert update["session"]["input_audio_format"] == "pcm"
    assert update["session"]["sample_rate"] == 16000
    assert update["session"]["turn_detection"]["type"] == "server_vad"

    pcm = b"\x01\x02" * 16000
    await client.send_audio(pcm)
    append = socket.sent[1]
    assert append["type"] == "input_audio_buffer.append"
    assert base64.b64decode(append["audio"]) == pcm

    await socket.incoming.put(json.dumps({"type": "session.created"}))
    assert await client.receive_event() == {
        "type": "connection.state",
        "state": "connected",
    }

    await socket.incoming.put(
        json.dumps(
            {
                "type": "conversation.item.input_audio_transcription.text",
                "text": "已确认",
                "stash": "临时内容",
                "emotion": "neutral",
            }
        )
    )
    assert await client.receive_event() == {
        "type": "transcript.partial",
        "text": "已确认临时内容",
        "at_ms": 1000,
    }

    await socket.incoming.put(
        json.dumps(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "最终文本",
                "emotion": "neutral",
            }
        )
    )
    assert await client.receive_event() == {
        "type": "transcript.final",
        "text": "最终文本",
        "start_ms": 0,
        "end_ms": 1000,
    }

    await client.finish()
    assert socket.sent[-1]["type"] == "session.finish"
    await socket.incoming.put(json.dumps({"type": "session.finished"}))
    assert await client.receive_event() == {
        "type": "connection.state",
        "state": "offline",
    }
    await client.close()
    await client.close()
    assert socket.closed is True


async def test_client_maps_disconnect_and_provider_errors_as_retryable():
    socket = FakeUpstreamSocket()

    async def connector(*_args, **_kwargs):
        return socket

    client = BailianRealtimeClient(realtime_settings(), connector=connector)
    await client.start()
    await socket.incoming.put(OSError("connection reset"))
    assert await client.receive_event() == {
        "type": "error",
        "code": "upstream_disconnected",
        "message": "实时字幕连接已断开，请重试。",
        "retryable": True,
    }

    await socket.incoming.put(
        json.dumps(
            {
                "type": "error",
                "error": {"code": "ServiceUnavailable", "message": "try later"},
            }
        )
    )
    assert await client.receive_event() == {
        "type": "error",
        "code": "ServiceUnavailable",
        "message": "try later",
        "retryable": True,
    }


async def test_client_rejects_missing_key_and_non_tls_endpoint():
    with pytest.raises(RealtimeConfigurationError, match="API_KEY"):
        await BailianRealtimeClient(
            realtime_settings(bailian_audio_api_key="")
        ).start()

    with pytest.raises(RealtimeConfigurationError, match="wss"):
        build_realtime_url("ws://insecure.example/realtime", "model")


class FakeRealtimeClient:
    instances: list["FakeRealtimeClient"] = []

    def __init__(self, _settings: Settings) -> None:
        self.audio: list[bytes] = []
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self.closed = False
        self.__class__.instances.append(self)

    async def start(self) -> None:
        await self.events.put({"type": "connection.state", "state": "connected"})

    async def send_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)
        await self.events.put(
            {"type": "transcript.partial", "text": "实时文本", "at_ms": 100}
        )

    async def finish(self) -> None:
        await self.events.put(
            {
                "type": "transcript.final",
                "text": "最终文本",
                "start_ms": 0,
                "end_ms": 100,
            }
        )
        await self.events.put({"type": "connection.state", "state": "offline"})

    async def receive_event(self):
        return await self.events.get()

    async def close(self) -> None:
        self.closed = True


def test_live_endpoint_forwards_desktop_pcm_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    current = get_settings()
    previous_provider = current.memecho_provider
    previous_data_dir = current.memecho_data_dir
    current.memecho_provider = "bailian"
    current.memecho_data_dir = tmp_path
    FakeRealtimeClient.instances.clear()
    monkeypatch.setattr(main, "realtime_client_factory", FakeRealtimeClient)
    try:
        with TestClient(main.app) as client:
            created = client.post(
                "/v1/sessions",
                headers={"Authorization": "Bearer change-me"},
                json={
                    "title": "Realtime test",
                    "context": "work",
                    "occurred_at": "2026-08-02T10:00:00+08:00",
                    "source_mode": "microphone",
                },
            ).json()
            with client.websocket_connect(
                f"/v1/sessions/{created['id']}/live?token=change-me"
            ) as websocket:
                assert websocket.receive_json() == {
                    "type": "connection.state",
                    "state": "connected",
                }
                websocket.send_bytes(b"\x00\x01" * 1600)
                assert websocket.receive_json() == {
                    "type": "transcript.partial",
                    "text": "实时文本",
                    "at_ms": 100,
                }
                websocket.send_text("end")
                assert websocket.receive_json()["type"] == "transcript.final"
                assert websocket.receive_json() == {
                    "type": "connection.state",
                    "state": "offline",
                }
    finally:
        current.memecho_provider = previous_provider
        current.memecho_data_dir = previous_data_dir

    upstream = FakeRealtimeClient.instances[-1]
    assert upstream.audio == [b"\x00\x01" * 1600]
    assert upstream.closed is True
