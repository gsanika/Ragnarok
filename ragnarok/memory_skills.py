"""Expose memory to the model as skills."""
from .skills import SkillRegistry


def register_memory_skills(reg: SkillRegistry, memory) -> None:
    @reg.register(description="Save a durable fact or preference about the user to long-term memory.")
    def remember(fact: str) -> str:
        return f"saved as memory #{memory.remember(fact, importance=0.7, source='user')}"

    @reg.register(description="Search long-term memory for facts relevant to a query.")
    def recall(query: str) -> str:
        facts = memory.recall(query, k=5)
        return "\n".join(f"#{f.id}: {f.text}" for f in facts) or "nothing found"

    @reg.register(description="Search the chronological log of past interactions for a keyword.")
    def search_history(keyword: str) -> str:
        eps = memory.store.search_episodes(keyword, limit=8)
        return "\n".join(f"{e.ts:.0f} [{e.kind}] {e.content[:160]}" for e in eps) or "no matches"

    @reg.register(description="Permanently delete one memory by its id number.", risky=True)
    def forget(memory_id: int) -> str:
        return "deleted" if memory.delete(int(memory_id)) else "not found"
