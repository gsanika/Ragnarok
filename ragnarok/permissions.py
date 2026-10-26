"""Role-based access control + just-in-time approvals (NFR4.3)."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROLES = {
    "viewer": {"allow": ["get_time", "calculate", "recall", "search_history", "list_workspace",
                         "read_note", "list_events", "list_notes", "list_tasks", "list_project",
                         "read_project_file", "search_project", "git_status",
                         "gcal_list_events", "gmail_check", "outlook_list_events", "outlook_check_mail"], "deny": []},
    "assistant": {"allow": ["*"], "deny": ["run_python"]},
    "admin": {"allow": ["*"], "deny": []},
}


@dataclass
class Policy:
    role: str = "assistant"
    roles: dict | None = None

    def __post_init__(self):
        self.roles = self.roles or DEFAULT_ROLES
        if self.role not in self.roles:
            raise ValueError(f"unknown role '{self.role}'")

    @classmethod
    def load(cls, data_dir: Path, role: str = "assistant") -> "Policy":
        p = Path(data_dir) / "policy.json"
        roles = json.loads(p.read_text()) if p.exists() else None
        return cls(role, roles)

    def allows(self, skill: str) -> bool:
        r = self.roles[self.role]
        if skill in r.get("deny", []):
            return False
        allow = r.get("allow", [])
        return "*" in allow or skill in allow


def _key(skill: str, args: dict) -> str:
    return hashlib.sha256(json.dumps([skill, args], sort_keys=True, default=str).encode()).hexdigest()[:12]


def _sync(fn):
    def wrapper(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)
    wrapper.__name__ = fn.__name__; wrapper.__doc__ = fn.__doc__
    return wrapper


class ApprovalStore:
    """Headless approvals: risky calls are parked as 'pending'; once the user grants one
    (CLI or web UI) the next identical call is allowed exactly once."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "approvals.json"
        self._lock = threading.RLock()

    def _load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def _save(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d, indent=2))

    @_sync
    def check(self, skill: str, args: dict) -> bool:
        """Callback for Agent.approve: True only if a granted request exists (and consumes it)."""
        d, k = self._load(), _key(skill, args)
        if d.get(k, {}).get("status") == "granted":
            del d[k]; self._save(d)
            return True
        d.setdefault(k, {"id": k, "skill": skill, "args": args, "status": "pending", "ts": time.time()})
        self._save(d)
        return False

    def pending(self) -> list[dict]:
        return [v for v in self._load().values() if v["status"] == "pending"]

    @_sync
    def grant(self, approval_id: str) -> bool:
        d = self._load()
        if approval_id in d:
            d[approval_id]["status"] = "granted"; self._save(d); return True
        return False

    @_sync
    def reject(self, approval_id: str) -> bool:
        d = self._load()
        if approval_id in d:
            del d[approval_id]; self._save(d); return True
        return False

    @_sync
    def get(self, approval_id: str) -> dict | None:
        e = self._load().get(approval_id)
        return e if e and e["status"] == "pending" else None

    @_sync
    def take(self, approval_id: str) -> dict | None:
        """Remove and return a pending request, so an approved action can only run once."""
        d = self._load(); e = d.get(approval_id)
        if not e or e["status"] != "pending":
            return None
        del d[approval_id]; self._save(d)
        return e
