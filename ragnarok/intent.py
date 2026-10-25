"""Fast intent routing (FR2.2, NFR2.1): embed skill descriptions once, then shortlist the
top-k skills for a query so the LLM sees fewer tools (lower latency, better selection)."""
from __future__ import annotations

from .memory.embedding import Embedder, cosine
from .skills import SkillRegistry


class SkillRouter:
    def __init__(self, registry: SkillRegistry, embedder: Embedder):
        self.registry, self.embedder = registry, embedder
        self._names: list[str] = []
        self._vecs: list[list[float]] = []
        self.refresh()

    def refresh(self) -> None:
        skills = [self.registry.get(n) for n in self.registry.names()]
        self._names = [s.name for s in skills]
        texts = [f"{s.name.replace('_', ' ')}: {s.description}" for s in skills]
        self._vecs = self.embedder.embed(texts) if texts else []

    def top(self, query: str, k: int = 6) -> list[str]:
        if len(self._names) <= k:
            return list(self._names)
        q = self.embedder.embed([query])[0]
        ranked = sorted(zip(self._names, self._vecs), key=lambda nv: -cosine(q, nv[1]))
        return [n for n, _ in ranked[:k]]
