"""High-level memory: recall, extraction, consolidation and forgetting (FR1.1-1.3)."""
from __future__ import annotations

import json
import math
import re
import threading
import time
from typing import Callable

from .embedding import Embedder, cosine

try:  # optional speed-up
    import numpy as np
except ImportError:  # pragma: no cover
    np = None
from .store import Fact, MemoryStore

DAY = 86400.0

EXTRACT_PROMPT = (
    "Extract durable facts or preferences about the USER worth remembering long-term from this "
    "exchange. Reply ONLY with a JSON list of short strings (maybe empty []). Skip trivia.\n\n"
    "User: {user}\nAssistant: {reply}"
)
SUMMARY_PROMPT = (
    "Summarize these past interactions into 1-3 short durable facts about the user's context, "
    "projects or preferences. Reply ONLY with a JSON list of strings.\n\n{log}"
)


def parse_json_list(text: str) -> list[str]:
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [s.strip() for s in data if isinstance(s, str) and s.strip()]


def _locked(fn):
    def wrapper(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)
    wrapper.__name__ = fn.__name__; wrapper.__doc__ = fn.__doc__
    return wrapper


class MemoryManager:
    def __init__(self, store: MemoryStore, embedder: Embedder, clock: Callable[[], float] = time.time,
                 dedupe_threshold: float = 0.92):
        self.store, self.embedder, self.clock = store, embedder, clock
        self.dedupe_threshold = dedupe_threshold
        self._cache: dict[int, Fact] | None = None
        self._ids: list[int] = []
        self._mat = None  # normalized embeddings: numpy matrix, or list of lists
        self._lock = threading.RLock()
        self.reembedded = self._sync_embedder()

    def _sync_embedder(self) -> int:
        """If the embedding model changed (e.g. offline hash -> Ollama), vectors are not comparable:
        recompute every fact's embedding once so recall stays correct."""
        name = getattr(self.embedder, "name", type(self.embedder).__name__)
        old = self.store.get_meta("embedder")
        n = 0
        if old is not None and old != name:
            facts = self.store.facts()
            for i in range(0, len(facts), 32):
                chunk = facts[i:i + 32]
                for f, v in zip(chunk, self.embedder.embed([f.text for f in chunk])):
                    self.store.update_fact(f.id, embedding=v)
                    n += 1
        if old != name:
            self.store.set_meta("embedder", name)
        return n

    @staticmethod
    def _unit(v):
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def _facts(self) -> dict[int, Fact]:
        if self._cache is None:
            self._cache = {f.id: f for f in self.store.facts()}
            self._ids = list(self._cache)
            units = [self._unit(self._cache[i].embedding) for i in self._ids]
            self._mat = (np.array(units, dtype="float32") if (np is not None and units) else units)
        return self._cache

    def _sims(self, q) -> list[float]:
        qu = self._unit(q)
        if np is not None and not isinstance(self._mat, list):
            return (self._mat @ np.array(qu, dtype="float32")).tolist()
        return [sum(a * b for a, b in zip(qu, row)) for row in self._mat]

    def _invalidate(self) -> None:
        self._cache = None

    # FR1.1 episodic
    @_locked
    def log_episode(self, kind: str, content: str) -> int:
        return self.store.add_episode(kind, content, ts=self.clock())

    # FR1.2 semantic
    @_locked
    def remember(self, text: str, importance: float = 0.5, source: str = "chat") -> int:
        text = text.strip()
        emb = self.embedder.embed([text])[0]
        for f in self._facts().values():
            if cosine(emb, f.embedding) >= self.dedupe_threshold:
                self.store.update_fact(f.id, importance=min(1.0, max(f.importance, importance) + 0.05))
                self.store.touch(f.id, self.clock())
                self._invalidate()
                return f.id
        fid = self.store.add_fact(text, emb, importance, source, ts=self.clock())
        self._invalidate()
        return fid

    @_locked
    def recall(self, query: str, k: int = 5, min_score: float = 0.15) -> list[Fact]:
        facts = self._facts()
        if not facts:
            return []
        q = self.embedder.embed([query])[0]
        now = self.clock()
        scored = []
        for fid, sim in zip(self._ids, self._sims(q)):
            f = facts[fid]
            recency = math.exp(-(now - f.last_used) / (30 * DAY))
            score = 0.75 * sim + 0.15 * f.importance + 0.10 * recency
            if sim >= min_score:
                scored.append((score, f))
        scored.sort(key=lambda x: -x[0])
        top = [f for _, f in scored[:k]]
        for f in top:
            self.store.touch(f.id, now)
            f.uses += 1; f.last_used = now      # keep the in-RAM copy current; no cache reload
        return top

    @_locked
    def extract_and_store(self, llm, user_text: str, reply: str) -> list[int]:
        try:
            msg = llm.chat([{"role": "user", "content": EXTRACT_PROMPT.format(user=user_text, reply=reply)}])
        except Exception:
            return []
        return [self.remember(t, importance=0.6) for t in parse_json_list(msg.get("content", ""))[:5]]

    # FR1.3 consolidation + forgetting
    @_locked
    def consolidate(self, llm=None, older_than_days: float = 1.0, batch: int = 40) -> int:
        cutoff = self.clock() - older_than_days * DAY
        eps = [e for e in self.store.episodes(limit=100000, only_unconsolidated=True) if e.ts <= cutoff]
        if not eps:
            return 0
        added = 0
        for i in range(0, len(eps), batch):
            chunk = eps[i:i + batch]
            log = "\n".join(f"[{e.kind}] {e.content[:300]}" for e in chunk)
            facts: list[str] = []
            if llm is not None:
                try:
                    facts = parse_json_list(llm.chat([{"role": "user", "content": SUMMARY_PROMPT.format(log=log)}]).get("content", ""))
                except Exception:
                    facts = []
            for t in facts[:3]:
                self.remember(t, importance=0.55, source="consolidation"); added += 1
            self.store.mark_consolidated([e.id for e in chunk])
        return added

    def retention_score(self, f: Fact) -> float:
        age_days = (self.clock() - f.last_used) / DAY
        return f.importance * math.exp(-age_days / 90.0) + 0.05 * min(f.uses, 10)

    @_locked
    def forget(self, threshold: float = 0.08, episode_retention_days: float = 90.0) -> dict:
        pruned = 0
        for f in list(self._facts().values()):
            if self.retention_score(f) < threshold:
                self.store.delete_fact(f.id); pruned += 1
        cutoff = self.clock() - episode_retention_days * DAY
        old = [e.id for e in self.store.episodes(limit=100000) if e.consolidated and e.ts < cutoff]
        self.store.delete_episodes(old)
        self._invalidate()
        return {"facts_pruned": pruned, "episodes_pruned": len(old)}

    # NFR4.2 user control
    @_locked
    def edit(self, fid: int, text: str) -> None:
        self.store.update_fact(fid, text=text, embedding=self.embedder.embed([text])[0])
        self._invalidate()

    @_locked
    def delete(self, fid: int) -> bool:
        ok = self.store.delete_fact(fid); self._invalidate(); return ok

    @_locked
    def export(self) -> dict:
        return {
            "facts": [{"id": f.id, "text": f.text, "importance": f.importance, "uses": f.uses, "ts": f.ts}
                      for f in self.store.facts()],
            "episodes": [{"id": e.id, "ts": e.ts, "kind": e.kind, "content": e.content}
                         for e in self.store.episodes(limit=100000)],
        }

    @_locked
    def wipe(self) -> None:
        self.store.wipe(); self._invalidate()
