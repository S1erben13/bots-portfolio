import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    kb_dir: Path = field(default_factory=lambda: Path(os.getenv("KB_DIR", "kb")))
    db_path: Path = field(default_factory=lambda: Path(os.getenv("DB_PATH", "data/leads.sqlite3")))
    provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "ollama"))
    bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    manager_chat_id: str = field(default_factory=lambda: os.getenv("MANAGER_CHAT_ID", ""))
    min_score: float = field(default_factory=lambda: float(os.getenv("RAG_MIN_SCORE", "0.8")))
    min_coverage: float = field(default_factory=lambda: float(os.getenv("RAG_MIN_COVERAGE", "0.5")))
