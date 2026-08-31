import asyncio
import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from websockets.exceptions import ConnectionClosed

from memecho_gateway import main
from memecho_gateway.config import Settings, get_settings
from memecho_gateway.realtime import (
    BailianRealtimeClient,
    RealtimeConfigurationError,
    RealtimeUpstreamDisconnected,
    _retryable_provider_error,
    build_realtime_url,
)


class FakeUpstreamSocket:
    def __init__(self) -> None:
        self.sent: list[dict | bytes] = []
        self.incoming: asyncio.Queue[object] = asyncio.Queue()
        self.closed = False

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message if isinstance(message, bytes) else json.loads(message))

    async def recv(self) -> str | bytes:
        item = await self.incoming.get()
        if isinstance(item, Exception):
            raise item
        if isinstance(item, bytes):
            return item
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


def test_build_realtime_url_keeps_inference_endpoint_fixed():
    url = build_realtime_url(
        "wss://workspace.example/api-ws/v1/inference",
        "qwen-audio-3.0-asr-flash-streaming",
    )
    assert url == "wss://workspace.example/api-ws/v1/inference"


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
    assert connection["additional_headers"]["OpenAI-Beta"] == "realtime=v1"
    assert connection["ping_interval"] == 20.0
    update = socket.sent[0]
    assert update["type"] == "session.update"
    assert update["session"]["input_audio_format"] == "pcm"
    assert update["session"]["sample_rate"] == 16000
    assert (
        update["session"]["input_audio_transcription"]["model"]
        == "qwen3-asr-flash-realtime"
    )
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


async def test_client_supports_qwen_audio_inference_protocol():
    socket = FakeUpstreamSocket()
    await socket.incoming.put(
        json.dumps({"header": {"task_id": "server", "event": "task-started"}, "payload": {}})
    )

    async def connector(url: str, **_kwargs):
        assert url.endswith("/api-ws/v1/inference")
        return socket

    client = BailianRealtimeClient(
        realtime_settings(
            bailian_realtime_ws_url="wss://workspace.example/api-ws/v1/inference",
            bailian_realtime_model="qwen-audio-3.0-asr-flash-streaming",
        ),
        connector=connector,
    )
    await client.start()
    run_task = socket.sent[0]
    assert isinstance(run_task, dict)
    assert run_task["header"]["action"] == "run-task"
    assert run_task["payload"]["model"] == "qwen-audio-3.0-asr-flash-streaming"
    assert await client.receive_event() == {
        "type": "connection.state",
        "state": "connected",
    }

    pcm = b"\x01\x02" * 1600
    await client.send_audio(pcm)
    assert socket.sent[1] == pcm
    await socket.incoming.put(
        json.dumps(
            {
                "header": {"task_id": client.task_id, "event": "result-generated"},
                "payload": {
                    "output": {
                        "sentence": {
                            "begin_time": 10,
                            "end_time": 100,
                            "text": "实时识别结果",
                            "sentence_end": True,
                        }
                    }
                },
            }
        )
    )
    assert await client.receive_event() == {
        "type": "transcript.final",
        "text": "实时识别结果",
        "start_ms": 10,
        "end_ms": 100,
    }
    await client.finish()
    finish_task = socket.sent[-1]
    assert isinstance(finish_task, dict)
    assert finish_task["header"]["action"] == "finish-task"


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


# ---------------------------------------------------------------------------
# Edge-case / unit tests for realtime.py internals
# ---------------------------------------------------------------------------


class TestRetryableProviderError:
    def test_transient_codes_are_retryable(self):
        for code in ("ServiceUnavailable", "InternalError", "Timeout", "RateLimit"):
            assert _retryable_provider_error(code) is True

    def test_permanent_codes_are_not_retryable(self):
        for code in ("auth_error", "InvalidParameter", "PermissionDenied", "Forbidden", "UnsupportedFormat", "QuotaExceeded"):
            assert _retryable_provider_error(code) is False

    def test_case_insensitive(self):
        assert _retryable_provider_error("AUTHFAILED") is False
        assert _retryable_provider_error("serviceunavailable") is True

    def test_empty_string_is_retryable(self):
        assert _retryable_provider_error("") is True


