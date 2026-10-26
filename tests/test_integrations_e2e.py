"""Every integration driven through the real agent loop (scripted model standing in for Ollama)."""
import datetime as dt
import imaplib
import json
import smtplib
from email.message import EmailMessage

import pytest

from ragnarok.config import Config
from ragnarok.credentials import CredentialStore
from ragnarok.memory import HashEmbedder
from ragnarok.runtime import Runtime

TOMORROW = (dt.date.today() + dt.timedelta(days=1)).isoformat()


class Scripted:
    """Calls the queued tools one per chat() turn, then echoes the last tool result as its answer."""
    def __init__(self): self.script, self.seen = [], []
    def chat(self, messages, tools=None):
        self.seen.append(messages)
        last = messages[-1]
        if last["role"] == "tool":
            return {"role": "assistant", "content": last["content"]}
        if "Extract durable facts" in (last.get("content") or ""):
            return {"role": "assistant", "content": "[]"}
        if self.script:
            name, args = self.script.pop(0)
            return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}
        return {"role": "assistant", "content": "ok"}


@pytest.fixture
def rt(tmp_path):
    r = Runtime(Config(data_dir=tmp_path), llm=Scripted(), embedder=HashEmbedder())
    yield r
    r.close()


def ask(rt, name, args, text="do it"):
    rt.llm.script.append((name, args))
    return rt.chat(text)


def test_model_is_told_todays_date_every_turn(rt):
    rt.chat("hello")
    system = " ".join(m["content"] for m in rt.llm.seen[0] if m["role"] == "system")
    assert "Current date and time:" in system and dt.date.today().strftime("%A") in system
    assert rt.llm.seen[0][-1]["role"] == "user"                      # the user's message is still last


def test_calendar_through_the_agent(rt):
    out = ask(rt, "add_event", {"title": "Dentist", "start": f"{TOMORROW} 15:00"})          # sloppy format a model may send
    assert out.startswith("event added: Dentist at ")
    out = ask(rt, "add_event", {"title": "Call mum", "start": f"{TOMORROW}T15:30:00Z".replace("Z", "")})
    assert "overlaps with 'Dentist'" in out
    assert "Dentist" in ask(rt, "list_events", {"date": TOMORROW})
    assert "bad" in ask(rt, "list_events", {"date": "bad"}).lower() or "not a date" in ask(rt, "list_events", {"date": "bad"})
    assert "not a date/time" in ask(rt, "add_event", {"title": "x", "start": "next tuesday"})
    # deleting needs approval: nothing is removed until the user approves
    reply = ask(rt, "delete_event", {"title": "Dentist", "date": TOMORROW}, "delete my dentist event")
    assert "WAITING FOR APPROVAL" in reply and "Dentist" in ask(rt, "list_events", {"date": TOMORROW})
    aid = rt.approvals.pending()[0]["id"]
    assert rt.run_approval(aid).startswith("deleted 1 event")
    assert "Dentist" not in ask(rt, "list_events", {"date": TOMORROW})


def test_tasks_and_notes_through_the_agent(rt):
    ask(rt, "add_task", {"title": "Finish report", "due": TOMORROW, "project": "college"})
    listing = ask(rt, "list_tasks", {})
    assert "Finish report" in listing and TOMORROW in listing
    tid = listing.split()[0]
    assert ask(rt, "complete_task", {"task_id": tid}) == "done"
    assert ask(rt, "list_tasks", {}) == "no open tasks"
    assert ask(rt, "write_note", {"name": "ideas.md", "text": "buy milk"}) == "saved ideas.md"
    assert ask(rt, "read_note", {"name": "ideas.md"}) == "buy milk"
    assert "not found" in ask(rt, "read_note", {"name": "nope.md"}).lower() or "No such file" in ask(rt, "read_note", {"name": "nope.md"})
    assert "WAITING FOR APPROVAL" in ask(rt, "delete_note", {"name": "ideas.md"})
    assert ask(rt, "read_note", {"name": "ideas.md"}) == "buy milk"                      # still there


