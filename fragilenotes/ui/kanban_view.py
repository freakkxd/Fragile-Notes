"""Kanban доска для FragileNotes — как Obsidian Kanban.

3 колонки: To Do / Doing / Done. Карточки — md файлы с frontmatter `status`
в `vault/Kanban/`. Drag-n-drop между колонками через Gtk.DragSource /
Gtk.DropTarget (GTK4, Dest). Создание карточек через диалог.

Формат карточки:
    ---
    status: todo | doing | done
    title: "Название"    # опционально, fallback — stem файла
    project: "MyProject" # опционально — для swimlane по проекту
    priority: high | medium | low | critical # опционально — для swimlane по приоритету
    created: 2026-09-03
    ---
    тело заметки (описание)

Swimlanes: группировка карточек по `project` или `priority` (настраивается в toolbar).
WIP лимиты: на каждую колонку задаётся max (0 = без лимита), превышение подсвечивается
красной рамкой/бэйджем и CSS-классом `kanban-column--wip-exceeded`.

Сохранение: vault/Kanban/<slug>.md — frontmatter обновляется при перетаскивании.
Интеграция как вкладка (VIEW_TITLES/View etc) + CSS там же.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from ..vault import parse_frontmatter, serialize_frontmatter  # noqa: E402
from .widgets import empty_state, view_header  # noqa: E402

# ── константы ───────────────────────────────────────────────────────

KANBAN_DIRNAME = "Kanban"

# канонические статусы
STATUS_TODO = "todo"
STATUS_DOING = "doing"
STATUS_DONE = "done"
STATUSES = (STATUS_TODO, STATUS_DOING, STATUS_DONE)

COLUMN_DEFS: list[tuple[str, str, str]] = [
    (STATUS_TODO, "To Do", "📝"),
    (STATUS_DOING, "Doing", "🚧"),
    (STATUS_DONE, "Done", "✅"),
]

# синонимы → канонический
_STATUS_ALIASES: dict[str, str] = {
    "todo": STATUS_TODO,
    "to do": STATUS_TODO,
    "to-do": STATUS_TODO,
    "backlog": STATUS_TODO,
    "open": STATUS_TODO,
    "planned": STATUS_TODO,
    "todo ": STATUS_TODO,
    "doing": STATUS_DOING,
    "in progress": STATUS_DOING,
    "in_progress": STATUS_DOING,
    "in-progress": STATUS_DOING,
    "wip": STATUS_DOING,
    "progress": STATUS_DOING,
    "active": STATUS_DOING,
    "done": STATUS_DONE,
    "completed": STATUS_DONE,
    "complete": STATUS_DONE,
    "finished": STATUS_DONE,
    "closed": STATUS_DONE,
    "archive": STATUS_DONE,
}

_SLUG_RE = re.compile(r"[^0-9A-Za-zА-Яа-яёЁ\-_ ]+")

# ── swimlanes ───────────────────────────────────────────────────────
SWIMLANE_NONE = "none"
SWIMLANE_PROJECT = "project"
SWIMLANE_PRIORITY = "priority"
SWIMLANES = (SWIMLANE_NONE, SWIMLANE_PROJECT, SWIMLANE_PRIORITY)
SWIMLANE_LABELS: dict[str, str] = {
    SWIMLANE_NONE: "Без группировки",
    SWIMLANE_PROJECT: "По проекту",
    SWIMLANE_PRIORITY: "По приоритету",
}
SWIMLANE_DD_LABELS = ["Без группировки", "По проекту", "По приоритету"]
_SWIMLANE_DD_TO_MODE = {
    0: SWIMLANE_NONE,
    1: SWIMLANE_PROJECT,
    2: SWIMLANE_PRIORITY,
}
_SWIMLANE_MODE_TO_DD = {v: k for k, v in _SWIMLANE_DD_TO_MODE.items()}

# ── WIP лимиты ──────────────────────────────────────────────────────
# 0 = без лимита. По ТЗ пример: To Do max 5.
DEFAULT_WIP_LIMITS: dict[str, int] = {
    STATUS_TODO: 5,
    STATUS_DOING: 3,
    STATUS_DONE: 0,
}

_PRIORITY_ALIASES: dict[str, str] = {
    "critical": "critical",
    "срочно": "critical",
    "urgent": "critical",
    "high": "high",
    "высокий": "high",
    "высокая": "high",
    "medium": "medium",
    "normal": "medium",
    "средний": "medium",
    "средняя": "medium",
    "low": "low",
    "низкий": "low",
    "низкая": "low",
}
_PRIORITY_ORDER: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "none": 99,
}
_PRIORITY_LABELS: dict[str, str] = {
    "critical": "🔴 Critical",
    "high": "🟠 High",
    "medium": "🟡 Medium",
    "low": "🟢 Low",
    "none": "— Без приоритета",
}


def _normalize_priority(raw: object) -> str:
    if raw is None:
        return "none"
    s = str(raw).strip().lower()
    if not s:
        return "none"
    s = s.strip("\"' ")
    if s in _PRIORITY_ALIASES:
        return _PRIORITY_ALIASES[s]
    # первое слово
    first = s.split()[0] if s else s
    return _PRIORITY_ALIASES.get(first, s if s in _PRIORITY_ORDER else "none")


def _kanban_root(settings: dict) -> Path:
    return Path(str(settings.get("vault_root") or Path.home() / "desktop")) / KANBAN_DIRNAME


def _ensure_kanban_dir(settings: dict) -> Path:
    p = _kanban_root(settings)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── WIP helpers ─────────────────────────────────────────────────────

def _get_wip_limits(settings: dict) -> dict[str, int]:
    """Вернуть WIP лимиты из settings или DEFAULT. 0 = без лимита."""
    out: dict[str, int] = dict(DEFAULT_WIP_LIMITS)
    raw = settings.get("kanban_wip_limits")
    if isinstance(raw, dict):
        for k in STATUSES:
            if k in raw:
                try:
                    v = int(raw[k])
                    out[k] = max(0, min(99, v))
                except (TypeError, ValueError):
                    pass
    # также поддержка плоских ключей kanban_wip_todo etc (совместимость)
    for k in STATUSES:
        fk = f"kanban_wip_{k}"
        if fk in settings:
            try:
                out[k] = max(0, min(99, int(settings[fk])))
            except (TypeError, ValueError):
                pass
    return out


def _set_wip_limit(settings: dict, status: str, limit: int) -> None:
    status = _normalize_status(status)
    limit = max(0, min(99, int(limit)))
    cur = _get_wip_limits(settings)
    cur[status] = limit
    settings["kanban_wip_limits"] = dict(cur)
    # синхронизируем плоские ключи для простоты инспекции
    for k in STATUSES:
        settings[f"kanban_wip_{k}"] = cur[k]
    # persist best-effort
    try:
        from ..config import save_settings

        save_settings(settings)
    except Exception:
        pass


def _is_wip_exceeded(count: int, limit: int) -> bool:
    return limit > 0 and count > limit


# ── swimlane helpers ────────────────────────────────────────────────

def _get_swimlane_mode(settings: dict) -> str:
    raw = str(settings.get("kanban_swimlane") or SWIMLANE_NONE).strip().lower()
    if raw in SWIMLANES:
        return raw
    # алиасы
    if raw in ("проект", "projects", "by_project"):
        return SWIMLANE_PROJECT
    if raw in ("приоритет", "priorities", "by_priority"):
        return SWIMLANE_PRIORITY
    return SWIMLANE_NONE


def _set_swimlane_mode(settings: dict, mode: str) -> None:
    mode = str(mode).strip().lower()
    if mode not in SWIMLANES:
        mode = SWIMLANE_NONE
    settings["kanban_swimlane"] = mode
    try:
        from ..config import save_settings

        save_settings(settings)
    except Exception:
        pass


def _swimlane_value(card: KanbanCard, mode: str) -> str:
    if mode == SWIMLANE_PROJECT:
        p = (card.project or "").strip()
        return p if p else "Без проекта"
    if mode == SWIMLANE_PRIORITY:
        pri = _normalize_priority(card.priority)
        # красиво отображаем
        if pri in _PRIORITY_LABELS:
            return _PRIORITY_LABELS[pri]
        return pri or "— Без приоритета"
    return "Все"


def _group_by_swimlane(cards: list[KanbanCard], mode: str) -> dict[str, list[KanbanCard]]:
    if mode == SWIMLANE_NONE:
        return {"Все": list(cards)}
    groups: dict[str, list[KanbanCard]] = defaultdict(list)
    for c in cards:
        key = _swimlane_value(c, mode)
        groups[key].append(c)
    return dict(groups)


def _sorted_swimlane_keys(keys: list[str], mode: str) -> list[str]:
    if mode == SWIMLANE_PRIORITY:
        # сортируем по приоритету critical → low → none ; внутри алфавит
        def prio_rank(k: str) -> int:
            # k уже label вроде "🔴 Critical" — извлекаем ключ
            low = k.lower()
            if "critical" in low:
                return 0
            if "high" in low or "высокий" in low:
                return 1
            if "medium" in low or "средний" in low or "normal" in low:
                return 2
            if "low" in low or "низкий" in low:
                return 3
            if "без приоритет" in low:
                return 99
            return 50

        return sorted(keys, key=lambda k: (prio_rank(k), k.lower()))
    if mode == SWIMLANE_PROJECT:
        # "Без проекта" в конец
        def proj_key(k: str) -> tuple[int, str]:
            if k == "Без проекта":
                return (1, k.lower())
            return (0, k.lower())

        return sorted(keys, key=proj_key)
    return sorted(keys, key=lambda s: s.lower())


def _normalize_status(raw: object) -> str:
    """Любой status → todo/doing/done. Неизвестный → todo."""
    if raw is None:
        return STATUS_TODO
    s = str(raw).strip().lower()
    if not s:
        return STATUS_TODO
    # убрать кавычки
    s = s.strip("\"' ")
    if s in _STATUS_ALIASES:
        return _STATUS_ALIASES[s]
    # прямые значения
    if s in STATUSES:
        return s
    # попробуем первые слова
    first = s.split()[0] if s else s
    return _STATUS_ALIASES.get(first, STATUS_TODO)


def _slugify(title: str, fallback: str = "card") -> str:
    t = (title or fallback).strip()
    if not t:
        t = fallback
    # транслит простой: оставляем буквы/цифры, пробел → -, нижний регистр
    t = _SLUG_RE.sub("", t)
    t = re.sub(r"\s+", "-", t).strip("-_")
    t = t.lower() or fallback
    # ограничим длину
    if len(t) > 64:
        t = t[:64].rstrip("-_")
    # префикс времени для уникальности если короткий
    return t


def _unique_path(root: Path, slug: str) -> Path:
    base = root / f"{slug}.md"
    if not base.exists():
        return base
    for i in range(2, 200):
        cand = root / f"{slug}-{i}.md"
        if not cand.exists():
            return cand
    # fallback с timestamp
    return root / f"{slug}-{int(time.time())}.md"


@dataclass(slots=True)
class KanbanCard:
    path: Path
    title: str
    status: str
    body: str
    fm: dict
    project: str | None = None
    priority: str | None = None


def _read_card(path: Path) -> KanbanCard | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fm, body = parse_frontmatter(text)
    raw_status = fm.get("status") if isinstance(fm, dict) else None
    status = _normalize_status(raw_status)
    # title: frontmatter title > первый заголовок > stem
    title = ""
    if isinstance(fm, dict) and fm.get("title"):
        title = str(fm["title"]).strip()
    if not title:
        # первый markdown заголовок
        m = re.search(r"^#\s+(.+)", body, re.MULTILINE)
        if m:
            title = m.group(1).strip()
    if not title:
        title = path.stem.replace("-", " ").replace("_", " ").strip() or path.stem
    # project / priority
    project = None
    priority = None
    if isinstance(fm, dict):
        if fm.get("project"):
            project = str(fm["project"]).strip() or None
        if fm.get("priority"):
            priority = str(fm["priority"]).strip() or None
        # также поддержка альтернативных ключей
        if project is None and fm.get("projects"):
            project = str(fm["projects"]).strip() or None
        if priority is None and fm.get("prio"):
            priority = str(fm["prio"]).strip() or None
    return KanbanCard(path=path, title=title, status=status, body=body, fm=dict(fm) if isinstance(fm, dict) else {}, project=project, priority=priority)


def scan_cards(settings: dict) -> list[KanbanCard]:
    """Все md карточки из vault/Kanban/ (рекурсивно)."""
    root = _kanban_root(settings)
    if not root.is_dir():
        return []
    out: list[KanbanCard] = []
    try:
        for p in root.rglob("*.md"):
            if p.is_file():
                c = _read_card(p)
                if c is not None:
                    out.append(c)
    except OSError:
        pass
    # сортировка: свежие сверху по mtime
    try:
        out.sort(key=lambda c: c.path.stat().st_mtime, reverse=True)
    except OSError:
        pass
    return out


def _write_card_status(path: Path, new_status: str) -> bool:
    """Обновить только frontmatter status, сохраняя тело."""
    ns = _normalize_status(new_status)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    fm, body = parse_frontmatter(text)
    if not isinstance(fm, dict):
        fm = {}
    fm["status"] = ns
    # гарантируем title если отсутствует — для читаемости kanban
    if "title" not in fm or not str(fm["title"]).strip():
        fm["title"] = path.stem
    content = serialize_frontmatter(fm) + body.lstrip("\n")
    try:
        path.write_text(content, encoding="utf-8")
        return True
    except OSError:
        return False


def create_card(settings: dict, title: str, status: str = STATUS_TODO, body: str = "", project: str | None = None, priority: str | None = None) -> Path | None:
    """Создать новую карточку в vault/Kanban/. Возвращает путь."""
    title = (title or "").strip()
    if not title:
        return None
    root = _ensure_kanban_dir(settings)
    slug = _slugify(title)
    path = _unique_path(root, slug)
    fm = {
        "status": _normalize_status(status),
        "title": title,
        "created": time.strftime("%Y-%m-%d"),
    }
    if project and str(project).strip():
        fm["project"] = str(project).strip()
    if priority and str(priority).strip():
        fm["priority"] = _normalize_priority(priority) if _normalize_priority(priority) != "none" else str(priority).strip()
    b = (body or "").strip()
    if b and not b.startswith("#"):
        # не навязываем заголовок — тело как есть
        pass
    content = serialize_frontmatter(fm) + (b + "\n" if b else "")
    try:
        path.write_text(content, encoding="utf-8")
        # инвалидируем vault кэши чтобы files_view увидел
        try:
            from ..services import vault as svc

            svc.invalidate_vault_cache()
        except Exception:
            pass
        return path
    except OSError:
        return None


# ── extra CSS для swimlanes + WIP ───────────────────────────────────
_KANBAN_EXTRA_CSS_LOADED = False

def _ensure_kanban_extra_css() -> None:
    global _KANBAN_EXTRA_CSS_LOADED
    if _KANBAN_EXTRA_CSS_LOADED:
        return
    _KANBAN_EXTRA_CSS_LOADED = True
    css = b"""
    .kanban-swimlane {
        background-color: transparent;
        border: 1px solid var(--ao-border-subtle);
        border-radius: var(--ao-radius-md);
        padding: 6px 6px 8px 6px;
        margin-bottom: 10px;
    }
    .kanban-swimlane__header {
        padding: 6px 8px 8px 8px;
        border-bottom: 1px solid var(--ao-border-subtle);
        margin-bottom: 6px;
    }
    .kanban-swimlane__title {
        font-size: 0.82em;
        font-weight: 700;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        color: var(--ao-text);
    }
    .kanban-swimlane__count {
        font-size: 0.75em;
        color: var(--ao-text-muted);
        border: 1px solid var(--ao-border-subtle);
        background: var(--ao-surface-control);
        border-radius: 999px;
        padding: 1px 8px;
    }
    .kanban-swimlane__cols {
        background: transparent;
    }
    .kanban-column--wip-exceeded {
        border-color: rgba(220,130,145,0.55) !important;
        box-shadow: inset 0 0 0 1px rgba(220,130,145,0.25), 0 0 0 2px rgba(220,130,145,0.12) !important;
        background-color: rgba(220,130,145,0.06) !important;
    }
    .kanban-column--wip-exceeded .kanban-column__head {
        border-bottom-color: rgba(220,130,145,0.35);
    }
    .kanban-column__count--exceeded {
        background-color: rgba(220,130,145,0.18) !important;
        border-color: rgba(220,130,145,0.45) !important;
        color: #f87171 !important;
    }
    .kanban-wip-badge {
        font-size: 0.68em;
        font-weight: 600;
        color: var(--ao-text-muted);
        opacity: 0.9;
    }
    .kanban-wip-badge--exceeded {
        color: #f87171;
        font-weight: 700;
    }
    .kanban-card__chips { margin-top: 2px; }
    .kanban-chip {
        border-radius: 999px;
        padding: 1px 7px;
        font-size: 0.68em;
        font-weight: 600;
        border: 1px solid var(--ao-border-subtle);
        background: var(--ao-surface-control);
        color: var(--ao-text-muted);
    }
    .kanban-chip--project {
        border-color: rgba(130,168,255,0.28);
        background: rgba(130,168,255,0.10);
        color: #a8c8ff;
    }
    .kanban-chip--priority-critical { border-color: rgba(220,130,145,0.45); background: rgba(220,130,145,0.14); color: #fca5a5; }
    .kanban-chip--priority-high { border-color: rgba(230,175,110,0.45); background: rgba(230,175,110,0.14); color: #facc8a; }
    .kanban-chip--priority-medium { border-color: rgba(130,168,255,0.32); background: rgba(130,168,255,0.10); color: #a8c8ff; }
    .kanban-chip--priority-low { border-color: rgba(120,210,150,0.32); background: rgba(120,210,150,0.10); color: #9ce8b4; }
    .kanban-wip-ctl { background: var(--ao-surface-glass-soft); border: 1px solid var(--ao-border-subtle); border-radius: var(--ao-radius-sm); padding: 4px 8px; }
    .kanban-wip-ctl label { font-size: 0.72em; font-weight: 600; color: var(--ao-text-muted); }
    """
    try:
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
    except Exception:
        pass


# ── виджеты ──────────────────────────────────────────────────────────


class KanbanCardWidget(Gtk.Box):
    """Карточка: DragSource (MOVE), клик — редактировать/открыть."""

    def __init__(self, card: KanbanCard, on_edit=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["kanban-card"])
        self.card = card
        self._on_edit = on_edit

        # header: title
        title_lbl = Gtk.Label(
            label=card.title,
            css_classes=["kanban-card__title"],
            halign=Gtk.Align.START,
            xalign=0,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            ellipsize=Pango.EllipsizeMode.NONE,
        )
        title_lbl.set_max_width_chars(28)
        self.append(title_lbl)

        # chips: project / priority (для swimlane-подсказки)
        chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["kanban-card__chips"])
        has_chip = False
        if card.project:
            pl = Gtk.Label(label=card.project, css_classes=["kanban-chip", "kanban-chip--project"], ellipsize=Pango.EllipsizeMode.MIDDLE)
            pl.set_max_width_chars(14)
            chips.append(pl)
            has_chip = True
        if card.priority:
            pri_norm = _normalize_priority(card.priority)
            if pri_norm != "none":
                pri_lbl = Gtk.Label(label=str(card.priority), css_classes=["kanban-chip", f"kanban-chip--priority-{pri_norm}"])
                chips.append(pri_lbl)
                has_chip = True
        if has_chip:
            self.append(chips)

        # preview тела (до 140 символов, одна строка)
        preview = (card.body or "").strip().replace("\n", " ")
        # убрать markdown заголовки
        preview = re.sub(r"^#+\s*", "", preview)
        preview = " ".join(preview.split())
        if preview:
            if len(preview) > 120:
                preview = preview[:120].rstrip() + "…"
            sub = Gtk.Label(
                label=preview,
                css_classes=["kanban-card__preview", "dim-label"],
                halign=Gtk.Align.START,
                xalign=0,
                wrap=True,
                wrap_mode=Pango.WrapMode.WORD_CHAR,
            )
            sub.set_max_width_chars(32)
            self.append(sub)

        # footer: путь + статус бейдж
        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        badge = Gtk.Label(label=card.status, css_classes=["kanban-badge", f"kanban-badge--{card.status}"])
        foot.append(badge)
        name = Gtk.Label(
            label=card.path.name,
            css_classes=["dim-label", "kanban-card__file"],
            halign=Gtk.Align.START,
            xalign=0,
            ellipsize=Pango.EllipsizeMode.MIDDLE,
            hexpand=True,
        )
        foot.append(name)
        self.append(foot)

        # ── DragSource (Gtk.DragSource) — источник перетаскивания ──
        drag = Gtk.DragSource()
        try:
            drag.set_actions(Gdk.DragAction.MOVE)
        except Exception:
            pass

        # Сохраняем путь как строку; prepare возвращает ContentProvider
        p_str = str(card.path)

        def _on_prepare(_src, _x, _y):
            # GTK4: Gdk.ContentProvider.new_for_value(GObject.Value)
            try:
                val = GObject.Value(GObject.TYPE_STRING, p_str)
                return Gdk.ContentProvider.new_for_value(val)
            except Exception:
                pass
            try:
                # fallback: bytes text/plain
                b = GLib.Bytes.new(p_str.encode("utf-8"))
                return Gdk.ContentProvider.new_for_bytes("text/plain", b)
            except Exception:
                return None

        try:
            drag.connect("prepare", _on_prepare)
        except Exception:
            pass

        def _on_drag_begin(src, _drag):
            # полупрозрачная иконка
            try:
                self.add_css_class("kanban-card--dragging")
            except Exception:
                pass

        def _on_drag_end(src, _drag, _ok):
            try:
                self.remove_css_class("kanban-card--dragging")
            except Exception:
                pass

        try:
            drag.connect("drag-begin", _on_drag_begin)
            drag.connect("drag-end", _on_drag_end)
        except Exception:
            pass

        self.add_controller(drag)
        self._drag_source = drag  # type: ignore[attr-defined]

        # ── клик — редактировать ──
        click = Gtk.GestureClick.new()
        click.set_button(1)

        def _on_released(_gest, _n, _x, _y):
            if self._on_edit is not None:
                try:
                    self._on_edit(self.card)
                except Exception:
                    pass

        click.connect("released", _on_released)
        self.add_controller(click)

        # accessibility
        try:
            self.set_focusable(True)
            self.update_property(Gtk.AccessibleProperty.LABEL, card.title)
        except Exception:
            pass


class KanbanColumn(Gtk.Box):
    """Одна колонка To Do / Doing / Done — DropTarget (Dest) + список карт + WIP."""

    def __init__(self, status: str, title: str, icon: str, on_drop=None, on_add=None, wip_limit: int = 0) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["kanban-column", f"kanban-column--{status}"])
        self.status = status
        self.title = title
        self.icon = icon
        self._on_drop = on_drop
        self._on_add = on_add
        self._wip_limit: int = max(0, int(wip_limit or 0))
        self._count: int = 0

        # header
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["kanban-column__head"])
        head.append(Gtk.Label(label=icon, css_classes=["kanban-column__icon"]))
        head.append(Gtk.Label(label=title, css_classes=["kanban-column__title"], hexpand=True, halign=Gtk.Align.START, xalign=0))
        # WIP бейдж "3 / 5" или "3"
        self._count_lbl = Gtk.Label(label="0", css_classes=["pill", "kanban-column__count"])
        head.append(self._count_lbl)
        self._wip_lbl = Gtk.Label(label="", css_classes=["kanban-wip-badge"])
        self._wip_lbl.set_visible(False)
        head.append(self._wip_lbl)
        self._head = head
        self.append(head)

        # scroller + list
        self._list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["kanban-list"])
        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True, css_classes=["kanban-scroller"])
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self._list)
        # не раздвигать окно
        try:
            scroller.set_propagate_natural_width(False)
        except AttributeError:
            pass
        self.append(scroller)

        # кнопка добавления внизу колонки
        add_btn = Gtk.Button(label="+  Карточка", css_classes=["kanban-add-btn"])
        add_btn.set_tooltip_text(f"Создать карточку в «{title}»")
        add_btn.connect("clicked", self._on_add_clicked)
        self.append(add_btn)

        # ── DropTarget (Gtk.DropTarget) — Dest, принимает перетаскивание из любой колонки ──
        # GTK4 Dest: Gtk.DropTarget.new(type, actions)
        drop: Gtk.DropTarget | None = None
        try:
            drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        except Exception:
            try:
                drop = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)  # type: ignore[arg-type]
            except Exception:
                drop = None
        if drop is not None:
            drop.connect("enter", self._on_enter)
            drop.connect("leave", self._on_leave)
            drop.connect("drop", self._on_drop_enter)
            # также принимаем bytes fallback (text/plain)
            self.add_controller(drop)
            self._drop_target = drop  # type: ignore[attr-defined]
        # запасной DropTarget для bytes (если DragSource использовал bytes)
        try:
            drop_b = Gtk.DropTarget.new(Gio.Bytes, Gdk.DragAction.MOVE)  # type: ignore[arg-type]
            drop_b.connect("drop", self._on_drop_bytes)
            self.add_controller(drop_b)
        except Exception:
            pass

        # применяем начальный WIP
        self.set_wip_limit(self._wip_limit)

    def _on_add_clicked(self, _btn) -> None:
        if self._on_add is not None:
            try:
                self._on_add(self.status)
            except Exception:
                pass

    def _on_enter(self, _target, _x, _y) -> Gdk.DragAction:
        try:
            self.add_css_class("kanban-column--drag-over")
        except Exception:
            pass
        return Gdk.DragAction.MOVE

    def _on_leave(self, _target) -> None:
        try:
            self.remove_css_class("kanban-column--drag-over")
        except Exception:
            pass

    def _on_drop_enter(self, _target, value, _x, _y) -> bool:
        try:
            self.remove_css_class("kanban-column--drag-over")
        except Exception:
            pass
        # value — строка пути
        path_str = None
        if isinstance(value, str):
            path_str = value
        elif isinstance(value, GObject.Value):
            try:
                path_str = value.get_string()  # type: ignore[attr-defined]
            except Exception:
                path_str = str(value)
        else:
            try:
                path_str = str(value)
            except Exception:
                path_str = None
        if not path_str:
            return False
        if self._on_drop is not None:
            try:
                # вернуть True если обработано
                res = self._on_drop(self.status, path_str)
                return bool(res) if res is not None else True
            except Exception:
                return False
        return False

    def _on_drop_bytes(self, _target, value, _x, _y) -> bool:
        # value — Gio.Bytes / GLib.Bytes
        path_str = None
        try:
            if hasattr(value, "get_data"):
                data = value.get_data()
                if isinstance(data, (bytes, bytearray)):
                    path_str = data.decode("utf-8", errors="replace")
                elif data is not None:
                    path_str = bytes(data).decode("utf-8", errors="replace")
        except Exception:
            pass
        if not path_str:
            try:
                path_str = str(value)
            except Exception:
                return False
        # bytes → содержит путь, проверим что это похоже на путь
        if "/" not in path_str and "\\" not in path_str and not path_str.endswith(".md"):
            # мусор — не обрабатываем
            return False
        if self._on_drop is not None:
            try:
                res = self._on_drop(self.status, path_str.strip())
                return bool(res) if res is not None else True
            except Exception:
                return False
        return False

    def clear(self) -> None:
        while (child := self._list.get_first_child()) is not None:
            self._list.remove(child)
        self._count = 0
        self._update_count_label()

    def set_count(self, n: int) -> None:
        self._count = max(0, int(n))
        self._update_count_label()

    def get_count(self) -> int:
        return self._count

    def set_wip_limit(self, limit: int) -> None:
        self._wip_limit = max(0, min(99, int(limit or 0)))
        # wip бейдж
        if self._wip_limit > 0:
            self._wip_lbl.set_text(f"WIP {self._wip_limit}")
            self._wip_lbl.set_visible(True)
            # tooltip с подсказкой
            try:
                self._wip_lbl.set_tooltip_text(f"Лимит WIP: {self._wip_limit}")
            except Exception:
                pass
        else:
            self._wip_lbl.set_text("")
            self._wip_lbl.set_visible(False)
        self._update_count_label()

    def get_wip_limit(self) -> int:
        return self._wip_limit

    def _update_count_label(self) -> None:
        limit = self._wip_limit
        n = self._count
        exceeded = _is_wip_exceeded(n, limit)
        # текст: "3 / 5" если лимит, иначе "3"
        if limit > 0:
            txt = f"{n} / {limit}"
            if exceeded:
                txt += " ⚠"
        else:
            txt = str(n)
        self._count_lbl.set_text(txt)
        # подсветка превышения
        try:
            if exceeded:
                self.add_css_class("kanban-column--wip-exceeded")
                self._count_lbl.add_css_class("kanban-column__count--exceeded")
                self._wip_lbl.add_css_class("kanban-wip-badge--exceeded")
                self.set_tooltip_text(f"Превышен WIP лимит: {n} > {limit}")
            else:
                self.remove_css_class("kanban-column--wip-exceeded")
                self._count_lbl.remove_css_class("kanban-column__count--exceeded")
                self._wip_lbl.remove_css_class("kanban-wip-badge--exceeded")
                self.set_tooltip_text(f"WIP: {n}" + (f" / {limit}" if limit > 0 else ""))
        except Exception:
            pass

    def add_card_widget(self, w: Gtk.Widget) -> None:
        self._list.append(w)


# ── основная вьюха ──────────────────────────────────────────────────

class KanbanView(Gtk.Box):
    """Вкладка Kanban: 3 колонки, drag-n-drop, создание + swimlanes + WIP."""

    def __init__(self, settings: dict, on_open=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        _ensure_kanban_extra_css()
        self.settings = settings
        self.on_open = on_open
        self._kanban_root = _ensure_kanban_dir(settings)
        self._columns: dict[str, KanbanColumn] = {}
        self._cards: list[KanbanCard] = []
        self._swimlane_mode: str = _get_swimlane_mode(settings)
        self._wip_limits: dict[str, int] = _get_wip_limits(settings)
        # контейнеры для swimlane-рендера
        self._board_container: Gtk.Box | None = None
        self._cols_box: Gtk.Box | None = None
        self._cols_scroller: Gtk.ScrolledWindow | None = None
        self._lanes_scroller: Gtk.ScrolledWindow | None = None
        self._lanes_box: Gtk.Box | None = None
        self._build_ui()
        self.reload()

    def _build_ui(self) -> None:
        self.append(view_header("📋", "Kanban", "Доска как в Obsidian Kanban — перетаскивай карточки между колонками"))
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["toolbar", "kanban-toolbar"])
        toolbar.set_margin_start(14)
        toolbar.set_margin_end(14)

        new_btn = Gtk.Button(label="＋ Новая карточка", css_classes=["suggested-action"], tooltip_text="Создать карточку (в To Do)")
        new_btn.connect("clicked", lambda *_: self._dialog_new_card(STATUS_TODO))
        toolbar.append(new_btn)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Обновить доску")
        refresh.connect("clicked", lambda *_: self.reload(force=True))
        toolbar.append(refresh)

        open_folder = Gtk.Button(label="Открыть папку", tooltip_text="Открыть vault/Kanban в файловом менеджере")
        open_folder.connect("clicked", self._on_open_folder)
        toolbar.append(open_folder)

        # ── swimlane селектор ──
        sw_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["kanban-wip-ctl"])
        sw_box.append(Gtk.Label(label="Swimlane:", css_classes=["dim-label"]))
        self._swim_dd = Gtk.DropDown.new_from_strings(SWIMLANE_DD_LABELS)
        try:
            self._swim_dd.set_selected(_SWIMLANE_MODE_TO_DD.get(self._swimlane_mode, 0))
        except Exception:
            self._swim_dd.set_selected(0)
        self._swim_dd.set_tooltip_text("Группировка карточек по проекту/приоритету")
        self._swim_dd.connect("notify::selected", self._on_swim_changed)
        sw_box.append(self._swim_dd)
        toolbar.append(sw_box)

        # ── WIP контролы ──
        wip_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["kanban-wip-ctl"])
        wip_box.append(Gtk.Label(label="WIP:", css_classes=["dim-label"]))
        self._wip_spins: dict[str, Gtk.SpinButton] = {}
        for status, short in [(STATUS_TODO, "To Do"), (STATUS_DOING, "Doing"), (STATUS_DONE, "Done")]:
            wip_box.append(Gtk.Label(label=short, css_classes=["dim-label"]))
            adj = Gtk.Adjustment(value=float(self._wip_limits.get(status, 0)), lower=0, upper=99, step_increment=1, page_increment=5)
            spin = Gtk.SpinButton(adjustment=adj, climb_rate=1, digits=0, css_classes=["kanban-wip-spin"])
            spin.set_tooltip_text(f"WIP лимит для {short} (0 = без лимита)")
            spin.set_size_request(56, -1)
            spin.connect("value-changed", self._on_wip_changed, status)
            self._wip_spins[status] = spin
            wip_box.append(spin)
        toolbar.append(wip_box)

        self._stats = Gtk.Label(label="", css_classes=["dim-hint", "kanban-stats"], hexpand=True, halign=Gtk.Align.END, xalign=1)
        toolbar.append(self._stats)
        self.append(toolbar)

        # единый контейнер доски — внутри либо 3 колонки, либо swimlane-вертикаль
        self._board_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True, vexpand=True)
        self._board_container.set_margin_start(14)
        self._board_container.set_margin_end(14)
        self._board_container.set_margin_bottom(14)
        self.append(self._board_container)

        # ── режим без swimlane: 3 колонки горизонталь ──
        cols = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, hexpand=True, vexpand=True)
        for status, title, icon in COLUMN_DEFS:
            col = KanbanColumn(status, title, icon, on_drop=self._on_card_dropped, on_add=self._dialog_new_card, wip_limit=self._wip_limits.get(status, 0))
            self._columns[status] = col
            col.set_hexpand(True)
            col.set_vexpand(True)
            cols.append(col)
        self._cols_box = cols
        cols_scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        cols_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        cols_scroller.set_child(cols)
        try:
            cols_scroller.set_propagate_natural_width(False)
        except AttributeError:
            pass
        self._cols_scroller = cols_scroller

        # ── режим swimlane: вертикальный scroller с lanes ──
        lanes_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True, vexpand=True)
        lanes_scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        lanes_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        lanes_scroller.set_child(lanes_box)
        try:
            lanes_scroller.set_propagate_natural_width(False)
        except AttributeError:
            pass
        self._lanes_box = lanes_box
        self._lanes_scroller = lanes_scroller

        # по умолчанию покажем cols (решение принимает reload)
        # пустое состояние под колонками
        self._empty = empty_state(
            "📋",
            "Доска пуста — создайте первую карточку",
            hint="Карточки — md файлы в vault/Kanban/ с frontmatter status: todo/doing/done",
            action_label="＋ Новая карточка",
            on_action=lambda: self._dialog_new_card(STATUS_TODO),
        )
        self._empty.set_visible(False)
        self.append(self._empty)

    def _on_swim_changed(self, dd, *_args) -> None:
        try:
            sel = int(dd.get_selected())
            mode = _SWIMLANE_DD_TO_MODE.get(sel, SWIMLANE_NONE)
        except Exception:
            mode = SWIMLANE_NONE
        if mode == self._swimlane_mode:
            return
        self._swimlane_mode = mode
        _set_swimlane_mode(self.settings, mode)
        self.reload()

    def _on_wip_changed(self, spin: Gtk.SpinButton, status: str) -> None:
        try:
            val = int(spin.get_value_as_int())
        except Exception:
            try:
                val = int(spin.get_value())
            except Exception:
                return
        cur = _get_wip_limits(self.settings)
        if cur.get(status, 0) == val:
            return
        _set_wip_limit(self.settings, status, val)
        self._wip_limits = _get_wip_limits(self.settings)
        # обновим лимиты на существующих колонках (без полного reload если можно)
        for col in self._columns.values():
            if col.status == status:
                col.set_wip_limit(val)
        # также в lanes — перерисовать для подсветки
        # проще перезагрузить
        self.reload()

    # ── загрузка ──────────────────────────────────────────────────
    def reload(self, force: bool = False) -> None:
        if force:
            try:
                from ..services import vault as svc

                svc.invalidate_vault_cache()
            except Exception:
                pass
        self._kanban_root = _ensure_kanban_dir(self.settings)
        self._wip_limits = _get_wip_limits(self.settings)
        self._swimlane_mode = _get_swimlane_mode(self.settings)
        # синхронизируем спинбатоны без сигнала
        if hasattr(self, "_wip_spins"):
            for st, lim in self._wip_limits.items():
                sp = self._wip_spins.get(st)
                if sp is not None:
                    try:
                        # блокируем notify чтобы не триггерить _on_wip_changed
                        sp.handler_block_by_func(self._on_wip_changed)  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    try:
                        if int(sp.get_value_as_int()) != lim:
                            sp.set_value(float(lim))
                    except Exception:
                        pass
                    try:
                        sp.handler_unblock_by_func(self._on_wip_changed)  # type: ignore[attr-defined]
                    except Exception:
                        pass
        # синхронизируем swim dd
        if hasattr(self, "_swim_dd"):
            try:
                want = _SWIMLANE_MODE_TO_DD.get(self._swimlane_mode, 0)
                if int(self._swim_dd.get_selected()) != want:
                    self._swim_dd.set_selected(want)
            except Exception:
                pass

        cards = scan_cards(self.settings)
        self._cards = cards

        # stats buckets глобально
        buckets_global: dict[str, list[KanbanCard]] = {s: [] for s in STATUSES}
        for c in cards:
            buckets_global.setdefault(c.status, buckets_global[STATUS_TODO]).append(c)
        total = len(cards)

        # очистить контейнеры
        assert self._board_container is not None
        # убрать все дети board_container
        while (child := self._board_container.get_first_child()) is not None:
            self._board_container.remove(child)
        # также очистить колонки (для повторного использования)
        for col in self._columns.values():
            col.clear()
            # обновим лимиты
            col.set_wip_limit(self._wip_limits.get(col.status, 0))

        has = total > 0
        self._empty.set_visible(not has)
        # всегда показываем board если есть карты, иначе пустое
        self._board_container.set_visible(has)
        if not has:
            # stats всё равно обновим
            self._stats.set_text("0 карточек")
            return

        if self._swimlane_mode == SWIMLANE_NONE:
            # классический вид — 3 колонки
            for status, _title, _icon in COLUMN_DEFS:
                col = self._columns[status]
                items = buckets_global.get(status, [])
                col.set_count(len(items))
                col.set_wip_limit(self._wip_limits.get(status, 0))
                for card in items:
                    w = KanbanCardWidget(card, on_edit=self._dialog_edit_card)
                    col.add_card_widget(w)
            # показываем scroller с cols
            assert self._cols_scroller is not None
            self._board_container.append(self._cols_scroller)
            self._cols_scroller.set_visible(True)
        else:
            # swimlanes — вертикальный список lanes, каждая с 3 колонками
            assert self._lanes_box is not None and self._lanes_scroller is not None
            # очистим lanes_box
            while (child := self._lanes_box.get_first_child()) is not None:
                self._lanes_box.remove(child)

            groups = _group_by_swimlane(cards, self._swimlane_mode)
            sorted_keys = _sorted_swimlane_keys(list(groups.keys()), self._swimlane_mode)

            for lane_key in sorted_keys:
                lane_cards = groups[lane_key]
                # lane buckets
                lane_buckets: dict[str, list[KanbanCard]] = {s: [] for s in STATUSES}
                for c in lane_cards:
                    lane_buckets.setdefault(c.status, lane_buckets[STATUS_TODO]).append(c)

                lane_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["kanban-swimlane"])
                # header lane
                head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["kanban-swimlane__header"])
                # иконка типа swimlane
                icon_txt = "📁" if self._swimlane_mode == SWIMLANE_PROJECT else "🚦"
                head.append(Gtk.Label(label=icon_txt))
                head.append(Gtk.Label(label=lane_key, css_classes=["kanban-swimlane__title"], hexpand=True, halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE))
                cnt_lbl = Gtk.Label(label=f"{len(lane_cards)}", css_classes=["kanban-swimlane__count"])
                head.append(cnt_lbl)
                # подсказка по WIP для lane (если какой-то столбец превышен — подсветим)
                lane_box.append(head)

                cols_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, hexpand=True, css_classes=["kanban-swimlane__cols"])
                for status, title, icon in COLUMN_DEFS:
                    # для lane каждая колонка — отдельный виджет с собственным WIP (тот же глобальный лимит, но подсветка per-lane)
                    # Чтобы не плодить переиспользуемые columns, создаём новые на каждую перерисовку
                    limit = self._wip_limits.get(status, 0)
                    col = KanbanColumn(status, title, icon, on_drop=self._on_card_dropped, on_add=self._dialog_new_card, wip_limit=limit)
                    col.set_hexpand(True)
                    col.set_vexpand(True)
                    items = lane_buckets.get(status, [])
                    col.set_count(len(items))
                    for card in items:
                        w = KanbanCardWidget(card, on_edit=self._dialog_edit_card)
                        col.add_card_widget(w)
                    cols_row.append(col)
                lane_box.append(cols_row)
                self._lanes_box.append(lane_box)

            self._board_container.append(self._lanes_scroller)
            self._lanes_scroller.set_visible(True)

        # stats
        todo_n = len(buckets_global[STATUS_TODO])
        doing_n = len(buckets_global[STATUS_DOING])
        done_n = len(buckets_global[STATUS_DONE])
        swim_txt = ""
        if self._swimlane_mode != SWIMLANE_NONE:
            swim_txt = f" · swimlane: {SWIMLANE_LABELS.get(self._swimlane_mode, self._swimlane_mode)}"
        wip_txt = ""
        # пометим превышения в статистике
        exceeds: list[str] = []
        for st, label in [(STATUS_TODO, "To Do"), (STATUS_DOING, "Doing"), (STATUS_DONE, "Done")]:
            lim = self._wip_limits.get(st, 0)
            cnt = len(buckets_global.get(st, []))
            if _is_wip_exceeded(cnt, lim):
                exceeds.append(f"{label} {cnt}/{lim} ⚠")
        if exceeds:
            wip_txt = " · WIP превышен: " + ", ".join(exceeds)
        self._stats.set_text(f"{total} карточек · To Do {todo_n} · Doing {doing_n} · Done {done_n}{swim_txt}{wip_txt}")

    # ── DnD ───────────────────────────────────────────────────────
    def _on_card_dropped(self, new_status: str, path_str: str) -> bool:
        """Перетаскивание — сменить frontmatter status и перезагрузить."""
        path_str = (path_str or "").strip()
        if not path_str:
            return False
        # путь может быть bytes repr — почистим
        # иногда приходит b'...' — уже декодировано
        p = Path(path_str)
        # валидация: должен быть внутри Kanban (или хотя бы md)
        if not p.is_file():
            # попробуем резолвить относительно vault_root если пришел относительный
            try:
                cand = _kanban_root(self.settings) / p.name
                if cand.is_file():
                    p = cand
                else:
                    # поиск по имени среди карт
                    for c in self._cards:
                        if c.path.name == p.name or str(c.path) == path_str:
                            p = c.path
                            break
            except Exception:
                pass
        if not p.is_file():
            self._toast(f"карточка не найдена: {Path(path_str).name}")
            return False
        # текущий статус
        cur = None
        for c in self._cards:
            if c.path == p or str(c.path) == str(p):
                cur = c.status
                break
        if cur is None:
            # прочитаем быстро
            tmp = _read_card(p)
            cur = tmp.status if tmp else STATUS_TODO
        ns = _normalize_status(new_status)
        if cur == ns:
            return True
        # WIP предупреждение (не блокируем, но тостим)
        try:
            lim = self._wip_limits.get(ns, 0)
            if lim > 0:
                # считаем сколько уже в целевой колонке
                cnt_target = sum(1 for c in self._cards if c.status == ns)
                # если перемещаем из другой колонки — +1
                if cnt_target + 1 > lim:
                    self._toast(f"⚠ WIP превышен: {ns} {cnt_target+1}/{lim} — перемещение всё равно выполнено")
        except Exception:
            pass
        ok = _write_card_status(p, ns)
        if not ok:
            self._toast(f"не удалось сохранить: {p.name}")
            return False
        try:
            from ..services import vault as svc

            svc.invalidate_vault_cache()
        except Exception:
            pass
        self._toast(f"«{p.stem}» → {ns}")
        self.reload()
        return True

    # ── создание / редактирование ─────────────────────────────────
    def _dialog_new_card(self, status: str) -> None:
        status = _normalize_status(status)
        dialog = Adw.Dialog(title="Новая карточка")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
        box.set_size_request(420, -1)
        # заголовок
        box.append(Gtk.Label(label=f"Колонка: {status}", css_classes=["dim-label"], halign=Gtk.Align.START, xalign=0))
        title_entry = Gtk.Entry(placeholder_text="Название карточки", hexpand=True, activates_default=True)
        box.append(title_entry)
        # проект / приоритет
        proj_entry = Gtk.Entry(placeholder_text="Проект (необязательно)", hexpand=True)
        box.append(proj_entry)
        prio_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        prio_row.append(Gtk.Label(label="Приоритет:", css_classes=["dim-label"]))
        prio_dd = Gtk.DropDown.new_from_strings(["—", "Low", "Medium", "High", "Critical"])
        prio_dd.set_selected(0)
        prio_row.append(prio_dd)
        box.append(prio_row)
        # выбор статуса
        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status_row.append(Gtk.Label(label="Статус:", css_classes=["dim-label"]))
        dd = Gtk.DropDown.new_from_strings(["To Do", "Doing", "Done"])
        try:
            idx = {STATUS_TODO: 0, STATUS_DOING: 1, STATUS_DONE: 2}[status]
            dd.set_selected(idx)
        except Exception:
            dd.set_selected(0)
        status_row.append(dd)
        box.append(status_row)
        body_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD, css_classes=["editor"])
        body_view.set_size_request(-1, 120)
        body_buf = body_view.get_buffer()
        sc = Gtk.ScrolledWindow(vexpand=False, css_classes=["editor-frame"])
        sc.set_child(body_view)
        sc.set_size_request(-1, 120)
        box.append(Gtk.Label(label="Описание (необязательно):", css_classes=["dim-label"], halign=Gtk.Align.START))
        box.append(sc)
        # кнопки
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Отмена", css_classes=["mod-neutral"])
        create = Gtk.Button(label="Создать", css_classes=["suggested-action", "mod-cta"])
        btn_row.append(cancel)
        btn_row.append(create)
        box.append(btn_row)
        dialog.set_child(box)
        cancel.connect("clicked", lambda *_: dialog.close())

        def _do_create(*_):
            title = title_entry.get_text().strip()
            if not title:
                self._toast("введите название")
                return
            sel = dd.get_selected()
            st = [STATUS_TODO, STATUS_DOING, STATUS_DONE][int(sel) if sel < 3 else 0]
            start, end = body_buf.get_bounds()
            body = body_buf.get_text(start, end, True)
            proj = proj_entry.get_text().strip() or None
            prio_sel = prio_dd.get_selected()
            prio_map = {0: None, 1: "low", 2: "medium", 3: "high", 4: "critical"}
            prio = prio_map.get(int(prio_sel), None)
            # WIP проверка перед созданием
            try:
                lim = self._wip_limits.get(st, 0)
                if lim > 0:
                    cnt = sum(1 for c in self._cards if c.status == st)
                    if cnt + 1 > lim:
                        self._toast(f"⚠ WIP {st} превышен: {cnt+1}/{lim}")
            except Exception:
                pass
            path = create_card(self.settings, title, st, body, project=proj, priority=prio)
            if path is None:
                self._toast("не удалось создать карточку")
                return
            dialog.close()
            self._toast(f"создана: {path.name}")
            self.reload()

        create.connect("clicked", _do_create)
        title_entry.connect("activate", _do_create)
        dialog.present(self.get_root())

    def _dialog_edit_card(self, card: KanbanCard) -> None:
        dialog = Adw.Dialog(title="Карточка")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
        box.set_size_request(480, -1)
        box.append(Gtk.Label(label=card.path.name, css_classes=["dim-label"], halign=Gtk.Align.START, ellipsize=Pango.EllipsizeMode.MIDDLE))
        title_entry = Gtk.Entry(text=card.title, hexpand=True, activates_default=True)
        box.append(Gtk.Label(label="Название:", css_classes=["dim-label"], halign=Gtk.Align.START))
        box.append(title_entry)
        # проект
        proj_entry = Gtk.Entry(text=card.project or "", placeholder_text="Проект", hexpand=True)
        box.append(Gtk.Label(label="Проект:", css_classes=["dim-label"], halign=Gtk.Align.START))
        box.append(proj_entry)
        # приоритет
        prio_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        prio_row.append(Gtk.Label(label="Приоритет:", css_classes=["dim-label"]))
        prio_dd = Gtk.DropDown.new_from_strings(["—", "Low", "Medium", "High", "Critical"])
        prio_norm = _normalize_priority(card.priority)
        prio_idx = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}.get(prio_norm, 0)
        prio_dd.set_selected(prio_idx)
        prio_row.append(prio_dd)
        box.append(prio_row)
        # статус
        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status_row.append(Gtk.Label(label="Статус:", css_classes=["dim-label"]))
        dd = Gtk.DropDown.new_from_strings(["To Do", "Doing", "Done"])
        try:
            dd.set_selected({STATUS_TODO: 0, STATUS_DOING: 1, STATUS_DONE: 2}[card.status])
        except Exception:
            dd.set_selected(0)
        status_row.append(dd)
        box.append(status_row)
        # тело
        body_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD, css_classes=["editor"])
        body_view.set_size_request(-1, 180)
        buf = body_view.get_buffer()
        buf.set_text(card.body or "")
        sc = Gtk.ScrolledWindow(vexpand=False, css_classes=["editor-frame"])
        sc.set_child(body_view)
        sc.set_size_request(-1, 180)
        box.append(Gtk.Label(label="Описание:", css_classes=["dim-label"], halign=Gtk.Align.START))
        box.append(sc)
        # кнопки: удалить / отменить / сохранить / открыть
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        del_btn = Gtk.Button(label="Удалить", css_classes=["destructive-action"])
        open_btn = Gtk.Button(label="Открыть файл")
        cancel = Gtk.Button(label="Отмена")
        save = Gtk.Button(label="Сохранить", css_classes=["suggested-action"])
        btn_row.append(del_btn)
        btn_row.append(open_btn)
        btn_row.append(cancel)
        btn_row.append(save)
        box.append(btn_row)
        dialog.set_child(box)

        cancel.connect("clicked", lambda *_: dialog.close())

        def _on_delete(*_):
            # подтверждение
            confirm = Adw.Dialog(title="Удалить?")
            cbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
            cbox.append(Gtk.Label(label=f"Удалить «{card.title}» безвозвратно?", wrap=True, xalign=0))
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
            c_cancel = Gtk.Button(label="Отмена")
            c_ok = Gtk.Button(label="Удалить", css_classes=["destructive-action"])
            row.append(c_cancel)
            row.append(c_ok)
            cbox.append(row)
            confirm.set_child(cbox)
            c_cancel.connect("clicked", lambda *_: confirm.close())
            def _do_del(*_):
                try:
                    card.path.unlink()
                except OSError as exc:
                    self._toast(f"ошибка удаления: {exc}")
                    return
                try:
                    from ..services import vault as svc

                    svc.invalidate_vault_cache()
                except Exception:
                    pass
                confirm.close()
                dialog.close()
                self._toast(f"удалена: {card.path.name}")
                self.reload()
            c_ok.connect("clicked", _do_del)
            confirm.present(self.get_root())

        del_btn.connect("clicked", _on_delete)

        def _on_open(*_):
            # делегируем на files_view если есть, иначе пробуем открыть папку
            if self.on_open is not None:
                try:
                    self.on_open(str(card.path))
                    dialog.close()
                    return
                except Exception:
                    pass
            # fallback: открыть папку
            self._on_open_folder(None)
            dialog.close()

        open_btn.connect("clicked", _on_open)

        def _on_save(*_):
            new_title = title_entry.get_text().strip() or card.title
            new_project = proj_entry.get_text().strip()
            prio_sel = prio_dd.get_selected()
            prio_map = {0: None, 1: "low", 2: "medium", 3: "high", 4: "critical"}
            new_prio = prio_map.get(int(prio_sel), None)
            sel = dd.get_selected()
            new_status = [STATUS_TODO, STATUS_DOING, STATUS_DONE][int(sel) if sel < 3 else 0]
            start, end = buf.get_bounds()
            new_body = buf.get_text(start, end, True)
            # читаем свежий frontmatter
            try:
                text = card.path.read_text(encoding="utf-8", errors="replace")
                fm, _old_body = parse_frontmatter(text)
                if not isinstance(fm, dict):
                    fm = {}
            except OSError:
                fm = {}
            fm["title"] = new_title
            fm["status"] = _normalize_status(new_status)
            # project / priority
            if new_project:
                fm["project"] = new_project
            else:
                fm.pop("project", None)
                fm.pop("projects", None)
            if new_prio:
                fm["priority"] = new_prio
            else:
                fm.pop("priority", None)
                fm.pop("prio", None)
            # не трогаем created, но обновим updated
            fm["updated"] = time.strftime("%Y-%m-%d")
            content = serialize_frontmatter(fm) + new_body.lstrip("\n")
            try:
                card.path.write_text(content, encoding="utf-8")
            except OSError as exc:
                self._toast(f"ошибка сохранения: {exc}")
                return
            # переименование файла если title сильно изменился? — не делаем автоматически,
            # чтобы не ломать ссылки; можно было бы, но оставляем имя
            try:
                from ..services import vault as svc

                svc.invalidate_vault_cache()
            except Exception:
                pass
            dialog.close()
            self._toast(f"сохранена: {card.path.name}")
            self.reload()

        save.connect("clicked", _on_save)
        dialog.present(self.get_root())

    def _on_open_folder(self, _btn) -> None:
        path = _ensure_kanban_dir(self.settings)
        try:
            Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)
        except Exception:
            try:
                # fallback xdg-open
                import subprocess

                subprocess.Popen(["xdg-open", str(path)])
            except Exception:
                self._toast(str(path))

    def _toast(self, msg: str) -> None:
        try:
            win = self.get_root()
            if win is not None and hasattr(win, "toast_overlay"):
                toast = Adw.Toast.new(msg)
                toast.set_timeout(3)
                win.toast_overlay.add_toast(toast)  # type: ignore[attr-defined]
                return
        except Exception:
            pass
        # fallback — stats
        try:
            self._stats.set_text(msg)
            GLib.timeout_add(2500, lambda: (self.reload(), False)[1])
        except Exception:
            pass
