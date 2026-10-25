from ragnarok import builtin_skills  # noqa: F401
from ragnarok.agent import Agent
from ragnarok.config import Config
from ragnarok.skills import SkillRegistry, registry


class FakeLLM:
    """Scripted replies so tests need no Ollama."""
    def __init__(self, replies):
        self.replies = list(replies)

    def chat(self, messages, tools=None):
        return self.replies.pop(0)

    def embed(self, texts):
        return [[0.0] for _ in texts]


def call(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def make(tmp_path, replies, reg=registry, **kw):
    return Agent(FakeLLM(replies), reg, Config(data_dir=tmp_path), **kw)


def test_tool_call_then_answer(tmp_path):
    a = make(tmp_path, [call("calculate", expression="2*(3+4)"), {"role": "assistant", "content": "14"}])
    assert a.run("what is 2*(3+4)?") == "14"
    events = [e["event"] for e in a.log.read()]
    assert events == ["user", "tool_call", "tool_result", "assistant"]


def test_failing_tool_does_not_crash(tmp_path):
    a = make(tmp_path, [call("calculate", expression="__import__('os')"), {"role": "assistant", "content": "sorry"}])
    assert a.run("hack") == "sorry"
    assert any(e["event"] == "tool_error" for e in a.log.read())


def test_risky_skill_needs_approval(tmp_path):
    reg = SkillRegistry()
    ran = []

    @reg.register(description="delete things", risky=True)
    def wipe(path: str):
        ran.append(path)
        return "deleted"

    a = make(tmp_path, [call("wipe", path="x"), {"role": "assistant", "content": "ok"}], reg)
    a.run("wipe x")
    assert ran == []  # denied by default

    a = make(tmp_path, [call("wipe", path="x"), {"role": "assistant", "content": "ok"}], reg, approve=lambda n, g: True)
    a.run("wipe x")
    assert ran == ["x"]


def test_schema_from_signature():
    s = registry.get("list_workspace").schema()["function"]["parameters"]
    assert s["properties"]["subdir"]["type"] == "string" and s["required"] == []


def test_memory_is_injected_and_extracted(tmp_path):
    from ragnarok.memory import HashEmbedder, MemoryManager, MemoryStore
    store = MemoryStore(tmp_path)
    mem = MemoryManager(store, HashEmbedder())
    mem.remember("User prefers answers in bullet points")
    seen = {}

    class Spy(FakeLLM):
        def chat(self, messages, tools=None):
            seen.setdefault("msgs", messages)
            return super().chat(messages, tools)

    llm = Spy([{"role": "assistant", "content": "ok"}, {"role": "assistant", "content": '["User is a student"]'}])
    a = Agent(llm, registry, Config(data_dir=tmp_path), memory=mem)
    a.run("how should answers be formatted? bullet points?")
    assert any("bullet points" in m["content"] for m in seen["msgs"] if m["role"] == "system")
    assert any("student" in f.text for f in mem.store.facts())
    assert len(mem.store.episodes()) == 2


def test_state_recovery(tmp_path):
    a = make(tmp_path, [{"role": "assistant", "content": "hi"}])
    a.run("hello")
    b = Agent(FakeLLM([]), registry, Config(data_dir=tmp_path), restore=True)
    assert any(m.get("content") == "hello" for m in b.history)
