"""Рабочее пространство: состав/порядок иконок ribbon и размеры окна.

Чистые функции без GTK — модель из настроек, описания элементов и операции
над списком иконок. Всё, что относится к отрисовке, живёт в app.py (ribbon)
и settings_view.py (редактор).
"""

from __future__ import annotations

from pathlib import Path

from ..config import DEFAULT_WORKSPACE

VIEW_ICONS = {
    "home": "🧬", "runner": "🔧", "tasks": "✅", "habits": "🌱", "daily": "📅", "calendar": "🗓",
    "review": "🔍", "srs": "🧠", "media": "🎬", "voice": "🎙️", "video": "📹", "files": "📁", "templates": "📑", "graph": "🕸", "canvas": "🎨", "whiteboard": "🧊", "kanban": "📋", "database": "🗄️", "slides": "🎞️", "mindmap": "🗺", "mermaid_live": "🧜", "latex_live": "∑", "tags": "#", "ai_chat": "🤖", "pomodoro": "🍅", "analytics": "📊", "plugin_store": "🧩", "theme_editor": "🎨", "settings": "⚙️",
}

VIEW_TITLES = {
    "home": "Рабочий стол",
    "runner": "AO Tasks",
    "tasks": "Сегодня",
    "habits": "Привычки",
    "daily": "Daily",
    "calendar": "Календарь",
    "review": "Обзор",
    "srs": "Повторение",
    "media": "Media · FreakyDB",
    "voice": "Голосовые",
    "video": "Видео",
    "files": "Заметки",
    "templates": "Шаблоны",
    "graph": "Граф",
    "canvas": "Canvas",
    "whiteboard": "Whiteboard",
    "kanban": "Kanban",
    "database": "База данных",
    "slides": "Презентация",
    "mindmap": "Mind Map",
    "mermaid_live": "Mermaid Live",
    "latex_live": "LaTeX Live",
    "tags": "Теги",
    "ai_chat": "AI Чат",
    "pomodoro": "Pomodoro",
    "analytics": "Аналитика",
    "plugin_store": "Магазин плагинов",
    "theme_editor": "Редактор темы",
    "settings": "Настройки",
}

# Встроенные действия ribbon (cmd). Иконка: символическая для кнопки,
# символ — для редактора наглядно.
CMD_TIPS = {
    "sidebar": "Сайдбар",
    "refresh": "Обновить статус",
    "search": "Поиск по заметкам (Ctrl+P)",
    "new": "Новая заметка",
    "save": "Сохранить",
}

CMD_ICONS = {
    "sidebar": "sidebar-show-symbolic",
    "refresh": "view-refresh-symbolic",
    "search": "edit-find-symbolic",
    "new": "document-new-symbolic",
    "save": "document-save-symbolic",
}

CMD_CHARS = {
    "sidebar": "☰", "refresh": "⟳", "search": "🔍",
    "new": "➕", "save": "💾",
}


def default_ribbon() -> list[dict]:
    """Копия дефолтного состава/порядка иконок ribbon."""
    return [dict(it) for it in DEFAULT_WORKSPACE["ribbon"]]


def is_ribbon_outdated(ribbon: list[dict]) -> bool:
    """Старый ribbon — без graph/voice/ai_chat/canvas/kanban/calendar/database/templates/review или без сепараторов (<3).

    Короткие фрагменты (<7) не считаем устаревшими — они используются в тестах
    фильтрации/нормализации.
    """
    if not isinstance(ribbon, list) or not ribbon:
        return False
    if len(ribbon) < 7:
        return False
    has_graph = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "graph"
        for it in ribbon
    )
    if not has_graph:
        return True
    has_voice = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "voice"
        for it in ribbon
    )
    if not has_voice:
        return True
    has_video = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "video"
        for it in ribbon
    )
    if not has_video:
        return True
    has_ai = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "ai_chat"
        for it in ribbon
    )
    if not has_ai:
        return True
    has_canvas = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "canvas"
        for it in ribbon
    )
    if not has_canvas:
        return True
    has_kanban = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "kanban"
        for it in ribbon
    )
    if not has_kanban:
        return True
    has_calendar = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "calendar"
        for it in ribbon
    )
    if not has_calendar:
        return True
    has_database = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "database"
        for it in ribbon
    )
    if not has_database:
        return True
    has_templates = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "templates"
        for it in ribbon
    )
    if not has_templates:
        return True
    has_slides = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "slides"
        for it in ribbon
    )
    if not has_slides:
        return True
    has_review = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "review"
        for it in ribbon
    )
    if not has_review:
        return True
    has_pomodoro = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "pomodoro"
        for it in ribbon
    )
    if not has_pomodoro:
        return True
    has_habits = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "habits"
        for it in ribbon
    )
    if not has_habits:
        return True
    has_srs = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "srs"
        for it in ribbon
    )
    if not has_srs:
        return True
    has_mindmap = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "mindmap"
        for it in ribbon
    )
    if not has_mindmap:
        return True
    has_whiteboard = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "whiteboard"
        for it in ribbon
    )
    if not has_whiteboard:
        return True
    has_analytics = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "analytics"
        for it in ribbon
    )
    if not has_analytics:
        return True
    has_plugin_store = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "plugin_store"
        for it in ribbon
    )
    if not has_plugin_store:
        return True
    has_mermaid_live = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "mermaid_live"
        for it in ribbon
    )
    if not has_mermaid_live:
        return True
    has_latex_live = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "latex_live"
        for it in ribbon
    )
    if not has_latex_live:
        return True
    has_theme_editor = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "theme_editor"
        for it in ribbon
    )
    if not has_theme_editor:
        return True
    sep_count = sum(1 for it in ribbon if isinstance(it, dict) and it.get("t") == "sep")
    if sep_count < 3:
        return True
    return False


