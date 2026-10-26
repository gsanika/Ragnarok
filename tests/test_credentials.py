import smtplib

import pytest

from ragnarok.config import Config
from ragnarok.credentials import CredentialStore
from ragnarok.integrations import SMTP_HELP, register_integrations, smtp_settings
from ragnarok.memory import HashEmbedder
from ragnarok.runtime import Runtime
from ragnarok.skills import SkillRegistry
from test_approvals import MailLLM


class FakeSMTP:
    logins, sent = [], []
    def __init__(self, host, port, **kw): self.host, self.port = host, port
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def login(self, u, p): FakeSMTP.logins.append((self.host, self.port, u, p))
    def send_message(self, m): FakeSMTP.sent.append(m)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.logins, FakeSMTP.sent = [], []
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)


def test_saved_login_is_encrypted_and_used_without_env(tmp_path):
    CredentialStore(tmp_path).put("smtp", {"host": "smtp.gmail.com", "port": 465, "user": "me@gmail.com", "password": "SECRETPASSWORD"})
    assert b"SECRETPASSWORD" not in (tmp_path / "credentials.enc").read_bytes()
    reg = SkillRegistry(); register_integrations(reg, tmp_path)
    assert "sent to a@b.c" in reg.get("send_email").func("a@b.c", "s", "b")           # no SMTP_* in the environment
    assert FakeSMTP.logins == [("smtp.gmail.com", 465, "me@gmail.com", "SECRETPASSWORD")]


def test_environment_overrides_saved_login(tmp_path, monkeypatch):
    CredentialStore(tmp_path).put("smtp", {"host": "old", "user": "old@x", "password": "old"})
    monkeypatch.setenv("SMTP_HOST", "new.host"); monkeypatch.setenv("SMTP_USER", "new@x"); monkeypatch.setenv("SMTP_PASSWORD", "newpw")
    s = smtp_settings(tmp_path)
    assert (s["host"], s["user"], s["password"]) == ("new.host", "new@x", "newpw")


def test_unreadable_store_counts_as_not_set_up(tmp_path):
    (tmp_path / "credentials.enc").write_bytes(b"garbage")
    assert smtp_settings(tmp_path) is None


def test_agent_refuses_to_queue_unsendable_email(tmp_path):
    rt = Runtime(Config(data_dir=tmp_path), llm=MailLLM(), embedder=HashEmbedder())
    try:
        out = rt.chat("send an email to a@b.c")
        assert "setup-email" in out and rt.approvals.pending() == []                   # nothing for the user to approve in vain
        assert any(e["event"] == "precheck_failed" for e in rt.agent.log.read())
        CredentialStore(tmp_path).put("smtp", {"host": "h", "user": "u@x", "password": "p"})
        rt.chat("send an email to a@b.c")                                                # now it can be queued
        assert len(rt.approvals.pending()) == 1
        aid = rt.approvals.pending()[0]["id"]
        assert "sent to a@b.c" in rt.run_approval(aid) and len(FakeSMTP.sent) == 1
    finally:
        rt.close()


def test_help_text_mentions_the_fix():
    assert "ragnarok setup-email" in SMTP_HELP and "SMTP not configured" in SMTP_HELP
