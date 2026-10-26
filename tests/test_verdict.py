import pytest

from ragnarok import __version__
from ragnarok.agent import SYSTEM_PROMPT, Agent
from ragnarok.cli import main
from ragnarok.config import Config
from ragnarok.skills import SkillRegistry

LIE = "Done! The email has been sent."


def make(tmp_path, approve, deferred=False, precheck=None, crash=False):
    reg, calls = SkillRegistry(), []
    def send(to: str):
        calls.append(to)
        if crash: raise RuntimeError("boom")
        return f"sent to {to}"
    reg.register(name="send", description="d", risky=True, precheck=precheck)(send)
    replies = iter([{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "send", "arguments": {"to": "a@b.c"}}}]},
                    {"role": "assistant", "content": LIE}])
    class L:
        def chat(self, m, tools=None): return next(replies)
    return Agent(L(), reg, Config(data_dir=tmp_path), approve=approve, deferred_approvals=deferred), calls


def test_pending_action_is_not_reported_as_sent(tmp_path):
    a, calls = make(tmp_path, lambda n, g: False, deferred=True)
    reply = a.run("send it")
    assert "WAITING FOR APPROVAL" in reply and "a@b.c" in reply and "sent" not in reply.lower().replace("not done", "")
    assert calls == [] and a.history[-1]["content"] == reply          # the lie is not kept in history either
    assert any(e["event"] == "verdict" for e in a.log.read())


def test_declined_action(tmp_path):
    a, calls = make(tmp_path, lambda n, g: False)
    assert "you declined" in a.run("send it") and calls == []


def test_precheck_failure_is_reported(tmp_path):
    a, calls = make(tmp_path, lambda n, g: True, precheck=lambda: "error: email is not set up")
    reply = a.run("send it")
    assert "Could not complete" in reply and "not set up" in reply and calls == []


def test_crash_after_approval_is_reported(tmp_path):
    a, _ = make(tmp_path, lambda n, g: True, crash=True)
    reply = a.run("send it")
    assert "Could not complete" in reply and "boom" in reply


def test_success_keeps_the_models_own_reply(tmp_path):
    a, calls = make(tmp_path, lambda n, g: True)
    assert a.run("send it") == LIE and calls == ["a@b.c"]


def test_state_resets_each_turn(tmp_path):
    a, _ = make(tmp_path, lambda n, g: False, deferred=True)
    a.run("x"); assert a._blocked
    a.llm.chat = lambda m, tools=None: {"role": "assistant", "content": "hello"}
    assert a.run("just chatting") == "hello"


def test_prompt_forbids_false_claims_and_version(capsys):
    assert "Never say an action was done" in SYSTEM_PROMPT
    with pytest.raises(SystemExit):
        main(["--version"])
    assert __version__ in capsys.readouterr().out
