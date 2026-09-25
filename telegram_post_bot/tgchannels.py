"""Чтение публичных Telegram-каналов через их веб-версию t.me/s/<канал>.

Не нужен ни аккаунт, ни API-ключ, ни добавление бота в канал.
Работает для публичных каналов (у которых есть @username).
"""

import logging
import re
from dataclasses import dataclass

import httpx
import lxml.html

log = logging.getLogger(__name__)

_RESERVED = {"joinchat", "addstickers", "addemoji", "share", "proxy", "socks", "iv", "login"}
CHANNEL_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})(?:/\d+)?"
    r"|@([A-Za-z][A-Za-z0-9_]{3,31})"
)
# рекламные посты не переписываем
AD_MARKERS = re.compile(r"\berid\b|#реклама|#ad\b|на правах рекламы", re.I)
MIN_TEXT = 80
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass
class ChannelPost:
    channel: str
    id: int
    text: str
    date: str

    @property
    def url(self) -> str:
        return f"https://t.me/{self.channel}/{self.id}"


def parse_channels(text: str) -> list[str]:
    """Достаёт @username каналов из текста: @name, t.me/name, https://t.me/s/name, t.me/name/123."""
    found = []
    for m in CHANNEL_RE.finditer(text):
        name = (m.group(1) or m.group(2)).lower()  # t.me не различает регистр
        if name not in _RESERVED and name not in found:
            found.append(name)
    return found


def only_channels(text: str) -> bool:
    """True, если сообщение состоит только из ссылок на каналы."""
    rest = CHANNEL_RE.sub("", text)
    return bool(parse_channels(text)) and not re.sub(r"[\s,;]+", "", rest)


def _element_text(el) -> str:
    for br in el.iter("br"):
        br.tail = "\n" + (br.tail or "")
    return el.text_content().strip()


def parse_page(channel: str, page: str) -> list[ChannelPost]:
    doc = lxml.html.fromstring(page)
    posts: dict[int, ChannelPost] = {}
    for msg in doc.xpath('//div[@data-post]'):
        data_post = msg.get("data-post", "")
        try:
            post_id = int(data_post.rsplit("/", 1)[1])
        except (IndexError, ValueError):
            continue
        texts = msg.xpath('.//div[contains(concat(" ", normalize-space(@class), " "), " js-message_text ")]')
        text = _element_text(texts[-1]) if texts else ""
        times = msg.xpath('.//time[@datetime]')
        date = times[-1].get("datetime", "")[:16].replace("T", " ") if times else ""
        posts[post_id] = ChannelPost(channel=channel, id=post_id, text=text, date=date)
    return sorted(posts.values(), key=lambda p: p.id)


async def fetch_posts(client: httpx.AsyncClient, channel: str) -> list[ChannelPost]:
    """Последние ~20 постов канала. Пустой список — если канал закрыт/не найден."""
    try:
        resp = await client.get(
            f"https://t.me/s/{channel}",
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
        )
        # закрытые/несуществующие каналы редиректят на t.me/<name> без /s/
        if resp.status_code != 200 or "/s/" not in str(resp.url):
            return []
        return parse_page(channel, resp.text)
    except Exception as e:
        log.warning("не удалось прочитать канал %s: %s", channel, e)
        return []


def is_rewritable(post: ChannelPost) -> bool:
    return len(post.text) >= MIN_TEXT and not AD_MARKERS.search(post.text)


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20, follow_redirects=True)
