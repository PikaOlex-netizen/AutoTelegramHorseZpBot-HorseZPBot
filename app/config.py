from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AppConfig:
    api_id: int | None
    api_hash: str | None
    delete_after_seconds: int
    max_delete_retries: int
    pch_groups: int
    pb_groups: int
    group_delay_seconds: int
    log_level: str
    auto_reconnect: bool
    host: str
    port: int
    data_dir: Path
    database_url: str
    telegram_session_path: str
    gemini_api_key: str | None
    gemini_model: str
    terminal_idle_report_seconds: int

    @classmethod
    def load(cls) -> "AppConfig":
        load_dotenv(PROJECT_ROOT / ".env")
        # This is normally the project's data directory. The override exists for
        # isolated tests and does not alter the documented default installation.
        data_dir = Path(os.getenv("SCHEDULER_DATA_DIR", str(DATA_DIR))).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        api_id_raw = os.getenv("API_ID", "").strip()
        return cls(
            api_id=int(api_id_raw) if api_id_raw.isdigit() else None,
            api_hash=os.getenv("API_HASH", "").strip() or None,
            delete_after_seconds=int(os.getenv("DELETE_AFTER_SECONDS", "60")),
            max_delete_retries=int(os.getenv("MAX_DELETE_RETRIES", "3")),
            pch_groups=int(os.getenv("PCH_GROUPS", "6")),
            pb_groups=int(os.getenv("PB_GROUPS", "12")),
            group_delay_seconds=int(os.getenv("GROUP_DELAY_SECONDS", "3600")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            auto_reconnect=_bool(os.getenv("AUTO_RECONNECT"), True),
            host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "8000")),
            data_dir=data_dir,
            database_url=f"sqlite:///{data_dir / 'scheduler.sqlite3'}",
            telegram_session_path=str(data_dir / "telegram.session"),
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip() or None,
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest").strip() or "gemini-flash-lite-latest",
            terminal_idle_report_seconds=max(10, int(os.getenv("TERMINAL_IDLE_REPORT_SECONDS", "60"))),
        )


DEFAULT_SETTINGS = {
    "delete_after_seconds": 60,
    "max_delete_retries": 3,
    "pch_groups": 6,
    "pb_groups": 12,
    "group_delay_seconds": 3600,
    "log_level": "INFO",
    "auto_reconnect": True,
    "theme": "dark",
}
