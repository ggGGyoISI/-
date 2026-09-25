"""Telegram-бот: пишет посты в стиле канала, сам ищет информацию (в интернете и в других
Telegram-каналах), делает картинки и публикует посты."""

import asyncio
import logging
import re
import secrets
from datetime import datetime, timedelta

import openai
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, CallbackQuery, Message, MessageOriginChannel
from aiogram.utils.keyboard import InlineKeyboardBuilder

from autopilot import autopilot_loop, run_cycle
from formatting import to_plain
from search import Source, research
from services import (
    cfg, create_draft, images, images_on, llm_error_text, publish, send_draft, send_text,
    storage, user_channel, writer,
)
from tgchannels import fetch_posts, http_client, only_channels, parse_channels

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

router = Router()

# ожидаемый ответ от пользователя: user_id -> ("edit" | "schedule", draft_id)
pending: dict[int, tuple[str, str]] = {}
# пересланные посты, по которым ждём выбора действия: token -> данные
forwards: dict[str, dict] = {}

if cfg.admin_ids:
    router.message.filter(F.from_user.id.in_(cfg.admin_ids))
    router.callback_query.filter(F.from_user.id.in_(cfg.admin_ids))
else:
    log.warning("ADMIN_IDS не задан — ботом может пользоваться любой человек!")

HELP = """<b>Что я умею</b>

✍️ <b>Писать посты</b> — пришли тему текстом (или <code>/post тема</code>). Я поищу свежую информацию в интернете, напишу пост в стиле твоего канала и нарисую картинку.

🤖 <b>Автопилот</b> — сам ищу новое каждый час:
• <code>/watch @канал1 @канал2</code> — следить за Telegram-каналами (можно просто прислать ссылки). Когда там выходит пост, я переписываю его в твоём стиле;
• <code>/topics ИИ; криптовалюта; стартапы</code> — темы для поиска свежих новостей;
• <code>/auto on</code> — включить, <code>/auto off</code> — выключить;
• <code>/auto publish</code> — сразу публиковать в канал, <code>/auto drafts</code> — присылать мне на проверку;
• <code>/auto 3</code> — сколько постов максимум за один проход;
• <code>/check</code> — проверить прямо сейчас.

🎨 <b>Стиль канала</b>
• перешли мне 5–15 постов из своего канала и выбери «🎨 Пример моего стиля»;
• <code>/style</code> описание — например «коротко, с иронией, без эмодзи»;
• <code>/clearstyle</code> — забыть стиль.

🖼 <b>Картинки</b> — <code>/images on|off</code>. Под черновиком есть кнопка «Новая картинка».

💡 <b>Идеи</b> — <code>/ideas тематика</code>: предложу темы по свежим новостям.

📢 <b>Публикация</b>
• <code>/channel @мойканал</code> — куда публиковать (бот должен быть админом канала);
• под каждым черновиком: опубликовать, запланировать, переписать, правки;
• <code>/queue</code> — отложенные посты.

Всё бесплатно: поиск — DuckDuckGo, каналы — веб-версия Telegram, тексты — бесплатная нейросеть, картинки — Pollinations."""


# ---------- помощники ----------

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
        if m.group(1) or when <= now:
            when += timedelta(days=1)
        return when
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s+(\d{1,2})[:.](\d{2})", t)
    if m:
        try:
            return datetime(int(m.group(3) or now.year), int(m.group(2)), int(m.group(1)),
                            int(m.group(4)), int(m.group(5)), tzinfo=cfg.timezone)
        except ValueError:
            return None
    return None


