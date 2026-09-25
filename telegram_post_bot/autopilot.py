"""Автопилот: раз в час ищет новое (посты отслеживаемых каналов + свежие новости по темам),
выбирает самое интересное, переписывает в стиле канала, делает картинку и
присылает черновик или сразу публикует."""

import asyncio
import logging
import time
from dataclasses import dataclass

import openai
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from search import Source, ddg_search, research
from services import (
    NO_PREVIEW, cfg, create_draft, images, llm_error_text, publish, send_draft, storage,
    user_channel, writer,
)
from tgchannels import fetch_posts, http_client, is_rewritable

log = logging.getLogger(__name__)
_lock = asyncio.Lock()


@dataclass
class Candidate:
    kind: str       # "channel" | "news"
    label: str      # откуда: @канал или сайт
    title: str
    text: str
    url: str

    def brief(self) -> str:
        where = f"Telegram-канал {self.label}" if self.kind == "channel" else f"новость, {self.label}"
        body = " ".join(self.text.split())[:350]
        return f"({where}) {self.title}\n{body}"


def _first_line(text: str, limit: int = 120) -> str:
    line = next((x.strip() for x in text.splitlines() if x.strip()), text.strip())
    return line[:limit]


async def collect(user_id: int) -> list[Candidate]:
    u = storage.user(user_id)
    candidates: list[Candidate] = []

    # 1. новые посты в отслеживаемых каналах
    if u["watch"]:
        async with http_client() as client:
            channels = list(u["watch"])
            results = await asyncio.gather(*(fetch_posts(client, ch) for ch in channels))
        for ch, posts in zip(channels, results):
            last_id = u["watch"][ch]
            new = [p for p in posts if p.id > last_id]
            if posts:
                u["watch"][ch] = max(last_id, posts[-1].id)
            for p in new:
                if is_rewritable(p):
                    candidates.append(Candidate("channel", f"@{ch}", _first_line(p.text), p.text, p.url))

    # 2. свежие новости по темам
    seen = set(u["seen_urls"])
    new_urls = []
    for topic in u["topics"]:
        for r in await asyncio.to_thread(ddg_search, topic, cfg.search_region, True, "d"):
            url = r.get("url") or r.get("href") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            new_urls.append(url)
            candidates.append(Candidate(
                "news", r.get("source") or url.split("/")[2],
                r.get("title", "").strip(), (r.get("body") or "").strip(), url,
            ))
    storage.mark_seen(user_id, new_urls)
    storage.save()
    return candidates


async def make_post(user_id: int, c: Candidate) -> str:
    if c.kind == "channel":
        origin = Source(title=f"{c.label}: {c.title}", url=c.url, snippet="", text=c.text[:3500])
        sources = await research([c.title[:100]], cfg.search_region, "w", pinned=[origin])
        return await create_draft(user_id, c.title, sources, rewrite_source=c.text, origin=c.url)
    origin = Source(title=c.title, url=c.url, snippet=c.text)
    sources = await research([c.title[:100]], cfg.search_region, "w", pinned=[origin])
    return await create_draft(user_id, c.title, sources, origin=c.url)


async def run_cycle(bot: Bot, user_id: int, manual: bool = False) -> None:
    """Один проход автопилота для пользователя."""
    async with _lock:  # не перегружаем бесплатные лимиты параллельными проходами
        u = storage.user(user_id)
        u["auto"]["last_run"] = time.time()
        storage.save()

        candidates = await collect(user_id)
        if not candidates:
            if manual:
                await bot.send_message(user_id, "🤖 Ничего нового не нашёл. Проверю позже.")
            return

        limit = u["auto"].get("posts") or cfg.auto_posts_per_run
        try:
            picked = await pick(u, candidates, limit)
        except openai.APIError as e:
            await _notify(bot, user_id, "🤖 Автопилот: " + llm_error_text(e))
            return
        if not picked:
            if manual:
                await bot.send_message(user_id, f"🤖 Нашёл {len(candidates)} новых материалов, "
                                                "но ничего стоящего для поста.")
            return

        for c in picked:
            try:
                draft_id = await make_post(user_id, c)
            except (openai.APIError, ValueError) as e:
                msg = llm_error_text(e) if isinstance(e, openai.APIError) else str(e)
                await _notify(bot, user_id, "🤖 Автопилот: " + msg)
                break
            draft = storage.get_draft(user_id, draft_id)
            storage.add_recent(user_id, draft["topic"])
            storage.save()

            channel = user_channel(user_id)
            if u["auto"]["publish"] and channel:
                err = await publish(bot, channel, draft["text"], draft.get("image"))
                if not err:
                    await _notify(bot, user_id, f"🤖 Опубликовал автопост в {channel}\n"
                                                f"Тема: {draft['topic']}\nИсточник: {c.url}")
                    continue
                await _notify(bot, user_id, f"🤖 {err}")
            await _notify(bot, user_id, f"🤖 Новое в {c.label}. Вот черновик:")
            await send_draft(bot, user_id, user_id, draft_id)


async def pick(u: dict, candidates: list[Candidate], limit: int) -> list[Candidate]:
    # не отправляем нейросети слишком много — сначала посты каналов, потом новости
    candidates = sorted(candidates, key=lambda c: c.kind != "channel")[:25]
    idx = await writer.pick([c.brief() for c in candidates], u["recent"], u["topics"],
                            u["style_note"], limit)
    return [candidates[i] for i in idx]


async def _notify(bot: Bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text, parse_mode=None, link_preview_options=NO_PREVIEW)
    except TelegramAPIError:
        pass


async def autopilot_loop(bot: Bot) -> None:
    last_cleanup = 0.0
    while True:
        now = time.time()
        for user_id in storage.user_ids():
            auto = storage.user(user_id)["auto"]
            if auto["enabled"] and now - auto.get("last_run", 0) >= cfg.auto_interval_min * 60:
                try:
                    await run_cycle(bot, user_id)
                except Exception:
                    log.exception("autopilot error for %s", user_id)
        if now - last_cleanup > 3600:
            images.cleanup(storage.used_images())
            last_cleanup = now
        await asyncio.sleep(60)
