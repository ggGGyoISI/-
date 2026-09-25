"""Бесплатные картинки-обложки к постам.

Провайдеры (IMAGE_PROVIDER — один или несколько через запятую, пробуются по порядку):
  cloudflare   — Cloudflare Workers AI: FLUX.2 [klein] (по умолчанию), Leonardo Lucid Origin
                 и др. Бесплатно 10 000 «нейронов» в день ≈ 90–100 картинок FLUX.2 [klein];
  pexels       — настоящие фото со стока Pexels (бесплатный ключ, 200 запросов в час);
  pollinations — нейросеть Pollinations.ai, без регистрации, но медленнее и с лимитами;
  cover        — обложка с заголовком, рисуется локально, работает всегда;
  none         — без картинок.
auto (по умолчанию) = cloudflare, если заданы ключи, затем pollinations.
Если все провайдеры не ответили, рисуется обложка.
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

WIDTH, HEIGHT = 1280, 720      # итоговый размер обложки (16:9)
GEN_W, GEN_H = 1024, 576       # размер генерации (16:9, кратно 64) — дешевле по лимитам
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
PROVIDERS = {"cloudflare", "pexels", "pollinations", "cover"}


class ImageMaker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dir = cfg.images_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._recent_photos: list[int] = []  # чтобы фото со стока не повторялись

    @property
    def enabled(self) -> bool:
        return self.cfg.image_provider != "none"

    @property
    def has_pexels(self) -> bool:
        return bool(self.cfg.pexels_key)

    def chain(self) -> list[str]:
        value = self.cfg.image_provider
        if value == "auto":
            has_cf = self.cfg.cf_account_id and self.cfg.cf_api_token
            return (["cloudflare"] if has_cf else []) + ["pollinations"]
        return [p.strip() for p in value.split(",") if p.strip() in PROVIDERS]

    async def make(self, prompt: str, title: str, query: str = "",
                   providers: list[str] | None = None) -> str | None:
        """Создаёт картинку и возвращает путь к файлу (None — если картинки выключены).

        prompt — описание для нейросети, query — ключевые слова для фотостока,
        title — заголовок (для обложки и надписи поверх картинки).
        """
        if not self.enabled:
            return None
        path = self.dir / f"{secrets.token_hex(6)}.jpg"
        full_prompt = f"{prompt}, {self.cfg.image_style}" if self.cfg.image_style else prompt

        for provider in providers or self.chain():
            if provider == "cover":
                break
            data = None
            if provider == "cloudflare":
                data = await self._cloudflare(full_prompt)
            elif provider == "pexels":
                data = await self._pexels(query or prompt)
            elif provider == "pollinations":
                data = await self._pollinations(full_prompt)
            if data and await asyncio.to_thread(self._save, data, path, title):
                log.info("картинка: %s", provider)
                return str(path)

        await asyncio.to_thread(self._cover, title, path)
        return str(path)

    # ---------- провайдеры ----------

    async def _cloudflare(self, prompt: str) -> bytes | None:
        if not (self.cfg.cf_account_id and self.cfg.cf_api_token):
            log.warning("cloudflare: не заданы CF_ACCOUNT_ID / CF_API_TOKEN")
            return None
        model = self.cfg.cf_image_model
        url = f"https://api.cloudflare.com/client/v4/accounts/{self.cfg.cf_account_id}/ai/run/{model}"
        headers = {"Authorization": f"Bearer {self.cfg.cf_api_token}"}
        seed = random.randint(1, 2**31 - 1)
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                if "flux-2" in model:
                    # модели FLUX.2 принимают только multipart/form-data
                    fields = {"prompt": prompt[:2000], "width": GEN_W, "height": GEN_H, "seed": seed}
                    if "flux-2-dev" in model:
                        fields["steps"] = 20
                    resp = await client.post(url, headers=headers,
                                             files={k: (None, str(v)) for k, v in fields.items()})
                elif "flux-1-schnell" in model:
                    resp = await client.post(url, headers=headers,
                                             json={"prompt": prompt[:2000], "steps": 8, "seed": seed})
                else:  # Leonardo Lucid Origin / Phoenix, SDXL и др.
                    resp = await client.post(url, headers=headers, json={
                        "prompt": prompt[:2000], "width": GEN_W, "height": GEN_H,
                        "steps": 25, "seed": seed,
                    })
            if resp.status_code != 200:
                log.warning("cloudflare %s: %s %s", model, resp.status_code, resp.text[:300])
                return None
            if resp.headers.get("content-type", "").startswith("image/"):
                return resp.content  # некоторые модели отдают картинку напрямую
            result = resp.json().get("result") or {}
            image_b64 = result.get("image") if isinstance(result, dict) else None
            return base64.b64decode(image_b64) if image_b64 else None
        except (httpx.HTTPError, ValueError) as e:
            log.warning("cloudflare %s: %s", model, e)
            return None

    async def _pexels(self, query: str) -> bytes | None:
        if not self.cfg.pexels_key:
            log.warning("pexels: не задан PEXELS_API_KEY")
            return None
        query = " ".join(query.split()[:6])
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(
                    "https://api.pexels.com/v1/search",
                    params={"query": query, "orientation": "landscape", "per_page": 15},
                    headers={"Authorization": self.cfg.pexels_key},
                )
                if resp.status_code != 200:
                    log.warning("pexels: %s %s", resp.status_code, resp.text[:200])
                    return None
                photos = [p for p in resp.json().get("photos", []) if p["id"] not in self._recent_photos]
                if not photos:
                    return None
                photo = random.choice(photos[:6])
                self._recent_photos = (self._recent_photos + [photo["id"]])[-200:]
                src = photo["src"].get("large2x") or photo["src"].get("large") or photo["src"]["original"]
                img = await client.get(src)
                return img.content if img.status_code == 200 else None
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("pexels: %s", e)
            return None

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

    # ---------- обработка ----------

    def _font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        for candidate in [self.cfg.font_path, *FONT_CANDIDATES]:
            if candidate and Path(candidate).exists():
                return ImageFont.truetype(candidate, size)
        return ImageFont.load_default(size=size)

    def _save(self, data: bytes, path: Path, title: str) -> bool:
        """Приводит картинку к 16:9 1280×720, при желании пишет заголовок, сохраняет JPEG."""
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception as e:  # SVG или битый файл
            log.warning("не удалось открыть картинку: %s", e)
            return False
        img = _fit(img, WIDTH, HEIGHT)
        if self.cfg.image_title:
            img = self._title_overlay(img, title)
        img.save(path, "JPEG", quality=90)
        return True

    def _title_overlay(self, img: Image.Image, title: str) -> Image.Image:
        """Заголовок внизу картинки на тёмном градиенте — как у новостных каналов."""
        title = " ".join(title.split())[:140]
        if not title:
            return img
        shade = Image.new("L", (1, HEIGHT))
        for y in range(HEIGHT):
            t = max(0.0, (y - HEIGHT * 0.35) / (HEIGHT * 0.65))
            shade.putpixel((0, y), int(220 * t ** 1.3))
        overlay = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
        img = Image.composite(overlay, img, shade.resize((WIDTH, HEIGHT)))
        draw = ImageDraw.Draw(img)
        margin = 60
        for size in (60, 54, 48, 42, 36):
            font = self._font(size)
            lines = _wrap(draw, title, font, WIDTH - 2 * margin)
            if len(lines) <= 3:
                break
        lines = lines[:3]
        line_h = int(size * 1.22)
        y = HEIGHT - margin - len(lines) * line_h
        for line in lines:
            draw.text((margin + 2, y + 2), line, font=font, fill=(0, 0, 0))
            draw.text((margin, y), line, font=font, fill=(255, 255, 255))
            y += line_h
        return img

    def _cover(self, title: str, path: Path) -> None:
        """Обложка без нейросети: градиент + декоративные круги + заголовок поста."""
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


def _fit(img: Image.Image, width: int, height: int) -> Image.Image:
    """Обрезает по центру до нужных пропорций и масштабирует."""
    target = width / height
    w, h = img.size
    if w / h > target:
        new_w = int(h * target)
        img = img.crop(((w - new_w) // 2, 0, (w + new_w) // 2, h))
    elif w / h < target:
        new_h = int(w / target)
        img = img.crop((0, (h - new_h) // 2, w, (h + new_h) // 2))
    return img.resize((width, height), Image.LANCZOS)


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
