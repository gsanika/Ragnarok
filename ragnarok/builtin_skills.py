"""A few safe starter skills. Importing this module registers them."""
import ast
import operator
from datetime import datetime
from pathlib import Path

from .skills import registry

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.USub: operator.neg,
    ast.Mod: operator.mod,
}


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("unsupported expression")


@registry.register(description="Get the current local date and time.")
def get_time() -> str:
    return datetime.now().strftime("%A, %d %B %Y, %H:%M")


@registry.register(description="Evaluate an arithmetic expression like '12*(3+4)'.")
def calculate(expression: str) -> str:
    return str(_eval(ast.parse(expression, mode="eval").body))


@registry.register(description="List files in a folder inside the agent workspace (data/).")
def list_workspace(subdir: str = ".") -> str:
    base = Path("data").resolve()
    target = (base / subdir).resolve()
    if base not in target.parents and target != base:
        raise PermissionError("outside workspace")
    return "\n".join(sorted(p.name for p in target.iterdir())) or "(empty)"
