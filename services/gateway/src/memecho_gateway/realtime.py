"""Bailian Qwen-ASR realtime WebSocket adapter.

The public gateway events intentionally hide the provider protocol. Qwen-ASR
does not provide timestamps, so event positions are derived from forwarded
16 kHz, signed 16-bit, mono PCM byte counts.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Awaitable, Callable
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .config import Settings


RealtimeEvent = dict[str, str | int | bool]


class RealtimeSocket(Protocol):
    async def send(self, message: str | bytes) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


RealtimeConnector = Callable[..., Awaitable[RealtimeSocket]]


class RealtimeConfigurationError(RuntimeError):
    """Raised when the provider is selected without usable credentials."""


class RealtimeUpstreamDisconnected(RuntimeError):
    """Raised when the upstream connection drops while sending audio."""


def build_realtime_url(base_url: str, model: str) -> str:
    """Add the configured model and the official long-silence heartbeat flag."""
    parsed = urlsplit(base_url)
    if parsed.scheme != "wss" or not parsed.netloc:
        raise RealtimeConfigurationError(
            "BAILIAN_REALTIME_WS_URL must be a complete wss:// endpoint"
        )
    # Qwen-Audio/Fun-ASR use the DashScope inference protocol. The endpoint is
    # fixed and the model belongs in the run-task payload, not the URL.
    if parsed.path.rstrip("/").endswith("/inference"):
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)
        )
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["model"] = model
    query["heartbeat"] = "true"
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _event_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _retryable_provider_error(code: str) -> bool:
    normalized = code.casefold()
    permanent_markers = (
        "auth",
        "invalid",
        "permission",
        "forbidden",
        "unsupported",
        "quota",
    )
    return not any(marker in normalized for marker in permanent_markers)


class BailianRealtimeClient:
    """Translate desktop PCM frames and Bailian events in one isolated session."""

    def __init__(
        self,
        settings: Settings,
        connector: RealtimeConnector = connect,
    ) -> None:
        self.settings = settings
        self.connector = connector
        self.socket: RealtimeSocket | None = None
        self.audio_bytes = 0
        self.last_final_ms = 0
        self.connected_emitted = False
        self.finished = False
        self.closed = False
        self.inference_protocol = False
        self.task_id = str(uuid4())
        self.pending_events: list[RealtimeEvent] = []

    @property
    def audio_ms(self) -> int:
        bytes_per_second = self.settings.bailian_realtime_sample_rate * 2
        return int(self.audio_bytes * 1000 / bytes_per_second)

    async def start(self) -> None:
        if not self.settings.bailian_audio_api_key:
            raise RealtimeConfigurationError("BAILIAN_AUDIO_API_KEY is required")
        url = build_realtime_url(
            self.settings.bailian_realtime_ws_url,
            self.settings.bailian_realtime_model,
        )
        self.inference_protocol = urlsplit(url).path.rstrip("/").endswith("/inference")
        headers = {
            "Authorization": f"Bearer {self.settings.bailian_audio_api_key}",
        }
        if not self.inference_protocol:
            headers["OpenAI-Beta"] = "realtime=v1"
        if self.settings.bailian_workspace_id:
            headers["X-DashScope-WorkSpace"] = self.settings.bailian_workspace_id
        self.socket = await self.connector(
            url,
            additional_headers=headers,
            ping_interval=self.settings.bailian_realtime_heartbeat_seconds,
            ping_timeout=self.settings.bailian_realtime_heartbeat_timeout_seconds,
            close_timeout=self.settings.bailian_realtime_close_timeout_seconds,
        )
        if self.inference_protocol:
            await self.socket.send(
                json.dumps(
                    {
                        "header": {
                            "action": "run-task",
                            "task_id": self.task_id,
                            "streaming": "duplex",
                        },
                        "payload": {
                            "task_group": "audio",
                            "task": "asr",
                            "function": "recognition",
                            "model": self.settings.bailian_realtime_model,
                            "parameters": {
                                "format": "pcm",
                                "sample_rate": self.settings.bailian_realtime_sample_rate,
                                "language_hints": [self.settings.bailian_realtime_language],
                                "heartbeat": True,
                            },
                            "input": {},
                        },
                    },
                    ensure_ascii=False,
                )
            )
            try:
                raw = await self.socket.recv()
                started = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            except (ConnectionClosed, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RealtimeUpstreamDisconnected(str(exc)) from exc
            header = started.get("header") if isinstance(started, dict) else None
            if not isinstance(header, dict) or header.get("event") != "task-started":
                message = header.get("error_message") if isinstance(header, dict) else None
                raise RealtimeConfigurationError(message or "Realtime ASR task did not start")
            self.connected_emitted = True
            self.pending_events.append({"type": "connection.state", "state": "connected"})
            return
        await self.socket.send(
            json.dumps(
                {
                    "event_id": _event_id("session"),
                    "type": "session.update",
                    "session": {
                        "input_audio_format": "pcm",
                        "sample_rate": self.settings.bailian_realtime_sample_rate,
                        "input_audio_transcription": {
                            "model": self.settings.bailian_realtime_model,
                            "language": self.settings.bailian_realtime_language
                        },
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": self.settings.bailian_realtime_vad_threshold,
                            "silence_duration_ms": (
                                self.settings.bailian_realtime_silence_duration_ms
                            ),
                        },
                    },
                },
                ensure_ascii=False,
            )
        )

    async def send_audio(self, pcm: bytes) -> None:
        if not pcm:
            return
        if self.socket is None or self.closed:
            raise RealtimeUpstreamDisconnected("realtime upstream is not connected")
        try:
            if self.inference_protocol:
                await self.socket.send(pcm)
                self.audio_bytes += len(pcm)
                return
            await self.socket.send(
                json.dumps(
                    {
                        "event_id": _event_id("audio"),
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(pcm).decode("ascii"),
                    }
                )
            )
            self.audio_bytes += len(pcm)
        except (ConnectionClosed, OSError) as exc:
            raise RealtimeUpstreamDisconnected(str(exc)) from exc

    async def finish(self) -> None:
        if self.socket is None or self.closed or self.finished:
            return
        try:
            if self.inference_protocol:
                await self.socket.send(
                    json.dumps(
                        {
                            "header": {
                                "action": "finish-task",
                                "task_id": self.task_id,
                                "streaming": "duplex",
                            },
                            "payload": {"input": {}},
                        }
                    )
                )
                self.finished = True
                return
            await self.socket.send(
                json.dumps(
                    {"event_id": _event_id("finish"), "type": "session.finish"}
                )
            )
            self.finished = True
        except (ConnectionClosed, OSError) as exc:
            raise RealtimeUpstreamDisconnected(str(exc)) from exc

    async def receive_event(self) -> RealtimeEvent | None:
        if self.pending_events:
            return self.pending_events.pop(0)
        if self.socket is None or self.closed:
            return {
                "type": "error",
                "code": "upstream_disconnected",
                "message": "实时字幕连接已断开，请重试。",
                "retryable": True,
            }
        try:
            raw = await self.socket.recv()
        except (ConnectionClosed, OSError):
            return {
                "type": "error",
                "code": "upstream_disconnected",
                "message": "实时字幕连接已断开，请重试。",
                "retryable": True,
            }
        try:
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if self.inference_protocol:
            header = payload.get("header")
            header = header if isinstance(header, dict) else {}
            event = str(header.get("event") or "")
            if event == "result-generated":
                body = payload.get("payload")
                body = body if isinstance(body, dict) else {}
                output = body.get("output")
                output = output if isinstance(output, dict) else {}
                sentence = output.get("sentence")
                sentence = sentence if isinstance(sentence, dict) else {}
                if sentence.get("heartbeat"):
                    return None
                text = str(sentence.get("text") or "")
                if not text:
                    return None
                if sentence.get("sentence_end"):
                    return {
                        "type": "transcript.final",
                        "text": text,
                        "start_ms": int(sentence.get("begin_time") or 0),
                        "end_ms": int(sentence.get("end_time") or self.audio_ms),
                    }
                return {
                    "type": "transcript.partial",
                    "text": text,
                    "at_ms": int(sentence.get("end_time") or self.audio_ms),
                }
            if event == "task-finished":
                self.finished = True
                return {"type": "connection.state", "state": "offline"}
            if event == "task-failed":
                code = str(header.get("error_code") or "upstream_error")
                return {
                    "type": "error",
                    "code": code,
                    "message": str(header.get("error_message") or "实时字幕服务返回错误。"),
                    "retryable": _retryable_provider_error(code),
                }
            return None
        event_type = payload.get("type")
        if event_type in {"session.created", "session.updated"}:
            if self.connected_emitted:
                return None
            self.connected_emitted = True
            return {"type": "connection.state", "state": "connected"}
        if event_type == "session.finished":
            self.finished = True
            return {"type": "connection.state", "state": "offline"}
        if event_type == "conversation.item.input_audio_transcription.text":
            text = f"{payload.get('text', '')}{payload.get('stash', '')}"
            return {
                "type": "transcript.partial",
                "text": text,
                "at_ms": self.audio_ms,
            }
        if event_type == "conversation.item.input_audio_transcription.completed":
            end_ms = max(self.last_final_ms, self.audio_ms)
            event: RealtimeEvent = {
                "type": "transcript.final",
                "text": str(payload.get("transcript", "")),
                "start_ms": self.last_final_ms,
                "end_ms": end_ms,
            }
            self.last_final_ms = end_ms
            return event
        if event_type in {
            "error",
            "conversation.item.input_audio_transcription.failed",
        }:
            detail = payload.get("error")
            detail = detail if isinstance(detail, dict) else {}
            code = str(detail.get("code") or event_type or "upstream_error")
            return {
                "type": "error",
                "code": code,
                "message": str(detail.get("message") or "实时字幕服务返回错误。"),
                "retryable": _retryable_provider_error(code),
            }
        return None

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.socket is not None:
            await self.socket.close()

