"""Agent loop: user message -> recall memory -> (LLM picks skills) -> run -> respond.
Everything is logged (NFR4.1), working state is checkpointed (NFR3.2)."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Callable

from .config import Config
from .llm import LLM
from .log import ExecutionLog
from .skills import SkillRegistry, normalize_args

SYSTEM_PROMPT = (
    "You are Ragnarok, a private personal assistant running entirely on the user's "
    "machine. Use the provided tools when they help. Be concise. "
    "Never say an action was done (sent, saved, deleted, scheduled) unless a tool result in this conversation says it "
    "succeeded. If a tool result says PENDING or error, tell the user exactly that."
)
MAX_HISTORY = 30


def always_deny(skill_name: str, args: dict) -> bool:
    return False


def atomic_write_json(path: Path, data) -> None:
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


class Agent:
    def __init__(self, llm: LLM, registry: SkillRegistry, config: Config | None = None,
                 approve: Callable[[str, dict], bool] = always_deny, memory=None,
                 policy=None, restore: bool = False, router=None,
                 deferred_approvals: bool = False):
        self.llm, self.registry = llm, registry
        self.config = config or Config()
        self.approve = approve
        self.memory = memory            # MemoryManager | None
        self.policy = policy            # permissions.Policy | None
        self.router = router            # intent.SkillRouter | None
        self.deferred_approvals = deferred_approvals  # True: risky calls are queued for the UI instead of asked inline
        self.log = ExecutionLog(self.config.log_path)
        self.state_path = self.config.data_dir / "state.json"
        self.history: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self._blocked: list[tuple[str, str, str]] = []   # risky actions that did NOT happen this turn
        if restore:
            self.load_state()

    # state recovery
    def save_state(self) -> None:
        atomic_write_json(self.state_path, {"history": self.history})

    def load_state(self) -> bool:
        if not self.state_path.exists():
            return False
        try:
            self.history = json.loads(self.state_path.read_text(encoding="utf-8"))["history"]
            return True
        except (json.JSONDecodeError, KeyError):
            return False

    def _trim(self) -> None:
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[:1] + self.history[-(MAX_HISTORY - 1):]
            while len(self.history) > 1 and self.history[1].get("role") == "tool":
                del self.history[1]

    def _flag(self, skill, kind: str, detail: str) -> None:
        if skill.risky:
            self._blocked.append((skill.name, kind, detail))

    def _verdict(self) -> str:
        """Plain status for risky actions that did not happen. Replaces the model's wording,
        because a small model may claim 'email sent' when nothing was sent."""
        if not self._blocked:
            return ""
        lines = []
        for name, kind, detail in self._blocked:
            if kind == "pending":
                lines.append(f"WAITING FOR APPROVAL - NOT done yet: {name} {detail}")
            elif kind == "denied":
                lines.append(f"Not done - you declined: {name} {detail}")
            else:
                lines.append(f"Could not complete {name}: {detail}")
        if any(k == "pending" for _, k, _ in self._blocked):
            lines.append("Open the Approvals tab and click Approve (or run `ragnarok approvals list`).")
        return "\n".join(lines)

    def _run_tool(self, name: str, args: dict) -> str:
        skill = self.registry.get(name)
        if skill is None:
            return f"error: unknown skill '{name}'"
        try:
            args = normalize_args(skill, args)
        except (ValueError, TypeError) as e:
            self.log.record("bad_arguments", skill=name, error=str(e))
            self._flag(skill, "failed", f"bad arguments: {e}")
            return f"error: bad arguments for '{name}': {e}"
        if self.policy is not None and not self.policy.allows(name):
            self.log.record("blocked_by_role", skill=name, role=self.policy.role)
            self._flag(skill, "failed", f"your role '{self.policy.role}' may not use it")
            return f"error: role '{self.policy.role}' may not use '{name}'"
        if skill.precheck is not None:           # don't ask the user to approve something that cannot work
            problem = skill.precheck()
            if problem:
                self.log.record("precheck_failed", skill=name, error=problem)
                self._flag(skill, "failed", problem)
                return problem
        if skill.risky and not self.approve(name, args):
            if self.deferred_approvals:
                self.log.record("approval_pending", skill=name, args=args)
                self._flag(skill, "pending", json.dumps(args, ensure_ascii=False, default=str)[:200])
                return ("PENDING: this action needs the user's approval and has NOT been done yet. It is queued in the Ragnarok UI "
                        "(Pending approvals) and will run automatically once approved. Do not call it again; tell the user it is "
                        "waiting for their approval.")
            self.log.record("denied", skill=name, args=args)
            self._flag(skill, "denied", json.dumps(args, ensure_ascii=False, default=str)[:200])
            return "error: user did not approve this action"
        try:
            result = str(skill.func(**args))
            self.log.record("tool_result", skill=name, args=args, result=result[:2000])
            if result.startswith("error"):
                self._flag(skill, "failed", result)
            return result
        except Exception as e:  # NFR3.1: a failing tool must not crash the run
            self.log.record("tool_error", skill=name, args=args, error=repr(e))
            self._flag(skill, "failed", f"{type(e).__name__}: {e}")
            return f"error: {type(e).__name__}: {e}"

    @staticmethod
    def _now_message() -> dict:
        """Small models don't know today's date; without this 'tomorrow 3pm' becomes a wrong calendar entry."""
        n = dt.datetime.now().astimezone()
        return {"role": "system", "content": (
            f"Current date and time: {n:%A %d %B %Y, %H:%M} ({n.tzname()}, UTC{n:%z}). Use it to turn words like today, "
            "tomorrow or Friday into exact dates (YYYY-MM-DD) and times (24-hour HH:MM) when calling tools.")}

    def _context_messages(self, user_text: str) -> list[dict]:
        ctx = [self._now_message()]
        if not self.memory:
            return ctx
        facts = self.memory.recall(user_text, k=5)
        if not facts:
            return ctx
        self.log.record("memory_recall", query=user_text, facts=[f.text for f in facts])
        bullet = "\n".join(f"- {f.text}" for f in facts)
        return ctx + [{"role": "system", "content": f"Relevant things you know about the user:\n{bullet}"}]

    def run(self, user_text: str) -> str:
        self.history.append({"role": "user", "content": user_text})
        self._blocked = []
        self.log.record("user", text=user_text)
        if self.memory:
            self.memory.log_episode("user", user_text)
        ctx = self._context_messages(user_text)
        tools = [t for t in self.registry.tools()
                 if self.policy is None or self.policy.allows(t["function"]["name"])]
        if self.router is not None:
            keep = set(self.router.top(user_text, k=6))
            tools = [t for t in tools if t["function"]["name"] in keep]
            self.log.record("intent_shortlist", skills=sorted(keep))
        reply = "I couldn't finish within the step limit."

        for _ in range(self.config.max_steps):
            msg = self.llm.chat(self.history[:1] + ctx + self.history[1:], tools=tools)
            self.history.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                reply = msg.get("content", "")
                self.log.record("assistant", text=reply)
                break
            for call in calls:
                fn = call["function"]
                args = fn.get("arguments", {}) or {}
                self.log.record("tool_call", skill=fn["name"], args=args)
                if isinstance(args, str):
                    fn["arguments"] = {}  # keep history valid for the next Ollama request
                out = self._run_tool(fn["name"], args)
                self.history.append({"role": "tool", "content": out, "tool_name": fn["name"]})
            self.save_state()
        else:
            self.log.record("max_steps_reached")

        verdict = self._verdict()
        if verdict:                       # never let the model's wording stand in for what really happened
            reply = verdict
            last = self.history[-1] if self.history else {}
            if last.get("role") == "assistant" and not last.get("tool_calls"):
                last["content"] = reply
            else:
                self.history.append({"role": "assistant", "content": reply})
            self.log.record("verdict", text=reply)

        if self.memory:
            self.memory.log_episode("assistant", reply)
            self.memory.extract_and_store(self.llm, user_text, reply)
        self._trim()
        self.save_state()
        return reply
