"""`ragnarok selftest`: check every integration for real, on this machine.

Integration checks are non-destructive: anything they create is removed again, a real email is only sent
with --send-mail, and cloud accounts only get read-only calls.
`--model` additionally checks that YOUR chosen Ollama model calls the right tool with the right arguments
for everyday requests (nothing is executed outside a throw-away folder)."""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


def _try(name: str, fn: Callable[[], object]) -> Check:
    try:
        r = fn()
        return r if isinstance(r, Check) else Check(name, PASS, str(r or ""))
    except Exception as e:
        return Check(name, FAIL, f"{type(e).__name__}: {e}")


# ----------------------------------------------------------------------------- integrations
def integration_checks(data_dir: Path, send_mail: bool = False, http=None) -> list[Check]:
    from .integrations import imap_settings, register_integrations, smtp_settings
    from .skills import SkillRegistry

    data_dir = Path(data_dir)
    reg = SkillRegistry()
    register_integrations(reg, data_dir)
    fn = lambda n: reg.get(n).func
    tag = uuid.uuid4().hex[:6]
    tomorrow = (dt.datetime.now() + dt.timedelta(days=1)).strftime("%Y-%m-%d")
    out: list[Check] = []

    def notes():
        name = f"_selftest_{tag}.md"
        try:
            fn("write_note")(name, "hello")
            assert fn("read_note")(name) == "hello", "note text came back different"
            assert name in fn("list_notes")(), "note not listed"
        finally:
            (data_dir / "notes" / name).unlink(missing_ok=True)
        return "write, read, list"
    out.append(_try("notes", notes))

    def calendar():
        title = f"[selftest {tag}]"
        ics = data_dir / "notes" / f"_selftest_{tag}.ics"
        try:
            assert "event added" in fn("add_event")(title, f"{tomorrow} 03:00"), "add failed"   # space format on purpose
            assert title in fn("list_events")(tomorrow), "event not listed"
            assert "overlaps" in fn("add_event")(title + " b", f"{tomorrow}T03:30"), "overlap not detected"
            assert "exported" in fn("export_calendar")(ics.name)
        finally:
            for t in (title, title + " b"):
                try: fn("delete_event")(t, tomorrow)
                except Exception: pass
            ics.unlink(missing_ok=True)
        assert title not in fn("list_events")(tomorrow), "cleanup failed"
        return "add, list, overlap warning, .ics export, delete"
    out.append(_try("calendar (local)", calendar))

    def tasks():
        title = f"[selftest {tag}]"
        try:
            res = fn("add_task")(title, "selftest", tomorrow)
            assert title in fn("list_tasks")("selftest"), "task not listed"
            assert fn("complete_task")(res.split()[1]) == "done"
            assert title not in fn("list_tasks")("selftest"), "task still open after completing"
        finally:
            f = data_dir / "tasks.json"
            if f.exists():
                f.write_text(json.dumps([t for t in json.loads(f.read_text()) if not t["title"].startswith("[selftest")], indent=2))
        return "add, list, complete"
    out.append(_try("tasks", tasks))

    def projects():
        roots = os.environ.get("RAGNAROK_PROJECTS", "").strip()
        if not roots:
            return Check("project / IDE tools", SKIP, "set RAGNAROK_PROJECTS to a code folder to enable (read-only)")
        root = Path(roots.split(os.pathsep)[0]).expanduser().resolve()
        assert fn("list_project")(str(root)) != "(empty)" or not any(root.iterdir()), "could not list the folder"
        extra = ""
        if (root / ".git").exists():
            assert fn("git_status")(str(root)), "git_status gave nothing"
            extra = ", git status"
        try:
            fn("read_project_file")(str(root.parent / "definitely-outside.txt"))
            raise AssertionError("a path outside the approved folder was NOT blocked")
        except PermissionError:
            pass
        return f"list{extra}, outside-folder access blocked"
    out.append(_try("project / IDE tools", projects))

    def send():
        s = smtp_settings(data_dir)
        if not s:
            return Check("email: sending", SKIP, "not set up: run `ragnarok setup-email`")
        if not send_mail:
            return Check("email: sending", SKIP, f"login saved for {s['user']}; add --send-mail to send a real test email to yourself")
        res = fn("send_email")(s["user"], "Ragnarok selftest", "If you can read this, email sending works.")
        return Check("email: sending", PASS if res.startswith("sent") else FAIL, res)
    out.append(_try("email: sending", send))

    def read():
        if not imap_settings(data_dir):
            return Check("email: reading", SKIP, "not set up: `ragnarok setup-email` (Gmail/Outlook/Yahoo) or IMAP_* variables")
        res = fn("check_email")()
        return Check("email: reading", FAIL if res.startswith("error") else PASS,
                     res if res.startswith("error") else ("no unread email" if res == "no unread email" else f"{len(res.splitlines())} unread shown (headers only)"))
    out.append(_try("email: reading", read))

    # optional cloud accounts: read-only calls
    from .cloud.oauth import TokenStore
    from .cloud.providers import register_cloud
    tokens = TokenStore(data_dir, http=http) if http else TokenStore(data_dir)
    creg = SkillRegistry()
    register_cloud(creg, tokens, http)
    today = dt.date.today().isoformat()
    for label, provider, calls in (("Google (Calendar + Gmail)", "google", ("gcal_list_events", "gmail_check")),
                                   ("Microsoft 365 (Outlook)", "microsoft", ("outlook_list_events", "outlook_check_mail"))):
        if not tokens.has(provider):
            out.append(Check(label, SKIP, f"not connected (optional): `ragnarok connect {provider}`"))
            continue

        def cloud(calls=calls):
            for c in calls:
                creg.get(c).func(today) if c.endswith("events") else creg.get(c).func()
            return "calendar and mail reachable (read-only)"
        out.append(_try(label, cloud))

    def memory():
        from .memory import HashEmbedder, MemoryManager, MemoryStore
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            m = MemoryManager(MemoryStore(Path(tmp)), HashEmbedder())
            try:
                fid = m.remember("selftest fact: the user likes chai")
                assert "chai" in m.recall("what does the user like to drink")[0].text, "recall missed the fact"
                assert m.delete(fid)
            finally:
                m.store.close()
        return "remember, recall, delete (temporary store)"
    out.append(_try("memory engine", memory))

    def sandbox():
        from .sandbox import run_python
        r = run_python("print(6*7)")
        assert r.ok and r.stdout.strip() == "42", r.text()
        return f"{r.backend} backend"
    out.append(_try("code sandbox", sandbox))

    def ollama():
        from .config import Config
        from .llm import OllamaClient
        st = OllamaClient(Config()).status()
        if not st["reachable"]:
            return Check("Ollama", FAIL, "not reachable: start it with `ollama serve`")
        miss = [m for m, ok in ((Config().chat_model, st.get("chat_model_ok")), (Config().embed_model, st.get("embed_model_ok"))) if not ok]
        return Check("Ollama", FAIL if miss else PASS, ("missing: " + ", ".join(f"`ollama pull {m}`" for m in miss)) if miss else "running, both models installed")
    out.append(_try("Ollama", ollama))
    return out