async def generate(message: Message, user_id: int, topic: str, previous: dict | None = None,
                   edit_request: str = "", rewrite_source: str = "", origin: str = "") -> None:
    status = await message.answer("🔎 Ищу информацию в интернете…")
    try:
        if previous:
            sources = [Source(**s) for s in previous["sources"]]
            rewrite_source = previous.get("rewrite_source", "")
            origin = previous.get("origin", "")
            await status.edit_text("✍️ Переписываю пост…")
        elif rewrite_source:
            sources = await research([topic[:100]], cfg.search_region, "w")
            await status.edit_text("✍️ Переписываю пост в стиле канала…")
        else:
            queries = await writer.search_queries(topic)
            sources = await research(queries, cfg.search_region)
            await status.edit_text(f"✍️ Нашёл источников: {len(sources)}. Пишу пост…")
        if images_on(user_id) and not (previous and previous.get("image")):
            await status.edit_text("✍️ Пишу пост и рисую картинку…")
        draft_id = await create_draft(user_id, topic, sources, previous=previous,
                                      edit_request=edit_request, rewrite_source=rewrite_source,
                                      origin=origin)
    except openai.APIError as e:
        log.exception("LLM error")
        await status.edit_text(llm_error_text(e))
        return
    except ValueError as e:
        await status.edit_text(f"🤷 {e}, попробуй ещё раз.")
        return
    await status.delete()
    await send_draft(message.bot, message.chat.id, user_id, draft_id, with_sources=not previous)


async def add_watch(user_id: int, names: list[str]) -> str:
    u = storage.user(user_id)
    lines = []
    async with http_client() as client:
        for name in names:
            if name in u["watch"]:
                lines.append(f"• @{name} — уже отслеживаю")
                continue
            posts = await fetch_posts(client, name)
            if not posts:
                lines.append(f"• @{name} — ❌ не открывается (канал должен быть публичным)")
                continue
            u["watch"][name] = posts[-1].id  # старые посты не трогаем, ждём новые
            lines.append(f"• @{name} — ✅ слежу (последний пост {posts[-1].date})")
    storage.save()
    tail = "" if u["auto"]["enabled"] else "\n\nАвтопилот выключен — включи: /auto on"
    return "👀 <b>Каналы</b>\n" + "\n".join(lines) + tail


def auto_status(user_id: int) -> str:
    u = storage.user(user_id)
    a = u["auto"]
    watch = ", ".join(f"@{c}" for c in u["watch"]) or "—"
    topics = "; ".join(u["topics"]) or "—"
    mode = "сразу публикую в канал" if a["publish"] else "присылаю черновики тебе"
    return (
        f"🤖 <b>Автопилот: {'включён ✅' if a['enabled'] else 'выключен ⏸'}</b>\n"
        f"Проверка каждые {cfg.auto_interval_min} мин, до {a.get('posts') or cfg.auto_posts_per_run} постов за раз\n"
        f"Режим: {mode}\n"
        f"Канал для публикации: {user_channel(user_id) or 'не задан'}\n"
        f"Картинки: {'да' if images_on(user_id) else 'нет'}\n\n"
        f"👀 Слежу за: {watch}\n"
        f"🔎 Темы новостей: {topics}\n\n"
        "<code>/auto on|off|publish|drafts|число</code>"
    )


# ---------- команды ----------

@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message):
    await message.answer(HELP)


@router.message(Command("channel"))
async def cmd_channel(message: Message, command: CommandObject):
    u = storage.user(message.from_user.id)
    if command.args:
        names = parse_channels(command.args)
        u["channel"] = f"@{names[0]}" if names else command.args.strip()
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
        "Пересылай мне посты из своего канала, чтобы я лучше понял стиль, "
        "или задай описание: <code>/style коротко, с юмором, без эмодзи</code>"
    )


@router.message(Command("addsample"))
async def cmd_addsample(message: Message, command: CommandObject):
    text = command.args or (message.reply_to_message and message.reply_to_message.html_text)
    if not text:
        await message.answer("Использование: <code>/addsample текст поста</code>")
        return
    n = storage.add_sample(message.from_user.id, text)
    await message.answer(f"👌 Пример добавлен. Всего примеров: {n}")


@router.message(Command("clearstyle"))
async def cmd_clearstyle(message: Message):
    u = storage.user(message.from_user.id)
    u["samples"], u["style_note"], u["style_sources"] = [], "", []
    storage.save()
    await message.answer("🧹 Стиль очищен.")


