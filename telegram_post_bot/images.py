"""Бесплатная генерация картинок к постам.

Провайдеры (IMAGE_PROVIDER):
  pollinations — нейросеть Pollinations.ai, без регистрации (по умолчанию);
  cloudflare   — Cloudflare Workers AI (FLUX), бесплатный дневной лимит, нужен аккаунт;
  cover        — обложка с заголовком, рисуется локально, работает всегда;
  none         — без картинок.
Если нейросеть недоступна, автоматически рисуется обложка.
"""

import asyncio
import base64
import hashlib
import io
import logging
import random
import secrets
from pathlib import Path
from urllib.parse import quote

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from config import Config

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 1280, 720
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]
PALETTES = [
    ((37, 38, 89), (218, 68, 83)),
    ((12, 52, 61), (46, 196, 182)),
    ((40, 24, 70), (255, 159, 67)),
    ((15, 32, 39), (44, 83, 100)),
    ((58, 28, 113), (215, 109, 119)),
    ((20, 30, 48), (36, 59, 85)),
    ((67, 20, 7), (235, 110, 35)),
]


class ImageMaker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dir = cfg.images_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self.cfg.image_provider != "none"

    async def make(self, prompt: str, title: str) -> str | None:
        """Создаёт картинку и возвращает путь к файлу (или None, если картинки выключены)."""
        if not self.enabled:
            return None
        path = self.dir / f"{secrets.token_hex(6)}.jpg"
        full_prompt = f"{prompt}, {self.cfg.image_style}" if self.cfg.image_style else prompt

        data = None
        if self.cfg.image_provider == "pollinations":
            data = await self._pollinations(full_prompt)
        elif self.cfg.image_provider == "cloudflare":
            data = await self._cloudflare(full_prompt)

        if data and await asyncio.to_thread(_save_jpeg, data, path):
            return str(path)
        # нейросеть не ответила или провайдер = cover — рисуем обложку сами
        await asyncio.to_thread(self._cover, title, path)
        return str(path)

    async def _pollinations(self, prompt: str) -> bytes | None:
        params = {"width": WIDTH, "height": HEIGHT, "seed": random.randint(1, 2**31 - 1), "nologo": "true"}
        if self.cfg.pollinations_model:
            params["model"] = self.cfg.pollinations_model
        headers = {"Authorization": f"Bearer {self.cfg.pollinations_key}"} if self.cfg.pollinations_key else {}
        urls = [self.cfg.pollinations_url, "https://image.pollinations.ai/prompt/"]
        async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
            for base in dict.fromkeys(urls):  # без дублей, порядок сохраняется
                try:
                    resp = await client.get(base + quote(prompt[:900]), params=params, headers=headers)
                    if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/"):
                        return resp.content
                    log.warning("pollinations %s: %s %s", base, resp.status_code, resp.text[:200])
                except httpx.HTTPError as e:
                    log.warning("pollinations %s: %s", base, e)
        return None

    async def _cloudflare(self, prompt: str) -> bytes | None:
        if not (self.cfg.cf_account_id and self.cfg.cf_api_token):
            log.warning("IMAGE_PROVIDER=cloudflare, но не заданы CF_ACCOUNT_ID / CF_API_TOKEN")
            return None
        url = (f"https://api.cloudflare.com/client/v4/accounts/{self.cfg.cf_account_id}"
               f"/ai/run/{self.cfg.cf_image_model}")
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.cfg.cf_api_token}"},
                    json={"prompt": prompt[:2000], "steps": 6},
                )
            if resp.status_code != 200:
                log.warning("cloudflare: %s %s", resp.status_code, resp.text[:300])
                return None
            if resp.headers.get("content-type", "").startswith("image/"):
                return resp.content  # модели SDXL отдают картинку напрямую
            image_b64 = (resp.json().get("result") or {}).get("image")  # FLUX отдаёт base64 в JSON
            return base64.b64decode(image_b64) if image_b64 else None
        except (httpx.HTTPError, ValueError) as e:
            log.warning("cloudflare: %s", e)
            return None

    def _font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        for candidate in [self.cfg.font_path, *FONT_CANDIDATES]:
            if candidate and Path(candidate).exists():
                return ImageFont.truetype(candidate, size)
        return ImageFont.load_default(size=size)

    def _cover(self, title: str, path: Path) -> None:
        """Обложка: градиент + декоративные круги + заголовок поста."""
        seed = int(hashlib.md5(title.encode()).hexdigest(), 16)
        rnd = random.Random(seed)
        top, bottom = PALETTES[seed % len(PALETTES)]

        img = Image.new("RGB", (WIDTH, HEIGHT))
        draw = ImageDraw.Draw(img)
        for y in range(HEIGHT):
            t = y / HEIGHT
            draw.line([(0, y), (WIDTH, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))

        blobs = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        bd = ImageDraw.Draw(blobs)
        for _ in range(5):
            r = rnd.randint(120, 360)
            x, y = rnd.randint(-100, WIDTH + 100), rnd.randint(-100, HEIGHT + 100)
            bd.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, rnd.randint(18, 40)))
        img = Image.alpha_composite(img.convert("RGBA"), blobs.filter(ImageFilter.GaussianBlur(40))).convert("RGB")
        draw = ImageDraw.Draw(img)

        title = " ".join(title.split())[:160] or "…"
        margin = 90
        for size in (76, 68, 60, 52, 46, 40):
            font = self._font(size)
            lines = _wrap(draw, title, font, WIDTH - 2 * margin)
            line_h = int(size * 1.25)
            if len(lines) * line_h <= HEIGHT - 2 * margin and len(lines) <= 5:
                break
        lines = lines[:5]
        block_h = len(lines) * line_h
        y = (HEIGHT - block_h) // 2
        draw.rectangle([margin - 30, y, margin - 22, y + block_h - (line_h - size)], fill=(255, 255, 255))
        for line in lines:
            draw.text((margin + 3, y + 3), line, font=font, fill=(0, 0, 0))
            draw.text((margin, y), line, font=font, fill=(255, 255, 255))
            y += line_h
        img.save(path, "JPEG", quality=90)

    def cleanup(self, keep: set[str]) -> None:
        """Удаляет картинки, на которые больше не ссылается ни черновик, ни отложенный пост."""
        keep_names = {Path(p).name for p in keep if p}
        for f in self.dir.glob("*.jpg"):
            if f.name not in keep_names:
                f.unlink(missing_ok=True)


def _save_jpeg(data: bytes, path: Path) -> bool:
    try:
        Image.open(io.BytesIO(data)).convert("RGB").save(path, "JPEG", quality=90)
        return True
    except Exception as e:  # SVG или битый файл
        log.warning("не удалось сохранить картинку: %s", e)
        return False


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines
