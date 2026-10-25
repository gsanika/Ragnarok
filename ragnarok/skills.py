"""Skill registry (FR2.1): register reusable skills; expose them as Ollama tools (FR2.2)."""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable

_PY_TO_JSON = {str: "string", int: "integer", float: "number", bool: "boolean"}


@dataclass
class Skill:
    name: str
    description: str
    func: Callable[..., Any]
    risky: bool = False  # risky skills need user approval (NFR4.3)

    def schema(self) -> dict:
        props, required = {}, []
        for pname, p in inspect.signature(self.func).parameters.items():
            props[pname] = {"type": _PY_TO_JSON.get(p.annotation, "string")}
            if p.default is inspect.Parameter.empty:
                required.append(pname)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": props, "required": required},
            },
        }


class SkillRegistry:
    def __init__(self):
        self._skills: dict[str, Skill] = {}

    def register(self, name: str | None = None, description: str | None = None, risky: bool = False):
        def deco(func: Callable[..., Any]):
            s = Skill(
                name=name or func.__name__,
                description=description or (inspect.getdoc(func) or "").split("\n")[0],
                func=func,
                risky=risky,
            )
            self._skills[s.name] = s
            return func

        return deco

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def names(self) -> list[str]:
        return list(self._skills)

    def tools(self) -> list[dict]:
        return [s.schema() for s in self._skills.values()]


registry = SkillRegistry()
