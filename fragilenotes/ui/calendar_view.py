"""Calendar view + heatmap для FragileNotes — как Obsidian Calendar.

Сетка месяца (кастом Grid, не Gtk.Calendar — гибче стилизовать heatmap),
heatmap интенсивности daily notes, клик по дате открывает/создаёт daily note,
данные из vault `daily_folder` (обычно ``02 Daily/YYYY-MM-DD.md``),
интеграция как вкладка.

Интеграция задач:
- отображение задач на календарных днях (due/scheduled/routine_next_date)
- перетаскивание задач на даты через Gtk.DragSource / Gtk.DropTarget (GTK4)
- панель нераспределённых задач (unscheduled) как DragSource
- дроп на день → перенос due_date/scheduled_date/routine_next_date + save
"""

from __future__ import annotations

import calendar
import datetime
import os
import re
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from ..paths import resolve_paths  # noqa: E402
from .widgets import view_header  # noqa: E402

# ── helpers: daily template (порт из daily_view) ─────────────────────

_DAILY_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


def _daily_slug(d: datetime.date) -> str:
    return d.isoformat()


def _daily_dir(settings: dict) -> Path:
    root = Path(str(settings.get("vault_root") or Path.home() / "desktop"))
    folder = str(settings.get("daily_folder") or "02 Daily")
    return root / folder


def _resolve_template(template: str) -> str | None:
    p = Path(template)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def _apply_template(tpl: str, d: datetime.date) -> str:
    days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    months_ru = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    out = tpl
    out = out.replace("{{date:YYYY-MM-DD}}", d.isoformat())
    out = out.replace("{{date:YYYY}}", f"{d.year:04d}")
    out = out.replace("{{date:MM}}", f"{d.month:02d}")
    out = out.replace("{{date:DD}}", f"{d.day:02d}")
    out = out.replace("{{date:dddd, D MMMM}}", f"{days_ru[d.weekday()]}, {d.day} {months_ru[d.month - 1]}")
    out = re.sub(r"\{\{date:[^}]*\}\}", d.isoformat(), out)
    return out


def _word_count(text: str) -> int:
    return len([w for w in re.split(r"\s+", text) if w.strip()])


# ── интенсивность (heatmap) ──────────────────────────────────────────

def _intensity_from_text(text: str) -> int:
    """0..4 по объёму daily-заметки (как GitHub heatmap)."""
    if not text or not text.strip():
        return 1  # существует но пусто — минимальный
    wc = _word_count(text)
    if wc < 30:
        return 1
    if wc < 120:
        return 2
    if wc < 400:
        return 3
    return 4


