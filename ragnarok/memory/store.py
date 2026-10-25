"""SQLite-backed encrypted memory store. All content and embeddings are encrypted;
only ids, timestamps and numeric scores are plaintext."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from ..crypto import Vault

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
  content BLOB NOT NULL, consolidated INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, text BLOB NOT NULL,
  embedding BLOB NOT NULL, importance REAL NOT NULL DEFAULT 0.5,
  last_used REAL NOT NULL, uses INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'chat');
"""


@dataclass
class Episode:
    id: int
    ts: float
    kind: str
    content: str
    consolidated: bool


@dataclass
class Fact:
    id: int
    ts: float
    text: str
    embedding: list[float]
    importance: float
    last_used: float
    uses: int
    source: str


class MemoryStore:
    def __init__(self, data_dir: Path, vault: Vault | None = None):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.vault = vault or Vault(self.dir)
        self.db = sqlite3.connect(self.dir / "memory.db", check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.commit()

    # episodes
    def add_episode(self, kind: str, content: str, ts: float | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO episodes(ts,kind,content) VALUES (?,?,?)",
            (ts or time.time(), kind, self.vault.encrypt(content)),
        )
        self.db.commit()
        return cur.lastrowid

    def _ep(self, r) -> Episode:
        return Episode(r[0], r[1], r[2], self.vault.decrypt(r[3]), bool(r[4]))

    def episodes(self, limit: int = 50, since: float | None = None, only_unconsolidated=False) -> list[Episode]:
        q, args = "SELECT id,ts,kind,content,consolidated FROM episodes WHERE 1=1", []
        if since is not None:
            q += " AND ts>=?"; args.append(since)
        if only_unconsolidated:
            q += " AND consolidated=0"
        q += " ORDER BY ts DESC LIMIT ?"; args.append(limit)
        return [self._ep(r) for r in self.db.execute(q, args)]

    def search_episodes(self, text: str, limit: int = 20) -> list[Episode]:
        needle = text.lower()
        hits = [e for e in self.episodes(limit=100000) if needle in e.content.lower()]
        return hits[:limit]

    def mark_consolidated(self, ids: list[int]) -> None:
        self.db.executemany("UPDATE episodes SET consolidated=1 WHERE id=?", [(i,) for i in ids])
        self.db.commit()

    def delete_episodes(self, ids: list[int]) -> None:
        self.db.executemany("DELETE FROM episodes WHERE id=?", [(i,) for i in ids])
        self.db.commit()

    # facts
    def _fact(self, r) -> Fact:
        return Fact(r[0], r[1], self.vault.decrypt(r[2]), json.loads(self.vault.decrypt(r[3])),
                    r[4], r[5], r[6], r[7])

    def add_fact(self, text: str, embedding: list[float], importance=0.5, source="chat", ts=None) -> int:
        now = ts or time.time()
        cur = self.db.execute(
            "INSERT INTO facts(ts,text,embedding,importance,last_used,uses,source) VALUES (?,?,?,?,?,0,?)",
            (now, self.vault.encrypt(text), self.vault.encrypt(json.dumps(embedding)), importance, now, source),
        )
        self.db.commit()
        return cur.lastrowid

    def facts(self) -> list[Fact]:
        rows = self.db.execute(
            "SELECT id,ts,text,embedding,importance,last_used,uses,source FROM facts ORDER BY id"
        ).fetchall()
        return [self._fact(r) for r in rows]

    def get_fact(self, fid: int) -> Fact | None:
        r = self.db.execute(
            "SELECT id,ts,text,embedding,importance,last_used,uses,source FROM facts WHERE id=?", (fid,)
        ).fetchone()
        return self._fact(r) if r else None

    def update_fact(self, fid: int, text: str | None = None, embedding=None, importance=None) -> None:
        if text is not None:
            self.db.execute("UPDATE facts SET text=? WHERE id=?", (self.vault.encrypt(text), fid))
        if embedding is not None:
            self.db.execute("UPDATE facts SET embedding=? WHERE id=?", (self.vault.encrypt(json.dumps(embedding)), fid))
        if importance is not None:
            self.db.execute("UPDATE facts SET importance=? WHERE id=?", (importance, fid))
        self.db.commit()

    def touch(self, fid: int, now: float | None = None) -> None:
        self.db.execute("UPDATE facts SET uses=uses+1, last_used=? WHERE id=?", (now or time.time(), fid))
        self.db.commit()

    def delete_fact(self, fid: int) -> bool:
        cur = self.db.execute("DELETE FROM facts WHERE id=?", (fid,))
        self.db.commit()
        return cur.rowcount > 0

    def wipe(self) -> None:
        """Permanently delete everything (NFR4.2) and compact the file."""
        self.db.execute("DELETE FROM episodes"); self.db.execute("DELETE FROM facts")
        self.db.commit(); self.db.execute("VACUUM")

    def close(self) -> None:
        self.db.close()
