import json

from ragnarok.planner import Orchestrator, Plan, Task, parse_plan, reflect
from ragnarok.sandbox import run_python


# ---- sandbox
def test_sandbox_runs_code():
    r = run_python("print(6*7)", backend="process")
    assert r.ok and r.stdout.strip() == "42"


def test_sandbox_timeout():
    r = run_python("while True: pass", timeout=1, backend="process")
    assert not r.ok and (r.timed_out or r.returncode != 0)


def test_sandbox_blocks_memory_bomb():
    r = run_python("x = bytearray(2*1024**3)\nprint('alloc')", mem_mb=128, backend="process")
    assert not r.ok


def test_sandbox_env_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "hunter2")
    r = run_python("import os; print(os.environ.get('SECRET_TOKEN'))", backend="process")
    assert r.stdout.strip() == "None"


# ---- planning
def test_parse_plan_valid_and_fallbacks():
    good = '{"tasks":[{"id":"a","description":"x","deps":[]},{"id":"b","description":"y","deps":["a"]}]}'
    assert [t.id for t in parse_plan(good, "g").tasks] == ["a", "b"]
    assert len(parse_plan("garbage", "g").tasks) == 1
    cyc = '{"tasks":[{"id":"a","description":"x","deps":["b"]},{"id":"b","description":"y","deps":["a"]}]}'
    assert len(parse_plan(cyc, "g").tasks) == 1
    missing = '{"tasks":[{"id":"a","description":"x","deps":["zzz"]}]}'
    assert parse_plan(missing, "g").tasks[0].id == "t1"


class JudgeLLM:
    def __init__(self, verdicts): self.v = list(verdicts)
    def chat(self, messages, tools=None):
        ok = self.v.pop(0) if self.v else True
        return {"content": json.dumps({"ok": ok, "reason": "r"})}


def test_retry_then_succeed(tmp_path):
    calls = []
    def ex(prompt, ctx): calls.append(prompt); return "output"
    orch = Orchestrator(JudgeLLM([False, True]), ex, tmp_path, notify=lambda m: None)
    plan = orch.run(Plan("g", [Task("t1", "do it")]))
    assert plan.tasks[0].status == "done" and plan.tasks[0].attempts == 2
    assert "Previous attempt problem" in calls[1]


def test_failure_skips_dependents_but_runs_independent(tmp_path):
    def ex(prompt, ctx):
        return "error: boom" if "bad" in prompt else "fine"
    notes = []
    plan = Plan("g", [Task("a", "bad step"), Task("b", "after bad", ["a"]), Task("c", "independent")])
    orch = Orchestrator(JudgeLLM([]), ex, tmp_path, notify=notes.append, max_attempts=2)
    orch.run(plan)
    st = {t.id: t.status for t in plan.tasks}
    assert st == {"a": "failed", "b": "skipped", "c": "done"} and notes


def test_dependency_results_passed_and_resume_after_crash(tmp_path):
    seen = {}
    def ex(prompt, ctx): seen[prompt] = dict(ctx); return "R:" + prompt
    plan = Plan("g", [Task("a", "first"), Task("b", "second", ["a"])])
    orch = Orchestrator(JudgeLLM([]), ex, tmp_path, notify=lambda m: None)
    orch.checkpoint(plan)
    plan.tasks[0].status = "running"; orch.checkpoint(plan)          # simulate crash mid-task
    assert orch.unfinished() == [plan.id]
    restored = orch.load(plan.id)
    assert restored.tasks[0].status == "pending"
    orch.run(restored)
    assert seen["second"] == {"a": "R:first"} and restored.finished()
    assert orch.unfinished() == []


def test_reflect_error_result_fails_fast():
    ok, _ = reflect(JudgeLLM([]), "t", "error: nope")
    assert ok is False