def scan_daily_intensity(settings: dict) -> dict[datetime.date, int]:
    """Сканирует `daily_folder` и возвращает {date: level 1..4}."""
    ddir = _daily_dir(settings)
    out: dict[datetime.date, int] = {}
    if not ddir.is_dir():
        return out
    try:
        with os.scandir(ddir) as it:
            for e in it:
                if not e.is_file() or not e.name.lower().endswith(".md"):
                    continue
                if not _DAILY_ISO_RE.match(e.name):
                    # допускаем также YYYY-MM-DD без проверки строгости
                    stem = e.name[:-3]
                    try:
                        d = datetime.date.fromisoformat(stem)
                    except ValueError:
                        continue
                else:
                    try:
                        d = datetime.date.fromisoformat(e.name[:-3])
                    except ValueError:
                        continue
                try:
                    text = Path(e.path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                out[d] = _intensity_from_text(text)
    except OSError:
        pass
    return out


def scan_vault_counts(settings: dict) -> dict[datetime.date, int]:
    """Кол-во заметок vault, изменённых в каждый день (для усиления heatmap)."""
    try:
        from ..paths import resolve_paths as _rp
        from ..services.vault import _iter_notes  # type: ignore

        root = _rp(settings).root
        counts: dict[datetime.date, int] = {}
        for p, mt in _iter_notes(root):
            try:
                d = datetime.date.fromtimestamp(mt)
            except Exception:
                continue
            counts[d] = counts.get(d, 0) + 1
        return counts
    except Exception:
        return {}


def combined_intensity(daily: dict[datetime.date, int], vault_counts: dict[datetime.date, int]) -> dict[datetime.date, int]:
    """Объединяет daily-уровень и vault-активность (максимум). Vault 5+ → 4."""
    out = dict(daily)
    for d, cnt in vault_counts.items():
        # vault-бонус: 1→1, 2→2, 3-4→3, 5+→4
        if cnt >= 5:
            lvl = 4
        elif cnt >= 3:
            lvl = 3
        elif cnt >= 2:
            lvl = 2
        elif cnt >= 1:
            lvl = 1
        else:
            lvl = 0
        if d in out:
            out[d] = max(out[d], lvl)
        else:
            # показываем активность даже без daily — но приглушённо
            # только если cnt >=2 (иначе шум)
            if cnt >= 2:
                out[d] = lvl
    return out


# ── tasks: helpers ───────────────────────────────────────────────────

def _task_display_date(task) -> datetime.date | None:  # type: ignore[no-untyped-def]
    """Дата, на которой задача должна отображаться в календаре."""
    try:
        # bill → due_date, routine → routine_next_date, regular → due or scheduled
        if getattr(task, "is_bill", False):
            return task.due_date  # type: ignore[return-value]
        if getattr(task, "is_routine", False):
            return task.routine_next_date  # type: ignore[return-value]
        # regular
        d = getattr(task, "due_date", None)
        if callable(d):
            d = d
        # properties return date or None
        due = task.due_date if hasattr(task, "due_date") else None  # type: ignore
        sched = task.scheduled_date if hasattr(task, "scheduled_date") else None  # type: ignore
        # для property вызов уже выполнен выше? перечитаем корректно
        # task.due_date — property → date|None
        return due or sched  # type: ignore[return-value]
    except Exception:
        return None


def _collect_tasks_by_date(settings: dict) -> tuple[dict[datetime.date, list], list]:
    """Группирует задачи по дате отображения. Возвращает (by_date, unscheduled)."""
    try:
        from ..core.tasks import load_tasks  # lazy

        paths = resolve_paths(settings)
        folder = paths.tm_tasks
        tasks = load_tasks(folder)
    except Exception:
        return {}, []
    by_date: dict[datetime.date, list] = {}
    unscheduled: list = []
    for t in tasks:
        # показываем только активные? просроченные тоже нужны, done/cancelled скрываем
        try:
            if getattr(t, "status", "") in ("done", "cancelled"):
                continue
        except Exception:
            pass
        d = _task_display_date(t)
        if d is None:
            unscheduled.append(t)
        else:
            by_date.setdefault(d, []).append(t)
    # сортировка внутри дня: приоритет / тип
    for lst in by_date.values():
        try:
            lst.sort(key=lambda x: (0 if getattr(x, "priority", "") in ("high", "critical") else 1, str(getattr(x, "title", ""))))
        except Exception:
            pass
    try:
        unscheduled.sort(key=lambda x: str(getattr(x, "title", "")))
    except Exception:
        pass
    return by_date, unscheduled


def _update_task_date(path_str: str, target: datetime.date) -> bool:
    """Переносит задачу на target дату (обновляет frontmatter + save)."""
    p = Path(path_str.strip())
    if not p.is_file():
        return False
    try:
        from ..core.tasks import BILL, ROUTINE, load_task

        t = load_task(p)
        if t is None:
            return False
        iso = target.isoformat()
        if t.task_type == ROUTINE:
            t.fm["routine_next_date"] = iso
            # также синхронизируем scheduled_date для совместимости отображений
            t.fm["scheduled_date"] = iso
        elif t.task_type == BILL:
            t.fm["due_date"] = iso
            t.fm["scheduled_date"] = iso
        else:
            # regular: приоритет due_date
            # если ранее была только scheduled_date — обновляем её
            has_due = bool(t.fm.get("due_date"))
            has_sched = bool(t.fm.get("scheduled_date"))
            if has_sched and not has_due:
                t.fm["scheduled_date"] = iso
            else:
                t.fm["due_date"] = iso
                # если scheduled был — синхронизируем
                if has_sched:
                    t.fm["scheduled_date"] = iso
        t.save()
        try:
            from ..services import vault as _vault

            _vault.invalidate_vault_cache()
        except Exception:
            pass
        return True
    except Exception:
        return False


# ── CSS инжект для задач в календаре ────────────────────────────────

_CAL_TASKS_CSS_LOADED = False

def _ensure_calendar_tasks_css() -> None:
    global _CAL_TASKS_CSS_LOADED
    if _CAL_TASKS_CSS_LOADED:
        return
    _CAL_TASKS_CSS_LOADED = True
    css = b"""
    .cal-day--drag-over {
        background-color: rgba(130,168,255,0.12) !important;
        border-color: rgba(130,168,255,0.45) !important;
        box-shadow: inset 0 0 0 1px rgba(130,168,255,0.35), 0 0 0 2px rgba(130,168,255,0.18) !important;
    }
    .cal-task {
        font-size: 0.62em;
        font-weight: 600;
        border-radius: 4px;
        padding: 0 4px;
        border: 1px solid var(--ao-border-subtle);
        background: var(--ao-surface-control);
        color: var(--ao-text-muted);
        min-height: 14px;
    }
    .cal-task--routine { border-color: rgba(120,200,220,0.35); background: rgba(120,200,220,0.12); color: #9adff0; }
    .cal-task--bill { border-color: rgba(230,175,110,0.35); background: rgba(230,175,110,0.12); color: #ffc69a; }
    .cal-task--overdue { border-color: rgba(220,130,145,0.45); background: rgba(220,130,145,0.14); color: #fca5a5; }
    .cal-task--high { border-color: rgba(220,130,145,0.35); }
    .cal-task__more {
        font-size: 0.62em;
        color: var(--ao-text-faint);
        font-weight: 600;
    }
    .cal-day__tasks { margin-top: 2px; }
    .cal-tasks-bar {
        background: var(--ao-surface-glass-soft);
        border: 1px solid var(--ao-border-subtle);
        border-radius: var(--ao-radius-md);
        padding: 8px 10px;
        margin: 0 14px;
    }
    .cal-tasks-bar__title {
        font-size: 0.72em;
        font-weight: 700;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        color: var(--ao-text-muted);
    }
    .cal-task-chip {
        border-radius: 999px;
        padding: 3px 8px;
        border: 1px solid var(--ao-border-subtle);
        background: var(--ao-surface-glass);
        color: var(--ao-text-secondary);
        font-size: 0.82em;
        font-weight: 500;
    }
    .cal-task-chip--dragging { opacity: 0.55; border-color: rgba(130,168,255,0.45); }
    .cal-day__tasks .cal-task + .cal-task { margin-top: 2px; }
    """
    try:
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    except Exception:
        pass


# ── draggable task chip ────────────────────────────────────────────

class TaskDragChip(Gtk.Box):
    """Чип задачи с DragSource (MOVE) — для панели unscheduled."""

    def __init__(self, task) -> None:  # type: ignore[no-untyped-def]
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["cal-task-chip"])
        self.task = task
        title = str(getattr(task, "title", "") or Path(str(getattr(task, "path", ""))).stem)
        # иконка типа
        icon = "☑️"
        try:
            if getattr(task, "is_routine", False):
                icon = "🔁"
            elif getattr(task, "is_bill", False):
                icon = "🧾"
        except Exception:
            pass
        self.append(Gtk.Label(label=icon, css_classes=["cal-task-chip__icon"]))
        lbl = Gtk.Label(label=title, css_classes=["cal-task-chip__label"], ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=20, halign=Gtk.Align.START, xalign=0, hexpand=True)
        self.append(lbl)
        pri = str(getattr(task, "priority", "") or "").strip()
        if pri and pri.lower() not in ("none", ""):
            pl = Gtk.Label(label=pri, css_classes=["cal-task-chip__prio", "dim-label"])
            self.append(pl)

        # DragSource
        drag = Gtk.DragSource()
        try:
            drag.set_actions(Gdk.DragAction.MOVE)
        except Exception:
            pass
        p_str = str(getattr(task, "path", ""))

        def _on_prepare(_src, _x, _y):  # type: ignore[no-untyped-def]
            try:
                val = GObject.Value(GObject.TYPE_STRING, p_str)
                return Gdk.ContentProvider.new_for_value(val)
            except Exception:
                pass
            try:
                b = GLib.Bytes.new(p_str.encode("utf-8"))
                return Gdk.ContentProvider.new_for_bytes("text/plain", b)
            except Exception:
                return None

        try:
            drag.connect("prepare", _on_prepare)
        except Exception:
            pass

        def _on_begin(_src, _drag):  # type: ignore[no-untyped-def]
            try:
                self.add_css_class("cal-task-chip--dragging")
            except Exception:
                pass

        def _on_end(_src, _drag, _ok):  # type: ignore[no-untyped-def]
            try:
                self.remove_css_class("cal-task-chip--dragging")
            except Exception:
                pass

        try:
            drag.connect("drag-begin", _on_begin)
            drag.connect("drag-end", _on_end)
        except Exception:
            pass
        self.add_controller(drag)
        self._drag_source = drag  # type: ignore[attr-defined]
        try:
            self.set_tooltip_text(f"{title} · перетащи на дату")
        except Exception:
            pass


