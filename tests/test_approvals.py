import datetime as dt
import json
import smtplib
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ragnarok.config import Config
from ragnarok.intent import SkillRouter
from ragnarok.llm import OllamaError
from ragnarok.memory import HashEmbedder
from ragnarok.runtime import Runtime
from ragnarok.server import make_handler
from ragnarok.skills import SkillRegistry
from ragnarok.triggers import DailyTrigger


class MailLLM:
    """Like a real model: asks to send mail, wording differs every time it asks."""
    def __init__(self): self.n = 0
    def chat(self, messages, tools=None):
        last = messages[-1]
        if last["role"] == "tool":
            return {"role": "assistant", "content": "reply: " + last["content"][:200]}
        if "Extract durable facts" in last["content"]:
            return {"role": "assistant", "content": "[]"}
        self.n += 1
        return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "send_email", "arguments": {
            "to": "a@b.c", "subject": f"Meeting v{self.n}", "body": f"Let's meet tomorrow ({self.n})"}}}]}


@pytest.fixture
def rt(tmp_path, monkeypatch):
    sent = []
    class S:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, p): pass
        def send_message(self, m): sent.append(m)
    monkeypatch.setattr(smtplib, "SMTP_SSL", S)
    for k in ("HOST", "USER", "PASSWORD"): monkeypatch.setenv("SMTP_" + k, "x")
    r = Runtime(Config(data_dir=tmp_path), llm=MailLLM(), embedder=HashEmbedder())
    r.sent = sent
    yield r
    r.close()


def test_approving_runs_the_exact_action_once(rt):
    assert "approval" in rt.chat("send an email to a@b.c").lower() and rt.sent == []   # nothing sent, model told it is pending
    pend = rt.approvals.pending()
    assert len(pend) == 1
    assert "sent to a@b.c" in rt.run_approval(pend[0]["id"])
    assert len(rt.sent) == 1 and rt.sent[0]["Subject"] == "Meeting v1"                 # exactly what the user reviewed
    assert rt.approvals.pending() == []
    assert "no longer pending" in rt.run_approval(pend[0]["id"]) and len(rt.sent) == 1  # cannot run twice
    assert any("approved your earlier request" in m.get("content", "") for m in rt.agent.history)
    assert any(e.kind == "action" for e in rt.store.episodes())


def test_reworded_retry_cannot_bypass_approval(rt):
    rt.chat("send mail"); rt.chat("send it again")
    assert len(rt.approvals.pending()) == 2 and rt.sent == []


def test_ui_grant_runs_action_and_model_errors_are_json(rt):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(rt, "tok"))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    def call(path, method="GET", body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Ragnarok-Token": "tok", "Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read())
    rt.chat("send")
    aid = rt.approvals.pending()[0]["id"]
    r = call(f"/api/approvals/{aid}/grant", "POST", {})
    assert r["ok"] and "sent to" in r["result"] and len(rt.sent) == 1
    assert call(f"/api/approvals/{aid}/grant", "POST", {})["ok"] is False
    def slow(messages, tools=None): raise OllamaError("Ollama did not answer within 600s")
    rt.llm.chat = slow
    with pytest.raises(urllib.error.HTTPError) as e:
        call("/api/chat", "POST", {"text": "hi"})
    assert e.value.code == 503 and "did not answer" in json.loads(e.value.read())["error"]
    assert call("/api/memory")["facts"] is not None                                    # server still alive
    srv.shutdown()


def test_router_survives_embedding_failure_and_sees_new_skills():
    reg = SkillRegistry()
    for i in range(8):
        reg.register(name=f"s{i}", description=f"skill {i} number")(lambda: "x")
    class Down:
        def embed(self, t): raise OllamaError("down")
    assert sorted(SkillRouter(reg, Down()).top("anything")) == sorted(reg.names())     # no crash: offers all tools
    r = SkillRouter(reg, HashEmbedder())
    assert len(r.top("skill 3", k=3)) == 3
    reg.register(name="late", description="brand new late skill")(lambda: "x")
    assert "late" in r.top("brand new late skill", k=3)


def test_daily_trigger_fires_once_per_day_across_restarts(tmp_path):
    morning = dt.datetime(2026, 10, 26, 10, 0).timestamp(); f = tmp_path / "state.json"
    assert DailyTrigger("brief", "08:00", "x", f).poll(morning) == ["x"]
    assert DailyTrigger("brief", "08:00", "x", f).poll(morning + 60) == []              # daemon restarted: no repeat
    assert DailyTrigger("brief", "08:00", "x", f).poll(morning + 86400) == ["x"]


def test_cli_interactive_decline_is_reported_as_declined(tmp_path):
    from ragnarok.agent import Agent
    reg = SkillRegistry()
    reg.register(name="wipe", description="d", risky=True)(lambda: "gone")
    replies = iter([{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "wipe", "arguments": {}}}]},
                    {"role": "assistant", "content": "ok"}])
    class L:
        def chat(self, m, tools=None): return next(replies)
    a = Agent(L(), reg, Config(data_dir=tmp_path), approve=lambda n, g: False)
    a.run("go")
    assert any(m.get("role") == "tool" and "did not approve" in m["content"] for m in a.history)
