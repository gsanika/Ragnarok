import sys

from . import builtin_skills  # noqa: F401  (registers starter skills)
from .agent import Agent
from .config import Config
from .llm import OllamaClient
from .skills import registry


def _ask_approval(skill: str, args: dict) -> bool:
    return input(f"Allow '{skill}' with {args}? [y/N] ").strip().lower() == "y"


def main() -> None:
    cfg = Config()
    agent = Agent(OllamaClient(cfg), registry, cfg, approve=_ask_approval)
    print(f"Ragnarok ready (model: {cfg.chat_model}). Ctrl-D to quit.")
    try:
        while True:
            text = input("you> ").strip()
            if text:
                print("ragnarok>", agent.run(text))
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()
