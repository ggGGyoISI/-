"""Общее ядро: создание черновиков, отправка и публикация постов."""

import html
from dataclasses import asdict
from pathlib import Path

import openai
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import FSInputFile, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import load_config
from formatting import to_plain, visible_length
from images import ImageMaker
from search import Source
from storage import Storage
from writer import Writer

cfg = load_config()
storage = Storage(cfg.data_file)
writer = Writer(cfg.llm_base_url, cfg.llm_api_key, cfg.llm_model, cfg.max_post_chars)
images = ImageMaker(cfg)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
CAPTION_LIMIT = 1024  # лимит подписи к фото в Telegram


def llm_error_text(e: Exception) -> str:
    if isinstance(e, openai.RateLimitError):
        return "⏳ Упёрлись в лимит бесплатного тарифа нейросети. Подожди минуту и попробуй снова."
    if isinstance(e, openai.AuthenticationError):
        return "🔑 Неверный LLM_API_KEY — проверь .env."
    if isinstance(e, openai.APIConnectionError):
        return "🌐 Не удалось связаться с нейросетью. Проверь интернет / LLM_BASE_URL."
    return f"⚠️ Ошибка нейросети: {e}"


def user_channel(user_id: int) -> str:
    return storage.user(user_id)["channel"] or cfg.default_channel


def images_on(user_id: int) -> bool:
    return images.enabled and storage.user(user_id)["images"]


async def create_draft(
    user_id: int,
    topic: str,
    sources: list[Source],
    *,
    previous: dict | None = None,
    edit_request: str = "",
    rewrite_source: str = "",
    origin: str = "",
) -> str:
    """Пишет пост (+ картинку) и сохраняет черновик. Бросает openai.APIError."""
    u = storage.user(user_id)
    want_image = images_on(user_id)
    text, image_prompt, image_query = await writer.write_post(
        topic, sources, u["samples"], u["style_note"],
        previous=previous["text"] if previous else "",
        edit_request=edit_request,
        rewrite_source=rewrite_source,
        want_image_prompt=want_image,
    )
    if not text:
        raise ValueError("Нейросеть вернула пустой ответ")

    image = None
    if previous and previous.get("image"):
        # при правках оставляем прежнюю картинку; новую можно сделать кнопкой
        image = previous["image"]
        image_prompt = previous.get("image_prompt") or image_prompt
        image_query = previous.get("image_query") or image_query
    elif want_image:
        image = await images.make(image_prompt or topic, post_title(text, topic), image_query)

    return storage.add_draft(user_id, {
        "topic": topic,
        "text": text,
        "sources": [asdict(s) for s in sources],
        "rewrite_source": rewrite_source,
        "origin": origin,
        "image": image,
        "image_prompt": image_prompt or topic,
        "image_query": image_query,
    })


def post_title(text: str, fallback: str = "") -> str:
    """Первая строка поста — заголовок для обложки."""
    plain = to_plain(text)
    return next((x.strip() for x in plain.splitlines() if x.strip()), fallback)[:120] or fallback


async def send_text(bot: Bot, chat_id: int | str, text: str, **kw) -> Message:
    """HTML; если Telegram не принял разметку — простым текстом."""
    try:
        return await bot.send_message(chat_id, text, link_preview_options=NO_PREVIEW, **kw)
    except TelegramBadRequest as e:
        if "parse" not in str(e).lower() and "entit" not in str(e).lower():
            raise
        return await bot.send_message(chat_id, to_plain(text), parse_mode=None,
                                      link_preview_options=NO_PREVIEW, **kw)


async def _send_photo(bot: Bot, chat_id: int | str, image: str, caption: str | None, **kw) -> Message:
    try:
        return await bot.send_photo(chat_id, FSInputFile(image), caption=caption, **kw)
    except TelegramBadRequest as e:
        if not caption or ("parse" not in str(e).lower() and "entit" not in str(e).lower()):
            raise
        return await bot.send_photo(chat_id, FSInputFile(image), caption=to_plain(caption),
                                    parse_mode=None, **kw)


async def send_post(bot: Bot, chat_id: int | str, text: str, image: str | None, **kw) -> Message:
    """Отправляет пост. Короткий пост — фото с подписью, длинный — фото и текст следом."""
    if image and Path(image).exists():
        if visible_length(text) <= CAPTION_LIMIT:
            return await _send_photo(bot, chat_id, image, text, **kw)
        await _send_photo(bot, chat_id, image, None)
    return await send_text(bot, chat_id, text, **kw)



def draft_kb(draft_id: str, has_image: bool):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Опубликовать", callback_data=f"pub:{draft_id}")
    kb.button(text="🕒 Запланировать", callback_data=f"sch:{draft_id}")
    kb.button(text="🔄 Другой вариант", callback_data=f"re:{draft_id}")
    kb.button(text="✏️ Правки", callback_data=f"ed:{draft_id}")
    kb.button(text="🎨 Новая картинка" if has_image else "🎨 Добавить картинку",
              callback_data=f"img:{draft_id}")
    if images.has_pexels:
        kb.button(text="📷 Фото со стока", callback_data=f"photo:{draft_id}")
    if has_image:
        kb.button(text="🚫 Без картинки", callback_data=f"noimg:{draft_id}")
    kb.button(text="🗑 Удалить", callback_data=f"del:{draft_id}")
    kb.adjust(2, 2, 2, 2)
    return kb.as_markup()


async def send_draft(bot: Bot, chat_id: int, user_id: int, draft_id: str, with_sources: bool = True) -> None:
    draft = storage.get_draft(user_id, draft_id)
    if not draft:
        return
    sources = draft["sources"]
    if with_sources and sources:
        links = "\n".join(
            f'{i}. <a href="{html.escape(s["url"])}">{html.escape(s["title"][:80] or s["url"])}</a>'
            for i, s in enumerate(sources[:6], 1)
        )
        await send_text(bot, chat_id, f"📚 <b>Источники</b>\n{links}")
    await send_post(bot, chat_id, draft["text"], draft.get("image"),
                    reply_markup=draft_kb(draft_id, bool(draft.get("image"))))


async def publish(bot: Bot, channel: str, text: str, image: str | None) -> str | None:
    """Публикует пост в канал. Возвращает текст ошибки или None."""
    if not channel:
        return "Не задан канал. Укажи его: /channel @имя_канала"
    try:
        await send_post(bot, channel, text, image)
        return None
    except TelegramAPIError as e:
        return (f"Не удалось опубликовать в {channel}: {e}\n"
                "Проверь, что бот добавлен в администраторы канала с правом публикации.")
