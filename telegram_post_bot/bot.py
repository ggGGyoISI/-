"""Telegram-бот: пишет посты в стиле канала, ищет информацию в интернете и публикует их."""

import asyncio
import html
import logging
import re
import secrets
from dataclasses import asdict
from datetime import datetime, timedelta

import openai
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    LinkPreviewOptions,
    Message,
    MessageOriginChannel,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import load_config
from formatting import to_plain
from search import Source, research
from storage import Storage
from writer import Writer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

cfg = load_config()
storage = Storage(cfg.data_file)
writer = Writer(cfg.llm_base_url, cfg.llm_api_key, cfg.llm_model, cfg.max_post_chars)
router = Router()
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# ожидаемый ответ от пользователя: user_id -> ("edit" | "schedule", draft_id)
pending: dict[int, tuple[str, str]] = {}

if cfg.admin_ids:
    router.message.filter(F.from_user.id.in_(cfg.admin_ids))
    router.callback_query.filter(F.from_user.id.in_(cfg.admin_ids))
else:
    log.warning("ADMIN_IDS не задан — ботом может пользоваться любой человек!")

HELP = """<b>Что я умею</b>

✍️ <b>Писать посты</b> — просто пришли тему текстом (или <code>/post тема</code>). Я поищу свежую информацию в интернете и напишу пост в стиле твоего канала.

🎨 <b>Учить стиль канала</b>
• перешли мне 5–15 постов из канала — я запомню манеру;
• <code>/style</code> описание — например «коротко, с иронией, без эмодзи, в конце вопрос к читателям»;
• <code>/addsample</code> текст — добавить пример вручную;
• <code>/clearstyle</code> — забыть стиль.

💡 <b>Идеи</b> — <code>/ideas тематика канала</code>: найду свежие новости и предложу темы.

📢 <b>Публикация</b>
• <code>/channel @мойканал</code> — куда публиковать (добавь бота в админы канала с правом публикации);
• под каждым черновиком кнопки: опубликовать, запланировать, переписать, правки;
• <code>/queue</code> — отложенные посты.

Всё бесплатно: поиск — DuckDuckGo, тексты — бесплатная нейросеть."""


# ---------- помощники ----------

def draft_kb(draft_id: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Опубликовать", callback_data=f"pub:{draft_id}")
    kb.button(text="🕒 Запланировать", callback_data=f"sch:{draft_id}")
    kb.button(text="🔄 Другой вариант", callback_data=f"re:{draft_id}")
    kb.button(text="✏️ Правки", callback_data=f"ed:{draft_id}")
    kb.button(text="🗑 Удалить", callback_data=f"del:{draft_id}")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


async def send_html(bot: Bot, chat_id: int | str, text: str, **kw) -> Message:
    """Отправляет HTML; если Telegram не принял разметку — отправляет простым текстом."""
    try:
        return await bot.send_message(chat_id, text, link_preview_options=NO_PREVIEW, **kw)
    except TelegramBadRequest as e:
        if "parse" not in str(e).lower() and "entit" not in str(e).lower():
            raise
        return await bot.send_message(
            chat_id, to_plain(text), parse_mode=None, link_preview_options=NO_PREVIEW, **kw
        )


def user_channel(user_id: int) -> str:
    return storage.user(user_id)["channel"] or cfg.default_channel


def parse_when(text: str) -> datetime | None:
    """'18:30', 'завтра 9:00', '25.12 10:00', '25.12.2026 10:00', 'через 2 ч', 'через 30 мин'."""
    now = datetime.now(cfg.timezone)
    t = text.strip().lower()
    m = re.fullmatch(r"через\s+(\d+)\s*(м|мин|минут[уы]?|ч|час|часа|часов)", t)
    if m:
        n = int(m.group(1))
        return now + (timedelta(minutes=n) if m.group(2).startswith("м") else timedelta(hours=n))
    m = re.fullmatch(r"(завтра\s+)?(\d{1,2})[:.](\d{2})", t)
    if m:
        when = now.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0)
        if m.group(1):
            when += timedelta(days=1)
        elif when <= now:
            when += timedelta(days=1)
        return when
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s+(\d{1,2})[:.](\d{2})", t)
    if m:
        year = int(m.group(3) or now.year)
        return datetime(year, int(m.group(2)), int(m.group(1)), int(m.group(4)),
                        int(m.group(5)), tzinfo=cfg.timezone)
    return None


