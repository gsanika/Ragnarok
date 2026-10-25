"""Multi-step planning, reflection/retry and crash-safe orchestration (FR3.1, FR3.3, NFR3)."""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .agent import atomic_write_json

PLAN_PROMPT = (
    "Break the user's goal into the smallest sensible ordered sub-tasks. Reply ONLY with JSON: "
    '{{"tasks":[{{"id":"t1","description":"...","deps":[]}}]}}. Use "deps" for ids that must finish '
    "first. Max {max_tasks} tasks. Available skills: {skills}.\n\nGoal: {goal}"
)
REFLECT_PROMPT = (
    "Task: {task}\nResult: {result}\n\nDid the result accomplish the task? Reply ONLY with JSON "
    '{{"ok": true|false, "reason": "short"}}.'
)


@dataclass
class Task:
    id: str
    description: str
    deps: list[str] = field(default_factory=list)
    status: str = "pending"   # pending | running | done | failed | skipped
    result: str = ""
    error: str = ""
    attempts: int = 0


@dataclass
class Plan:
    goal: str
    tasks: list[Task]
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        return cls(d["goal"], [Task(**t) for t in d["tasks"]], d["id"], d.get("created", 0))

    def ready(self) -> list[Task]:
        done = {t.id for t in self.tasks if t.status == "done"}
        return [t for t in self.tasks if t.status == "pending" and all(d in done for d in t.deps)]

    def finished(self) -> bool:
        return all(t.status in ("done", "failed", "skipped") for t in self.tasks)


def _has_cycle(tasks: list[Task]) -> bool:
    deps, state = {t.id: t.deps for t in tasks}, {}
    def visit(n):
        if state.get(n) == 1: return True
        if state.get(n) == 2: return False
        state[n] = 1
        if any(visit(d) for d in deps[n]): return True
        state[n] = 2
        return False
    return any(visit(n) for n in deps)


def parse_plan(text: str, goal: str, max_tasks: int = 8) -> Plan:
    """Parse + validate an LLM plan; fall back to a single-task plan on any problem."""
    fallback = Plan(goal, [Task("t1", goal)])
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return fallback
    try:
        raw = json.loads(m.group(0))["tasks"][:max_tasks]
        tasks = [Task(str(t["id"]), str(t["description"]), [str(d) for d in t.get("deps", [])]) for t in raw]
    except (json.JSONDecodeError, KeyError, TypeError):
        return fallback
    ids = [t.id for t in tasks]
    if not tasks or len(set(ids)) != len(ids) or any(d not in ids for t in tasks for d in t.deps) or _has_cycle(tasks):
        return fallback
    return Plan(goal, tasks)


def make_plan(llm, goal: str, skill_names: list[str], max_tasks: int = 8) -> Plan:
    try:
        msg = llm.chat([{"role": "user", "content": PLAN_PROMPT.format(
            goal=goal, skills=", ".join(skill_names), max_tasks=max_tasks)}])
        return parse_plan(msg.get("content", ""), goal, max_tasks)
    except Exception:
        return Plan(goal, [Task("t1", goal)])


def reflect(llm, task: str, result: str) -> tuple[bool, str]:
    if result.startswith("error"):
        return False, result[:200]
    try:
        content = llm.chat([{"role": "user", "content": REFLECT_PROMPT.format(task=task, result=result[:1500])}]).get("content", "")
        m = re.search(r"\{.*\}", content, re.S)
        d = json.loads(m.group(0))
        return bool(d["ok"]), str(d.get("reason", ""))
    except Exception:
        return True, "reflection unavailable; accepted"


class Orchestrator:
    def __init__(self, llm, executor: Callable[[str, dict[str, str]], str], data_dir: Path,
                 notify: Callable[[str], None] = print, max_attempts: int = 3, log=None):
        self.llm, self.executor, self.notify = llm, executor, notify
        self.max_attempts, self.log = max_attempts, log
        self.dir = Path(data_dir) / "plans"

    def _path(self, plan: Plan) -> Path:
        return self.dir / f"{plan.id}.json"

    def checkpoint(self, plan: Plan) -> None:
        atomic_write_json(self._path(plan), plan.to_dict())

    def load(self, plan_id: str) -> Plan:
        plan = Plan.from_dict(json.loads((self.dir / f"{plan_id}.json").read_text(encoding="utf-8")))
        for t in plan.tasks:            # crash recovery: interrupted tasks run again
            if t.status == "running":
                t.status = "pending"
        return plan

    def unfinished(self) -> list[str]:
        if not self.dir.exists():
            return []
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                if not Plan.from_dict(json.loads(p.read_text())).finished():
                    out.append(p.stem)
            except Exception:
                continue
        return out

    def _event(self, event: str, **kw):
        if self.log:
            self.log.record(event, **kw)

    def run(self, plan: Plan) -> Plan:
        self.checkpoint(plan)
        while not plan.finished():
            ready = plan.ready()
            if not ready:  # everything left is blocked by a failure
                for t in plan.tasks:
                    if t.status == "pending":
                        t.status = "skipped"; t.error = "blocked by failed dependency"
                        self.notify(f"skipped '{t.description}' (dependency failed)")
                self.checkpoint(plan)
                break
            for t in ready:
                self._run_task(plan, t)
        self._event("plan_finished", plan=plan.id, statuses={t.id: t.status for t in plan.tasks})
        return plan

    def _run_task(self, plan: Plan, task: Task) -> None:
        context = {d.id: d.result for d in plan.tasks if d.id in task.deps}
        feedback = ""
        task.status = "running"; self.checkpoint(plan)
        while task.attempts < self.max_attempts:
            task.attempts += 1
            prompt = task.description + (f"\n\nPrevious attempt problem: {feedback}" if feedback else "")
            try:
                result = self.executor(prompt, context)
            except Exception as e:
                result = f"error: {e!r}"
            ok, reason = reflect(self.llm, task.description, result)
            self._event("task_attempt", plan=plan.id, task=task.id, attempt=task.attempts, ok=ok, reason=reason)
            if ok:
                task.status, task.result, task.error = "done", result, ""
                self.checkpoint(plan)
                return
            feedback = reason
            task.error = reason
        task.status = "failed"
        self.notify(f"task failed after {task.attempts} attempts: '{task.description}' ({task.error})")
        self.checkpoint(plan)

    def summarize(self, plan: Plan) -> str:
        lines = [f"Goal: {plan.goal}"]
        for t in plan.tasks:
            lines.append(f"- [{t.status}] {t.description}" + (f" -> {t.result[:300]}" if t.result else ""))
        body = "\n".join(lines)
        try:
            msg = self.llm.chat([{"role": "user", "content": f"Write a brief report for the user.\n\n{body}"}])
            return msg.get("content") or body
        except Exception:
            return body
