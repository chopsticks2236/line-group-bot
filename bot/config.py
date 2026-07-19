"""設定の読み込み（.env + messages.json）"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "messages.db"

MESSAGES_PATH = ROOT / "messages.json"


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


LINE_CHANNEL_ACCESS_TOKEN = _env("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = _env("LINE_CHANNEL_SECRET")
LINE_GROUP_ID = _env("LINE_GROUP_ID")

XAI_API_KEY = _env("XAI_API_KEY")
XAI_MODEL = _env("XAI_MODEL", "grok-4.5")
XAI_BASE_URL = _env("XAI_BASE_URL", "https://api.x.ai/v1")

CRON_SECRET = _env("CRON_SECRET", "change-me")
PORT = int(_env("PORT", "8000") or "8000")


def load_messages() -> dict:
    with open(MESSAGES_PATH, encoding="utf-8") as f:
        return json.load(f)


def has_line_credentials() -> bool:
    return bool(LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET)


def has_ai_key() -> bool:
    return bool(XAI_API_KEY)