# ----------------------------------------------------------------------------- does the model use the tools correctly?
def _args(call: dict) -> dict:
    a = call.get("args", {})
    if isinstance(a, str):
        try: a = json.loads(a)
        except json.JSONDecodeError: a = {}
    return a if isinstance(a, dict) else {}


def _cases(today: dt.date):
    from .builtin_skills import calculate
    from .integrations import _day, _when

    tomorrow = today + dt.timedelta(days=1)

    def event(a):
        w = _when(str(a.get("start", "")))
        return ("dentist" in str(a.get("title", "")).lower() and w == f"{tomorrow.isoformat()}T15:00",
                f"title={a.get('title')!r} start={a.get('start')!r} (expected {tomorrow.isoformat()}T15:00)")

    def listing(a):
        d = _day(str(a.get("date", "")))
        return d in ("", today.isoformat()), f"date={a.get('date')!r}"

    def task(a):
        try:
            d = dt.date.fromisoformat(_day(str(a.get("due", ""))))
        except Exception:
            return False, f"due={a.get('due')!r} is not a date"
        return ("report" in str(a.get("title", "")).lower() and d.weekday() == 4 and 0 <= (d - today).days <= 7,
                f"title={a.get('title')!r} due={a.get('due')!r} (expected the coming Friday)")

    def note(a):
        return "ideas" in str(a.get("name", "")).lower() and "milk" in str(a.get("text", "")).lower(), f"name={a.get('name')!r} text={a.get('text')!r}"

    def mail(a):
        return (str(a.get("to", "")).strip() == "test@example.com" and "hi" in str(a.get("subject", "")).lower()
                and "hello" in str(a.get("body", "")).lower()), f"to={a.get('to')!r} subject={a.get('subject')!r} body={a.get('body')!r}"

    def remember(a):
        return "blue" in str(a.get("fact", "")).lower(), f"fact={a.get('fact')!r}"

    def calc(a):
        try:
            return calculate(str(a.get("expression", ""))) == "84", f"expression={a.get('expression')!r}"
        except Exception:
            return False, f"expression={a.get('expression')!r} did not evaluate"

    return [
        ("calendar: add event", "Add a calendar event called Dentist tomorrow at 3pm.", "add_event", event),
        ("calendar: today", "What's on my calendar today?", "list_events", listing),
        ("tasks: add", "Add a task to finish my report, due Friday.", "add_task", task),
        ("notes: save", "Save a note called ideas.md that says buy milk.", "write_note", note),
        ("email: send", "Email test@example.com with subject Hi and body Hello there.", "send_email", mail),
        ("email: read", "Do I have any new email?", "check_email", lambda a: (True, "")),
        ("memory: remember", "Remember that my favourite colour is blue.", "remember", remember),
        ("calculator", "What is 12*(3+4)?", "calculate", calc),
    ]


