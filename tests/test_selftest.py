import datetime as dt
import imaplib
import smtplib
from email.message import EmailMessage

import httpx

from ragnarok import selftest as st
from ragnarok.cloud.oauth import TokenStore
from ragnarok.credentials import CredentialStore


def status(checks):
    return {c.name: c.status for c in checks}


def test_local_integrations_pass_and_leave_nothing_behind(tmp_path):
    checks = st.integration_checks(tmp_path)
    s = status(checks)
    for n in ("notes", "calendar (local)", "tasks", "memory engine", "code sandbox"):
        assert s[n] == "PASS", [c for c in checks if c.name == n]
    assert s["email: sending"] == s["email: reading"] == s["Google (Calendar + Gmail)"] == s["Microsoft 365 (Outlook)"] == "SKIP"
    assert not list((tmp_path / "notes").glob("_selftest_*"))
    for f in ("calendar.json", "tasks.json"):
        assert not (tmp_path / f).exists() or "selftest" not in (tmp_path / f).read_text()


def test_email_checks_only_send_when_asked(tmp_path, monkeypatch):
    CredentialStore(tmp_path).put("smtp", {"host": "smtp.gmail.com", "port": 465, "user": "me@gmail.com", "password": "pw"})
    sent = []
    class S:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, p): pass
        def send_message(self, m): sent.append(m)
    class I:
        def __init__(self, host, port): assert host == "imap.gmail.com"        # derived from the saved Gmail login
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, p): pass
        def select(self, *a, **k): pass
        def uid(self, cmd, *a):
            if cmd == "search": return "OK", [b"1 2"]
            m = EmailMessage(); m["Subject"] = "Hello"; m["From"] = "x@y.z"; return "OK", [(b"", m.as_bytes())]
    monkeypatch.setattr(smtplib, "SMTP_SSL", S); monkeypatch.setattr(imaplib, "IMAP4_SSL", I)
    s = status(st.integration_checks(tmp_path))
    assert s["email: sending"] == "SKIP" and s["email: reading"] == "PASS" and sent == []
    s = status(st.integration_checks(tmp_path, send_mail=True))
    assert s["email: sending"] == "PASS" and len(sent) == 1 and sent[0]["To"] == "me@gmail.com"


def test_cloud_checks_report_pass_and_fail(tmp_path):
    ok = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"items": [], "messages": [], "value": []})))
    TokenStore(tmp_path, http=ok).put("google", {"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    assert status(st.integration_checks(tmp_path, http=ok))["Google (Calendar + Gmail)"] == "PASS"
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    c = {x.name: x for x in st.integration_checks(tmp_path, http=bad)}["Google (Calendar + Gmail)"]
    assert c.status == "FAIL" and "500" in c.detail


class ScriptedModel:
    def __init__(self, today, wrong_date=False, silent=False):
        self.today, self.wrong_date, self.silent = today, wrong_date, silent
    def chat(self, messages, tools=None):
        if self.silent:
            return {"role": "assistant", "content": "Sure, done!"}
        user = [m for m in messages if m["role"] == "user"][-1]["content"].lower()
        day = self.today + dt.timedelta(days=0 if self.wrong_date else 1)
        friday = self.today + dt.timedelta((4 - self.today.weekday()) % 7)
        table = [("dentist", "add_event", {"title": "Dentist", "start": f"{day} 15:00"}),
                 ("on my calendar", "list_events", {"date": str(self.today)}),
                 ("task", "add_task", {"title": "Finish report", "due": str(friday)}),
                 ("ideas.md", "write_note", {"name": "ideas.md", "text": "buy milk"}),
                 ("test@example.com", "send_email", {"to": "test@example.com", "subject": "Hi", "body": "Hello there."}),
                 ("new email", "check_email", {}),
                 ("remember", "remember", {"fact": "favourite colour is blue"}),
                 ("12*(3+4)", "calculate", {"expression": "12*(3+4)"})]
        for key, name, args in table:
            if key in user:
                return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}
        return {"role": "assistant", "content": "?"}


def test_model_check_passes_a_model_that_uses_tools_correctly():
    today = dt.date(2026, 10, 26)                                              # a Monday
    res = st.model_checks(ScriptedModel(today), today=today)
    assert len(res) == 8 and all(c.status == "PASS" for c in res), res


def test_model_check_catches_wrong_dates_and_silent_models():
    today = dt.date(2026, 10, 26)
    res = {c.name: c for c in st.model_checks(ScriptedModel(today, wrong_date=True), today=today)}
    assert res["calendar: add event"].status == "FAIL" and "expected 2026-10-27T15:00" in res["calendar: add event"].detail
    assert sum(c.status == "PASS" for c in res.values()) == 7
    silent = st.model_checks(ScriptedModel(today, silent=True), today=today)
    assert all(c.status == "FAIL" and "answered in text" in c.detail for c in silent)


def test_render_and_summary():
    cs = [st.Check("a", "PASS", "x"), st.Check("b", "FAIL", "y"), st.Check("c", "SKIP", "z")]
    assert st.summary(cs) == "1 passed, 1 failed, 1 skipped" and "FAIL  b" in st.render(cs)


def test_model_check_reports_a_down_server_once():
    from ragnarok.llm import OllamaError
    class Down:
        def chat(self, messages, tools=None): raise OllamaError("Can't reach Ollama")
    res = st.model_checks(Down())
    assert len(res) == 1 and res[0].status == "FAIL" and "Can't reach Ollama" in res[0].detail