def test_email_read_and_send_through_the_agent(rt, tmp_path, monkeypatch):
    assert "setup-email" in ask(rt, "check_email", {})                                    # honest when not set up
    assert "setup-email" in ask(rt, "send_email", {"to": "a@b.c", "subject": "s", "body": "b"})
    assert rt.approvals.pending() == []                                                   # nothing useless queued
    CredentialStore(tmp_path).put("smtp", {"host": "smtp.gmail.com", "port": 465, "user": "me@gmail.com", "password": "pw"})
    sent = []
    class S:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, p): pass
        def send_message(self, m): sent.append(m)
    class I:
        def __init__(self, host, port): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, p): pass
        def select(self, *a, **k): pass
        def uid(self, cmd, *a):
            if cmd == "search": return "OK", [b"5"]
            m = EmailMessage(); m["Subject"] = "Interview call"; m["From"] = "hr@x.com"; return "OK", [(b"", m.as_bytes())]
    monkeypatch.setattr(smtplib, "SMTP_SSL", S); monkeypatch.setattr(imaplib, "IMAP4_SSL", I)
    assert "Interview call - hr@x.com" in ask(rt, "check_email", {})
    reply = ask(rt, "send_email", {"to": "a@b.c", "subject": "Welcome", "body": "Hello"}, "send a welcome email")
    assert "WAITING FOR APPROVAL" in reply and sent == []
    assert "sent to a@b.c" in rt.run_approval(rt.approvals.pending()[0]["id"])
    assert len(sent) == 1 and sent[0]["Subject"] == "Welcome" and sent[0]["From"] == "me@gmail.com"


def test_new_mail_triggers_only_for_mail_arriving_after_start(rt, tmp_path, monkeypatch):
    CredentialStore(tmp_path).put("smtp", {"host": "smtp.gmail.com", "user": "me@gmail.com", "password": "pw"})
    inbox = [("1", "Old newsletter", "news@x.com"), ("2", "Old promo", "promo@x.com")]
    monkeypatch.setattr("ragnarok.runtime.imap_fetch_unseen", lambda limit=10, data_dir=None: list(inbox))
    runner = rt.start_daemon(poll_seconds=3600, brief_at="23:59", maintenance_at="23:59", watch_email=True)
    email_trigger = next(t for t in runner.triggers if t.name == "email"); email_trigger.every = 0
    notes = lambda: (rt.config.data_dir / "notifications.jsonl").read_text() if (rt.config.data_dir / "notifications.jsonl").exists() else ""
    runner.tick()
    assert "Old newsletter" not in notes() and "[email]" not in notes()                           # backlog ignored
    inbox.append(("3", "Offer letter", "hr@x.com"))
    runner.tick(); runner.tick()
    assert notes().count("[email]") == 1                                                          # once, not twice
    assert "<external>" in json.dumps(rt.llm.seen[-1]) and "Offer letter" in json.dumps(rt.llm.seen[-1])
    runner.stop()


def test_watch_email_without_mailbox_explains_itself(rt):
    runner = rt.start_daemon(poll_seconds=3600, watch_email=True)
    assert "no mailbox is set up" in (rt.config.data_dir / "notifications.jsonl").read_text()
    assert not any(t.name == "email" for t in runner.triggers)
    runner.stop()


def test_project_tools_through_the_agent(rt, tmp_path, monkeypatch):
    proj = tmp_path / "code"; proj.mkdir(); (proj / "app.py").write_text("print('hi')\n")
    assert "no project folders" in ask(rt, "read_project_file", {"path": str(proj / "app.py")})
    monkeypatch.setenv("RAGNAROK_PROJECTS", str(proj))
    assert "print('hi')" in ask(rt, "read_project_file", {"path": str(proj / "app.py")})
    assert "outside" in ask(rt, "read_project_file", {"path": str(tmp_path / "secret.txt")})


def test_memory_skills_through_the_agent(rt):
    assert "saved as memory" in ask(rt, "remember", {"fact": "User's exam is on Friday"})
    assert "exam" in ask(rt, "recall", {"query": "when is my exam"})
    assert "WAITING FOR APPROVAL" in ask(rt, "forget", {"memory_id": 1})
    assert "exam" in ask(rt, "recall", {"query": "exam"})                                         # not deleted until approved
