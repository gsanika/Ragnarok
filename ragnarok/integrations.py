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


def imap_fetch_unseen(limit: int = 10) -> list[tuple[str, str, str]]:
    host, user, pw = os.environ.get("IMAP_HOST"), os.environ.get("IMAP_USER"), os.environ.get("IMAP_PASSWORD")
    if not (host and user and pw):
        return []
    out = []
    port = int(os.environ.get("IMAP_PORT", "993"))
    with imaplib.IMAP4_SSL(host, port) as m:
        m.login(user, pw); m.select("INBOX", readonly=True)
        _, data = m.search(None, "UNSEEN")
        for uid in data[0].split()[-limit:]:
            _, msg = m.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            h = email.message_from_bytes(msg[0][1])
            out.append((uid.decode(), str(make_header(decode_header(h.get("Subject", "")))), h.get("From", "")))
    return out


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

    @reg.register(description="Add a calendar event. start and end are ISO like 2026-10-26T15:00.")
    def add_event(title: str, start: str, end: str = "") -> str:
        dt.datetime.fromisoformat(start)
        ev = _events() + [{"title": title, "start": start, "end": end}]
        cal.write_text(json.dumps(sorted(ev, key=lambda e: e["start"]), indent=2)); return "event added"

    @reg.register(description="List calendar events on a date (YYYY-MM-DD), or all upcoming if empty.")
    def list_events(date: str = "") -> str:
        evs = [e for e in _events() if (e["start"].startswith(date) if date else e["start"] >= dt.datetime.now().isoformat())]
        return "\n".join(f"{e['start']}  {e['title']}" for e in evs) or "no events"

    @reg.register(description="Check the inbox for unread emails (subjects and senders only).")
    def check_email() -> str:
        msgs = imap_fetch_unseen()
        return "\n".join(f"{s} — {f}" for _, s, f in msgs) or "no unread email (or IMAP not configured)"

    @reg.register(description="Send an email. Requires the user's approval.", risky=True)
    def send_email(to: str, subject: str, body: str) -> str:
        host, user, pw = os.environ.get("SMTP_HOST"), os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
        if not (host and user and pw):
            return "error: SMTP not configured (set SMTP_HOST, SMTP_USER, SMTP_PASSWORD)"
        msg = EmailMessage(); msg["From"], msg["To"], msg["Subject"] = user, to, subject; msg.set_content(body)
        port = int(os.environ.get("SMTP_PORT", "465"))
        if port == 465:                       # implicit TLS
            with smtplib.SMTP_SSL(host, port) as s:
                s.login(user, pw); s.send_message(msg)
        else:                                 # 587 etc.: STARTTLS
            with smtplib.SMTP(host, port) as s:
                s.starttls(); s.login(user, pw); s.send_message(msg)
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
        if due:
            dt.date.fromisoformat(due)
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
