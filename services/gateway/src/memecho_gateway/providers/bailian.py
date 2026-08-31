from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..config import Settings
from ..contracts import validate_result
from ..models import AnalysisResult


SYSTEM_PROMPT = """你是 memEcho 对话分析服务。只分析可观察的表达，不诊断心理状态，不推断隐藏意图。
严格输出 memEcho 1.1 JSON。事实主张只表示可核验类型，不表示已核实。明确行动必须是 discussed/confirmed，
分析建议必须是 suggested/proposed。每项重要解释必须引用 evidence_refs、confidence 和 alternatives。
VAD 表示情境中的表达状态，不表示真实内心。仅可使用输入中明确提供的语言和声学证据。
每项 evidence 必须包含 track 字段，表示该证据来源的音频轨。
model_errors 是限定到 track 的局部失败，不代表整场会话失败。必须优先检查
session.observations.evidence_availability：当 has_usable_text=true 且存在 aligned_segments 时，
必须分析这些可用文本，不得因为另一轨静音、超时或转写失败而声称“没有文本”“所有转写不可用”
或将 analysis_mode 设为 insufficient/text_only。失败轨只能作为缺失信号和不确定性说明，不能否定成功轨证据。
当存在 aligned_segments 时，minutes.summary 和 evidence 不得为空；evidence.id 必须优先使用输入片段的
evidence_id，segment_id/track/excerpt 必须忠实对应输入。即使谈话内容很短，也要概括实际说了什么，
不能返回视觉上空白的报告。"""


TEXT_ONLY_PROMPT = """TEXT-ONLY MODE: no audio or acoustic observation is available.
Set analysis_mode=text_only. Use only transcript and linguistic signals; list acoustic, pitch,
energy, speech_rate, and voice_quality in scope.signals_missing. Every VAD point must use
linguistic_weight=1 and acoustic_weight=0. Do not mention inferred pitch, loudness, pace,
pauses, voice quality, or audio emotion. Evidence must use only the exact evidence_id,
segment_id, and verbatim excerpt from session.observations.text_segments, with
source_type=transcript. If the text does not support a conclusion, omit it or record the
uncertainty instead of inventing evidence."""


class BailianProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def _chat_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> str:
        effective_key = api_key or self.settings.bailian_text_api_key
        effective_url = (base_url or self.settings.bailian_text_base_url).rstrip("/")
        effective_model = model or self.settings.bailian_text_model
        if not effective_url or not effective_key:
            raise RuntimeError("Bailian text endpoint is not configured")
        payload = {
            "model": effective_model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 16384,
            "enable_thinking": False,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {effective_key}"}
        url = f"{effective_url}/chat/completions"
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
            if match:
                return json.loads(match.group(1))
            match = re.search(r"\{.*\}", text, re.S)
            if match:
                return json.loads(match.group(0))
            raise

    @staticmethod
    def _enforce_conservative_recommendations(result: dict[str, Any]) -> None:
        """Recommendations can never be presented as discussed commitments."""
        minutes = result.get("minutes")
        if not isinstance(minutes, dict):
            return
        recommendations = minutes.get("recommendations")
        if not isinstance(recommendations, list):
            return
        for item in recommendations:
            if isinstance(item, dict):
                item["origin"] = "suggested"
                item["status"] = "proposed"

    @staticmethod
    def _aligned_content_errors(
        result: dict[str, Any], has_aligned_text: bool
    ) -> list[str]:
        if not has_aligned_text:
            return []
        errors: list[str] = []
        if result.get("analysis_mode") == "insufficient":
            errors.append(
                "analysis_mode cannot be insufficient when aligned transcript evidence exists"
            )
        elif result.get("analysis_mode") == "text_only":
            errors.append(
                "analysis_mode cannot be text_only when aligned audio transcript evidence exists"
            )
        minutes = result.get("minutes")
        summary = minutes.get("summary") if isinstance(minutes, dict) else None
        if not isinstance(summary, str) or not summary.strip():
            errors.append("minutes.summary cannot be blank when aligned transcript exists")
        evidence = result.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append("evidence cannot be empty when aligned transcript exists")
        return errors

    def _attach_model_manifest(
        self, result: dict[str, Any], model: str | None
    ) -> None:
        provenance = result.get("provenance")
        if not isinstance(provenance, dict):
            return
        settings = getattr(self, "settings", None)
        provenance["model_manifest"] = [
            {
                "provider": "bailian",
                "model": model
                or getattr(settings, "bailian_text_model", "configured-text-model"),
            }
        ]

    async def analyze(
        self, session: dict[str, Any], tracks: list[str], request: dict[str, Any], **kwargs
    ) -> dict[str, Any]:
        source = request.get("source") or {}
        result_schema = AnalysisResult.model_json_schema()
        schema_prompt = json.dumps(result_schema, ensure_ascii=False, separators=(",", ":"))
        system_prompt = (
            f"{SYSTEM_PROMPT}\n\nREQUIRED OUTPUT JSON SCHEMA:\n{schema_prompt}\n"
            "Return the AnalysisResult object itself, not a wrapper or prose. "
            "Populate every required field and use null only where the schema permits it."
        )
        if source.get("type") in {"text", "transcript"}:
            system_prompt = f"{system_prompt}\n\n{TEXT_ONLY_PROMPT}"

        # Audio adapters write normalized observations into session before this call.
        prompt = json.dumps(
            {"session": session, "tracks": tracks, "request": request},
            ensure_ascii=False,
        )
        text = await self._chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            **kwargs,
        )
        try:
            result = self._extract_json(text)
        except Exception:
            repaired = await self._chat_completion(
                [
                    {
                        "role": "system",
                        "content": (
                            "Repair JSON syntax only. Return the AnalysisResult object "
                            "as JSON with no prose or wrapper."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                **kwargs,
            )
            result = self._extract_json(repaired)

        self._enforce_conservative_recommendations(result)
        self._attach_model_manifest(result, kwargs.get("model"))

        text_segments = (
            session.get("observations", {}).get("text_segments")
            if source.get("type") in {"text", "transcript"}
            else None
        )
        contract_errors = validate_result(result, text_segments=text_segments)
        observations = session.get("observations", {})
        has_aligned_text = bool(observations.get("aligned_segments"))
        contract_errors.extend(self._aligned_content_errors(result, has_aligned_text))
        if not contract_errors:
            return result
        errors = [
            {"field": "contract", "message": message}
            for message in contract_errors
        ]

        repaired = await self._chat_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "Perform one structural repair of a memEcho AnalysisResult. "
                        "Return only the AnalysisResult JSON object. Preserve supported "
                        "meaning, do not invent evidence, and use only evidence identifiers "
                        "and excerpts present in the original input. Every evidence_refs "
                        "entry must exactly match an id in the repaired evidence array. "
                        "Prefer reconnecting a claim to an existing evidence item; omit a "
                        "claim when no supplied evidence supports it."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "required_schema": result_schema,
                            "validation_errors": errors,
                            "invalid_result": result,
                            "original_input": {
                                "session": session,
                                "tracks": tracks,
                                "request": request,
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            **kwargs,
        )
        result = self._extract_json(repaired)
        self._enforce_conservative_recommendations(result)
        self._attach_model_manifest(result, kwargs.get("model"))
        final_errors = validate_result(result, text_segments=text_segments)
        final_errors.extend(self._aligned_content_errors(result, has_aligned_text))
        if final_errors:
            raise ValueError("upstream analysis ignored usable aligned transcript evidence")
        return result

    async def chat(self, question: str, context: dict[str, Any], **kwargs) -> str:
        return await self._chat_completion(
            [
                {
                    "role": "system",
                    "content": "你是 memEcho。只依据用户明确选择的单次会谈内容回答，并给出证据范围和不确定性。",
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"question": question, "context": context}, ensure_ascii=False
                    ),
                },
            ],
            **kwargs,
        )