class TestAudioMsCalculation:
    def test_audio_ms_derives_from_byte_count(self):
        client = BailianRealtimeClient(realtime_settings())
        assert client.audio_ms == 0
        # 16000 Hz * 2 bytes/sample = 32000 bytes/sec → 32 bytes = 1 ms
        client.audio_bytes = 32000
        assert client.audio_ms == 1000
        client.audio_bytes = 16000
        assert client.audio_ms == 500


class TestSendAudioEdgeCases:
    async def test_empty_pcm_is_ignored(self):
        socket = FakeUpstreamSocket()

        async def connector(*_a, **_kw):
            return socket

        client = BailianRealtimeClient(realtime_settings(), connector=connector)
        await client.start()
        await client.send_audio(b"")
        # Only session.update was sent, no audio append
        assert len(socket.sent) == 1

    async def test_raises_when_socket_is_none(self):
        client = BailianRealtimeClient(realtime_settings())
        with pytest.raises(RealtimeUpstreamDisconnected):
            await client.send_audio(b"\x00" * 100)

    async def test_raises_when_socket_is_closed(self):
        socket = FakeUpstreamSocket()

        async def connector(*_a, **_kw):
            return socket

        client = BailianRealtimeClient(realtime_settings(), connector=connector)
        await client.start()
        client.closed = True
        with pytest.raises(RealtimeUpstreamDisconnected):
            await client.send_audio(b"\x00" * 100)

    async def test_raises_on_connection_closed(self):
        socket = FakeUpstreamSocket()

        async def connector(*_a, **_kw):
            return socket

        original_send = socket.send

        async def failing_send(message: str) -> None:
            raise ConnectionClosed(None, None)  # type: ignore[arg-type]

        client = BailianRealtimeClient(realtime_settings(), connector=connector)
        await client.start()
        socket.send = failing_send  # type: ignore[assignment]
        with pytest.raises(RealtimeUpstreamDisconnected):
            await client.send_audio(b"\x00" * 100)


class TestFinishEdgeCases:
    async def test_finish_raises_on_connection_closed(self):
        socket = FakeUpstreamSocket()

        async def connector(*_a, **_kw):
            return socket

        client = BailianRealtimeClient(realtime_settings(), connector=connector)
        await client.start()
        # Make send fail
        async def failing_send(message: str) -> None:
            raise ConnectionClosed(None, None)  # type: ignore[arg-type]

        socket.send = failing_send  # type: ignore[assignment]
        with pytest.raises(RealtimeUpstreamDisconnected):
            await client.finish()

    async def test_finish_is_idempotent(self):
        socket = FakeUpstreamSocket()

        async def connector(*_a, **_kw):
            return socket

        client = BailianRealtimeClient(realtime_settings(), connector=connector)
        await client.start()
        await client.finish()
        assert client.finished is True
        # Second call is a no-op
        await client.finish()
        # Only session.update + session.finish were sent
        assert len(socket.sent) == 2


