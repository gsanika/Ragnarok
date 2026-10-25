"""Contextual proactivity (FR4.2): a daily brief built from memory and recent activity."""
from __future__ import annotations

import time

BRIEF_PROMPT = (
    "You are the user's private assistant. Using ONLY the notes below, write a short morning "
    "brief: what they were working on, open threads, and 2-3 concrete suggestions for today.\n\n"
    "Known facts:\n{facts}\n\nRecent activity:\n{recent}\n\nToday: {today}"
)


def morning_brief(memory, llm=None, now: float | None = None) -> str:
    now = now or time.time()
    facts = sorted(memory.store.facts(), key=lambda f: -(f.importance + 0.02 * f.uses))[:10]
    eps = [e for e in memory.store.episodes(limit=15) if e.kind == "user"]
    facts_txt = "\n".join(f"- {f.text}" for f in facts) or "(none yet)"
    recent_txt = "\n".join(f"- {e.content[:140]}" for e in eps) or "(nothing yet)"
    today = time.strftime("%A %d %B %Y", time.localtime(now))
    if llm is not None:
        try:
            out = llm.chat([{"role": "user", "content": BRIEF_PROMPT.format(
                facts=facts_txt, recent=recent_txt, today=today)}]).get("content", "")
            if out.strip():
                return out.strip()
        except Exception:
            pass
    return f"Good morning! ({today})\n\nWhat I know:\n{facts_txt}\n\nRecent:\n{recent_txt}"