def llm_error_text(e: Exception) -> str:
    if isinstance(e, openai.RateLimitError):
        return "⏳ Упёрлись в лимит бесплатного тарифа нейросети. Подожди минуту и попробуй снова."
    if isinstance(e, openai.AuthenticationError):
        return "🔑 Неверный LLM_API_KEY — проверь .env."
    if isinstance(e, openai.APIConnectionError):
        return "🌐 Не удалось связаться с нейросетью. Проверь интернет / LLM_BASE_URL."
    return f"⚠️ Ошибка нейросети: {e}"


async def generate(message: Message, user_id: int, topic: str,
                   previous: dict | None = None, edit_request: str = "") -> None:
    u = storage.user(user_id)
    status = await message.answer("🔎 Ищу информацию в интернете…")
    try:
        if previous:
            sources = [Source(**s) for s in previous["sources"]]
            await status.edit_text("✍️ Переписываю пост…")
        else:
            queries = await writer.search_queries(topic)
            sources = await research(queries, cfg.search_region)
            await status.edit_text(f"✍️ Нашёл источников: {len(sources)}. Пишу пост…")
        text = await writer.write_post(
            topic, sources, u["samples"], u["style_note"],
            previous=previous["text"] if previous else "", edit_request=edit_request,
        )
    except openai.APIError as e:
        log.exception("LLM error")
        await status.edit_text(llm_error_text(e))
        return

    if not text:
        await status.edit_text("🤷 Нейросеть вернула пустой ответ, попробуй ещё раз.")
        return

    draft_id = storage.add_draft(user_id, {
        "topic": topic, "text": text, "sources": [asdict(s) for s in sources],
    })
    await status.delete()
    if sources:
        links = "\n".join(f'{i}. <a href="{html.escape(s.url)}">{html.escape(s.title[:80] or s.url)}</a>'
                          for i, s in enumerate(sources[:6], 1))
        await send_html(message.bot, message.chat.id, f"📚 <b>Источники</b>\n{links}")
    await send_html(message.bot, message.chat.id, text, reply_markup=draft_kb(draft_id))


async def publish(bot: Bot, channel: str, text: str) -> str | None:
    """Публикует пост. Возвращает текст ошибки или None."""
    if not channel:
        return "Не задан канал. Укажи его: /channel @имя_канала"
    try:
        await send_html(bot, channel, text)
        return None
    except TelegramAPIError as e:
        return (f"Не удалось опубликовать в {channel}: {e}\n"
                "Проверь, что бот добавлен в администраторы канала с правом публикации.")


# ---------- команды ----------

@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message):
    await message.answer(HELP)


@router.message(Command("channel"))
async def cmd_channel(message: Message, command: CommandObject):
    u = storage.user(message.from_user.id)
    if command.args:
        u["channel"] = command.args.strip()
        storage.save()
        await message.answer(f"📢 Буду публиковать в {u['channel']}.\n"
                             "Не забудь сделать бота админом канала с правом публикации.")
    else:
        await message.answer(f"Текущий канал: {user_channel(message.from_user.id) or 'не задан'}\n"
                             "Изменить: <code>/channel @имя_канала</code>")


@router.message(Command("style"))
async def cmd_style(message: Message, command: CommandObject):
    u = storage.user(message.from_user.id)
    if command.args:
        u["style_note"] = command.args.strip()
        storage.save()
        await message.answer("🎨 Описание стиля сохранено.")
        return
    await message.answer(
        f"🎨 Примеров постов: <b>{len(u['samples'])}</b>\n"
        f"Описание стиля: {u['style_note'] or '—'}\n\n"
        "Пересылай мне посты из канала, чтобы я лучше понял стиль, "
        "или задай описание: <code>/style коротко, с юмором, без эмодзи</code>"
    )


@router.message(Command("addsample"))
async def cmd_addsample(message: Message, command: CommandObject):
    text = command.args or (message.reply_to_message and message.reply_to_message.html_text)
    if not text:
        await message.answer("Использование: <code>/addsample текст поста</code> "
                             "или ответь этой командой на сообщение.")
        return
    n = storage.add_sample(message.from_user.id, text)
    await message.answer(f"👌 Пример добавлен. Всего примеров: {n}")


@router.message(Command("clearstyle"))
async def cmd_clearstyle(message: Message):
    u = storage.user(message.from_user.id)
    u["samples"], u["style_note"] = [], ""
    storage.save()
    await message.answer("🧹 Стиль очищен.")


@router.message(Command("post"))
async def cmd_post(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Напиши тему: <code>/post новые правила для самозанятых</code>")
        return
    await generate(message, message.from_user.id, command.args.strip())


@router.message(Command("ideas"))
async def cmd_ideas(message: Message, command: CommandObject):
    u = storage.user(message.from_user.id)
    niche = (command.args or u.get("niche") or "").strip()
    if not niche:
        await message.answer("Укажи тематику канала: <code>/ideas личные финансы и инвестиции</code>")
        return
    u["niche"] = niche
    storage.save()
    status = await message.answer("🔎 Смотрю свежие новости…")
    try:
        sources = await research([niche, f"{niche} новости"], cfg.search_region, timelimit="w")
        ideas = await writer.ideas(niche, sources, u["samples"], u["style_note"])
    except openai.APIError as e:
        await status.edit_text(llm_error_text(e))
        return
    if not ideas:
        await status.edit_text("Не получилось придумать идеи, попробуй ещё раз.")
        return
    u["ideas"] = ideas
    storage.save()
    kb = InlineKeyboardBuilder()
    for i in range(len(ideas)):
        kb.button(text=f"✍️ {i + 1}", callback_data=f"idea:{i}")
    kb.adjust(6)
    text = "💡 <b>Идеи для постов</b>\n\n" + "\n\n".join(
        f"{i}. {to_plain(x)}" for i, x in enumerate(ideas, 1))
    await status.delete()
    await send_html(message.bot, message.chat.id, text + "\n\nНажми номер — напишу пост.",
                    reply_markup=kb.as_markup())


@router.message(Command("queue"))
async def cmd_queue(message: Message):
    items = storage.scheduled_for(message.from_user.id)
    if not items:
        await message.answer("Отложенных постов нет.")
        return
    for x in items:
        when = datetime.fromtimestamp(x["at"], cfg.timezone).strftime("%d.%m %H:%M")
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отменить", callback_data=f"unsch:{x['id']}")
        preview = to_plain(x["text"])[:200]
        await message.answer(f"🕒 {when} → {x['channel']}\n\n{preview}…",
                             parse_mode=None, reply_markup=kb.as_markup())


# ---------- кнопки ----------

@router.callback_query(F.data.startswith("idea:"))
async def cb_idea(call: CallbackQuery):
    ideas = storage.user(call.from_user.id)["ideas"]
    i = int(call.data.split(":")[1])
    await call.answer()
    if i < len(ideas):
        await generate(call.message, call.from_user.id, ideas[i])


@router.callback_query(F.data.regexp(r"^(pub|sch|re|ed|del):"))
async def cb_draft(call: CallbackQuery):
    action, draft_id = call.data.split(":", 1)
    user_id = call.from_user.id
    draft = storage.get_draft(user_id, draft_id)
    if not draft:
        await call.answer("Черновик не найден (устарел).", show_alert=True)
        return

    if action == "pub":
        err = await publish(call.bot, user_channel(user_id), draft["text"])
        if err:
            await call.answer()
            await call.message.answer(err, parse_mode=None)
        else:
            await call.answer("Опубликовано!")
            await call.message.edit_reply_markup(reply_markup=None)
            await call.message.reply(f"✅ Опубликовано в {user_channel(user_id)}")
    elif action == "sch":
        if not user_channel(user_id):
            await call.answer("Сначала укажи канал: /channel @имя", show_alert=True)
            return
        pending[user_id] = ("schedule", draft_id)
        await call.answer()
        await call.message.answer(
            "🕒 Когда опубликовать? Например: <code>18:30</code>, <code>завтра 9:00</code>, "
            "<code>25.12 10:00</code>, <code>через 2 ч</code>")
    elif action == "re":
        await call.answer("Пишу другой вариант…")
        await generate(call.message, user_id, draft["topic"], previous=draft)
    elif action == "ed":
        pending[user_id] = ("edit", draft_id)
        await call.answer()
        await call.message.answer("✏️ Напиши, что поправить (например: «короче, добавь вывод в конце»).")
    elif action == "del":
        storage.delete_draft(user_id, draft_id)
        await call.answer("Удалено")
        await call.message.delete()


@router.callback_query(F.data.startswith("unsch:"))
async def cb_unschedule(call: CallbackQuery):
    ok = storage.cancel_scheduled(call.from_user.id, call.data.split(":", 1)[1])
    await call.answer("Отменено" if ok else "Уже опубликован или отменён")
    await call.message.delete()


# ---------- обычные сообщения ----------

@router.message(F.forward_origin)
async def on_forward(message: Message):
    text = message.html_text if (message.text or message.caption) else ""
    if not text:
        await message.answer("В пересланном сообщении нет текста — пропускаю.")
        return
    n = storage.add_sample(message.from_user.id, text)
    reply = f"🎨 Запомнил стиль. Примеров: {n}."
    origin = message.forward_origin
    u = storage.user(message.from_user.id)
    if isinstance(origin, MessageOriginChannel) and not user_channel(message.from_user.id):
        chan = f"@{origin.chat.username}" if origin.chat.username else str(origin.chat.id)
        u["channel"] = chan
        storage.save()
        reply += f"\n📢 Канал для публикации: {chan} (сделай бота админом канала)."
    if n < 5:
        reply += "\nПерешли ещё несколько постов — чем больше примеров, тем точнее стиль."
    await message.answer(reply)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message):
    user_id = message.from_user.id
    if user_id in pending:
        kind, draft_id = pending.pop(user_id)
        draft = storage.get_draft(user_id, draft_id)
        if not draft:
            await message.answer("Черновик не найден.")
            return
        if kind == "edit":
            await generate(message, user_id, draft["topic"], previous=draft,
                           edit_request=message.text)
            return
        when = parse_when(message.text)
        if not when or when <= datetime.now(cfg.timezone):
            pending[user_id] = (kind, draft_id)
            await message.answer("Не понял время 🤔 Пример: <code>18:30</code> или <code>25.12 10:00</code>")
            return
        storage.schedule({
            "id": secrets.token_hex(4), "user_id": user_id, "at": when.timestamp(),
            "channel": user_channel(user_id), "text": draft["text"],
        })
        await message.answer(f"🕒 Запланировано на {when.strftime('%d.%m %H:%M')} "
                             f"в {user_channel(user_id)}. Список: /queue")
        return
    await generate(message, user_id, message.text.strip())


# ---------- планировщик ----------

async def scheduler(bot: Bot):
    while True:
        try:
            for item in storage.pop_due(datetime.now(cfg.timezone).timestamp()):
                err = await publish(bot, item["channel"], item["text"])
                note = f"❌ {err}" if err else f"✅ Отложенный пост опубликован в {item['channel']}"
                try:
                    await bot.send_message(item["user_id"], note, parse_mode=None)
                except TelegramAPIError:
                    pass
        except Exception:
            log.exception("scheduler error")
        await asyncio.sleep(20)


async def main():
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="post", description="Написать пост на тему"),
        BotCommand(command="ideas", description="Идеи постов по свежим новостям"),
        BotCommand(command="style", description="Стиль канала"),
        BotCommand(command="channel", description="Канал для публикации"),
        BotCommand(command="queue", description="Отложенные посты"),
        BotCommand(command="help", description="Помощь"),
    ])
    task = asyncio.create_task(scheduler(bot))
    log.info("Бот запущен, модель: %s", cfg.llm_model)
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
