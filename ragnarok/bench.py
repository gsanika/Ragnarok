"""Benchmarks for the non-functional requirements. `ragnarok bench` prints a pass/fail table.

NFR2.1  retrieval + intent classification < 800 ms
NFR2.2  always-on background CPU share < 30% of one baseline core budget
NFR3.2  state recovery < 5 s
Use --ollama to use the real embedding model instead of the offline hash embedder."""
from __future__ import annotations

import json
import os
import statistics
import tempfile
import time
from pathlib import Path

from .agent import Agent
from .config import Config
from .intent import SkillRouter
from .memory import HashEmbedder, MemoryManager, MemoryStore
from .memory.embedding import Embedder
from .planner import Orchestrator, Plan, Task
from .skills import registry
from .triggers import IntervalTrigger, TriggerRunner


def _p95(xs: list[float]) -> float:
    xs = sorted(xs); return xs[min(len(xs) - 1, int(0.95 * len(xs)))]


def run(embedder: Embedder | None = None, n_facts: int = 2000, queries: int = 50, out: Path | None = None) -> dict:
    embedder = embedder or HashEmbedder()
    results: dict = {"embedder": type(embedder).__name__, "n_facts": n_facts}
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        mem = MemoryManager(MemoryStore(d), embedder)
        topics = ["project", "food", "travel", "exam", "music", "health routine", "friend", "deadline", "code", "book"]
        texts = [f"fact {i}: user {topics[i % 10]} detail number {i} about {topics[(i * 7) % 10]}" for i in range(n_facts)]
        for i in range(0, n_facts, 64):                       # bulk-load: batch embed, skip per-fact dedupe
            chunk = texts[i:i + 64]
            for t, v in zip(chunk, embedder.embed(chunk)):
                mem.store.add_fact(t, v, 0.5)
        mem._invalidate()
        # retrieval latency (warm cache, as in a long-running daemon)
        mem.recall("warmup")
        lat = []
        for q in range(queries):
            t = time.perf_counter(); mem.recall(f"what about the {topics[q % 10]} deadline"); lat.append((time.perf_counter() - t) * 1000)
        # intent classification
        router = SkillRouter(registry, embedder)
        ilat = []
        for q in range(queries):
            t = time.perf_counter(); router.top(f"please check my {topics[q % 10]} and schedule it", k=6); ilat.append((time.perf_counter() - t) * 1000)
        results["retrieval_ms_p95"] = round(_p95(lat), 2)
        results["intent_ms_p95"] = round(_p95(ilat), 2)
        results["nfr2_1_pass"] = results["retrieval_ms_p95"] + results["intent_ms_p95"] < 800

        # idle background footprint with 3 triggers polling every 0.05 s for 3 s
        runner = TriggerRunner([IntervalTrigger(f"t{i}", 3600, "x") for i in range(3)], lambda *_: None, poll_seconds=0.05)
        c0, w0 = time.process_time(), time.perf_counter()
        runner.start(); time.sleep(3.0); runner.stop()
        cpu = (time.process_time() - c0) / (time.perf_counter() - w0) * 100
        results["background_cpu_pct"] = round(cpu, 2)
        results["nfr2_2_pass_background"] = cpu < 30
        results["cpu_count"] = os.cpu_count()

        # state recovery with a big history and 30 plan checkpoints
        cfg = Config(data_dir=d)
        class Nop:
            def chat(self, m, tools=None): return {"role": "assistant", "content": "ok"}
        a = Agent(Nop(), registry, cfg)
        a.history += [{"role": "user", "content": "x" * 400} for _ in range(30)]
        a.save_state()
        orch = Orchestrator(Nop(), lambda p, c: "r", d, notify=lambda m: None)
        for i in range(30):
            orch.checkpoint(Plan(f"goal {i}", [Task(f"t{j}", "step") for j in range(8)]))
        t = time.perf_counter()
        b = Agent(Nop(), registry, cfg, restore=True)
        pending = orch.unfinished()
        rec = time.perf_counter() - t
        results["recovery_s"] = round(rec, 3); results["recovered_plans"] = len(pending)
        results["nfr3_2_pass"] = rec < 5 and len(b.history) > 30
    if out:
        Path(out).write_text(json.dumps(results, indent=2))
    return results


def report(r: dict) -> str:
    ok = lambda b: "PASS" if b else "FAIL"
    return "\n".join([
        f"embedder: {r['embedder']}  facts: {r['n_facts']}",
        f"NFR2.1 retrieval p95 {r['retrieval_ms_p95']} ms + intent p95 {r['intent_ms_p95']} ms  (< 800)  {ok(r['nfr2_1_pass'])}",
        f"NFR2.2 background CPU {r['background_cpu_pct']}%  (< 30)  {ok(r['nfr2_2_pass_background'])}   [model inference share must be measured on your hardware]",
        f"NFR3.2 recovery {r['recovery_s']} s  (< 5)  {ok(r['nfr3_2_pass'])}",
    ])
