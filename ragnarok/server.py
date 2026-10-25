"""Local-only web UI + JSON API for memory control, approvals, chat and the execution log
(NFR4.1/4.2/4.3). Binds to 127.0.0.1, requires a per-run token, checks the Host header."""
from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .runtime import Runtime

PAGE = (Path(__file__).parent / "ui.html").read_text(encoding="utf-8") if (Path(__file__).parent / "ui.html").exists() else "<h1>ui.html missing</h1>"


def make_handler(rt: Runtime, token: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _send(self, code: int, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else json.dumps(body, default=str).encode()
            self.send_response(code); self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(data)

        def _guard(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                self._send(403, {"error": "bad host"}); return False
            if self.path == "/" or self.path.startswith("/?"):
                return True
            if self.headers.get("X-Ragnarok-Token") != token:
                self._send(401, {"error": "bad token"}); return False
            return True

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):
            if not self._guard(): return
            if self.path.startswith("/?") or self.path == "/":
                return self._send(200, PAGE.replace("__TOKEN__", token), "text/html; charset=utf-8")
            if self.path == "/api/memory":
                return self._send(200, rt.memory.export())
            if self.path == "/api/approvals":
                return self._send(200, rt.approvals.pending())
            if self.path == "/api/log":
                return self._send(200, rt.agent.log.read()[-100:])
            if self.path == "/api/brief":
                return self._send(200, {"text": rt.brief()})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._guard(): return
            b = self._body()
            if self.path == "/api/memory":
                return self._send(200, {"id": rt.memory.remember(str(b["text"]), importance=0.7, source="user")})
            if self.path == "/api/chat":
                return self._send(200, {"reply": rt.chat(str(b["text"]))})
            if self.path == "/api/wipe":
                if b.get("confirm") != "WIPE":
                    return self._send(400, {"error": "send confirm=WIPE"})
                rt.memory.wipe(); return self._send(200, {"ok": True})
            if self.path.startswith("/api/approvals/"):
                _, _, _, aid, action = self.path.split("/")
                ok = rt.approvals.grant(aid) if action == "grant" else rt.approvals.reject(aid)
                return self._send(200, {"ok": ok})
            self._send(404, {"error": "not found"})

        def do_PUT(self):
            if not self._guard(): return
            if self.path.startswith("/api/memory/"):
                rt.memory.edit(int(self.path.rsplit("/", 1)[1]), str(self._body()["text"]))
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

        def do_DELETE(self):
            if not self._guard(): return
            if self.path.startswith("/api/memory/"):
                return self._send(200, {"ok": rt.memory.delete(int(self.path.rsplit("/", 1)[1]))})
            self._send(404, {"error": "not found"})
    return H


def serve(rt: Runtime, port: int = 8765) -> None:
    token = secrets.token_urlsafe(16)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(rt, token))
    print(f"Ragnarok UI: http://127.0.0.1:{port}/?t={token}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
