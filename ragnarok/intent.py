"""Fast intent routing (FR2.2, NFR2.1): embed skill descriptions once, then shortlist the
top-k skills for a query so the LLM sees fewer tools (lower latency, better selection).
If embeddings are unavailable (Ollama down) it falls back to offering every tool."""
from __future__ import annotations

from .memory.embedding import Embedder, cosine
from .skills import SkillRegistry


class SkillRouter:
    def __init__(self, registry: SkillRegistry, embedder: Embedder):
        self.registry, self.embedder = registry, embedder
        self._names: list[str] = []
        self._vecs: list[list[float]] = []
        self._ready = False

    def refresh(self) -> bool:
        skills = [self.registry.get(n) for n in self.registry.names()]
        texts = [f"{s.name.replace('_', ' ')}: {s.description}" for s in skills]
        try:
            vecs = self.embedder.embed(texts) if texts else []
        except Exception:
            self._ready = False
            return False
        self._names, self._vecs, self._ready = [s.name for s in skills], vecs, True
        return True

    def top(self, query: str, k: int = 6) -> list[str]:
        names = self.registry.names()
        if not self._ready or set(self._names) != set(names):      # first use, or skills were added
            if not self.refresh():
                return names
        if len(self._names) <= k:
            return list(self._names)
        try:
            q = self.embedder.embed([query])[0]
        except Exception:
            return names
        ranked = sorted(zip(self._names, self._vecs), key=lambda nv: -cosine(q, nv[1]))
        return [n for n, _ in ranked[:k]]
