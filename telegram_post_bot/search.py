"""Бесплатный поиск в интернете: DuckDuckGo (без ключа) + чтение страниц."""

import asyncio
import logging
from dataclasses import dataclass

import httpx
import trafilatura
from ddgs import DDGS

log = logging.getLogger(__name__)

PAGE_CHARS = 3500  # сколько текста страницы отдаём нейросети
FETCH_PAGES = 4    # сколько страниц читаем целиком
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass
class Source:
    title: str
    url: str
    snippet: str
    date: str = ""
    text: str = ""


def ddg_search(query: str, region: str, news: bool, timelimit: str | None) -> list[dict]:
    try:
        with DDGS() as d:
            if news:
                return d.news(query, region=region, timelimit=timelimit, max_results=6)
            return d.text(query, region=region, timelimit=timelimit, max_results=6)
    except Exception as e:  # поисковик мог ничего не найти или ограничить запросы
        log.warning("DDG search failed (%s, news=%s): %s", query, news, e)
        return []


async def search(queries: list[str], region: str, timelimit: str | None = None) -> list[Source]:
    tasks = []
    for q in queries:
        tasks.append(asyncio.to_thread(ddg_search, q, region, True, timelimit or "m"))
        tasks.append(asyncio.to_thread(ddg_search, q, region, False, timelimit))
    results = await asyncio.gather(*tasks)

    seen: set[str] = set()
    sources: list[Source] = []
    for batch in results:
        for r in batch:
            url = r.get("url") or r.get("href") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append(Source(
                title=r.get("title", "").strip(),
                url=url,
                snippet=(r.get("body") or "").strip(),
                date=(r.get("date") or "")[:10],
            ))
    return sources


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str:
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        if "html" not in resp.headers.get("content-type", ""):
            return ""
        text = await asyncio.to_thread(
            trafilatura.extract, resp.text, include_comments=False, include_tables=False
        )
        return (text or "")[:PAGE_CHARS]
    except Exception as e:
        log.info("fetch failed %s: %s", url, e)
        return ""


async def research(queries: list[str], region: str, timelimit: str | None = None,
                   pinned: list[Source] | None = None) -> list[Source]:
    """Ищет по запросам и дочитывает первые страницы целиком.

    pinned — источники, которые обязательно идут первыми (например, исходная новость).
    """
    pinned = pinned or []
    pinned_urls = {s.url for s in pinned}
    found = await search(queries, region, timelimit) if queries else []
    sources = pinned + [s for s in found if s.url not in pinned_urls]
    top = [s for s in sources[:FETCH_PAGES] if not s.text and s.url.startswith("http")]
    async with httpx.AsyncClient(
        timeout=15, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        texts = await asyncio.gather(*(_fetch_text(client, s.url) for s in top))
    for s, t in zip(top, texts):
        s.text = t
    return sources[:10]


def format_sources(sources: list[Source]) -> str:
    """Текстовое представление найденного для промпта."""
    parts = []
    for i, s in enumerate(sources, 1):
        body = s.text or s.snippet
        date = f" ({s.date})" if s.date else ""
        parts.append(f"[{i}] {s.title}{date}\n{s.url}\n{body}")
    return "\n\n---\n\n".join(parts)
