"""Skill registry (FR2.1): register reusable skills; expose them as Ollama tools (FR2.2)."""
from __future__ import annotations

import inspect
import json
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


def _coerce(value, typ):
    if typ is bool and isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    if typ in (int, float, str) and not isinstance(value, typ):
        return typ(value)
    return value


def normalize_args(skill: "Skill", args) -> dict:
    """Local models often send arguments as a JSON string or with wrong scalar types."""
    if isinstance(args, str):
        args = json.loads(args) if args.strip() else {}
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    params = inspect.signature(skill.func).parameters
    out = {}
    for k, v in args.items():
        if k not in params:
            continue                      # ignore hallucinated parameters
        out[k] = _coerce(v, params[k].annotation)
    missing = [n for n, p in params.items() if p.default is inspect.Parameter.empty and n not in out]
    if missing:
        raise ValueError(f"missing required argument(s): {', '.join(missing)}")
    return out


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
