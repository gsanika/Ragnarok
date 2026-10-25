"""Wires every component together."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import builtin_skills  # noqa: F401  (registers starter skills)
from .agent import Agent
from .config import Config
from .crypto import Vault
from .integrations import imap_fetch_unseen, register_integrations
from .intent import SkillRouter
from .llm import OllamaClient, OllamaError
from .memory import HashEmbedder, MemoryManager, MemoryStore
from .memory_skills import register_memory_skills
from .permissions import ApprovalStore, Policy
from .planner import Orchestrator, Plan, make_plan
from .proactive import morning_brief
from .skills import registry
from .triggers import DailyTrigger, EmailTrigger, FileWatchTrigger, TriggerRunner

BRIEF, CONSOLIDATE = "__morning_brief__", "__consolidate__"


class Runtime:
    def __init__(self, config: Config | None = None, llm=None, embedder=None, role: str = "assistant",
                 interactive_approval=None):
        self.config = config or Config()
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self.llm = llm or OllamaClient(self.config)
        self.embedder = embedder or (self.llm if hasattr(self.llm, "embed") else HashEmbedder())
        self.vault = Vault(self.config.data_dir)
        self.store = MemoryStore(self.config.data_dir, self.vault)
        self.memory = MemoryManager(self.store, self.embedder)
        register_memory_skills(registry, self.memory)
        register_integrations(registry, self.config.data_dir)
        self.registry = registry
        self.policy = Policy.load(self.config.data_dir, role)
        self.approvals = ApprovalStore(self.config.data_dir)
        self.approve = interactive_approval or self.approvals.check
        self.router = SkillRouter(registry, self.embedder)
        self.agent = Agent(self.llm, registry, self.config, approve=self.approve, memory=self.memory,
                           policy=self.policy, restore=True, router=self.router)
        self.orchestrator = Orchestrator(self.llm, self._execute_task, self.config.data_dir,
                                         notify=self.notify, log=self.agent.log)
        self.runner: TriggerRunner | None = None
        self._lock = threading.RLock()   # one conversation at a time (daemon, UI and CLI share the agent)

    # ---- helpers
    def notify(self, text: str) -> None:
        p = self.config.data_dir / "notifications.jsonl"
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "text": text}, ensure_ascii=False) + "\n")
        print(f"[ragnarok] {text}")

    def _execute_task(self, prompt: str, context: dict[str, str]) -> str:
        sub = Agent(self.llm, self.registry, self.config, approve=self.approve, memory=None,
                    policy=self.policy, router=self.router)
        sub.state_path = self.config.data_dir / "state_subtask.json"
        ctx = "\n".join(f"Result of {k}: {v[:800]}" for k, v in context.items())
        return sub.run((f"Context from earlier steps:\n{ctx}\n\n" if ctx else "") + prompt)

    # ---- public API
    def chat(self, text: str) -> str:
        with self._lock:
            return self.agent.run(text)

    def do(self, goal: str) -> str:
        with self._lock:
            plan = make_plan(self.llm, goal, self.registry.names())
            self.orchestrator.run(plan)
            return self.orchestrator.summarize(plan)

    def brief(self) -> str:
        return morning_brief(self.memory, self.llm)

    def resume_unfinished(self) -> list[str]:
        out = []
        for pid in self.orchestrator.unfinished():
            plan = self.orchestrator.load(pid)
            self.orchestrator.run(plan)
            out.append(self.orchestrator.summarize(plan))
        return out

    # ---- always-on
    def _on_trigger(self, task: str, name: str) -> None:
        try:
            self._handle_trigger(task, name)
        except OllamaError as e:       # model server down: tell the user, keep the sentinel alive
            self.notify(f"[{name}] could not run: {e}")

    def _handle_trigger(self, task: str, name: str) -> None:
        if task == BRIEF:
            self.notify(self.brief())
        elif task == CONSOLIDATE:
            added = self.memory.consolidate(self.llm)
            res = self.memory.forget()
            self.notify(f"Memory maintenance: +{added} consolidated, pruned {res}")
        else:
            self.notify(f"[{name}] {self.chat(task)}")

    def start_daemon(self, poll_seconds: float = 5.0, brief_at: str = "08:00", maintenance_at: str = "03:00") -> TriggerRunner:
        inbox = self.config.data_dir / "inbox"; inbox.mkdir(exist_ok=True)
        triggers = [
            DailyTrigger("morning-brief", brief_at, BRIEF),
            DailyTrigger("memory-maintenance", maintenance_at, CONSOLIDATE),
            FileWatchTrigger("inbox-folder", inbox, "A file changed in my inbox folder: {path}. Take a look and tell me what it is."),
        ]
        if os.environ.get("IMAP_HOST"):
            triggers.append(EmailTrigger("email", imap_fetch_unseen))
        self.runner = TriggerRunner(triggers, self._on_trigger, poll_seconds, log=self.agent.log)
        try:
            for s in self.resume_unfinished():
                self.notify("Resumed unfinished plan after restart:\n" + s)
        except OllamaError as e:
            self.notify(f"Could not resume unfinished plans yet: {e}")
        self.runner.start()
        return self.runner

    def close(self) -> None:
        if self.runner: self.runner.stop()
        self.store.close()
