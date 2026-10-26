import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    ollama_url: str = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    chat_model: str = os.environ.get("RAGNAROK_CHAT_MODEL", "llama3.1:8b")
    embed_model: str = os.environ.get("RAGNAROK_EMBED_MODEL", "nomic-embed-text")
    timeout: float = float(os.environ.get("RAGNAROK_TIMEOUT", "600"))      # seconds to wait for one model reply
    keep_alive: str = os.environ.get("RAGNAROK_KEEP_ALIVE", "30m")         # keep the model loaded between requests
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("RAGNAROK_HOME", "data")))
    max_steps: int = 6  # max tool-call rounds per user message

    @property
    def log_path(self) -> Path:
        return self.data_dir / "execution_log.jsonl"
