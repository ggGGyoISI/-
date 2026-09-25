import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    default_channel: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    timezone: ZoneInfo
    search_region: str
    max_post_chars: int
    data_file: Path


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Не задан BOT_TOKEN в .env")
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("Не задан LLM_API_KEY в .env (бесплатный ключ Gemini: https://aistudio.google.com/apikey)")

    admin_ids = frozenset(
        int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
    )
    return Config(
        bot_token=token,
        admin_ids=admin_ids,
        default_channel=os.getenv("CHANNEL", "").strip(),
        llm_base_url=os.getenv(
            "LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
        ).strip(),
        llm_api_key=api_key,
        llm_model=os.getenv("LLM_MODEL", "gemini-2.5-flash").strip(),
        timezone=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow").strip()),
        search_region=os.getenv("SEARCH_REGION", "ru-ru").strip(),
        max_post_chars=min(int(os.getenv("MAX_POST_CHARS", "2500")), 4000),
        data_file=BASE_DIR / "data" / "state.json",
    )
