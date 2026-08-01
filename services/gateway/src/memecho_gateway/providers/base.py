from __future__ import annotations

from typing import Any, Protocol


class Provider(Protocol):
    async def analyze(
        self, session: dict[str, Any], tracks: list[str], request: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def chat(self, question: str, context: dict[str, Any]) -> str: ...