class TestReceiveEventEdgeCases:
    async def test_returns_disconnected_when_socket_is_none(self):
        client = BailianRealtimeClient(realtime_settings())
        event = await client.receive_event()
        assert event["type"] == "error"
        assert event["code"] == "upstream_disconnected"
        assert event["retryable"] is True

    async def test_returns_disconnected_when_closed(self):
        socket = FakeUpstreamSocket()

        async def connector(*_a, **_kw):
            return socket

        client = BailianRealtimeClient(realtime_settings(), connector=connector)
        await client.start()
        client.closed = True
        event = await client.receive_event()
        assert event["type"] == "error"
        assert event["code"] == "upstream_disconnected"

    async def test_handles_malformed_json(self):
        socket = FakeUpstreamSocket()

        async def connector(*_a, **_kw):
            return socket

        client = BailianRealtimeClient(realtime_settings(), connector=connector)
        await client.start()
        await socket.incoming.put("not valid json {{{")
        assert await client.receive_event() is None

    async def test_handles_bytes_payload(self):
        socket = FakeUpstreamSocket()

        async def connector(*_a, **_kw):
            return socket

        client = BailianRealtimeClient(realtime_settings(), connector=connector)
        await client.start()
        await socket.incoming.put(
            json.dumps({"type": "session.created"}).encode("utf-8")
        )
        assert await client.receive_event() == {
            "type": "connection.state",
            "state": "connected",
        }

    async def test_handles_non_dict_json(self):
        socket = FakeUpstreamSocket()

        async def connector(*_a, **_kw):
            return socket

        client = BailianRealtimeClient(realtime_settings(), connector=connector)
        await client.start()
        await socket.incoming.put('"just a string"')
        assert await client.receive_event() is None

    async def test_duplicate_session_created_is_filtered(self):
        socket = FakeUpstreamSocket()

        async def connector(*_a, **_kw):
            return socket

        client = BailianRealtimeClient(realtime_settings(), connector=connector)
        await client.start()
        await socket.incoming.put(json.dumps({"type": "session.created"}))
        first = await client.receive_event()
        assert first == {"type": "connection.state", "state": "connected"}
        # Second session.created should be filtered
        await socket.incoming.put(json.dumps({"type": "session.created"}))
        assert await client.receive_event() is None

    async def test_session_updated_also_emits_connected(self):
        socket = FakeUpstreamSocket()

        async def connector(*_a, **_kw):
            return socket

        client = BailianRealtimeClient(realtime_settings(), connector=connector)
        await client.start()
        await socket.incoming.put(json.dumps({"type": "session.updated"}))
        assert await client.receive_event() == {
            "type": "connection.state",
            "state": "connected",
        }

    async def test_transcription_failed_maps_to_error(self):
        socket = FakeUpstreamSocket()

        async def connector(*_a, **_kw):
            return socket

        client = BailianRealtimeClient(realtime_settings(), connector=connector)
        await client.start()
        await socket.incoming.put(
            json.dumps(
                {
                    "type": "conversation.item.input_audio_transcription.failed",
                    "error": {"code": "ServiceUnavailable", "message": "busy"},
                }
            )
        )
        event = await client.receive_event()
        assert event["type"] == "error"
        assert event["code"] == "ServiceUnavailable"
        assert event["retryable"] is True

    async def test_permanent_error_is_not_retryable(self):
        socket = FakeUpstreamSocket()

        async def connector(*_a, **_kw):
            return socket

        client = BailianRealtimeClient(realtime_settings(), connector=connector)
        await client.start()
        await socket.incoming.put(
            json.dumps(
                {
                    "type": "error",
                    "error": {"code": "InvalidParameter", "message": "bad input"},
                }
            )
        )
        event = await client.receive_event()
        assert event["retryable"] is False

    async def test_error_without_detail_dict(self):
        socket = FakeUpstreamSocket()

        async def connector(*_a, **_kw):
            return socket

        client = BailianRealtimeClient(realtime_settings(), connector=connector)
        await client.start()
        await socket.incoming.put(json.dumps({"type": "error"}))
        event = await client.receive_event()
        assert event["type"] == "error"
        assert event["code"] == "error"
        assert event["retryable"] is True

    async def test_unknown_event_types_return_none(self):
        socket = FakeUpstreamSocket()

        async def connector(*_a, **_kw):
            return socket

        client = BailianRealtimeClient(realtime_settings(), connector=connector)
        await client.start()
        await socket.incoming.put(json.dumps({"type": "some.future.event"}))
        assert await client.receive_event() is None

    async def test_final_transcript_advances_last_final_ms(self):
        socket = FakeUpstreamSocket()

        async def connector(*_a, **_kw):
            return socket

        client = BailianRealtimeClient(realtime_settings(), connector=connector)
        await client.start()
        client.audio_bytes = 32000  # 1000 ms
        await socket.incoming.put(
            json.dumps(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "第一段",
                }
            )
        )
        event1 = await client.receive_event()
        assert event1["start_ms"] == 0
        assert event1["end_ms"] == 1000
        assert client.last_final_ms == 1000

        client.audio_bytes = 64000  # 2000 ms
        await socket.incoming.put(
            json.dumps(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "第二段",
                }
            )
        )
        event2 = await client.receive_event()
        assert event2["start_ms"] == 1000
        assert event2["end_ms"] == 2000
