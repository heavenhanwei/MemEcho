from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..config import Settings


SYSTEM_PROMPT = """你是 memEcho 对话分析服务。只分析可观察的表达，不诊断心理状态，不推断隐藏意图。
严格输出 memEcho 1.1 JSON。事实主张只表示可核验类型，不表示已核实。明确行动必须是 discussed/confirmed，
分析建议必须是 suggested/proposed。每项重要解释必须引用 evidence_refs、confidence 和 alternatives。
VAD 表示情境中的表达状态，不表示真实内心。仅可使用输入中明确提供的语言和声学证据。"""


class BailianProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def _chat_completion(self, messages: list[dict[str, Any]]) -> str:
        if not self.settings.bailian_text_base_url or not self.settings.bailian_text_api_key:
            raise RuntimeError("Bailian text endpoint is not configured")
        payload = {
            "model": self.settings.bailian_text_model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 16384,
            "enable_thinking": False,
        }
        headers = {"Authorization": f"Bearer {self.settings.bailian_text_api_key}"}
        url = f"{self.settings.bailian_text_base_url.rstrip('/')}/chat/completions"
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

    async def analyze(
        self, session: dict[str, Any], tracks: list[str], request: dict[str, Any]
    ) -> dict[str, Any]:
        # Audio adapters write normalized observations into session before this call.
        prompt = json.dumps(
            {"session": session, "tracks": tracks, "request": request},
            ensure_ascii=False,
        )
        text = await self._chat_completion(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
        )
        try:
            return self._extract_json(text)
        except Exception:
            repaired = await self._chat_completion(
                [
                    {"role": "system", "content": "只修复 JSON 语法与 memEcho 1.1 字段结构，不改变语义。"},
                    {"role": "user", "content": text},
                ]
            )
            return self._extract_json(repaired)

    async def chat(self, question: str, context: dict[str, Any]) -> str:
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
            ]
        )

