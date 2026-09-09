"""Ядро FreakyDB: чтение media-корпуса (порт из freakyDbRunner + mediaTypes)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

FREAKYDB_REL = "04 FreakyWiki/_System/FreakyDB"
MEDIA_LITE = f"{FREAKYDB_REL}/Data/media_lite_index.jsonl"
MEDIA_STATS = f"{FREAKYDB_REL}/Data/media_stats.json"

MEDIA_TYPES = ("anime", "manga", "game", "movie", "music", "book")

MEDIA_TYPE_LABELS = {
    "anime": "Аниме",
    "manga": "Манга",
    "game": "Игры",
    "movie": "Фильмы",
    "music": "Музыка",
    "book": "Книги",
}

# ключи в корпусе могут быть и во множественном числе
MEDIA_TYPE_ALIASES = {
    "games": "game",
    "movies": "movie",
    "books": "book",
}

STATUS_LABELS = {
    "planned": "запланировано",
    "completed": "пройдено",
    "watching": "смотрю",
    "rewatching": "пересматриваю",
    "dropped": "брошено",
    "on_hold": "на паузе",
}


@dataclass
class MediaRecord:
    type: str
    path: str
    title: str
    original_title: str | None = None
    status: str | None = None
    rating: int = 0
    year: int | None = None
    cover_url: str | None = None

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status or "", self.status or "")


def load_media_lite(vault_root: Path | str) -> list[MediaRecord]:
    """Читает media_lite_index.jsonl из vault (одна JSON-строка на запись)."""
    path = Path(vault_root) / MEDIA_LITE
    records: list[MediaRecord] = []
    if not path.exists():
        return records
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                try:
                    rtype = str(obj.get("t", ""))
                    rtype = MEDIA_TYPE_ALIASES.get(rtype, rtype)
                    records.append(
                        MediaRecord(
                            type=rtype,
                            path=str(obj.get("o", "")),
                            title=str(obj.get("title") or obj.get("ot") or "?"),
                            original_title=(
                                str(obj["ot"]) if obj.get("ot") else None
                            ),
                            status=str(obj["status"]) if obj.get("status") else None,
                            rating=int(obj.get("rating") or 0),
                            year=int(obj["year"]) if obj.get("year") else None,
                            cover_url=str(obj["cover_url"]) if obj.get("cover_url") else None,
                        )
                    )
                except (ValueError, TypeError):
                    # одна битая запись (плохой int, лишние поля) не должна
                    # ронять весь корпус на 38k записей
                    continue
    except OSError:
        return []
    return records


def media_stats(vault_root: Path | str) -> dict:
    path = Path(vault_root) / MEDIA_STATS
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
