"""Thin client for a local Ollama server. The only network target is localhost."""
from __future__ import annotations

from typing import Any, Protocol

import httpx

from .config import Config


class OllamaError(RuntimeError):
    """Raised with an actionable message the CLI can show directly."""


class LLM(Protocol):
    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OllamaClient:
    def __init__(self, config: Config | None = None, timeout: float | None = None):
        self.config = config or Config()
        self.timeout = timeout or self.config.timeout
        self._http = httpx.Client(base_url=self.config.ollama_url, timeout=httpx.Timeout(self.timeout, connect=10.0))
        self.name = f"ollama:{self.config.embed_model}"      # embedder identity (see MemoryManager)

    def _post(self, path: str, payload: dict, model: str) -> dict:
        try:
            r = self._http.post(path, json=payload)
        except httpx.ConnectError as e:
            raise OllamaError(f"Can't reach Ollama at {self.config.ollama_url}. Start it with `ollama serve`.") from e
        except httpx.TimeoutException as e:
            raise OllamaError(f"Ollama did not answer within {int(self.timeout)}s. The model may still be loading or is too big for this machine: "
                              "try a smaller one (`ollama pull llama3.2:3b`, then set RAGNAROK_CHAT_MODEL) or raise RAGNAROK_TIMEOUT.") from e
        if r.status_code == 404:
            raise OllamaError(f"Model '{model}' not found. Run `ollama pull {model}`.")
        if r.status_code >= 400:
            detail = ""
            try: detail = r.json().get("error", "")
            except Exception: detail = r.text[:200]
            if "does not support tools" in detail:
                raise OllamaError(f"Model '{model}' doesn't support tool calling. Use e.g. llama3.1:8b or qwen2.5:7b "
                                  "(set RAGNAROK_CHAT_MODEL).")
            raise OllamaError(f"Ollama error {r.status_code}: {detail}")
        return r.json()

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """Returns the assistant message dict: {role, content, tool_calls?}."""
        payload: dict[str, Any] = {"model": self.config.chat_model, "messages": messages, "stream": False,
                                   "keep_alive": self.config.keep_alive}
        if tools:
            payload["tools"] = tools
        return self._post("/api/chat", payload, self.config.chat_model)["message"]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._post("/api/embed", {"model": self.config.embed_model, "input": texts, "keep_alive": self.config.keep_alive},
                          self.config.embed_model)["embeddings"]

    def status(self) -> dict:
        """For `ragnarok doctor`: reachable? which models are installed?"""
        try:
            tags = self._http.get("/api/tags", timeout=5).json()
        except Exception as e:
            return {"reachable": False, "error": str(e), "models": []}
        names = [m["name"] for m in tags.get("models", [])]
        have = lambda want: any(n == want or n.split(":")[0] == want.split(":")[0] and (":" not in want or n == want) for n in names)
        return {"reachable": True, "models": names,
                "chat_model_ok": have(self.config.chat_model), "embed_model_ok": have(self.config.embed_model)}
