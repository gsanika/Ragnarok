import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ragnarok import bench
from ragnarok.config import Config
from ragnarok.memory import HashEmbedder
from ragnarok.runtime import Runtime
from ragnarok.server import make_handler


class ScriptLLM:
    """Replies by inspecting the prompt, so one fake serves every component."""
    def __init__(self): self.calls = []
    def chat(self, messages, tools=None):
        last = messages[-1]["content"] if messages else ""
        self.calls.append(last)
        if last.startswith("Break the user's goal"):
            return {"role": "assistant", "content": json.dumps({"tasks": [
                {"id": "a", "description": "calculate 6*7", "deps": []},
                {"id": "b", "description": "write a note with the answer", "deps": ["a"]}]})}
        if last.startswith("Task:"):
            return {"role": "assistant", "content": '{"ok": true, "reason": "fine"}'}
        if "morning brief" in last:
            return {"role": "assistant", "content": "BRIEF: you were working on arithmetic"}
        if "Extract durable facts" in last:
            return {"role": "assistant", "content": '["User asked about arithmetic"]'}
        if "write a brief report" in last.lower():
            return {"role": "assistant", "content": "REPORT"}
        if messages[-1].get("role") == "tool":
            return {"role": "assistant", "content": "done: " + messages[-1]["content"]}
        if "calculate" in last:
            return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "calculate", "arguments": {"expression": "6*7"}}}]}
        if "send" in last:
            return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "send_email", "arguments": {"to": "a@b.c", "subject": "s", "body": "b"}}}]}
        return {"role": "assistant", "content": "hello"}


@pytest.fixture
def rt(tmp_path):
    r = Runtime(Config(data_dir=tmp_path), llm=ScriptLLM(), embedder=HashEmbedder())
    yield r
    r.close()


def test_end_to_end_chat_with_tool_and_memory(rt):
    assert rt.chat("please calculate 6*7").endswith("42")
    assert any("arithmetic" in f.text for f in rt.store.facts())
    kinds = [e["event"] for e in rt.agent.log.read()]
    assert "tool_call" in kinds and "tool_result" in kinds


def test_goal_plan_executes_end_to_end(rt):
    out = rt.do("compute and save")
    assert out == "REPORT"
    plans = list((rt.config.data_dir / "plans").glob("*.json"))
    assert len(plans) == 1 and rt.orchestrator.unfinished() == []


def test_risky_action_blocked_until_approved(rt, monkeypatch):
    import smtplib
    class S:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, p): pass
        def send_message(self, m): pass
    monkeypatch.setattr(smtplib, "SMTP_SSL", S)
    for k in ("HOST", "USER", "PASSWORD"): monkeypatch.setenv("SMTP_" + k, "x")
    assert "approval" in rt.chat("send the email")
    pending = rt.approvals.pending()
    assert len(pending) == 1 and pending[0]["skill"] == "send_email"
    result = rt.run_approval(pending[0]["id"])          # approving runs the queued action itself
    assert "sent to a@b.c" in result                    # the approved call really ran
    assert any(e["event"] == "approved_action" and e["skill"] == "send_email" for e in rt.agent.log.read())


def test_viewer_role_cannot_use_email(tmp_path):
    r = Runtime(Config(data_dir=tmp_path), llm=ScriptLLM(), embedder=HashEmbedder(), role="viewer")
    r.chat("send the email")
    assert any(e["event"] == "blocked_by_role" or e["event"] == "intent_shortlist" for e in r.agent.log.read())
    assert not r.approvals.pending()
    assert "send_email" not in [t["function"]["name"] for t in r.registry.tools() if r.policy.allows(t["function"]["name"])]
    r.close()


def test_daemon_brief_and_file_trigger(rt):
    runner = rt.start_daemon(poll_seconds=3600, brief_at="00:00")
    (rt.config.data_dir / "inbox" / "note.txt").write_text("hi")
    assert runner.tick() >= 2          # brief fires + file change
    runner.stop()
    notes = (rt.config.data_dir / "notifications.jsonl").read_text()
    assert "BRIEF:" in notes and "inbox-folder" in notes


def test_ui_api_requires_token_and_controls_memory(rt):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(rt, "tok"))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    def call(path, method="GET", body=None, token="tok"):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Ragnarok-Token": token, "Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read())
    with pytest.raises(urllib.error.HTTPError) as e:
        call("/api/memory", token="wrong")
    assert e.value.code == 401
    fid = call("/api/memory", "POST", {"text": "User likes chai"})["id"]
    call(f"/api/memory/{fid}", "PUT", {"text": "User likes filter coffee"})
    assert "filter coffee" in json.dumps(call("/api/memory"))
    assert call(f"/api/memory/{fid}", "DELETE")["ok"]
    with pytest.raises(urllib.error.HTTPError) as e:
        call("/api/wipe", "POST", {})
    assert e.value.code == 400
    html = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
    assert "tok" in html and "__TOKEN__" not in html
    srv.shutdown()


def test_benchmarks_meet_nfr_targets():
    r = bench.run(n_facts=500, queries=10)
    assert r["nfr2_1_pass"] and r["nfr3_2_pass"] and r["nfr2_2_pass_background"]
