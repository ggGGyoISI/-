"""Генерация постов через бесплатную нейросеть (любой OpenAI-совместимый API)."""

import json
import re
from datetime import datetime

from openai import AsyncOpenAI

from formatting import sanitize_html, visible_length
from search import Source, format_sources

FORMAT_RULES = """Форматирование — HTML для Telegram. Разрешены ТОЛЬКО теги:
<b>, <i>, <u>, <s>, <code>, <blockquote>, <tg-spoiler>, <a href="...">.
Никакого Markdown (**, __, #), никаких <p>, <br>, <ul>, <li>, <h1>. Переносы строк — обычные.
Эмодзи — только если они есть в стиле канала."""

IMAGE_RULE = """После текста поста добавь последнюю отдельную строку:
IMAGE_PROMPT: <описание иллюстрации к посту на английском, 15–40 слов: что изображено, сцена, настроение; без текста и надписей на картинке>"""

_IMAGE_LINE = re.compile(r"^\s*\**\s*IMAGE[_ ]PROMPT\s*\**\s*:\s*(.+?)\s*$", re.I | re.M)


class Writer:
    def __init__(self, base_url: str, api_key: str, model: str, max_chars: int):
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key, max_retries=3, timeout=120)
        self.model = model
        self.max_chars = max_chars

    async def _chat(self, system: str, user: str, temperature: float = 0.8) -> str:
        resp = await self.client.chat.completions.create(
            model=self.model,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (resp.choices[0].message.content or "").strip()

    @staticmethod
    def _style_block(samples: list[str], style_note: str) -> str:
        parts = []
        if style_note:
            parts.append(f"Описание стиля от автора канала:\n{style_note}")
        if samples:
            joined = "\n\n=====\n\n".join(samples[-12:])
            parts.append(
                "Примеры постов канала (копируй манеру: длину, тон, структуру, "
                f"обращение к читателю, эмодзи, хэштеги, подписи):\n\n{joined}"
            )
        if not parts:
            parts.append(
                "Примеров стиля нет — пиши живо, по делу, без канцелярита, "
                "с цепляющей первой строкой."
            )
        return "\n\n".join(parts)

    async def search_queries(self, topic: str) -> list[str]:
        today = datetime.now().strftime("%d.%m.%Y")
        raw = await self._chat(
            "Ты помогаешь искать информацию в интернете. Отвечай только JSON-массивом строк.",
            f"Сегодня {today}. Придумай 3 коротких поисковых запроса (на языке темы; "
            f"если тема международная — один запрос на английском), чтобы найти свежие "
            f"факты для поста на тему:\n{topic}",
            temperature=0.2,
        )
        return [str(q) for q in _json_list(raw)][:3] or [topic]

    async def write_post(
        self,
        topic: str,
        sources: list[Source],
        samples: list[str],
        style_note: str,
        previous: str = "",
        edit_request: str = "",
        rewrite_source: str = "",
        want_image_prompt: bool = False,
    ) -> tuple[str, str]:
        """Возвращает (текст поста в HTML, промпт для картинки)."""
        today = datetime.now().strftime("%d.%m.%Y")
        system = (
            "Ты — автор Telegram-канала. Пишешь посты строго в стилистике канала.\n\n"
            f"{self._style_block(samples, style_note)}\n\n"
            f"{FORMAT_RULES}\n\n"
            f"Длина поста — не больше {self.max_chars} символов.\n"
            "Опирайся на факты из найденных материалов; не выдумывай цифры, даты и цитаты. "
            "Если данных мало — пиши осторожнее. Ссылки на источники вставляй, только если "
            "так принято в стиле канала. Выведи ТОЛЬКО текст поста, без пояснений."
        )
        if want_image_prompt:
            system += "\n\n" + IMAGE_RULE

        user = f"Сегодня {today}.\nТема поста: {topic}\n\n"
        if rewrite_source:
            user += (
                "Исходная публикация из другого канала. Перескажи её своими словами в стиле "
                "нашего канала: не копируй фразы дословно, не упоминай исходный канал, "
                "сохрани факты, при необходимости дополни их найденными материалами:\n\n"
                f"{rewrite_source}\n\n"
            )
        if sources:
            user += f"Найденные в интернете материалы:\n\n{format_sources(sources)}\n\n"
        elif not rewrite_source:
            user += "Поиск ничего не дал — пиши на основе общих знаний, без конкретных свежих цифр.\n\n"
        if previous:
            user += f"Предыдущая версия поста:\n{previous}\n\n"
            user += (
                f"Перепиши её с учётом правок: {edit_request}"
                if edit_request
                else "Напиши другой вариант — с другим заходом и подачей."
            )

        text, image_prompt = _split_image_prompt(await self._chat(system, user))
        text = sanitize_html(_strip_fences(text))

        if visible_length(text) > 4000:
            shorter, _ = _split_image_prompt(await self._chat(
                system,
                f"Сократи этот пост до {self.max_chars} символов, сохранив стиль и HTML-теги:\n\n{text}",
                temperature=0.3,
            ))
            text = sanitize_html(_strip_fences(shorter))
        return text, image_prompt

    async def pick(self, candidates: list[str], recent: list[str], topics: list[str],
                   style_note: str, limit: int) -> list[int]:
        """Выбирает номера (с 0) самых интересных материалов для автопостинга."""
        listing = "\n\n".join(f"[{i}] {c}" for i, c in enumerate(candidates))
        recent_block = "\n".join(f"- {r}" for r in recent[-15:]) or "нет"
        raw = await self._chat(
            "Ты — главный редактор Telegram-канала. Отвечай только JSON-массивом чисел.",
            f"Тематика канала: {', '.join(topics) or style_note or 'не указана'}\n\n"
            f"Недавно уже вышли посты на темы:\n{recent_block}\n\n"
            f"Новые материалы:\n\n{listing}\n\n"
            f"Выбери до {limit} материалов для новых постов: самые интересные и важные для "
            "аудитории канала. Не бери то, что повторяет недавние посты или другой выбранный "
            "материал. Посты из отслеживаемых Telegram-каналов важнее новостей из поиска. "
            "Если подходящего нет — верни []. Ответ: JSON-массив номеров, например [0, 3].",
            temperature=0.2,
        )
        picked = []
        for x in _json_list(raw):
            if isinstance(x, int) and 0 <= x < len(candidates) and x not in picked:
                picked.append(x)
        return picked[:limit]

    async def ideas(self, niche: str, sources: list[Source], samples: list[str], style_note: str) -> list[str]:
        today = datetime.now().strftime("%d.%m.%Y")
        raw = await self._chat(
            "Ты — контент-менеджер Telegram-канала.\n\n"
            f"{self._style_block(samples, style_note)}",
            f"Сегодня {today}. Тематика канала: {niche}\n\n"
            f"Свежие новости и материалы:\n\n{format_sources(sources) or 'нет'}\n\n"
            "Предложи 6 идей для постов, которые зайдут аудитории этого канала — "
            "опирайся на свежие новости. Каждая идея — одна строка (до 150 символов), "
            "формат: '1. ...'. Без вступлений и пояснений.",
        )
        ideas = [
            re.sub(r"^\s*\d+[.)]\s*", "", line).replace("**", "").strip()
            for line in raw.splitlines()
            if re.match(r"^\s*\d+[.)]", line)
        ]
        return ideas[:6]


def _json_list(raw: str) -> list:
    m = re.search(r"\[.*\]", raw, re.S)
    try:
        value = json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _split_image_prompt(text: str) -> tuple[str, str]:
    matches = list(_IMAGE_LINE.finditer(text))
    if not matches:
        return text, ""
    prompt = matches[-1].group(1).strip().strip("<>\"'")
    return _IMAGE_LINE.sub("", text).strip(), prompt


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", text, re.S)
    return m.group(1).strip() if m else text
