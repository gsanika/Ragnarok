import threading

import httpx
import pytest

from ragnarok.agent import Agent
from ragnarok.config import Config
from ragnarok.crypto import Vault
from ragnarok.llm import OllamaClient, OllamaError
from ragnarok.memory import HashEmbedder, MemoryManager, MemoryStore
from ragnarok.skills import SkillRegistry, normalize_args


def _skill():
    reg = SkillRegistry()
    @reg.register(description="d")
    def f(name: str, count: int, flag: bool = False): return f"{name}{count}{flag}"
    return reg, reg.get("f")


def test_args_coerced_from_strings_and_json():
    _, s = _skill()
    assert normalize_args(s, '{"name":"a","count":"3"}') == {"name": "a", "count": 3}
    assert normalize_args(s, {"name": "a", "count": 2.0, "flag": "true", "junk": 1}) == {"name": "a", "count": 2, "flag": True}
    with pytest.raises(ValueError):
        normalize_args(s, {"name": "a"})


def test_agent_survives_bad_tool_arguments(tmp_path):
    reg, _ = _skill()
    replies = iter([{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "f", "arguments": {"name": "a"}}}]},
                    {"role": "assistant", "content": "recovered"}])
    class L:
        def chat(self, m, tools=None): return next(replies)
    a = Agent(L(), reg, Config(data_dir=tmp_path))
    assert a.run("go") == "recovered"
    assert any(e["event"] == "bad_arguments" for e in a.log.read())


def _client(handler):
    c = OllamaClient(Config(ollama_url="http://x"))
    c._http = httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler))
    return c


def test_ollama_friendly_errors():
    def down(req): raise httpx.ConnectError("nope")
    with pytest.raises(OllamaError, match="ollama serve"):
        _client(down).chat([{"role": "user", "content": "hi"}])
    with pytest.raises(OllamaError, match="ollama pull"):
        _client(lambda r: httpx.Response(404, json={"error": "model not found"})).chat([])
    with pytest.raises(OllamaError, match="tool calling"):
        _client(lambda r: httpx.Response(400, json={"error": "registry.ollama.ai/x does not support tools"})).chat([], tools=[{}])
    ok = _client(lambda r: httpx.Response(200, json={"message": {"role": "assistant", "content": "yo"}}))
    assert ok.chat([])["content"] == "yo"


def test_embedder_change_triggers_reembed(tmp_path):
    store = MemoryStore(tmp_path, Vault(tmp_path, passphrase="p"))
    m1 = MemoryManager(store, HashEmbedder(64))
    m1.remember("User studies computer engineering in Pune")
    class Other(HashEmbedder):
        name = "other-model"
    m2 = MemoryManager(store, Other(128))
    assert m2.reembedded == 1
    assert len(store.facts()[0].embedding) == 128
    assert "engineering" in m2.recall("what does the user study")[0].text


def test_memory_is_thread_safe(tmp_path):
    store = MemoryStore(tmp_path, Vault(tmp_path, passphrase="p"))
    m = MemoryManager(store, HashEmbedder())
    errors = []
    def work(i):
        try:
            for j in range(15):
                m.remember(f"thread {i} fact {j} about topic{j % 4}")
                m.recall(f"topic{j % 4}")
                m.log_episode("user", f"t{i} {j}")
        except Exception as e:
            errors.append(e)
    ts = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errors and len(store.facts()) >= 30 and len(store.episodes(limit=1000)) == 60
