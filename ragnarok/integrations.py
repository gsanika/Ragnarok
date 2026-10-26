"""User-approved app integrations (FR3.2): notes, calendar (local JSON), email (IMAP/SMTP).
Credentials come from environment variables and never leave the machine except to the
user's own mail server."""
from __future__ import annotations

import datetime as dt
import email
import imaplib
import json
import os
import smtplib
import re
import subprocess
import uuid
from email.header import decode_header, make_header
from email.message import EmailMessage
from pathlib import Path

from .skills import SkillRegistry


def _safe(base: Path, name: str) -> Path:
    base = base.resolve(); p = (base / name).resolve()
    if base not in p.parents:
        raise PermissionError("path outside notes folder")
    return p


IMAP_FOR_SMTP = {"smtp.gmail.com": "imap.gmail.com", "smtp-mail.outlook.com": "outlook.office365.com",
                 "smtp.office365.com": "outlook.office365.com", "smtp.mail.yahoo.com": "imap.mail.yahoo.com"}


def imap_settings(data_dir: Path | None = None) -> dict | None:
    """Reading mail reuses the login saved by `ragnarok setup-email` for known providers; IMAP_* env vars override."""
    cfg: dict = {}
    if data_dir is not None:
        smtp = smtp_settings(data_dir)
        if smtp and IMAP_FOR_SMTP.get(smtp["host"]):
            cfg = {"host": IMAP_FOR_SMTP[smtp["host"]], "user": smtp["user"], "password": smtp["password"]}
    for k in ("host", "user", "password", "port"):
        v = os.environ.get("IMAP_" + k.upper())
        if v:
            cfg[k] = v
    return cfg if all(cfg.get(k) for k in ("host", "user", "password")) else None


def imap_fetch_unseen(limit: int = 10, data_dir: Path | None = None) -> list[tuple[str, str, str]]:
    """Unread mail as (uid, subject, sender). Headers only, read-only mailbox: nothing is ever marked as read."""
    cfg = imap_settings(data_dir)
    if not cfg:
        return []
    out = []
    with imaplib.IMAP4_SSL(cfg["host"], int(cfg.get("port") or 993)) as m:
        m.login(cfg["user"], cfg["password"].strip()); m.select("INBOX", readonly=True)
        _, data = m.uid("search", None, "UNSEEN")          # UIDs stay stable when mail arrives or is deleted
        for uid in data[0].split()[-limit:]:
            _, msg = m.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            h = email.message_from_bytes(msg[0][1])
            out.append((uid.decode(), str(make_header(decode_header(h.get("Subject", "")))), h.get("From", "")))
    return out


SMTP_HELP = ("error: SMTP not configured. Run `ragnarok setup-email` once (it saves your login encrypted), "
             "or set SMTP_HOST, SMTP_USER and SMTP_PASSWORD.")


def smtp_settings(data_dir: Path) -> dict | None:
    """Environment variables win; otherwise the login saved by `ragnarok setup-email`."""
    try:
        from .credentials import CredentialStore
        cfg = dict(CredentialStore(data_dir).get("smtp") or {})
    except Exception:           # unreadable store (e.g. different key): treat as not set up
        cfg = {}
    for k in ("host", "user", "password", "port"):
        v = os.environ.get("SMTP_" + k.upper())
        if v:
            cfg[k] = v
    return cfg if all(cfg.get(k) for k in ("host", "user", "password")) else None