def migrate_ribbon(ribbon: list[dict]) -> list[dict]:
    """Миграция старого ribbon в DEFAULT_WORKSPACE, сохраняя open-элементы."""
    open_items: list[dict] = []
    for it in ribbon:
        if not isinstance(it, dict) or it.get("t") != "open":
            continue
        path = str(it.get("path") or "").strip()
        if not path:
            continue
        open_items.append({
            "t": "open",
            "id": it.get("id") or f"open-{len(open_items)}",
            "icon": str(it.get("icon") or "📄")[:2],
            "tip": str(it.get("tip") or path).strip() or path,
            "path": path,
        })
    base: list[dict] = [dict(it) for it in DEFAULT_WORKSPACE["ribbon"]]
    if not open_items:
        return base
    last_sep = -1
    for idx, it in enumerate(base):
        if it.get("t") == "sep":
            last_sep = idx
    insert_at = last_sep if last_sep != -1 else len(base)
    for offset, oi in enumerate(open_items):
        base.insert(insert_at + offset, oi)
    return base


# Алиасы для совместимости
_is_ribbon_outdated = is_ribbon_outdated
_migrate_ribbon = migrate_ribbon


SIDEBAR_WIDTH_DEFAULT = 276
SIDEBAR_WIDTH_MIN = 232
SIDEBAR_WIDTH_MAX = 400
SIDEBAR_COLLAPSED_WIDTH = 44  # только ribbon, как в Obsidian при hide


def _clamp_sidebar_width(value) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return SIDEBAR_WIDTH_DEFAULT
    return max(SIDEBAR_WIDTH_MIN, min(SIDEBAR_WIDTH_MAX, v))


def normalize(settings: dict) -> dict:
    """Достаёт/чинит workspace из настроек. Пустой/битый → дефолт.

    Автоматически мигрирует старый ribbon (без graph/сепараторов) в
    DEFAULT_WORKSPACE, сохраняя пользовательские open-элементы и порядок.
    """
    ws = settings.get("workspace") or {}
    raw_ribbon = ws.get("ribbon") or []
    # Миграция DEFAULT_WORKSPACE если ribbon устарел
    if isinstance(raw_ribbon, list) and is_ribbon_outdated(raw_ribbon):
        raw_ribbon = migrate_ribbon(raw_ribbon)
    items: list[dict] = []
    for it in (raw_ribbon or []):
        if not isinstance(it, dict):
            continue
        t = it.get("t")
        if t == "sep":
            if items and items[-1].get("t") == "sep":
                continue
            items.append({"t": "sep"})
        elif t == "view":
            if it.get("id") in VIEW_ICONS:
                items.append({"t": "view", "id": it["id"]})
        elif t == "cmd":
            if it.get("id") in CMD_TIPS:
                items.append({"t": "cmd", "id": it["id"]})
        elif t == "open":
            path = str(it.get("path") or "").strip()
            if path:
                items.append({
                    "t": "open",
                    "id": it.get("id") or f"open-{len(items)}",
                    "icon": str(it.get("icon") or "📄")[:2],
                    "tip": str(it.get("tip") or path).strip() or path,
                    "path": path,
                })
    while items and items[0].get("t") == "sep":
        items.pop(0)
    if not items:
        items = default_ribbon()
    sidebar_width = ws.get("sidebar_width")
    if sidebar_width is None:
        sidebar_width = SIDEBAR_WIDTH_DEFAULT
    else:
        sidebar_width = _clamp_sidebar_width(sidebar_width)
    return {
        "ribbon": items,
        "window_width": ws.get("window_width"),
        "window_height": ws.get("window_height"),
        "sidebar_width": sidebar_width,
    }


def item_label(item: dict) -> str:
    if item["t"] == "view":
        return VIEW_TITLES.get(item.get("id"), item.get("id", "?"))
    if item["t"] == "cmd":
        return CMD_TIPS.get(item.get("id"), item.get("id", "?"))
    if item["t"] == "open":
        return str(item.get("tip") or Path(item.get("path", "")).name)
    return "Разделитель"


def item_char(item: dict) -> str:
    if item["t"] == "view":
        return VIEW_ICONS.get(item.get("id"), "?")
    if item["t"] == "cmd":
        return CMD_CHARS.get(item.get("id"), "?")
    if item["t"] == "open":
        return str(item.get("icon") or "📄")
    return "·"


def item_kind_name(item: dict) -> str:
    return {
        "view": "Вьюха",
        "cmd": "Действие",
        "open": "Заметка",
        "sep": "Разделитель",
    }.get(item["t"], "?")


def move(items: list[dict], index: int, delta: int) -> list[dict]:
    """Сдвигает элемент на delta позиций (не выходя за границы). items мутируется."""
    if not 0 <= index < len(items):
        return items
    target = max(0, min(len(items) - 1, index + delta))
    if target == index:
        return items
    thing = items.pop(index)
    items.insert(target, thing)
    return items


def ribbon_views(items: list[dict]) -> list[str]:
    return [it["id"] for it in items if it.get("t") == "view"]
