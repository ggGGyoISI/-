import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


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
    images_dir: Path
    # картинки
    image_provider: str
    image_style: str
    pollinations_url: str
    pollinations_key: str
    pollinations_model: str
    cf_account_id: str
    cf_api_token: str
    cf_image_model: str
    pexels_key: str
    image_title: bool
    font_path: str
    # автопилот
    auto_interval_min: int
    auto_posts_per_run: int


def load_config() -> Config:
    token = _env("BOT_TOKEN")
    if not token:
        raise SystemExit("Не задан BOT_TOKEN в .env")
    api_key = _env("LLM_API_KEY")
    if not api_key:
        raise SystemExit("Не задан LLM_API_KEY в .env (бесплатный ключ Gemini: https://aistudio.google.com/apikey)")

    admin_ids = frozenset(int(x) for x in _env("ADMIN_IDS").replace(" ", "").split(",") if x)
    return Config(
        bot_token=token,
        admin_ids=admin_ids,
        default_channel=_env("CHANNEL"),
        llm_base_url=_env("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"),
        llm_api_key=api_key,
        llm_model=_env("LLM_MODEL", "gemini-2.5-flash"),
        timezone=ZoneInfo(_env("TIMEZONE", "Europe/Moscow")),
        search_region=_env("SEARCH_REGION", "ru-ru"),
        max_post_chars=min(int(_env("MAX_POST_CHARS", "2500")), 4000),
        data_file=BASE_DIR / "data" / "state.json",
        images_dir=BASE_DIR / "data" / "images",
        image_provider=_env("IMAGE_PROVIDER", "auto").lower().replace(" ", ""),
        image_style=_env("IMAGE_STYLE", "modern digital illustration, vibrant colors, high detail, no text, no letters"),
        pollinations_url=_env("POLLINATIONS_URL", "https://gen.pollinations.ai/image/"),
        pollinations_key=_env("POLLINATIONS_KEY"),
        pollinations_model=_env("POLLINATIONS_MODEL"),
        cf_account_id=_env("CF_ACCOUNT_ID"),
        cf_api_token=_env("CF_API_TOKEN"),
        cf_image_model=_env("CF_IMAGE_MODEL", "@cf/black-forest-labs/flux-2-klein-4b"),
        pexels_key=_env("PEXELS_API_KEY"),
        image_title=_env("IMAGE_TITLE", "off").lower() in ("1", "on", "true", "yes", "да"),
        font_path=_env("FONT_PATH"),
        auto_interval_min=max(10, int(_env("AUTO_INTERVAL_MIN", "60"))),
        auto_posts_per_run=max(1, int(_env("AUTO_POSTS_PER_RUN", "2"))),
    )
