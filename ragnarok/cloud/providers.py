"""Google (Calendar, Gmail) and Microsoft Graph (Outlook calendar + mail) as skills.
Skills are registered only for providers the user has connected."""
from __future__ import annotations

import base64
import datetime as dt
import urllib.parse
from email.message import EmailMessage

import httpx

from ..skills import SkillRegistry
from .oauth import AuthError, TokenStore

G_CAL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
G_MAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
GRAPH = "https://graph.microsoft.com/v1.0/me"


def _utc(iso: str) -> dt.datetime:
    d = dt.datetime.fromisoformat(iso)
    return d.astimezone(dt.timezone.utc) if d.tzinfo else d.astimezone().astimezone(dt.timezone.utc)


def _end(start: str, end: str) -> dt.datetime:
    return _utc(end) if end else _utc(start) + dt.timedelta(hours=1)


class Api:
    def __init__(self, tokens: TokenStore, provider: str, http: httpx.Client | None = None):
        self.tokens, self.provider = tokens, provider
        self.http = http or tokens.http

    def call(self, method: str, url: str, **kw):
        h = {"Authorization": f"Bearer {self.tokens.access_token(self.provider)}", **kw.pop("headers", {})}
        r = self.http.request(method, url, headers=h, timeout=30, **kw)
        if r.status_code == 401:
            raise AuthError(f"{self.provider} rejected the saved sign-in. Run `ragnarok connect {self.provider}` again.")
        if r.status_code >= 400:
            raise RuntimeError(f"{self.provider} API error {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}


def _mime(to: str, subject: str, body: str, sender: str = "") -> EmailMessage:
    m = EmailMessage()                       # EmailMessage rejects header injection (newlines) with ValueError
    m["To"], m["Subject"] = to, subject
    m.set_content(body)
    return m


# -------------------------------------------------------------------- Google
def google_unread(api: Api, limit: int = 10) -> list[tuple[str, str, str]]:
    lst = api.call("GET", f"{G_MAIL}/messages", params={"q": "is:unread in:inbox", "maxResults": limit}).get("messages", [])
    out = []
    for m in lst:
        j = api.call("GET", f"{G_MAIL}/messages/{m['id']}", params={"format": "metadata", "metadataHeaders": ["Subject", "From"]})
        h = {x["name"]: x["value"] for x in j.get("payload", {}).get("headers", [])}
        out.append((m["id"], h.get("Subject", "(no subject)"), h.get("From", "")))
    return out


def register_google(reg: SkillRegistry, api: Api) -> None:
    @reg.register(description="List Google Calendar events for a date (YYYY-MM-DD).")
    def gcal_list_events(date: str) -> str:
        day = dt.datetime.fromisoformat(date).astimezone()
        j = api.call("GET", G_CAL, params={"timeMin": day.isoformat(), "timeMax": (day + dt.timedelta(days=1)).isoformat(),
                                           "singleEvents": "true", "orderBy": "startTime"})
        ev = [f"{e['start'].get('dateTime', e['start'].get('date'))}  {e.get('summary', '(untitled)')}" for e in j.get("items", [])]
        return "\n".join(ev) or "no events"

    @reg.register(description="Add an event to Google Calendar. start/end are ISO like 2026-10-26T15:00 (end defaults to +1h).")
    def gcal_add_event(title: str, start: str, end: str = "") -> str:
        s, e = _utc(start), _end(start, end)
        j = api.call("POST", G_CAL, json={"summary": title, "start": {"dateTime": s.isoformat(), "timeZone": "UTC"},
                                          "end": {"dateTime": e.isoformat(), "timeZone": "UTC"}})
        return f"added to Google Calendar: {j.get('htmlLink', j.get('id', 'ok'))}"

    @reg.register(description="Check Gmail for unread emails (sender and subject only).")
    def gmail_check() -> str:
        return "\n".join(f"{s} — {f}" for _, s, f in google_unread(api)) or "no unread email"

    @reg.register(description="Send an email through Gmail. Requires the user's approval.", risky=True)
    def gmail_send(to: str, subject: str, body: str) -> str:
        raw = base64.urlsafe_b64encode(_mime(to, subject, body).as_bytes()).decode()
        api.call("POST", f"{G_MAIL}/messages/send", json={"raw": raw})
        return f"sent to {to} via Gmail"


# -------------------------------------------------------------------- Microsoft
def microsoft_unread(api: Api, limit: int = 10) -> list[tuple[str, str, str]]:
    # Graph wants literal $ and %20 for spaces, so build the query string ourselves
    q = urllib.parse.quote("isRead eq false", safe="")
    j = api.call("GET", f"{GRAPH}/mailFolders/inbox/messages?$filter={q}&$top={int(limit)}&$select=id,subject,from")
    return [(m["id"], m.get("subject") or "(no subject)", m.get("from", {}).get("emailAddress", {}).get("address", ""))
            for m in j.get("value", [])]


def register_microsoft(reg: SkillRegistry, api: Api) -> None:
    @reg.register(description="List Outlook calendar events for a date (YYYY-MM-DD).")
    def outlook_list_events(date: str) -> str:
        day = dt.datetime.fromisoformat(date).astimezone()
        j = api.call("GET", f"{GRAPH}/calendarView", headers={"Prefer": 'outlook.timezone="UTC"'},
                     params={"startDateTime": day.astimezone(dt.timezone.utc).isoformat(),
                             "endDateTime": (day + dt.timedelta(days=1)).astimezone(dt.timezone.utc).isoformat(),
                             "$orderby": "start/dateTime", "$top": 25})
        return "\n".join(f"{e['start']['dateTime']} UTC  {e.get('subject', '(untitled)')}" for e in j.get("value", [])) or "no events"

    @reg.register(description="Add an event to the Outlook calendar. start/end are ISO like 2026-10-26T15:00 (end defaults to +1h).")
    def outlook_add_event(title: str, start: str, end: str = "") -> str:
        s, e = _utc(start), _end(start, end)
        api.call("POST", f"{GRAPH}/events", json={"subject": title,
                 "start": {"dateTime": s.replace(tzinfo=None).isoformat(), "timeZone": "UTC"},
                 "end": {"dateTime": e.replace(tzinfo=None).isoformat(), "timeZone": "UTC"}})
        return "added to Outlook calendar"

    @reg.register(description="Check Outlook / Microsoft 365 mail for unread emails (sender and subject only).")
    def outlook_check_mail() -> str:
        return "\n".join(f"{s} — {f}" for _, s, f in microsoft_unread(api)) or "no unread email"

    @reg.register(description="Send an email through Outlook / Microsoft 365. Requires the user's approval.", risky=True)
    def outlook_send_mail(to: str, subject: str, body: str) -> str:
        _mime(to, subject, body)                      # validates headers (no injection)
        api.call("POST", f"{GRAPH}/sendMail", json={"message": {"subject": subject, "body": {"contentType": "Text", "content": body},
                 "toRecipients": [{"emailAddress": {"address": to}}]}, "saveToSentItems": True})
        return f"sent to {to} via Outlook"


def register_cloud(reg: SkillRegistry, tokens: TokenStore, http: httpx.Client | None = None) -> dict:
    """Registers skills only for connected providers; returns {provider: unread_fetcher} for the email trigger."""
    fetchers = {}
    if tokens.has("google"):
        api = Api(tokens, "google", http); register_google(reg, api); fetchers["google"] = lambda: google_unread(api)
    if tokens.has("microsoft"):
        api = Api(tokens, "microsoft", http); register_microsoft(reg, api); fetchers["microsoft"] = lambda: microsoft_unread(api)
    return fetchers
