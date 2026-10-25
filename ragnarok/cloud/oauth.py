"""OAuth2 for the user's own cloud accounts. No client secrets are bundled: you register your own
OAuth app (see README) and set GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET or MS_CLIENT_ID.
Tokens are stored encrypted with the same user-held key as memory."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

from ..crypto import Vault

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE = "https://oauth2.googleapis.com/revoke"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar.events",
                 "https://www.googleapis.com/auth/gmail.readonly",
                 "https://www.googleapis.com/auth/gmail.send"]
MS_SCOPES = ["offline_access", "Calendars.ReadWrite", "Mail.Read", "Mail.Send"]
PROVIDERS = ("google", "microsoft")


class AuthError(RuntimeError):
    pass


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class TokenStore:
    def __init__(self, data_dir: Path, vault: Vault | None = None, http: httpx.Client | None = None, clock=time.time):
        self.path = Path(data_dir) / "oauth.enc"
        self.vault = vault or Vault(Path(data_dir))
        self.http = http or httpx.Client(timeout=30)
        self.clock = clock
        self._lock = threading.RLock()

    def _load(self) -> dict:
        return json.loads(self.vault.decrypt(self.path.read_bytes())) if self.path.exists() else {}

    def _save(self, d: dict) -> None:
        self.path.write_bytes(self.vault.encrypt(json.dumps(d)))
        try: os.chmod(self.path, 0o600)
        except OSError: pass

    def has(self, provider: str) -> bool:
        with self._lock:
            return provider in self._load()

    def connected(self) -> list[str]:
        with self._lock:
            return [p for p in PROVIDERS if p in self._load()]

    def put(self, provider: str, tok: dict) -> None:
        with self._lock:
            d = self._load()
            old = d.get(provider, {})
            d[provider] = {"access_token": tok["access_token"],
                           "refresh_token": tok.get("refresh_token") or old.get("refresh_token", ""),
                           "expires_at": self.clock() + int(tok.get("expires_in", 3600)) - 60}
            self._save(d)

    def remove(self, provider: str) -> bool:
        with self._lock:
            d = self._load()
            t = d.pop(provider, None)
            self._save(d)
        if t and provider == "google":                    # best-effort revoke
            try: self.http.post(GOOGLE_REVOKE, data={"token": t.get("refresh_token") or t["access_token"]})
            except httpx.HTTPError: pass
        return t is not None

    def access_token(self, provider: str) -> str:
        with self._lock:
            t = self._load().get(provider)
            if not t:
                raise AuthError(f"{provider} is not connected. Run `ragnarok connect {provider}`.")
            if t["expires_at"] > self.clock():
                return t["access_token"]
            if not t.get("refresh_token"):
                raise AuthError(f"{provider} session expired. Run `ragnarok connect {provider}` again.")
            url, kw = self._refresh_request(provider, t["refresh_token"])
            r = self.http.post(url, **kw)
            if r.status_code >= 400:
                raise AuthError(f"{provider} refresh failed ({r.status_code}). Run `ragnarok connect {provider}` again.")
            self.put(provider, r.json())
            return self._load()[provider]["access_token"]

    @staticmethod
    def _refresh_request(provider: str, refresh_token: str):
        if provider == "google":
            return GOOGLE_TOKEN, {"data": {"client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
                                           "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
                                           "refresh_token": refresh_token, "grant_type": "refresh_token"}}
        tenant = os.environ.get("MS_TENANT", "common")
        return (f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                {"data": {"client_id": os.environ.get("MS_CLIENT_ID", ""), "refresh_token": refresh_token,
                          "grant_type": "refresh_token", "scope": " ".join(MS_SCOPES)}})


# ------------------------------------------------------------------ Google: loopback + PKCE
def google_auth_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    q = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(GOOGLE_SCOPES),
         "code_challenge": challenge, "code_challenge_method": "S256", "access_type": "offline",
         "prompt": "consent", "state": state}
    return GOOGLE_AUTH + "?" + urllib.parse.urlencode(q)


def google_exchange(http, code: str, verifier: str, redirect_uri: str) -> dict:
    r = http.post(GOOGLE_TOKEN, data={"code": code, "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
                                      "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
                                      "redirect_uri": redirect_uri, "grant_type": "authorization_code",
                                      "code_verifier": verifier})
    if r.status_code >= 400:
        raise AuthError(f"Google rejected the sign-in: {r.text[:200]}")
    return r.json()


def connect_google(store: TokenStore, open_browser=webbrowser.open, timeout: float = 180) -> None:
    if not os.environ.get("GOOGLE_CLIENT_ID"):
        raise AuthError("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (your own OAuth 'Desktop app' client). See README.")
    verifier, challenge = pkce_pair(); state = secrets.token_urlsafe(16); got: dict = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state and "code" in q:
                got["code"] = q["code"][0]
            else:
                got["error"] = q.get("error", ["bad state"])[0]
            self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers()
            self.wfile.write(b"Ragnarok is connected. You can close this tab." if "code" in got else b"Sign-in failed.")

    srv = HTTPServer(("127.0.0.1", 0), H); srv.timeout = 1
    redirect = f"http://127.0.0.1:{srv.server_address[1]}"
    url = google_auth_url(os.environ["GOOGLE_CLIENT_ID"], redirect, challenge, state)
    print("Opening your browser to sign in to Google. If nothing opens, visit:\n" + url)
    open_browser(url)
    end = time.time() + timeout
    while not got and time.time() < end:
        srv.handle_request()
    srv.server_close()
    if "code" not in got:
        raise AuthError("Google sign-in was cancelled or timed out: " + got.get("error", "no response"))
    store.put("google", google_exchange(store.http, got["code"], verifier, redirect))


# ------------------------------------------------------------------ Microsoft: device code
def connect_microsoft(store: TokenStore, say=print, sleep=time.sleep, clock=time.time) -> None:
    cid = os.environ.get("MS_CLIENT_ID")
    if not cid:
        raise AuthError("Set MS_CLIENT_ID (your own Entra 'public client' app). See README.")
    base = f"https://login.microsoftonline.com/{os.environ.get('MS_TENANT', 'common')}/oauth2/v2.0"
    r = store.http.post(base + "/devicecode", data={"client_id": cid, "scope": " ".join(MS_SCOPES)})
    if r.status_code >= 400:
        raise AuthError(f"Microsoft sign-in could not start: {r.text[:200]}")
    d = r.json()
    say(d.get("message") or f"Go to {d['verification_uri']} and enter code {d['user_code']}")
    interval, end = int(d.get("interval", 5)), clock() + int(d.get("expires_in", 900))
    while clock() < end:
        sleep(interval)
        t = store.http.post(base + "/token", data={"client_id": cid, "device_code": d["device_code"],
                                                   "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        j = t.json()
        if "access_token" in j:
            store.put("microsoft", j); return
        err = j.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5; continue
        raise AuthError(f"Microsoft sign-in failed: {j.get('error_description', err)}")
    raise AuthError("Microsoft sign-in timed out.")