# ── UI ───────────────────────────────────────────────────────────────

_RU_MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
_RU_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# CSS-классы heatmap (см. style.py дополнение)
_HEAT_CSS = {
    0: "cal-day--heat0",
    1: "cal-day--heat1",
    2: "cal-day--heat2",
    3: "cal-day--heat3",
    4: "cal-day--heat4",
}


class CalendarView(Gtk.Box):
    """Вкладка Календарь: сетка месяца + heatmap, клик → daily note."""

    def __init__(self, settings: dict, on_open=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        _ensure_calendar_tasks_css()
        self.settings = settings
        self.on_open = on_open  # callable(path: str)
        self._current = datetime.date.today().replace(day=1)
        self._selected = datetime.date.today()
        self._intensity: dict[datetime.date, int] = {}
        self._daily_map: dict[datetime.date, int] = {}
        self._vault_counts: dict[datetime.date, int] = {}
        self._cells: list[Gtk.Button] = []
        self._weekday_labels: list[Gtk.Label] = []
        self._tasks_by_date: dict[datetime.date, list] = {}
        self._unscheduled: list = []

        self.append(view_header("🗓", "Календарь", "Obsidian Calendar · heatmap daily notes, клик по дате — открыть/создать · перетащи задачу на дату"))
        self._build_toolbar()
        self._build_tasks_bar()
        self._build_calendar()
        self._build_detail_bar()
        self._build_heatmap_legend()

        # первичная загрузка — мгновенно по daily, vault — в фоне
        self._sync_month_label()
        self._render_grid()
        self.reload()

    # ── toolbar ──────────────────────────────────────────────────
    def _build_toolbar(self) -> None:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["toolbar", "cal-toolbar"])
        bar.set_margin_start(14)
        bar.set_margin_end(14)

        self._prev_btn = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Предыдущий месяц")
        self._prev_btn.connect("clicked", lambda *_: self._shift_month(-1))
        bar.append(self._prev_btn)

        self._next_btn = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Следующий месяц")
        self._next_btn.connect("clicked", lambda *_: self._shift_month(1))
        bar.append(self._next_btn)

        self._today_btn = Gtk.Button(label="Сегодня", css_classes=["pill"])
        self._today_btn.set_tooltip_text("Перейти к сегодняшнему месяцу")
        self._today_btn.connect("clicked", lambda *_: self._go_today())
        bar.append(self._today_btn)

        self._month_label = Gtk.Label(label="", css_classes=["cal-month"], halign=Gtk.Align.START, xalign=0, hexpand=True)
        self._month_label.set_ellipsize(Pango.EllipsizeMode.END)
        bar.append(self._month_label)

        # year spin — быстрый прыжок
        adj = Gtk.Adjustment(value=float(self._current.year), lower=1970, upper=2100, step_increment=1, page_increment=1)
        self._year_spin = Gtk.SpinButton(adjustment=adj, climb_rate=1, digits=0, css_classes=["cal-year-spin"])
        self._year_spin.set_numeric(True)
        self._year_spin.set_tooltip_text("Год")
        self._year_spin.connect("value-changed", self._on_year_changed)
        bar.append(self._year_spin)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Обновить heatmap")
        refresh.connect("clicked", lambda *_: self.reload(force=True))
        bar.append(refresh)

        self.append(bar)

    def _build_tasks_bar(self) -> None:
        """Панель нераспределённых задач — DragSource чипы, дроп на календарь."""
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["cal-tasks-bar"])
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.append(Gtk.Label(label="Задачи", css_classes=["cal-tasks-bar__title"], halign=Gtk.Align.START, xalign=0, hexpand=True))
        self._tasks_count_lbl = Gtk.Label(label="", css_classes=["dim-label", "cal-tasks-bar__count"])
        head.append(self._tasks_count_lbl)
        self._tasks_hint = Gtk.Label(label="перетащи на дату → перенос", css_classes=["dim-hint"])
        head.append(self._tasks_hint)
        outer.append(head)

        # flow + scroller
        self._tasks_flow = Gtk.FlowBox(max_children_per_line=12, selection_mode=Gtk.SelectionMode.NONE, css_classes=["cal-tasks-bar__flow"], homogeneous=False, column_spacing=6, row_spacing=6)
        self._tasks_flow.set_halign(Gtk.Align.START)
        scroller = Gtk.ScrolledWindow(hexpand=True, css_classes=["cal-tasks-bar__scroller"])
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        scroller.set_min_content_height(36)
        # FlowBox должен быть ребёнком viewport — оборачиваем в Box для скролла
        box_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box_wrap.append(self._tasks_flow)
        scroller.set_child(box_wrap)
        outer.append(scroller)

        self._tasks_bar = outer
        self._tasks_scroller = scroller
        self.append(outer)

    # ── calendar grid ──────────────────────────────────────────
    def _build_calendar(self) -> None:
        clamp = Adw.Clamp(maximum_size=780, tightening_threshold=560)
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, css_classes=["cal-frame", "card"])
        frame.set_margin_start(14)
        frame.set_margin_end(14)

        grid = Gtk.Grid(column_spacing=4, row_spacing=4, css_classes=["cal-grid"])
        grid.set_margin_top(10)
        grid.set_margin_bottom(10)
        grid.set_margin_start(10)
        grid.set_margin_end(10)
        grid.set_column_homogeneous(True)
        grid.set_row_homogeneous(True)

        # weekday header (row 0)
        for col, wd in enumerate(_RU_WEEKDAYS):
            lbl = Gtk.Label(label=wd, css_classes=["cal-weekday"])
            if col >= 5:
                lbl.add_css_class("cal-weekend")
            grid.attach(lbl, col, 0, 1, 1)
            self._weekday_labels.append(lbl)

        # 6 недель ×7 =42 ячейки (row 1..6)
        for idx in range(42):
            row = 1 + idx // 7
            col = idx % 7
            btn = Gtk.Button(css_classes=["cal-day", "cal-day--heat0"])
            btn.set_can_focus(True)
            btn.set_focusable(True)
            # содержимое: число + задачи + точка heatmap
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, hexpand=True)
            num = Gtk.Label(label="", css_classes=["cal-day__num"])
            tasks_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, css_classes=["cal-day__tasks"], halign=Gtk.Align.CENTER)
            tasks_box.set_visible(False)
            dot = Gtk.Box(css_classes=["cal-day__dot"])
            dot.set_size_request(6, 6)
            box.append(num)
            box.append(tasks_box)
            box.append(dot)
            btn.set_child(box)
            btn._cal_num = num  # type: ignore[attr-defined]
            btn._cal_dot = dot  # type: ignore[attr-defined]
            btn._cal_tasks = tasks_box  # type: ignore[attr-defined]
            btn._cal_date: datetime.date | None = None  # type: ignore[attr-defined]
            btn.connect("clicked", self._on_day_clicked)
            # DropTarget для задач на эту дату
            self._attach_day_drop(btn)
            grid.attach(btn, col, row, 1, 1)
            self._cells.append(btn)

        frame.append(grid)
        clamp.set_child(frame)
        self._grid = grid
        self._clamp = clamp
        self.append(clamp)

    def _attach_day_drop(self, btn: Gtk.Button) -> None:
        """DropTarget (Dest) на ячейке дня — принимает строку пути задачи."""
        drop: Gtk.DropTarget | None = None
        try:
            drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        except Exception:
            try:
                drop = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)  # type: ignore[arg-type]
            except Exception:
                drop = None
        if drop is not None:
            drop.connect("enter", self._on_day_drag_enter, btn)
            drop.connect("leave", self._on_day_drag_leave, btn)
            drop.connect("drop", self._on_day_drop, btn)
            btn.add_controller(drop)
            btn._cal_drop = drop  # type: ignore[attr-defined]
        # fallback bytes DropTarget (если DragSource отдал bytes)
        try:
            drop_b = Gtk.DropTarget.new(Gio.Bytes, Gdk.DragAction.MOVE)  # type: ignore[arg-type]
            drop_b.connect("drop", self._on_day_drop_bytes, btn)
            # enter/leave для bytes не критичны — визуал уже от string-target
            btn.add_controller(drop_b)
        except Exception:
            pass

    def _on_day_drag_enter(self, _target, _x, _y, btn: Gtk.Button) -> Gdk.DragAction:  # type: ignore[no-untyped-def]
        try:
            btn.add_css_class("cal-day--drag-over")
        except Exception:
            pass
        return Gdk.DragAction.MOVE

    def _on_day_drag_leave(self, _target, btn: Gtk.Button) -> None:  # type: ignore[no-untyped-def]
        try:
            btn.remove_css_class("cal-day--drag-over")
        except Exception:
            pass

    def _on_day_drop(self, _target, value, _x, _y, btn: Gtk.Button) -> bool:  # type: ignore[no-untyped-def]
        try:
            btn.remove_css_class("cal-day--drag-over")
        except Exception:
            pass
        d: datetime.date | None = getattr(btn, "_cal_date", None)
        if d is None:
            return False
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
        return self._handle_task_drop(path_str, d)

    def _on_day_drop_bytes(self, _target, value, _x, _y, btn: Gtk.Button) -> bool:  # type: ignore[no-untyped-def]
        d: datetime.date | None = getattr(btn, "_cal_date", None)
        if d is None:
            return False
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
        if "/" not in path_str and "\\" not in path_str and not path_str.strip().endswith(".md"):
            return False
        return self._handle_task_drop(path_str.strip(), d)

    def _handle_task_drop(self, path_str: str, target: datetime.date) -> bool:
        path_str = (path_str or "").strip()
        if not path_str:
            return False
        p = Path(path_str)
        # относительный путь → резолвим
        if not p.is_absolute():
            try:
                p = resolve_paths(self.settings).tm_tasks / p.name
                if not p.is_file():
                    # пробуем как абсолютный внутри vault_root
                    cand = Path(str(self.settings.get("vault_root") or Path.home() / "desktop")) / path_str
                    if cand.is_file():
                        p = cand
            except Exception:
                pass
        ok = _update_task_date(str(p), target)
        if ok:
            try:
                self._toast(f"задача → {target.isoformat()}")
            except Exception:
                pass
            # обновим локально
            self._reload_tasks()
            self._render_grid()
            return True
        else:
            try:
                self._toast("не удалось перенести задачу")
            except Exception:
                pass
            return False

    def _build_detail_bar(self) -> None:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["cal-detail"])
        bar.set_margin_start(14)
        bar.set_margin_end(14)
        self._detail_label = Gtk.Label(label="", css_classes=["dim-hint", "cal-detail__label"], halign=Gtk.Align.START, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        bar.append(self._detail_label)
        self._open_btn = Gtk.Button(label="Открыть", css_classes=["suggested-action", "mod-cta"])
        self._open_btn.set_tooltip_text("Открыть/создать daily note для выбранной даты")
        self._open_btn.connect("clicked", lambda *_: self._open_selected())
        bar.append(self._open_btn)
        self._today_detail_btn = Gtk.Button(label="Сегодня")
        self._today_detail_btn.connect("clicked", lambda *_: self._select_date(datetime.date.today()))
        bar.append(self._today_detail_btn)
        self.append(bar)

    def _build_heatmap_legend(self) -> None:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["cal-legend"])
        row.set_margin_start(14)
        row.set_margin_end(14)
        row.append(Gtk.Label(label="Интенсивность:", css_classes=["dim-label"]))
        for lvl in range(5):
            sw = Gtk.Box(css_classes=["cal-legend__swatch", _HEAT_CSS[lvl]])
            sw.set_size_request(14, 14)
            sw.set_tooltip_text(f"уровень {lvl}: {'нет заметки' if lvl==0 else '1 заметка' if lvl==1 else f'{lvl}+' }")
            row.append(sw)
        row.append(Gtk.Label(label="меньше", css_classes=["dim-label", "cal-legend__edge"]))
        # dots уже показывают, текст для a11y
        row.append(Gtk.Label(label="больше", css_classes=["dim-label", "cal-legend__edge"]))
        # streak
        self._streak_label = Gtk.Label(label="", css_classes=["dim-hint", "cal-streak"], halign=Gtk.Align.END, xalign=1, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        row.append(self._streak_label)
        self.append(row)

    # ── month nav ─────────────────────────────────────────────
    def _sync_month_label(self) -> None:
        y, m = self._current.year, self._current.month
        self._month_label.set_text(f"{_RU_MONTHS[m-1]} {y}")
        # блокируем сигнал spin чтобы не зациклить
        try:
            self._year_spin.handler_block_by_func(self._on_year_changed)  # type: ignore[attr-defined]
        except Exception:
            pass
        self._year_spin.set_value(float(y))
        try:
            self._year_spin.handler_unblock_by_func(self._on_year_changed)  # type: ignore[attr-defined]
        except Exception:
            pass

    def _on_year_changed(self, _spin: Gtk.SpinButton) -> None:
        y = int(_spin.get_value())
        if y == self._current.year:
            return
        self._current = self._current.replace(year=y)
        self._sync_month_label()
        self._render_grid()

    def _shift_month(self, delta: int) -> None:
        y, m = self._current.year, self._current.month
        m += delta
        while m > 12:
            m -= 12
            y += 1
        while m < 1:
            m += 12
            y -= 1
        self._current = datetime.date(y, m, 1)
        self._sync_month_label()
        self._render_grid()

    def _go_today(self) -> None:
        self._current = datetime.date.today().replace(day=1)
        self._selected = datetime.date.today()
        self._sync_month_label()
        self._render_grid()

    # ── tasks bar render ──────────────────────────────────────
    def _reload_tasks(self) -> None:
        try:
            by_date, unsched = _collect_tasks_by_date(self.settings)
        except Exception:
            by_date, unsched = {}, []
        self._tasks_by_date = by_date
        self._unscheduled = unsched
        self._render_tasks_bar()

    def _render_tasks_bar(self) -> None:
        # очистить FlowBox
        while (child := self._tasks_flow.get_first_child()) is not None:
            self._tasks_flow.remove(child)
        total_sched = sum(len(v) for v in self._tasks_by_date.values())
        total_unsched = len(self._unscheduled)
        try:
            self._tasks_count_lbl.set_text(f"{total_unsched} без даты · {total_sched} в календаре")
        except Exception:
            pass
        if total_unsched == 0:
            hint = Gtk.Label(label="нет задач без даты", css_classes=["dim-hint"])
            self._tasks_flow.append(hint)
            try:
                self._tasks_bar.set_visible(True)
            except Exception:
                pass
            return
        for t in self._unscheduled[:24]:
            chip = TaskDragChip(t)
            self._tasks_flow.append(chip)
        if len(self._unscheduled) > 24:
            more = Gtk.Label(label=f"+{len(self._unscheduled)-24}", css_classes=["dim-label"])
            self._tasks_flow.append(more)
        try:
            self._tasks_bar.set_visible(True)
        except Exception:
            pass

    # ── grid render ───────────────────────────────────────────
    def _month_cells(self) -> list[datetime.date]:
        """42 даты сетки (понедельник — первый день, как в Obsidian Calendar)."""
        first = self._current
        # calendar.monthcalendar с monday=0
        cal = calendar.Calendar(firstweekday=0)
        weeks = cal.monthdatescalendar(first.year, first.month)
        # должно быть 6 недель, добиваем если меньше
        flat: list[datetime.date] = [d for w in weeks for d in w]
        while len(flat) < 42:
            # добавить неделю вперёд
            last = flat[-1]
            nxt = [last + datetime.timedelta(days=i) for i in range(1, 8)]
            flat.extend(nxt)
        return flat[:42]

    def _render_grid(self) -> None:
        today = datetime.date.today()
        cells = self._month_cells()
        # streak для легенды
        streak = self._calc_streak()
        if streak:
            self._streak_label.set_text(f"🔥 серия {streak} дн.")
        else:
            self._streak_label.set_text("")
        for btn, d in zip(self._cells, cells):
            btn._cal_date = d  # type: ignore[attr-defined]
            num: Gtk.Label = btn._cal_num  # type: ignore[attr-defined]
            num.set_text(str(d.day))
            # reset classes
            for lvl_cls in _HEAT_CSS.values():
                btn.remove_css_class(lvl_cls)
            btn.remove_css_class("cal-day--outside")
            btn.remove_css_class("cal-day--today")
            btn.remove_css_class("cal-day--selected")
            btn.remove_css_class("cal-day--weekend")
            btn.remove_css_class("cal-day--drag-over")
            dot: Gtk.Box = btn._cal_dot  # type: ignore[attr-defined]
            tasks_box: Gtk.Box = btn._cal_tasks  # type: ignore[attr-defined]
            # очистим задачи
            while (child := tasks_box.get_first_child()) is not None:
                tasks_box.remove(child)
            # outside month — приглушён
            if d.month != self._current.month:
                btn.add_css_class("cal-day--outside")
            if d.weekday() >= 5:
                btn.add_css_class("cal-day--weekend")
            # heatmap
            lvl = self._intensity.get(d, 0)
            btn.add_css_class(_HEAT_CSS.get(lvl, "cal-day--heat0"))
            # dot visibility — показываем только если есть intensity
            if lvl > 0:
                dot.set_visible(True)
                # цвет точки уже через heat класс, но дублируем класс
                for c in list(dot.get_css_classes()):
                    if c.startswith("cal-dot--"):
                        dot.remove_css_class(c)
                dot.add_css_class(f"cal-dot--{lvl}")
            else:
                dot.set_visible(False)
            if d == today:
                btn.add_css_class("cal-day--today")
            if d == self._selected:
                btn.add_css_class("cal-day--selected")

            # ── задачи на дне ──
            tasks = self._tasks_by_date.get(d, [])
            if tasks:
                tasks_box.set_visible(True)
                # показать до 3 задач, остальные — +N
                for t in tasks[:3]:
                    title = str(getattr(t, "title", "") or Path(str(getattr(t, "path", ""))).stem)
                    # обрезать
                    if len(title) > 12:
                        title = title[:11] + "…"
                    cls = "cal-task"
                    try:
                        if getattr(t, "is_routine", False):
                            cls_extra = "cal-task--routine"
                        elif getattr(t, "is_bill", False):
                            cls_extra = "cal-task--bill"
                        else:
                            cls_extra = ""
                        # overdue
                        is_over = False
                        try:
                            is_over = t.is_overdue(today)  # type: ignore[attr-defined]
                        except Exception:
                            pass
                        if is_over:
                            cls_extra = "cal-task--overdue"
                        pri = str(getattr(t, "priority", "") or "").lower()
                        if pri in ("high", "critical", "urgent"):
                            cls_extra = (cls_extra + " cal-task--high").strip()
                    except Exception:
                        cls_extra = ""
                    lbl = Gtk.Label(label=title, css_classes=["cal-task"] + ([cls_extra] if cls_extra else []), ellipsize=Pango.EllipsizeMode.END, max_width_chars=10)
                    lbl.set_halign(Gtk.Align.CENTER)
                    tasks_box.append(lbl)
                if len(tasks) > 3:
                    more = Gtk.Label(label=f"+{len(tasks)-3}", css_classes=["cal-task__more"])
                    tasks_box.append(more)
                # tooltip с полным списком
                try:
                    tip_titles = ", ".join(str(getattr(x, "title", "") or Path(str(getattr(x, "path", ""))).stem) for x in tasks[:6])
                    if len(tasks) > 6:
                        tip_titles += f" +{len(tasks)-6}"
                    btn.set_tooltip_text(f"{d.isoformat()} · задач: {len(tasks)} — {tip_titles}")
                except Exception:
                    pass
            else:
                tasks_box.set_visible(False)

            # a11y — если задач нет, оставить heatmap tooltip, иначе расширенный уже выше
            if not tasks:
                try:
                    tip = d.isoformat()
                    vc = self._vault_counts.get(d, 0)
                    if lvl > 0:
                        parts = []
                        if d in self._daily_map:
                            parts.append("daily ✓")
                        if vc:
                            parts.append(f"{vc} заметок в vault")
                        tip += " · " + ", ".join(parts) + f" · уровень {lvl}"
                    else:
                        tip += " · нет заметок"
                    if d == today:
                        tip += " · сегодня"
                    btn.set_tooltip_text(tip)
                    btn.update_property(Gtk.AccessibleProperty.LABEL, tip)
                except Exception:
                    pass
            else:
                try:
                    tip = btn.get_tooltip_text() or d.isoformat()
                    if d == today:
                        tip += " · сегодня"
                    btn.update_property(Gtk.AccessibleProperty.LABEL, tip)
                except Exception:
                    pass
        self._render_detail()

    def _calc_streak(self) -> int:
        """Серия подряд дней с заметками до сегодня (как в Obsidian Calendar streak)."""
        if not self._intensity:
            return 0
        cur = datetime.date.today()
        s = 0
        for _ in range(366):
            if self._intensity.get(cur, 0) > 0:
                s += 1
                cur -= datetime.timedelta(days=1)
            else:
                break
        return s

    def _render_detail(self) -> None:
        d = self._selected
        lvl = self._intensity.get(d, 0)
        vc = self._vault_counts.get(d, 0)
        has_daily = d in self._daily_map
        tasks = self._tasks_by_date.get(d, [])
        parts = [f"{_RU_WEEKDAYS[d.weekday()]}, {d.day} {_RU_MONTHS[d.month-1].lower()} {d.year}"]
        if has_daily:
            parts.append("daily ✓")
        if vc:
            parts.append(f"{vc} заметок")
        if lvl:
            parts.append(f"уровень {lvl}")
        else:
            parts.append("нет заметок")
        if tasks:
            parts.append(f"задач: {len(tasks)}")
            # показать до 3 заголовков
            titles = ", ".join(str(getattr(x, "title", "")) for x in tasks[:3])
            if len(tasks) > 3:
                titles += f" +{len(tasks)-3}"
            parts.append(titles)
        self._detail_label.set_text(" · ".join(parts))
        # open btn label
        ddir = _daily_dir(self.settings)
        path = ddir / f"{_daily_slug(d)}.md"
        self._open_btn.set_label("Открыть" if path.exists() else "Создать")

    # ── selection / open ──────────────────────────────────────
    def _select_date(self, d: datetime.date) -> None:
        self._selected = d
        # если выбран месяц вне текущего — переключить
        if d.year != self._current.year or d.month != self._current.month:
            self._current = d.replace(day=1)
            self._sync_month_label()
        self._render_grid()

    def _on_day_clicked(self, btn: Gtk.Button) -> None:
        d: datetime.date | None = getattr(btn, "_cal_date", None)
        if d is None:
            return
        # клик уже выбрал — второй клик открывает? сейчас: выбирает + detail, один клик открывает тоже?
        # Spec: клик по дате открывает/creates daily note. Делаем select + open.
        self._select_date(d)
        # короткий debounce: открываем сразу (UX Obsidian: клик → открыть)
        self._open_daily(d)

    def _open_selected(self) -> None:
        self._open_daily(self._selected)

    def _open_daily(self, d: datetime.date) -> None:
        """Открыть/создать daily note для `d`; делегирует в files view через on_open."""
        ddir = _daily_dir(self.settings)
        path = ddir / f"{_daily_slug(d)}.md"
        if not path.exists():
            tpl_path = str(self.settings.get("daily_template") or resolve_paths(self.settings).tm_templates / "Daily note.md")
            tpl = _resolve_template(tpl_path)
            content = _apply_template(tpl or "# {{date:YYYY-MM-DD}}\n", d)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                try:
                    from ..services import vault as _vault
                    _vault.invalidate_vault_cache()
                except Exception:
                    pass
            except OSError as exc:
                self._toast(f"не удалось создать {path.name}: {exc}")
                return
            self._toast(f"создана {path.name}")
        else:
            # даже если есть — инвалидация не нужна, но heatmap обновим при next reload
            pass
        if self.on_open is not None:
            try:
                self.on_open(str(path))
            except Exception:
                pass
        else:
            # fallback — xdg-open
            try:
                from gi.repository import Gio
                Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)
            except Exception:
                pass

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

    # ── data loading ──────────────────────────────────────────
    def reload(self, force: bool = False) -> None:
        """Перечитать heatmap (daily — синхронно, vault — фон) + задачи."""
        # tasks — синхронно (быстро)
        self._reload_tasks()
        # daily — быстро, делаем сразу
        try:
            daily = scan_daily_intensity(self.settings)
        except Exception:
            daily = {}
        self._daily_map = daily
        # vault counts — в фоне если force или холодный кэш, иначе синхронно (малый объём)
        # для простоты — всегда в фоне чтобы не блокировать UI
        if force:
            try:
                from ..services import vault as _vault
                _vault.invalidate_vault_cache()
            except Exception:
                pass
        settings_copy = dict(self.settings)

        def work() -> None:
            try:
                vc = scan_vault_counts(settings_copy)
            except Exception:
                vc = {}
            GLib.idle_add(self._on_vault_counts, vc)

        threading.Thread(target=work, daemon=True).start()
        # пока vault грузится — показываем только daily
        self._vault_counts = {}
        self._intensity = combined_intensity(self._daily_map, self._vault_counts)
        self._render_grid()

    def _on_vault_counts(self, vc: dict[datetime.date, int]) -> bool:
        self._vault_counts = vc
        self._intensity = combined_intensity(self._daily_map, self._vault_counts)
        self._render_grid()
        return False

    # для внешнего FileMonitor — alias
    def refresh(self) -> None:
        self.reload()
