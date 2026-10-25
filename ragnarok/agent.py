"""Agent loop: user message -> (LLM picks skills) -> run -> respond. Fully logged."""
from __future__ import annotations

from typing import Callable

from .config import Config
from .llm import LLM
from .log import ExecutionLog
from .skills import SkillRegistry

SYSTEM_PROMPT = (
    "You are Ragnarok, a private personal assistant running entirely on the user's "
    "machine. Use the provided tools when they help. Be concise."
)


def always_deny(skill_name: str, args: dict) -> bool:
    return False


class Agent:
    def __init__(
        self,
        llm: LLM,
        registry: SkillRegistry,
        config: Config | None = None,
        approve: Callable[[str, dict], bool] = always_deny,
    ):
        self.llm = llm
        self.registry = registry
        self.config = config or Config()
        self.approve = approve  # NFR4.3: gate for risky skills
        self.log = ExecutionLog(self.config.log_path)
        self.history: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    def _run_tool(self, name: str, args: dict) -> str:
        skill = self.registry.get(name)
        if skill is None:
            return f"error: unknown skill '{name}'"
        if skill.risky and not self.approve(name, args):
            self.log.record("denied", skill=name, args=args)
            return "error: user did not approve this action"
        try:
            result = str(skill.func(**args))
            self.log.record("tool_result", skill=name, args=args, result=result)
            return result
        except Exception as e:  # NFR3.1: a failing tool must not crash the run
            self.log.record("tool_error", skill=name, args=args, error=repr(e))
            return f"error: {e!r}"

    def run(self, user_text: str) -> str:
        self.history.append({"role": "user", "content": user_text})
        self.log.record("user", text=user_text)
        tools = self.registry.tools()

        for _ in range(self.config.max_steps):
            msg = self.llm.chat(self.history, tools=tools)
            self.history.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                self.log.record("assistant", text=msg.get("content", ""))
                return msg.get("content", "")
            for call in calls:
                fn = call["function"]
                self.log.record("tool_call", skill=fn["name"], args=fn.get("arguments", {}))
                out = self._run_tool(fn["name"], fn.get("arguments", {}) or {})
                self.history.append({"role": "tool", "content": out, "tool_name": fn["name"]})

        self.log.record("max_steps_reached")
        return "I couldn't finish within the step limit."
