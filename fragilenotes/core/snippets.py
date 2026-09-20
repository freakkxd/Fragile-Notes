"""Сниппеты FragileNotes: хранение trigger -> expansion, автозамена при вводе + палитра.

Хранение: vault/_System/snippets.json  (JSON dict trigger->expansion).
Автозамена: при вводе Tab/Space если слово перед курсором == trigger — заменить на expansion.
Палитра команд: список сниппетов с поиском и вставкой.

Формат файла: {"omw": "On my way!", "todo": "- [ ] {{date}}", ...}
Поддерживаются переменные как в templates: {{date}}, {{time}}, {{title}}, {{uuid}}.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

SNIPPETS_REL = "_System/snippets.json"

# Дефолтные примеры — показываются если файл отсутствует
DEFAULT_SNIPPETS: dict[str, str] = {
    "omw": "On my way!",
    "brb": "Be right back",
    "todo": "- [ ] ",
    "date": "{{date}}",
    "time": "{{time}}",
    "shrug": "¯\\_(ツ)_/¯",
}

# Regex для переменных (как в templates)
_VAR_RE = re.compile(r"\{\{\s*(date|time|title|uuid)(?::([^}]+))?\s*\}\}")

# Валидация триггера: непустой, без пробелов/переводов строк, длина 1..64
_TRIGGER_RE = re.compile(r"^\S{1,64}$")


def _format_date(d: datetime, fmt: str | None) -> str:
    if fmt is None or not fmt.strip():
        return d.date().isoformat()
    raw = fmt.strip()
    days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    months_ru = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    out = raw
    if "dddd" in out:
        out = out.replace("dddd", days_ru[d.weekday()])
    if "MMMM" in out:
        out = out.replace("MMMM", months_ru[d.month - 1])
    for token, val in [
        ("YYYY", f"{d.year:04d}"),
        ("YY", f"{d.year % 100:02d}"),
        ("MM", f"{d.month:02d}"),
        ("DD", f"{d.day:02d}"),
        ("HH", f"{d.hour:02d}"),
        ("mm", f"{d.minute:02d}"),
        ("ss", f"{d.second:02d}"),
    ]:
        out = out.replace(token, val)
    return out


def _format_time(d: datetime, fmt: str | None) -> str:
    if fmt is None or not fmt.strip():
        return d.strftime("%H:%M")
    raw = fmt.strip()
    out = raw.replace("HH", f"{d.hour:02d}").replace("mm", f"{d.minute:02d}").replace("ss", f"{d.second:02d}")
    out = out.replace("YYYY", f"{d.year:04d}").replace("MM", f"{d.month:02d}").replace("DD", f"{d.day:02d}")
    return out


def render_expansion(
    expansion: str,
    title: str = "",
    *,
    now: datetime | None = None,
    uuid_str: str | None = None,
) -> str:
    """Подставить переменные {{date}}, {{time}}, {{title}}, {{uuid}} в expansion."""
    if expansion is None:
        return ""
    d = now or datetime.now()
    uid = uuid_str if uuid_str is not None else str(uuid.uuid4())
    title_val = str(title or "")

    def repl(m: re.Match) -> str:
        kind = m.group(1)
        fmt = m.group(2)
        if kind == "date":
            return _format_date(d, fmt)
        if kind == "time":
            return _format_time(d, fmt)
        if kind == "title":
            return title_val
        if kind == "uuid":
            return uid
        return m.group(0)

    return _VAR_RE.sub(repl, expansion)


# ── утилы путей ──────────────────────────────────────────────────

def get_snippets_path(settings: dict) -> Path:
    """Путь к vault/_System/snippets.json."""
    root = settings.get("vault_root") if isinstance(settings, dict) else None
    if not root:
        root = str(Path.home() / "desktop")
    return Path(str(root)) / SNIPPETS_REL


def get_snippets_dir(settings: dict) -> Path:
    return get_snippets_path(settings).parent


def ensure_snippets_dir(settings: dict) -> Path:
    p = get_snippets_dir(settings)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── CRUD ─────────────────────────────────────────────────────────

def load_snippets(settings: dict | None = None) -> dict[str, str]:
    """Загрузить сниппеты из JSON. Если файла нет — вернуть копию DEFAULT_SNIPPETS."""
    if settings is None:
        settings = {}
    path = get_snippets_path(settings)
    if not path.is_file():
        return dict(DEFAULT_SNIPPETS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_SNIPPETS)
    if not isinstance(data, dict):
        return dict(DEFAULT_SNIPPETS)
    out: dict[str, str] = {}
    for k, v in data.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        kk = k.strip()
        if not kk or not _TRIGGER_RE.match(kk):
            continue
        out[kk] = v
    return out


def save_snippets(settings: dict, snippets: dict[str, str]) -> Path:
    """Сохранить dict trigger->expansion в JSON (атомарно)."""
    ensure_snippets_dir(settings)
    path = get_snippets_path(settings)
    # валидация перед записью
    clean: dict[str, str] = {}
    for k, v in snippets.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        kk = k.strip()
        if not kk or not _TRIGGER_RE.match(kk):
            continue
        clean[kk] = v
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def list_snippets(settings: dict | None = None) -> list[tuple[str, str]]:
    """Отсортированный список (trigger, expansion)."""
    data = load_snippets(settings or {})
    return sorted(data.items(), key=lambda kv: kv[0].lower())


def list_triggers(settings: dict | None = None) -> list[str]:
    return [k for k, _ in list_snippets(settings)]


def get_snippet(settings: dict, trigger: str) -> str | None:
    data = load_snippets(settings)
    return data.get(trigger.strip())


def add_snippet(settings: dict, trigger: str, expansion: str) -> dict[str, str]:
    """Добавить/обновить сниппет, сохранить, вернуть новый dict."""
    trigger = trigger.strip()
    if not trigger or not _TRIGGER_RE.match(trigger):
        raise ValueError(f"invalid trigger {trigger!r}: 1..64 non-space chars")
    if not isinstance(expansion, str):
        raise TypeError("expansion must be str")
    data = load_snippets(settings)
    data[trigger] = expansion
    save_snippets(settings, data)
    return data


def update_snippet(settings: dict, trigger: str, expansion: str) -> dict[str, str]:
    return add_snippet(settings, trigger, expansion)


def delete_snippet(settings: dict, trigger: str) -> bool:
    """Удалить триггер, вернуть True если был."""
    trigger = trigger.strip()
    data = load_snippets(settings)
    if trigger not in data:
        return False
    data.pop(trigger, None)
    save_snippets(settings, data)
    return True


# алиасы
remove_snippet = delete_snippet


# ── расширение ───────────────────────────────────────────────────

def expand_trigger(
    trigger: str,
    settings: dict | None = None,
    snippets: dict[str, str] | None = None,
    *,
    title: str = "",
    now: datetime | None = None,
    uuid_str: str | None = None,
) -> str | None:
    """Вернуть expansion для trigger (с рендером переменных) или None."""
    trig = trigger.strip()
    if not trig:
        return None
    data = snippets if snippets is not None else load_snippets(settings or {})
    raw = data.get(trig)
    if raw is None:
        return None
    return render_expansion(raw, title=title, now=now, uuid_str=uuid_str)


def find_trigger_at_offset(text: str, offset: int, snippets: dict[str, str]) -> tuple[str, int, int] | None:
    """Найти триггер слева от offset.

    Берём токен \\S+ перед курсором (до пробела/начала строки). Если токен == trigger — возвращаем.
    Returns: (trigger, start, end) где start/end — индексы в text.
    """
    if offset < 0 or offset > len(text):
        return None
    left = text[:offset]
    m = re.search(r"(\S+)$", left)
    if not m:
        return None
    token = m.group(1)
    if token in snippets:
        start = offset - len(token)
        return token, start, offset
    return None


def try_expand_at_offset(
    text: str,
    offset: int,
    snippets: dict[str, str],
    *,
    title: str = "",
    now: datetime | None = None,
    uuid_str: str | None = None,
) -> tuple[str, int] | None:
    """Попытаться расширить сниппет в точке offset.

    Returns: (new_text, new_offset) если найден триггер, иначе None.
    new_offset — позиция курсора после вставки.
    """
    found = find_trigger_at_offset(text, offset, snippets)
    if found is None:
        return None
    trigger, start, end = found
    expansion = render_expansion(snippets[trigger], title=title, now=now, uuid_str=uuid_str)
    new_text = text[:start] + expansion + text[end:]
    new_offset = start + len(expansion)
    return new_text, new_offset


def apply_expansion_to_text(
    text: str,
    snippets: dict[str, str] | None = None,
    settings: dict | None = None,
    *,
    title: str = "",
    now: datetime | None = None,
) -> str:
    """Применить все триггеры как whole-word замены (для batch/preview).

    Заменяет только отдельные токены, разделённые пробельными символами.
    """
    data = snippets if snippets is not None else load_snippets(settings or {})
    if not data or not text:
        return text
    # сортировка по длине убыв. чтобы более длинные триггеры побеждали
    triggers = sorted(data.keys(), key=len, reverse=True)
    # build word-boundary safe regex: (?<!\\S)trigger(?!\\S) — токен окружён пробелом/началом/концом
    # нельзя использовать \\b т.к. trigger может содержать символы типа ; : .
    parts: list[str] = []
    for trig in triggers:
        parts.append(re.escape(trig))
    if not parts:
        return text
    # создаём отображение trigger->rendered
    rendered: dict[str, str] = {k: render_expansion(v, title=title, now=now) for k, v in data.items()}
    # единый regex с именованными группами нерационально — делаем поиском токенов
    # простой split с сохранением разделителей
    tokens = re.split(r"(\s+)", text)
    out: list[str] = []
    for tok in tokens:
        if tok and not tok[0].isspace() and tok in rendered:
            out.append(rendered[tok])
        else:
            out.append(tok)
    return "".join(out)


# ── палитра ──────────────────────────────────────────────────────

def get_palette_items(settings: dict | None = None) -> list[dict[str, str]]:
    """Элементы для палитры команд: [{trigger, expansion, label}, ...]."""
    data = load_snippets(settings or {})
    items: list[dict[str, str]] = []
    for trig, exp in sorted(data.items(), key=lambda kv: kv[0].lower()):
        # короткий preview expansion (до 60 символов)
        preview = exp.replace("\n", " ⏎ ")
        if len(preview) > 60:
            preview = preview[:57] + "…"
        items.append({"trigger": trig, "expansion": exp, "label": f"{trig} → {preview}"})
    return items


def search_snippets(query: str, settings: dict | None = None, snippets: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """Фильтр сниппетов по подстроке в trigger или expansion."""
    data = snippets if snippets is not None else load_snippets(settings or {})
    q = query.strip().lower()
    if not q:
        return sorted(data.items(), key=lambda kv: kv[0].lower())
    out: list[tuple[str, str]] = []
    for k, v in data.items():
        if q in k.lower() or q in v.lower():
            out.append((k, v))
    out.sort(key=lambda kv: kv[0].lower())
    return out


# ── SnippetManager (объектная обёртка) ───────────────────────────

class SnippetManager:
    """Объектная обёртка для DI в UI/тестах."""

    def __init__(self, settings: dict | None = None) -> None:
        self.settings: dict[str, Any] = dict(settings) if isinstance(settings, dict) else {}
        self._cache: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._cache is None:
            self._cache = load_snippets(self.settings)
        return self._cache

    def reload(self) -> dict[str, str]:
        self._cache = load_snippets(self.settings)
        return self._cache

    def all(self) -> dict[str, str]:
        return dict(self._load())

    def list(self) -> list[tuple[str, str]]:
        return sorted(self._load().items(), key=lambda kv: kv[0].lower())

    def get(self, trigger: str) -> str | None:
        return self._load().get(trigger.strip())

    def add(self, trigger: str, expansion: str) -> dict[str, str]:
        data = add_snippet(self.settings, trigger, expansion)
        self._cache = data
        return data

    def delete(self, trigger: str) -> bool:
        ok = delete_snippet(self.settings, trigger)
        if ok:
            self._cache = load_snippets(self.settings)
        return ok

    remove = delete

    def update_settings(self, settings: dict) -> None:
        self.settings = dict(settings) if isinstance(settings, dict) else {}
        self._cache = None

    def expand(self, trigger: str, title: str = "", now: datetime | None = None) -> str | None:
        return expand_trigger(trigger, self.settings, self._load(), title=title, now=now)

    def try_expand_at(self, text: str, offset: int, title: str = "") -> tuple[str, int] | None:
        return try_expand_at_offset(text, offset, self._load(), title=title)

    def palette(self) -> list[dict[str, str]]:
        return get_palette_items(self.settings)


__all__ = [
    "SNIPPETS_REL",
    "DEFAULT_SNIPPETS",
    "get_snippets_path",
    "get_snippets_dir",
    "ensure_snippets_dir",
    "load_snippets",
    "save_snippets",
    "list_snippets",
    "list_triggers",
    "get_snippet",
    "add_snippet",
    "update_snippet",
    "delete_snippet",
    "remove_snippet",
    "render_expansion",
    "expand_trigger",
    "find_trigger_at_offset",
    "try_expand_at_offset",
    "apply_expansion_to_text",
    "get_palette_items",
    "search_snippets",
    "SnippetManager",
]
