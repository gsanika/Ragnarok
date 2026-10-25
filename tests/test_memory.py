import sqlite3

import pytest

from ragnarok.crypto import Vault
from ragnarok.memory import HashEmbedder, MemoryManager, MemoryStore
from ragnarok.memory.manager import DAY, parse_json_list


class Clock:
    def __init__(self, t=1_000_000.0): self.t = t
    def __call__(self): return self.t


class FakeLLM:
    def __init__(self, content): self.content = content
    def chat(self, messages, tools=None): return {"role": "assistant", "content": self.content}


def mk(tmp_path, clock=None):
    store = MemoryStore(tmp_path, Vault(tmp_path, passphrase="pw"))
    return MemoryManager(store, HashEmbedder(), clock or Clock())


def test_recall_finds_relevant(tmp_path):
    m = mk(tmp_path)
    m.remember("User is learning Rust for embedded systems")
    m.remember("User's favourite food is masala dosa")
    top = m.recall("what programming language is the user learning", k=1)
    assert "Rust" in top[0].text


def test_dedupe_merges_near_duplicates(tmp_path):
    m = mk(tmp_path)
    a = m.remember("User likes masala dosa")
    b = m.remember("User likes masala dosa")
    assert a == b and len(m.store.facts()) == 1


def test_data_encrypted_at_rest(tmp_path):
    m = mk(tmp_path)
    m.remember("secret-launch-code-ALPHA")
    m.log_episode("user", "my diary says BRAVO")
    m.store.db.commit()
    raw = (tmp_path / "memory.db").read_bytes()
    assert b"secret-launch-code-ALPHA" not in raw and b"BRAVO" not in raw


def test_wrong_key_fails(tmp_path):
    m = mk(tmp_path); m.remember("hello world fact"); m.store.close()
    bad = MemoryStore(tmp_path, Vault(tmp_path, passphrase="other"))
    with pytest.raises(ValueError):
        bad.facts()


def test_consolidation_and_forgetting(tmp_path):
    clock = Clock()
    m = mk(tmp_path, clock)
    for i in range(3):
        m.log_episode("user", f"working on the Ragnarok agent, step {i}")
    clock.t += 3 * DAY
    added = m.consolidate(FakeLLM('["User is building the Ragnarok agent"]'), older_than_days=1)
    assert added == 1 and all(e.consolidated for e in m.store.episodes())
    # stale low-importance fact is pruned, fresh one kept
    old = m.remember("trivia from long ago", importance=0.1)
    clock.t += 400 * DAY
    keep = m.remember("recent important preference", importance=0.9)
    res = m.forget()
    ids = {f.id for f in m.store.facts()}
    assert old not in ids and keep in ids and res["facts_pruned"] >= 1


def test_edit_export_delete_wipe(tmp_path):
    m = mk(tmp_path)
    fid = m.remember("User lives in Pune")
    m.edit(fid, "User lives in Mumbai")
    assert "Mumbai" in m.export()["facts"][0]["text"]
    assert m.delete(fid) and not m.store.facts()
    m.remember("x y z"); m.wipe()
    assert m.export() == {"facts": [], "episodes": []}


def test_parse_json_list_robust():
    assert parse_json_list('Sure! ["a", "b"] done') == ["a", "b"]
    assert parse_json_list("no list") == []
    assert parse_json_list("[1, 2]") == []
