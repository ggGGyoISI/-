"""Простое хранилище в JSON-файле: стиль канала, черновики, отложенные посты, автопилот."""

import json
import secrets
from pathlib import Path
from typing import Any

MAX_SAMPLES = 30
MAX_DRAFTS = 30
MAX_SEEN_URLS = 1000
MAX_RECENT = 30


class Storage:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.data: dict[str, Any] = json.loads(self.path.read_text("utf-8"))
        else:
            self.data = {}
        self.data.setdefault("users", {})
        self.data.setdefault("scheduled", [])

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.path)

    def user(self, user_id: int) -> dict[str, Any]:
        u = self.data["users"].setdefault(str(user_id), {})
        u.setdefault("samples", [])
        u.setdefault("style_note", "")
        u.setdefault("style_sources", [])  # каналы, пересланные посты из которых = примеры стиля
        u.setdefault("channel", "")
        u.setdefault("drafts", {})
        u.setdefault("ideas", [])
        u.setdefault("images", True)
        # автопилот
        u.setdefault("auto", {"enabled": False, "publish": False, "posts": 0, "last_run": 0})
        u.setdefault("watch", {})       # канал -> id последнего увиденного поста
        u.setdefault("topics", [])      # темы для поиска новостей
        u.setdefault("seen_urls", [])   # новости, которые уже видели
        u.setdefault("recent", [])      # темы недавних постов, чтобы не повторяться
        return u

    def user_ids(self) -> list[int]:
        return [int(x) for x in self.data["users"]]

    # --- стиль ---
    def add_sample(self, user_id: int, text: str) -> int:
        u = self.user(user_id)
        text = text.strip()
        if text and text not in u["samples"]:
            u["samples"].append(text)
            u["samples"] = u["samples"][-MAX_SAMPLES:]
            self.save()
        return len(u["samples"])

    # --- автопилот ---
    def mark_seen(self, user_id: int, urls: list[str]) -> None:
        u = self.user(user_id)
        u["seen_urls"] = (u["seen_urls"] + urls)[-MAX_SEEN_URLS:]

    def add_recent(self, user_id: int, topic: str) -> None:
        u = self.user(user_id)
        u["recent"] = (u["recent"] + [topic])[-MAX_RECENT:]

    # --- черновики ---
    def add_draft(self, user_id: int, draft: dict[str, Any]) -> str:
        u = self.user(user_id)
        draft_id = secrets.token_hex(4)
        u["drafts"][draft_id] = draft
        for old in list(u["drafts"])[:-MAX_DRAFTS]:
            del u["drafts"][old]
        self.save()
        return draft_id

    def get_draft(self, user_id: int, draft_id: str) -> dict[str, Any] | None:
        return self.user(user_id)["drafts"].get(draft_id)

    def delete_draft(self, user_id: int, draft_id: str) -> None:
        self.user(user_id)["drafts"].pop(draft_id, None)
        self.save()

    def used_images(self) -> set[str]:
        used = {x.get("image") for x in self.data["scheduled"]}
        for u in self.data["users"].values():
            used |= {d.get("image") for d in u.get("drafts", {}).values()}
        return {x for x in used if x}

    # --- отложенные публикации ---
    def schedule(self, item: dict[str, Any]) -> None:
        self.data["scheduled"].append(item)
        self.save()

    def pop_due(self, now_ts: float) -> list[dict[str, Any]]:
        due = [x for x in self.data["scheduled"] if x["at"] <= now_ts]
        if due:
            self.data["scheduled"] = [x for x in self.data["scheduled"] if x["at"] > now_ts]
            self.save()
        return due

    def scheduled_for(self, user_id: int) -> list[dict[str, Any]]:
        return sorted(
            (x for x in self.data["scheduled"] if x["user_id"] == user_id),
            key=lambda x: x["at"],
        )

    def cancel_scheduled(self, user_id: int, sched_id: str) -> bool:
        before = len(self.data["scheduled"])
        self.data["scheduled"] = [
            x for x in self.data["scheduled"]
            if not (x["user_id"] == user_id and x["id"] == sched_id)
        ]
        self.save()
        return len(self.data["scheduled"]) < before