@router.message(Command("post"))
async def cmd_post(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Напиши тему: <code>/post новые правила для самозанятых</code>")
        return
    await generate(message, message.from_user.id, command.args.strip())


@router.message(Command("watch"))
async def cmd_watch(message: Message, command: CommandObject):
    names = parse_channels(command.args or "")
    if not names:
        u = storage.user(message.from_user.id)
        current = "\n".join(f"• @{c}" for c in u["watch"]) or "пока ни за кем"
        await message.answer(f"👀 Слежу за каналами:\n{current}\n\n"
                             "Добавить: <code>/watch @канал https://t.me/другой</code>\n"
                             "Убрать: <code>/unwatch @канал</code>")
        return
    await message.answer(await add_watch(message.from_user.id, names))


@router.message(Command("unwatch"))
async def cmd_unwatch(message: Message, command: CommandObject):
    u = storage.user(message.from_user.id)
    removed = [n for n in parse_channels(command.args or "") if u["watch"].pop(n, None) is not None]
    storage.save()
    await message.answer(f"Больше не слежу: {', '.join('@' + n for n in removed)}" if removed
                         else "Таких каналов в списке нет. Список: /watch")


@router.message(Command("topics"))
async def cmd_topics(message: Message, command: CommandObject):
    u = storage.user(message.from_user.id)
    if command.args:
        u["topics"] = [t.strip() for t in re.split(r"[;\n]", command.args) if t.strip()][:10]
        storage.save()
    topics = "\n".join(f"• {t}" for t in u["topics"]) or "не заданы"
    await message.answer(f"🔎 Темы для поиска новостей:\n{topics}\n\n"
                         "Задать: <code>/topics ИИ; нейросети; стартапы</code>")


@router.message(Command("auto"))
async def cmd_auto(message: Message, command: CommandObject):
    user_id = message.from_user.id
    u = storage.user(user_id)
    arg = (command.args or "").strip().lower()
    if arg in ("on", "вкл", "включить"):
        if not u["watch"] and not u["topics"]:
            await message.answer("Сначала скажи, где искать: <code>/watch @канал</code> "
                                 "и/или <code>/topics темы</code>")
            return
        u["auto"]["enabled"] = True
        u["auto"]["last_run"] = 0  # первая проверка — в ближайшую минуту
    elif arg in ("off", "выкл", "выключить"):
        u["auto"]["enabled"] = False
    elif arg in ("publish", "публиковать"):
        if not user_channel(user_id):
            await message.answer("Сначала укажи канал: <code>/channel @имя</code>")
            return
        u["auto"]["publish"] = True
    elif arg in ("drafts", "черновики"):
        u["auto"]["publish"] = False
    elif arg.isdigit():
        u["auto"]["posts"] = max(1, min(int(arg), 10))
    storage.save()
    await message.answer(auto_status(user_id))


@router.message(Command("check"))
async def cmd_check(message: Message):
    u = storage.user(message.from_user.id)
    if not u["watch"] and not u["topics"]:
        await message.answer("Не знаю, где искать: добавь <code>/watch @канал</code> "
                             "или <code>/topics темы</code>")
        return
    await message.answer("🤖 Проверяю каналы и новости…")
    await run_cycle(message.bot, message.from_user.id, manual=True)


@router.message(Command("images"))
async def cmd_images(message: Message, command: CommandObject):
    u = storage.user(message.from_user.id)
    arg = (command.args or "").strip().lower()
    if arg in ("on", "off"):
        u["images"] = arg == "on"
        storage.save()
    if not images.enabled:
        await message.answer("Картинки выключены в настройках (IMAGE_PROVIDER=none).")
        return
    await message.answer(f"🖼 Картинки к постам: {'включены' if u['images'] else 'выключены'}\n"
                         "<code>/images on</code> или <code>/images off</code>")


@router.message(Command("ideas"))
async def cmd_ideas(message: Message, command: CommandObject):
    u = storage.user(message.from_user.id)
    niche = (command.args or u.get("niche") or "; ".join(u["topics"])).strip()
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
    await send_text(message.bot, message.chat.id, text + "\n\nНажми номер — напишу пост.",
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
        pic = " 🖼" if x.get("image") else ""
        await message.answer(f"🕒 {when} → {x['channel']}{pic}\n\n{to_plain(x['text'])[:200]}…",
                             parse_mode=None, reply_markup=kb.as_markup())


# ---------- кнопки ----------

@router.callback_query(F.data.startswith("idea:"))
async def cb_idea(call: CallbackQuery):
    ideas = storage.user(call.from_user.id)["ideas"]
    i = int(call.data.split(":")[1])
    await call.answer()
    if i < len(ideas):
        await generate(call.message, call.from_user.id, ideas[i])


@router.callback_query(F.data.regexp(r"^(pub|sch|re|ed|del|img|noimg):"))
async def cb_draft(call: CallbackQuery):
    action, draft_id = call.data.split(":", 1)
    user_id = call.from_user.id
    draft = storage.get_draft(user_id, draft_id)
    if not draft:
        await call.answer("Черновик не найден (устарел).", show_alert=True)
        return

    if action == "pub":
        channel = user_channel(user_id)
        err = await publish(call.bot, channel, draft["text"], draft.get("image"))
        if err:
            await call.answer()
            await call.message.answer(err, parse_mode=None)
        else:
            await call.answer("Опубликовано!")
            await call.message.edit_reply_markup(reply_markup=None)
            await call.message.reply(f"✅ Опубликовано в {channel}")
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
    elif action == "img":
        await call.answer("Рисую…")
        status = await call.message.answer("🖼 Рисую новую картинку…")
        draft["image"] = await images.make(draft.get("image_prompt") or draft["topic"],
                                           to_plain(draft["text"]).split("\n")[0][:120])
        storage.save()
        await status.delete()
        await send_draft(call.bot, call.message.chat.id, user_id, draft_id, with_sources=False)
    elif action == "noimg":
        draft["image"] = None
        storage.save()
        await call.answer("Картинка убрана")
        await send_draft(call.bot, call.message.chat.id, user_id, draft_id, with_sources=False)
    elif action == "del":
        storage.delete_draft(user_id, draft_id)
        await call.answer("Удалено")
        await call.message.delete()


@router.callback_query(F.data.startswith("unsch:"))
async def cb_unschedule(call: CallbackQuery):
    ok = storage.cancel_scheduled(call.from_user.id, call.data.split(":", 1)[1])
    await call.answer("Отменено" if ok else "Уже опубликован или отменён")
    await call.message.delete()


@router.callback_query(F.data.regexp(r"^fw_(style|rw|watch):"))
async def cb_forward(call: CallbackQuery):
    action, token = call.data.split(":", 1)
    fw = forwards.pop(token, None)
    if not fw:
        await call.answer("Устарело — перешли пост ещё раз.", show_alert=True)
        return
    user_id = call.from_user.id
    u = storage.user(user_id)
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)

    if action == "fw_style":
        n = storage.add_sample(user_id, fw["text"])
        reply = f"🎨 Запомнил стиль. Примеров: {n}."
        if fw["source_id"] and fw["source_id"] not in u["style_sources"]:
            u["style_sources"].append(fw["source_id"])
            reply += "\nДальше посты из этого канала буду сразу считать примерами стиля."
        if fw["username"] and not user_channel(user_id):
            u["channel"] = f"@{fw['username']}"
            reply += f"\n📢 Канал для публикации: {u['channel']} (сделай бота админом канала)."
        storage.save()
        if n < 5:
            reply += "\nПерешли ещё несколько постов — чем больше примеров, тем точнее стиль."
        await call.message.answer(reply)
    elif action == "fw_rw":
        plain = to_plain(fw["text"])
        topic = next((x.strip() for x in plain.splitlines() if x.strip()), plain)[:120]
        await generate(call.message, user_id, topic, rewrite_source=plain)
    elif action == "fw_watch":
        await call.message.answer(await add_watch(user_id, [fw["username"]]))


# ---------- обычные сообщения ----------

@router.message(F.forward_origin)
async def on_forward(message: Message):
    user_id = message.from_user.id
    u = storage.user(user_id)
    text = message.html_text if (message.text or message.caption) else ""
    origin = message.forward_origin
    username = source_id = None
    if isinstance(origin, MessageOriginChannel):
        username, source_id = (origin.chat.username or "").lower() or None, origin.chat.id
    if not text and not username:
        if not message.media_group_id:  # у альбомов текст только у одного сообщения
            await message.answer("В пересланном сообщении нет текста — пропускаю.")
        return

    # посты из своего канала — сразу в примеры стиля
    own = source_id and (source_id in u["style_sources"]
                         or (username and user_channel(user_id).lower() == f"@{username}".lower()))
    if own and text:
        n = storage.add_sample(user_id, text)
        await message.answer(f"🎨 Добавил в примеры стиля. Всего: {n}.")
        return

    token = secrets.token_hex(4)
    forwards[token] = {"text": text, "username": username, "source_id": source_id}
    for old in list(forwards)[:-200]:
        del forwards[old]
    kb = InlineKeyboardBuilder()
    if text:
        kb.button(text="🎨 Пример моего стиля", callback_data=f"fw_style:{token}")
        kb.button(text="✍️ Переписать в пост", callback_data=f"fw_rw:{token}")
    if username and username not in u["watch"]:
        kb.button(text=f"👀 Следить за @{username}", callback_data=f"fw_watch:{token}")
    kb.adjust(1)
    await message.answer("Что сделать с этим постом?", reply_markup=kb.as_markup())


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
            await generate(message, user_id, draft["topic"], previous=draft, edit_request=message.text)
            return
        when = parse_when(message.text)
        if not when or when <= datetime.now(cfg.timezone):
            pending[user_id] = (kind, draft_id)
            await message.answer("Не понял время 🤔 Пример: <code>18:30</code> или <code>25.12 10:00</code>")
            return
        storage.schedule({
            "id": secrets.token_hex(4), "user_id": user_id, "at": when.timestamp(),
            "channel": user_channel(user_id), "text": draft["text"], "image": draft.get("image"),
        })
        await message.answer(f"🕒 Запланировано на {when.strftime('%d.%m %H:%M')} "
                             f"в {user_channel(user_id)}. Список: /queue")
        return
    # прислали только ссылки на каналы — начинаем за ними следить
    if only_channels(message.text):
        await message.answer(await add_watch(user_id, parse_channels(message.text)))
        return
    await generate(message, user_id, message.text.strip())


# ---------- отложенные публикации ----------

async def scheduler(bot: Bot):
    while True:
        try:
            for item in storage.pop_due(datetime.now(cfg.timezone).timestamp()):
                err = await publish(bot, item["channel"], item["text"], item.get("image"))
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
        BotCommand(command="auto", description="Автопилот: статус и настройки"),
        BotCommand(command="watch", description="Следить за Telegram-каналами"),
        BotCommand(command="topics", description="Темы для поиска новостей"),
        BotCommand(command="check", description="Проверить новое прямо сейчас"),
        BotCommand(command="ideas", description="Идеи постов по свежим новостям"),
        BotCommand(command="style", description="Стиль канала"),
        BotCommand(command="images", description="Картинки к постам вкл/выкл"),
        BotCommand(command="channel", description="Канал для публикации"),
        BotCommand(command="queue", description="Отложенные посты"),
        BotCommand(command="help", description="Помощь"),
    ])
    tasks = [asyncio.create_task(scheduler(bot)), asyncio.create_task(autopilot_loop(bot))]
    log.info("Бот запущен. Нейросеть: %s, картинки: %s", cfg.llm_model, cfg.image_provider)
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