def model_checks(llm=None, today: dt.date | None = None) -> list[Check]:
    from .config import Config
    from .llm import OllamaClient, OllamaError
    from .runtime import Runtime

    today = today or dt.date.today()
    llm = llm or OllamaClient(Config())
    out: list[Check] = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        rt = Runtime(Config(data_dir=Path(tmp), max_steps=1), llm=llm)       # one model call per prompt, throw-away folder
        rt.agent.memory = None
        try:
            for name, prompt, expected, verify in _cases(today):
                rt.agent.history = rt.agent.history[:1]
                n0 = len(rt.agent.log.read())
                try:
                    rt.agent.run(prompt)
                except Exception as e:
                    if isinstance(e, OllamaError):          # the server itself is the problem: say it once
                        return [Check("model", FAIL, str(e))]
                    out.append(Check(name, FAIL, f"model call failed: {type(e).__name__}: {e}")); continue
                calls = [e for e in rt.agent.log.read()[n0:] if e["event"] == "tool_call"]
                if not calls:
                    out.append(Check(name, FAIL, f"answered in text instead of calling {expected}")); continue
                match = next((c for c in calls if c["skill"] == expected), None)
                if match is None:
                    out.append(Check(name, FAIL, f"called {calls[0]['skill']} instead of {expected}")); continue
                ok, detail = verify(_args(match))
                out.append(Check(name, PASS if ok else FAIL, f"{expected}({detail})" if detail else expected))
        finally:
            rt.close()
    return out


def summary(checks: list[Check]) -> str:
    n = {s: sum(c.status == s for c in checks) for s in (PASS, FAIL, SKIP)}
    return f"{n[PASS]} passed, {n[FAIL]} failed, {n[SKIP]} skipped"


def render(checks: list[Check]) -> str:
    return "\n".join(f"  {c.status:<5} {c.name:<28} {c.detail}" for c in checks)
