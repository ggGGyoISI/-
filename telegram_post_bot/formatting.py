"""Приведение ответа нейросети к HTML, который гарантированно примет Telegram."""

import html
import re
from html.parser import HTMLParser

ALLOWED = {
    "b": "b", "strong": "b", "i": "i", "em": "i", "u": "u", "ins": "u",
    "s": "s", "strike": "s", "del": "s", "code": "code", "pre": "pre",
    "blockquote": "blockquote", "tg-spoiler": "tg-spoiler", "a": "a",
}
BLOCK_BREAK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "ul", "ol"}
SKIP_CONTENT = {"script", "style"}
KNOWN_TAGS = sorted(set(ALLOWED) | BLOCK_BREAK | SKIP_CONTENT | {"h5", "h6", "span"}, key=len, reverse=True)
# «<», за которым не идёт известный тег, — это просто символ в тексте (например «a < b»)
_LONE_LT = re.compile(r"<(?!/?(?:%s)\b)" % "|".join(map(re.escape, KNOWN_TAGS)), re.I)


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_CONTENT:
            self.skip += 1
            return
        if tag == "li":
            self.out.append("\n• ")
            return
        if tag in BLOCK_BREAK:
            self.out.append("\n")
            return
        name = ALLOWED.get(tag)
        if not name:
            return
        if name == "a":
            href = dict(attrs).get("href") or ""
            if not href.startswith(("http://", "https://", "tg://")):
                return
            self.out.append(f'<a href="{html.escape(href, quote=True)}">')
        else:
            self.out.append(f"<{name}>")
        self.stack.append(name)

    def handle_endtag(self, tag):
        if tag in SKIP_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if tag in BLOCK_BREAK:
            self.out.append("\n")
            return
        name = ALLOWED.get(tag)
        if name and name in self.stack:
            # закрываем всё, что было открыто внутри, чтобы не нарушить вложенность
            while self.stack:
                top = self.stack.pop()
                self.out.append(f"</{top}>")
                if top == name:
                    break

    def handle_data(self, data):
        if self.skip:
            return
        self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def _markdown_to_html(text: str) -> str:
    # на случай, если модель всё же ответила Markdown-разметкой
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"<i>\1</i>", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.M)
    return text


def sanitize_html(text: str) -> str:
    p = _Sanitizer()
    p.feed(_LONE_LT.sub("&lt;", _markdown_to_html(text)))
    p.close()
    out = p.result()
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def visible_length(text: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def to_plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))
