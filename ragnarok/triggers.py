"""Always-on triggers (FR4.1): interval, daily time, file changes, new email."""
from __future__ import annotations

import datetime as dt
import threading
import time
import traceback
from pathlib import Path
from typing import Callable


class Trigger:
    name = "trigger"
    def poll(self, now: float) -> list[str]:  # returns task texts to run
        return []


class IntervalTrigger(Trigger):
    def __init__(self, name: str, seconds: float, task: str):
        self.name, self.seconds, self.task, self._last = name, seconds, task, None

    def poll(self, now):
        if self._last is None:
            self._last = now; return []
        if now - self._last >= self.seconds:
            self._last = now; return [self.task]
        return []


class DailyTrigger(Trigger):
    def __init__(self, name: str, hhmm: str, task: str):
        self.name, self.task = name, task
        self.h, self.m = map(int, hhmm.split(":"))
        self._fired_on: dt.date | None = None

    def poll(self, now):
        t = dt.datetime.fromtimestamp(now)
        if (t.hour, t.minute) >= (self.h, self.m) and self._fired_on != t.date():
            self._fired_on = t.date(); return [self.task]
        return []


class FileWatchTrigger(Trigger):
    def __init__(self, name: str, folder: Path, task_template: str = "File changed: {path}"):
        self.name, self.folder, self.tpl = name, Path(folder), task_template
        self._seen = self._snapshot()

    def _snapshot(self) -> dict[str, float]:
        if not self.folder.exists():
            return {}
        return {str(p): p.stat().st_mtime for p in self.folder.rglob("*") if p.is_file()}

    def poll(self, now):
        cur = self._snapshot()
        changed = [p for p, m in cur.items() if self._seen.get(p) != m]
        self._seen = cur
        return [self.tpl.format(path=p) for p in changed]


class EmailTrigger(Trigger):
    """Polls for unseen mail. `fetch` returns [(uid, subject, sender)] and is injected so the
    trigger can be tested (and so IMAP credentials stay in integrations.py)."""
    def __init__(self, name: str, fetch: Callable[[], list[tuple[str, str, str]]],
                 task_template: str = "Triage new email from {sender}: {subject}", every: float = 60):
        self.name, self.fetch, self.tpl, self.every = name, fetch, task_template, every
        self._last, self._seen = 0.0, set()

    def poll(self, now):
        if now - self._last < self.every:
            return []
        self._last = now
        out = []
        for uid, subject, sender in self.fetch():
            if uid not in self._seen:
                self._seen.add(uid); out.append(self.tpl.format(sender=sender, subject=subject))
        return out


class TriggerRunner:
    def __init__(self, triggers: list[Trigger], handler: Callable[[str, str], None],
                 poll_seconds: float = 5.0, log=None, clock: Callable[[], float] = time.time):
        self.triggers, self.handler, self.poll_seconds = triggers, handler, poll_seconds
        self.log, self.clock = log, clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self, now: float | None = None) -> int:
        now = now if now is not None else self.clock()
        n = 0
        for t in self.triggers:
            try:
                for task in t.poll(now):
                    n += 1
                    if self.log: self.log.record("trigger_fired", trigger=t.name, task=task)
                    self.handler(task, t.name)
            except Exception as e:  # one broken trigger must not kill the sentinel
                if self.log: self.log.record("trigger_error", trigger=t.name, error=repr(e), tb=traceback.format_exc()[-500:])
        return n

    def start(self) -> None:
        def loop():
            while not self._stop.wait(self.poll_seconds):
                self.tick()
        self._thread = threading.Thread(target=loop, daemon=True, name="ragnarok-triggers")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread: self._thread.join(timeout=5)
