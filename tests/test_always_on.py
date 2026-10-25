import datetime as dt
import json

from ragnarok.integrations import register_integrations
from ragnarok.permissions import ApprovalStore, Policy
from ragnarok.proactive import morning_brief
from ragnarok.skills import SkillRegistry
from ragnarok.triggers import DailyTrigger, EmailTrigger, FileWatchTrigger, IntervalTrigger, Trigger, TriggerRunner


def test_interval_and_daily():
    t = IntervalTrigger("i", 10, "ping")
    assert t.poll(0) == [] and t.poll(5) == [] and t.poll(11) == ["ping"] and t.poll(12) == []
    morning = dt.datetime(2026, 10, 26, 8, 0).timestamp()
    d = DailyTrigger("d", "08:00", "brief")
    assert d.poll(morning - 60) == [] and d.poll(morning) == ["brief"] and d.poll(morning + 60) == []
    assert d.poll(morning + 86400) == ["brief"]


def test_file_watch(tmp_path):
    w = FileWatchTrigger("f", tmp_path)
    assert w.poll(0) == []
    (tmp_path / "a.txt").write_text("x")
    assert len(w.poll(1)) == 1 and w.poll(2) == []


def test_email_trigger_dedupes():
    t = EmailTrigger("e", lambda: [("1", "Hi", "bob"), ("2", "Yo", "amy")], every=0)
    first = t.poll(1)
    assert len(first) == 2 and t.poll(2) == []


def test_runner_survives_broken_trigger(tmp_path):
    class Boom(Trigger):
        name = "boom"
        def poll(self, now): raise RuntimeError("x")
    got = []
    r = TriggerRunner([Boom(), IntervalTrigger("i", 0, "go")], lambda task, name: got.append(task))
    r.tick(0); r.tick(1)
    assert got == ["go"]


def test_rbac():
    assert Policy("viewer").allows("calculate") and not Policy("viewer").allows("send_email")
    assert Policy("assistant").allows("send_email") and not Policy("assistant").allows("run_python")
    assert Policy("admin").allows("run_python")


def test_approvals_one_shot(tmp_path):
    a = ApprovalStore(tmp_path)
    assert a.check("send_email", {"to": "x"}) is False
    p = a.pending()
    assert len(p) == 1
    assert a.grant(p[0]["id"]) and a.check("send_email", {"to": "x"}) is True
    assert a.check("send_email", {"to": "x"}) is False   # consumed


def test_notes_calendar_and_path_safety(tmp_path):
    reg = SkillRegistry(); register_integrations(reg, tmp_path)
    reg.get("write_note").func("a.md", "hello")
    assert reg.get("read_note").func("a.md") == "hello"
    try:
        reg.get("read_note").func("../../etc/passwd"); assert False
    except PermissionError:
        pass
    reg.get("add_event").func("Review", "2026-10-26T15:00")
    assert "Review" in reg.get("list_events").func("2026-10-26")
    assert reg.get("send_email").risky and "not configured" in reg.get("send_email").func("a@b.c", "s", "b")


def test_morning_brief_offline(tmp_path):
    from ragnarok.memory import HashEmbedder, MemoryManager, MemoryStore
    m = MemoryManager(MemoryStore(tmp_path), HashEmbedder())
    m.remember("User is building Ragnarok", importance=0.9); m.log_episode("user", "fix the sandbox")
    out = morning_brief(m)
    assert "Ragnarok" in out and "sandbox" in out
