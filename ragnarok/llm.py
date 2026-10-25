"""Thin client for a local Ollama server. The only network target is localhost."""
from __future__ import annotations

from typing import Any, Protocol

import httpx

from .config import Config


class LLM(Protocol):
    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OllamaClient:
    def __init__(self, config: Config | None = None, timeout: float = 120.0):
        self.config = config or Config()
        self._http = httpx.Client(base_url=self.config.ollama_url, timeout=timeout)

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """Returns the assistant message dict: {role, content, tool_calls?}."""
        payload: dict[str, Any] = {
            "model": self.config.chat_model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        r = self._http.post("/api/chat", json=payload)
        r.raise_for_status()
        return r.json()["message"]

    def embed(self, texts: list[str]) -> list[list[float]]:
        r = self._http.post(
            "/api/embed", json={"model": self.config.embed_model, "input": texts}
        )
        r.raise_for_status()
        return r.json()["embeddings"]