def _when(s: str) -> str:
    """Accept what a model may send ('2026-10-27 15:00', '...T15:00:00Z', '2026-10-27') and store YYYY-MM-DDTHH:MM local."""
    t = s.strip().replace("Z", "+00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        t += "T00:00"
    elif re.match(r"\d{4}-\d{2}-\d{2} \d", t):
        t = t.replace(" ", "T", 1)
    try:
        d = dt.datetime.fromisoformat(t)
    except ValueError:
        raise ValueError(f"'{s}' is not a date/time; use YYYY-MM-DDTHH:MM, e.g. 2026-10-27T15:00") from None
    if d.tzinfo:
        d = d.astimezone().replace(tzinfo=None)
    return d.strftime("%Y-%m-%dT%H:%M")


def _day(s: str) -> str:
    if not s:
        return ""
    try:
        return dt.date.fromisoformat(s.strip()[:10]).isoformat()
    except ValueError:
        raise ValueError(f"'{s}' is not a date; use YYYY-MM-DD") from None


def _span(e: dict) -> tuple[dt.datetime, dt.datetime]:
    s = dt.datetime.fromisoformat(e["start"])
    return s, (dt.datetime.fromisoformat(e["end"]) if e.get("end") else s + dt.timedelta(hours=1))


def register_integrations(reg: SkillRegistry, data_dir: Path) -> None:
    data_dir = Path(data_dir)
    notes = data_dir / "notes"; notes.mkdir(parents=True, exist_ok=True)
    cal = data_dir / "calendar.json"

    @reg.register(description="Save a text note. Name like 'ideas.md'.")
    def write_note(name: str, text: str) -> str:
        _safe(notes, name).write_text(text, encoding="utf-8"); return f"saved {name}"

    @reg.register(description="Read a saved note by name.")
    def read_note(name: str) -> str:
        return _safe(notes, name).read_text(encoding="utf-8")

    @reg.register(description="List saved notes.")
    def list_notes() -> str:
        return "\n".join(sorted(p.name for p in notes.iterdir())) or "(none)"

    def _events() -> list[dict]:
        return json.loads(cal.read_text()) if cal.exists() else []

    def _save_events(evs: list[dict]) -> None:
        cal.write_text(json.dumps(evs, indent=2))

    @reg.register(description="Add a calendar event. start and end like 2026-10-27T15:00 (24-hour); end defaults to one hour later.")
    def add_event(title: str, start: str, end: str = "") -> str:
        new = {"title": title, "start": _when(start), "end": _when(end) if end else ""}
        ns, ne = _span(new)
        if ne <= ns:
            raise ValueError("the event must end after it starts")
        existing = _events()
        clashes = [x for x in existing if ns < _span(x)[1] and _span(x)[0] < ne]
        _save_events(sorted(existing + [new], key=lambda x: x["start"]))
        msg = f"event added: {title} at {new['start'].replace('T', ' ')}"
        if clashes:
            msg += "; note: it overlaps with " + ", ".join(f"'{x['title']}' ({x['start'].replace('T', ' ')})" for x in clashes)
        return msg

    @reg.register(description="List calendar events on a date (YYYY-MM-DD), or all upcoming events if no date is given.")
    def list_events(date: str = "") -> str:
        day = _day(date)
        now = dt.datetime.now().strftime("%Y-%m-%dT%H:%M")
        evs = [e for e in _events() if (e["start"].startswith(day) if day else e["start"] >= now)]
        return "\n".join(f"{e['start'].replace('T', ' ')}  {e['title']}" for e in evs) or (f"no events on {day}" if day else "no upcoming events")

    @reg.register(description="Delete calendar event(s) by title; give the date (YYYY-MM-DD) if several share the title. Requires approval.", risky=True)
    def delete_event(title: str, date: str = "") -> str:
        day, evs = _day(date), _events()
        hit = [e for e in evs if e["title"].lower() == title.strip().lower() and (not day or e["start"].startswith(day))]
        if not hit:
            return f"error: no event called '{title}'" + (f" on {day}" if day else "")
        if len(hit) > 1 and not day:
            return "error: several events have that title; say which date (YYYY-MM-DD): " + ", ".join(e["start"][:10] for e in hit)
        _save_events([e for e in evs if e not in hit])
        return f"deleted {len(hit)} event(s): {title}"

    @reg.register(description="Check the inbox for unread emails (sender and subject only).")
    def check_email() -> str:
        if not imap_settings(data_dir):
            return ("error: reading email is not set up. Run `ragnarok setup-email` (Gmail/Outlook), or set IMAP_HOST, "
                    f"IMAP_USER and IMAP_PASSWORD. [looking in data folder: {data_dir.resolve()}]")
        try:
            msgs = imap_fetch_unseen(data_dir=data_dir)
        except imaplib.IMAP4.error:
            return "error: the mail server rejected the login. Use an app password and make sure IMAP is enabled for the account."
        except OSError as e:
            return f"error: could not reach the mail server ({type(e).__name__}: {e})."
        return "\n".join(f"{s} - {f}" for _, s, f in msgs) or "no unread email"

    @reg.register(description="Send an email. Requires the user's approval.", risky=True,
                  precheck=lambda: None if smtp_settings(data_dir) else f"{SMTP_HELP} [looking in data folder: {Path(data_dir).resolve()}]")
    def send_email(to: str, subject: str, body: str) -> str:
        cfg = smtp_settings(data_dir)
        if not cfg:
            return f"{SMTP_HELP} [looking in data folder: {Path(data_dir).resolve()}]"
        host, user, pw = cfg["host"], cfg["user"], cfg["password"]
        msg = EmailMessage(); msg["From"], msg["To"], msg["Subject"] = user, to, subject; msg.set_content(body)
        port = int(cfg.get("port") or 465)
        try:
            if port == 465:                       # implicit TLS
                with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                    s.login(user, pw.strip()); s.send_message(msg)
            else:                                 # 587 etc.: STARTTLS
                with smtplib.SMTP(host, port, timeout=30) as s:
                    s.starttls(); s.login(user, pw.strip()); s.send_message(msg)
        except smtplib.SMTPAuthenticationError:
            return ("error: the mail server rejected the login. For Gmail/Outlook use an *app password* "
                    "(2-step verification must be on), and check SMTP_USER is the full address.")
        except smtplib.SMTPRecipientsRefused:
            return f"error: the server refused the recipient '{to}'."
        except (OSError, smtplib.SMTPException) as e:
            return f"error: could not reach the mail server {host}:{port} ({type(e).__name__}: {e}). Check SMTP_HOST/SMTP_PORT and your internet connection."
        return f"sent to {to}"

    @reg.register(description="Delete a saved note. Requires the user's approval.", risky=True)
    def delete_note(name: str) -> str:
        _safe(notes, name).unlink(); return f"deleted {name}"

    register_tasks(reg, data_dir)
    register_projects(reg)
    register_ics(reg, data_dir)



# ---------------------------------------------------------------- project management (local)
def register_tasks(reg: SkillRegistry, data_dir: Path) -> None:
    f = Path(data_dir) / "tasks.json"

    def load() -> list[dict]:
        return json.loads(f.read_text()) if f.exists() else []

    def save(t: list[dict]) -> None:
        f.write_text(json.dumps(t, indent=2))

    @reg.register(description="Add a to-do task. project and due (YYYY-MM-DD) are optional.")
    def add_task(title: str, project: str = "", due: str = "") -> str:
        due = _day(due)
        t = load(); tid = uuid.uuid4().hex[:6]
        t.append({"id": tid, "title": title, "project": project, "due": due, "done": False}); save(t)
        return f"task {tid} added"

    @reg.register(description="List open tasks, optionally for one project. Sorted by due date.")
    def list_tasks(project: str = "") -> str:
        rows = [x for x in load() if not x["done"] and (not project or x["project"] == project)]
        rows.sort(key=lambda x: x["due"] or "9999")
        return "\n".join(f"{x['id']}  {x['due'] or 'no date':10}  [{x['project'] or '-'}] {x['title']}" for x in rows) or "no open tasks"

    @reg.register(description="Mark a task done by its id.")
    def complete_task(task_id: str) -> str:
        t = load()
        for x in t:
            if x["id"] == task_id:
                x["done"] = True; save(t); return "done"
        return "task not found"


# ---------------------------------------------------------------- IDE / project folders (read-only)
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", "dist", "build"}


def _project_roots() -> list[Path]:
    return [Path(p).expanduser().resolve() for p in os.environ.get("RAGNAROK_PROJECTS", "").split(os.pathsep) if p.strip()]


def _in_projects(path: str) -> Path:
    roots = _project_roots()
    if not roots:
        raise PermissionError("no project folders approved: set RAGNAROK_PROJECTS to your project path(s)")
    p = Path(path).expanduser().resolve()
    if not any(r == p or r in p.parents for r in roots):
        raise PermissionError("path is outside the approved project folders")
    return p


def register_projects(reg: SkillRegistry) -> None:
    @reg.register(description="List files in an approved project folder (RAGNAROK_PROJECTS).")
    def list_project(path: str = "") -> str:
        p = _in_projects(path or str(_project_roots()[0] if _project_roots() else "."))
        out = [str(q.relative_to(p)) + ("/" if q.is_dir() else "") for q in sorted(p.iterdir()) if q.name not in SKIP_DIRS]
        return "\n".join(out[:200]) or "(empty)"

    @reg.register(description="Read a text/code file from an approved project folder (first 200 lines unless start is given).")
    def read_project_file(path: str, start: int = 1) -> str:
        p = _in_projects(path)
        if p.stat().st_size > 2_000_000:
            raise ValueError("file too large")
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        chunk = lines[max(start, 1) - 1: max(start, 1) - 1 + 200]
        return "\n".join(f"{i}: {l}" for i, l in enumerate(chunk, max(start, 1))) or "(empty)"

    @reg.register(description="Search for text in files of an approved project folder.")
    def search_project(folder: str, text: str) -> str:
        root = _in_projects(folder); hits = []
        for q in root.rglob("*"):
            if any(part in SKIP_DIRS for part in q.parts) or not q.is_file() or q.stat().st_size > 1_000_000:
                continue
            try:
                for i, l in enumerate(q.read_text(encoding="utf-8").splitlines(), 1):
                    if text.lower() in l.lower():
                        hits.append(f"{q.relative_to(root)}:{i}: {l.strip()[:120]}")
            except (UnicodeDecodeError, OSError):
                continue
            if len(hits) >= 40:
                break
        return "\n".join(hits) or "no matches"

    @reg.register(description="Show git status and the last 5 commits of an approved project folder.")
    def git_status(folder: str) -> str:
        p = _in_projects(folder)
        run = lambda *a: subprocess.run(["git", "-C", str(p), *a], capture_output=True, text=True, timeout=15).stdout.strip()
        return f"{run('status', '--short', '--branch')}\n\n{run('log', '--oneline', '-5')}"


# ---------------------------------------------------------------- calendar interchange (.ics)
def register_ics(reg: SkillRegistry, data_dir: Path) -> None:
    cal = Path(data_dir) / "calendar.json"

    def events() -> list[dict]:
        return json.loads(cal.read_text()) if cal.exists() else []

    def fmt(iso: str) -> str:
        return dt.datetime.fromisoformat(iso).strftime("%Y%m%dT%H%M%S")

    @reg.register(description="Export the local calendar to an .ics file in the notes folder so Google/Outlook/Apple can import it.")
    def export_calendar(name: str = "ragnarok.ics") -> str:
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Ragnarok//EN"]
        for e in events():
            lines += ["BEGIN:VEVENT", f"UID:{uuid.uuid4()}@ragnarok", f"DTSTART:{fmt(e['start'])}",
                      *([f"DTEND:{fmt(e['end'])}"] if e.get("end") else []),
                      "SUMMARY:" + re.sub(r"[\r\n]+", " ", e["title"]), "END:VEVENT"]
        lines.append("END:VCALENDAR")
        out = _safe(Path(data_dir) / "notes", name); out.write_text("\r\n".join(lines), encoding="utf-8")
        return f"exported {len(events())} events to notes/{name}"

    @reg.register(description="Import events from an .ics file saved in the notes folder (floating/UTC times).")
    def import_calendar(name: str) -> str:
        text = _safe(Path(data_dir) / "notes", name).read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"\r?\n[ \t]", "", text)                      # unfold lines
        added, cur = 0, events()
        for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
            g = lambda k: (re.search(rf"^{k}[^:\n]*:(.+)$", block, re.M) or [None, ""])[1].strip()
            s, title = g("DTSTART"), g("SUMMARY")
            m = re.match(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2}))?", s)
            if not (m and title):
                continue
            y, mo, d, hh, mi, _ = m.groups()
            start = f"{y}-{mo}-{d}T{hh or '00'}:{mi or '00'}"
            if not any(e["title"] == title and e["start"] == start for e in cur):
                cur.append({"title": title, "start": start, "end": ""}); added += 1
        cal.write_text(json.dumps(sorted(cur, key=lambda e: e["start"]), indent=2))
        return f"imported {added} new events"
