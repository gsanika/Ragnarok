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
    with imaplib.IMAP4_SSL(host) as m:
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
        with smtplib.SMTP_SSL(host) as s:
            s.login(user, pw); s.send_message(msg)
        return f"sent to {to}"

    @reg.register(description="Delete a saved note. Requires the user's approval.", risky=True)
    def delete_note(name: str) -> str:
        _safe(notes, name).unlink(); return f"deleted {name}"
