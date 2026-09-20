"""Еженедельный/ежемесячный обзор — вкладка FragileNotes.

Собирает заметки и задачи за период (неделя/месяц), показывает статистику,
позволяет создать обзорную заметку из шаблона. Интегрируется как вкладка."""

from __future__ import annotations

import calendar
import datetime
import re
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from ..paths import resolve_paths  # noqa: E402
from .widgets import empty_state, view_header  # noqa: E402

# ── константы / локализация ──────────────────────────────────────────

_RU_MONTHS = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]
_RU_MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
_RU_MONTHS_SHORT = ["янв.", "февр.", "марта", "апр.", "мая", "июня", "июля", "авг.", "сент.", "окт.", "нояб.", "дек."]
_RU_WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

_WEEK_TEMPLATE_VARS = ("{{weekStart}}", "{{weekEnd}}", "{{weekId}}", "{{weekLabel}}")

# ── чистые функции: периоды ──────────────────────────────────────────


def week_range(ref: datetime.date) -> tuple[datetime.date, datetime.date]:
    """Понедельник-воскресенье ISO-недели для `ref`."""
    start = ref - datetime.timedelta(days=ref.weekday())
    end = start + datetime.timedelta(days=6)
    return start, end


def month_range(ref: datetime.date) -> tuple[datetime.date, datetime.date]:
    """Первый и последний день месяца для `ref`."""
    first = ref.replace(day=1)
    last_day = calendar.monthrange(ref.year, ref.month)[1]
    last = ref.replace(day=last_day)
    return first, last


def iso_week_id(d: datetime.date) -> str:
    """ID недели вида 2026-W33."""
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_label(start: datetime.date, end: datetime.date) -> str:
    """Человекочитаемая метка недели: '10 авг. — 16 авг. 2026'."""
    if start.year == end.year and start.month == end.month:
        return f"{start.day} — {end.day} {_RU_MONTHS_SHORT[end.month - 1]} {end.year}"
    if start.year == end.year:
        return f"{start.day} {_RU_MONTHS_SHORT[start.month - 1]} — {end.day} {_RU_MONTHS_SHORT[end.month - 1]} {end.year}"
    return f"{start.day} {_RU_MONTHS_SHORT[start.month - 1]} {start.year} — {end.day} {_RU_MONTHS_SHORT[end.month - 1]} {end.year}"


def month_label(d: datetime.date) -> str:
    """Метка месяца: 'Август 2026'."""
    return f"{_RU_MONTHS[d.month - 1]} {d.year}"


def period_range(kind: str, ref: datetime.date) -> tuple[datetime.date, datetime.date]:
    """Универсальный геттер диапазона."""
    if kind == "month":
        return month_range(ref)
    return week_range(ref)


def period_label(kind: str, ref: datetime.date) -> str:
    """Универсальная метка периода."""
    s, e = period_range(kind, ref)
    if kind == "month":
        return month_label(ref)
    return week_label(s, e)


def _parse_date(value: str | None) -> datetime.date | None:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value).strip().split("T")[0].split(" ")[0])
    except Exception:
        return None


# ── пути обзоров ─────────────────────────────────────────────────────


def get_review_folder(settings: dict) -> Path:
    """Папка для обзорных заметок: vault/Review (настраивается через weekly_review_folder)."""
    root = Path(str(settings.get("vault_root") or Path.home() / "desktop"))
    folder = str(settings.get("weekly_review_folder") or "Review")
    return root / folder


def get_review_path(settings: dict, kind: str, ref: datetime.date) -> Path:
    """Путь к файлу обзора для периода.

    Неделя:  Review/2026/2026-W33.md
    Месяц:   Review/2026/2026-08.md
    Для совместимости: недельный фолбэк без подпапки тоже поддерживается при чтении,
    но создание всегда в Review/YYYY/.
    """
    folder = get_review_folder(settings)
    y = ref.year
    if kind == "month":
        name = f"{ref.year}-{ref.month:02d}.md"
        # месячные складываем в Review/YYYY/ как и недельные для единообразия
        return folder / str(y) / name
    # weekly
    wid = iso_week_id(ref)
    # используем год ISO недели для имени, но папку — по календарному году ref
    # чтобы совпадало с примером Review/2026/2026-W33.md где 2026-W33 → начало 10 авг 2026
    # Для пограничных недель (1 января) берём isoyear из start недели
    s, _ = week_range(ref)
    iso_y = s.isocalendar()[0]
    wid = f"{iso_y}-W{s.isocalendar()[1]:02d}"
    return folder / str(s.year) / f"{wid}.md"


def review_exists(settings: dict, kind: str, ref: datetime.date) -> bool:
    return get_review_path(settings, kind, ref).exists()


# ── сканирование заметок / задач ─────────────────────────────────────


def scan_notes_for_period(
    settings: dict, start: datetime.date, end: datetime.date
) -> list[tuple[Path, float]]:
    """Заметки vault, изменённые/созданные в [start, end] включительно (по mtime)."""
    try:
        from ..paths import resolve_paths as _rp
        from ..services.vault import _iter_notes  # type: ignore

        root = _rp(settings).root
        out: list[tuple[Path, float]] = []
        for p_str, mt in _iter_notes(root):
            try:
                d = datetime.date.fromtimestamp(mt)
            except Exception:
                continue
            if start <= d <= end:
                out.append((Path(p_str), mt))
        out.sort(key=lambda x: x[1], reverse=True)
        return out
    except Exception:
        return []


def scan_daily_notes_for_period(
    settings: dict, start: datetime.date, end: datetime.date
) -> list[Path]:
    """Daily-заметки за период (по имени файла YYYY-MM-DD)."""
    try:
        daily_dir = resolve_paths(settings).daily
    except Exception:
        root = Path(str(settings.get("vault_root") or Path.home() / "desktop"))
        daily_dir = root / str(settings.get("daily_folder") or "02 Daily")
    if not daily_dir.is_dir():
        return []
    out: list[Path] = []
    cur = start
    while cur <= end:
        p = daily_dir / f"{cur.isoformat()}.md"
        if p.is_file():
            out.append(p)
        cur += datetime.timedelta(days=1)
    return out


def collect_tasks_stats(
    settings: dict, start: datetime.date, end: datetime.date
) -> dict:
    """Статистика задач за период.

    Возвращает dict:
      total, active, done_period, due_period, overdue, routine_done, bills_due
      lists: due_tasks, done_tasks (опционально для рендера)
    """
    try:
        from ..core import tasks as tm
        from ..paths import resolve_paths as _rp

        all_tasks = tm.load_tasks(_rp(settings).tm_tasks)
    except Exception:
        return {
            "total": 0, "active": 0, "due_period": 0, "overdue": 0,
            "done_period": 0, "routine": 0, "bills": 0,
            "due_tasks": [], "done_tasks": [],
        }
    total = len(all_tasks)
    active = len([t for t in all_tasks if t.is_active])
    routine = len([t for t in all_tasks if t.is_routine])
    bills = len([t for t in all_tasks if t.is_bill])
    # задачи к выполнению в период (due/scheduled/routine_next в интервале)
    due_tasks: list = []
    for t in all_tasks:
        d = t.due_date or t.scheduled_date or t.routine_next_date
        if d is not None and start <= d <= end:
            due_tasks.append(t)
        elif t.is_routine and t.routine_next_date and start <= t.routine_next_date <= end:
            if t not in due_tasks:
                due_tasks.append(t)
    # выполненные в период — по last_done_date / routine_last_done
    done_tasks: list = []
    for t in all_tasks:
        # пробуем несколько полей
        for key in ("last_done_date", "routine_last_done_date", "last_done", "completed_date"):
            v = t.fm.get(key)
            d = _parse_date(str(v) if v is not None else None)
            if d and start <= d <= end:
                done_tasks.append(t)
                break
        else:
            # также проверяем статус done и дату изменения: если статус done и due внутри периода
            if t.status == "done":
                d = t.due_date or t.scheduled_date
                if d and start <= d <= end:
                    done_tasks.append(t)
    overdue = len([t for t in all_tasks if t.is_overdue(end)])
    return {
        "total": total,
        "active": active,
        "due_period": len(due_tasks),
        "overdue": overdue,
        "done_period": len(done_tasks),
        "routine": routine,
        "bills": bills,
        "due_tasks": due_tasks,
        "done_tasks": done_tasks,
    }


def collect_review_data(settings: dict, kind: str, ref: datetime.date) -> dict:
    """Собрать всё для рендера обзора: заметки, daily, задачи, статистика."""
    start, end = period_range(kind, ref)
    notes = scan_notes_for_period(settings, start, end)
    daily = scan_daily_notes_for_period(settings, start, end)
    tasks = collect_tasks_stats(settings, start, end)
    # общая статистика
    # кол-во слов в заметках периода (быстрая оценка)
    words = 0
    try:
        for p, _ in notes:
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
                words += len([w for w in re.split(r"\s+", txt) if w.strip()])
            except OSError:
                continue
    except Exception:
        words = 0
    stats = {
        "period_kind": kind,
        "start": start,
        "end": end,
        "label": period_label(kind, ref),
        "notes_count": len(notes),
        "daily_count": len(daily),
        "words": words,
        **tasks,
    }
    return {
        "start": start,
        "end": end,
        "label": stats["label"],
        "notes": notes,
        "daily": daily,
        "tasks": tasks,
        "stats": stats,
    }


# ── генерация markdown обзора ────────────────────────────────────────


def _load_weekly_template_text(settings: dict) -> str | None:
    # путь из настроек weekly_template
    raw = str(settings.get("weekly_template") or "")
    candidates: list[Path] = []
    root = Path(str(settings.get("vault_root") or Path.home() / "desktop"))
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = root / p
        candidates.append(p)
    # фолбэк vault/_System/Templates/Weekly Review.md
    candidates.append(root / "_System/TaskManagerMain/Templates/Weekly Review.md")
    candidates.append(root / "_System/Templates/Weekly Review.md")
    for p in candidates:
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8")
            except OSError:
                continue
    return None


def _load_monthly_template_text(settings: dict) -> str | None:
    root = Path(str(settings.get("vault_root") or Path.home() / "desktop"))
    candidates = [
        root / "_System/TaskManagerMain/Templates/Mounthly Review.md",
        root / "_System/TaskManagerMain/Templates/Monthly Review.md",
        root / "_System/Templates/Mounthly Review.md",
        root / "_System/Templates/Monthly Review.md",
    ]
    for p in candidates:
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8")
            except OSError:
                continue
    return None


def _apply_weekly_template(tpl: str, start: datetime.date, end: datetime.date, label: str) -> str:
    wid = iso_week_id(start)
    # tpl из vault использует {{weekStart}} etc. без пробелов
    out = tpl
    out = out.replace("{{weekStart}}", start.isoformat())
    out = out.replace("{{week_start}}", start.isoformat())
    out = out.replace("{{weekStart:YYYY-MM-DD}}", start.isoformat())
    out = out.replace("{{weekEnd}}", end.isoformat())
    out = out.replace("{{week_end}}", end.isoformat())
    out = out.replace("{{weekId}}", wid)
    out = out.replace("{{week_id}}", wid)
    out = out.replace("{{weekLabel}}", label)
    out = out.replace("{{week_label}}", label)
    out = out.replace("{{weekLabel}}", label)
    # также поддержим {{date}} варианты для совместимости
    out = out.replace("{{date:YYYY-MM-DD}}", start.isoformat())
    out = out.replace("{{date}}", start.isoformat())
    return out


def build_review_markdown(
    settings: dict,
    kind: str,
    ref: datetime.date,
    data: dict | None = None,
) -> str:
    """Сгенерировать markdown для обзорной заметки.

    Если есть шаблон недельного/месячного обзора — подставляет переменные,
    иначе возвращает фолбэк-шаблон со статистикой.
    """
    start, end = period_range(kind, ref)
    label = period_label(kind, ref)
    done = data or collect_review_data(settings, kind, ref)
    stats = done.get("stats") or done  # совместимость

    # Попытка шаблона
    tpl: str | None = None
    if kind == "month":
        tpl = _load_monthly_template_text(settings)
        if tpl is not None:
            # monthly шаблоны часто используют Templater <% tp.date... %> — оставляем как есть
            # но пробуем подменить простые {{date}} если есть
            try:
                out = tpl.replace("{{month}}", month_label(ref))
                out = out.replace("{{label}}", label)
                out = out.replace("{{date}}", start.isoformat())
                return out
            except Exception:
                return tpl
    else:
        tpl = _load_weekly_template_text(settings)
        if tpl is not None:
            return _apply_weekly_template(tpl, start, end, label)

    # ── фолбэк ─────────────────────────────────────────────────────
    if kind == "month":
        m_label = month_label(ref)
        notes_cnt = stats.get("notes_count", 0)
        daily_cnt = stats.get("daily_count", 0)
        words = stats.get("words", 0)
        due = stats.get("due_period", 0)
        done_cnt = stats.get("done_period", 0)
        overdue = stats.get("overdue", 0)
        return (
            f"---\ntype: monthly-review\nmonth: {ref.year}-{ref.month:02d}\n"
            f"month_label: \"{m_label}\"\n"
            f"start: {start.isoformat()}\nend: {end.isoformat()}\n"
            f"cssclasses:\n  - ao-glass\n  - tm-note-glass\n"
            f"tags:\n  - monthly-review\n---\n\n"
            f"# Месячный обзор · {m_label}\n\n"
            f"## Статистика\n\n"
            f"- Заметок за месяц: **{notes_cnt}** (daily: {daily_cnt}, слов: {words})\n"
            f"- Задач к выполнению: **{due}**, выполнено: **{done_cnt}**, просрочено на конец: **{overdue}**\n\n"
            f"## Главные достижения месяца\n\n-\n\n"
            f"## Главные проблемы / риски\n\n-\n\n"
            f"## Планы на следующий месяц\n\n-\n"
        )
    # weekly fallback
    wid = iso_week_id(start)
    notes_cnt = stats.get("notes_count", 0)
    daily_cnt = stats.get("daily_count", 0)
    words = stats.get("words", 0)
    due = stats.get("due_period", 0)
    done_cnt = stats.get("done_period", 0)
    overdue = stats.get("overdue", 0)
    return (
        f"---\ntype: weekly-review\nweek_start: {start.isoformat()}\nweek_end: {end.isoformat()}\n"
        f"week_id: {wid}\nweek_label: \"{label}\"\n"
        f"cssclasses:\n  - ao-glass\n  - tm-note-glass\n"
        f"tags:\n  - weekly-review\n  - task-manager\n---\n\n"
        f"# Недельный обзор · {label}\n\n"
        f"<!-- Статистика периода: заметок {notes_cnt} (daily {daily_cnt}, слов {words}), "
        f"задач {due}, выполнено {done_cnt}, просрочено {overdue} -->\n\n"
        f"```dataviewjs\nawait dv.view(\"_System/TaskManagerMain/_system/review/views/tm-weekly-review\", {{\n"
        f"  weekStart: \"{start.isoformat()}\",\n  weekEnd: \"{end.isoformat()}\"\n}});\n```\n\n"
        f"## Главные достижения недели\n\n-\n\n"
        f"## Главные проблемы / риски\n\n-\n"
    )


def create_review_note(
    settings: dict,
    kind: str,
    ref: datetime.date,
    *,
    overwrite: bool = False,
) -> Path:
    """Создать файл обзорной заметки на диске и вернуть путь."""
    start, end = period_range(kind, ref)
    target = get_review_path(settings, kind, ref)
    if target.exists() and not overwrite:
        return target
    data = collect_review_data(settings, kind, ref)
    content = build_review_markdown(settings, kind, ref, data)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    # инвалидировать кэши vault
    try:
        from ..services import vault as _vault

        _vault.invalidate_vault_cache()
    except Exception:
        pass
    return target


# ── UI ───────────────────────────────────────────────────────────────


class ReviewView(Gtk.Box):
    """Вкладка Обзор: свитч неделя/месяц, навигация периода, статистика, списки, создание."""

    def __init__(self, settings: dict, on_open=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self.on_open = on_open
        self._kind: str = "week"  # week | month
        self._ref: datetime.date = datetime.date.today()
        self._data: dict | None = None

        self.append(view_header("🔍", "Обзор", "Еженедельный/ежемесячный: заметки и задачи за период, создание обзорной заметки"))
        self._build_toolbar()
        self._build_hero()
        self._build_kpi()
        self._build_lists()
        self._build_actions()
        self.reload()

    # ── toolbar ──────────────────────────────────────────────────
    def _build_toolbar(self) -> None:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["toolbar", "review-toolbar"])
        bar.set_margin_start(14)
        bar.set_margin_end(14)

        # свитч week/month
        self._week_btn = Gtk.ToggleButton(label="Неделя", css_classes=["pill"])
        self._week_btn.set_active(True)
        self._month_btn = Gtk.ToggleButton(label="Месяц", css_classes=["pill"], group=self._week_btn)
        self._week_btn.connect("toggled", self._on_kind_toggled)
        self._month_btn.connect("toggled", self._on_kind_toggled)
        bar.append(self._week_btn)
        bar.append(self._month_btn)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        self._prev_btn = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Предыдущий период")
        self._prev_btn.connect("clicked", lambda *_: self._shift(-1))
        bar.append(self._prev_btn)
        self._next_btn = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Следующий период")
        self._next_btn.connect("clicked", lambda *_: self._shift(1))
        bar.append(self._next_btn)
        self._today_btn = Gtk.Button(label="Сегодня", css_classes=["pill"])
        self._today_btn.connect("clicked", lambda *_: self._go_today())
        bar.append(self._today_btn)

        self._period_label = Gtk.Label(label="", css_classes=["cal-month"], halign=Gtk.Align.START, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        bar.append(self._period_label)

        self._refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Обновить")
        self._refresh_btn.connect("clicked", lambda *_: self.reload(force=True))
        bar.append(self._refresh_btn)

        self.append(bar)

    def _build_hero(self) -> None:
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, css_classes=["tm-hero-v2", "tm-hero-v2--dash"])
        hero.append(Gtk.Label(label="Обзор периода", css_classes=["tm-hero-v2__eyebrow"], halign=Gtk.Align.START, xalign=0))
        self._hero_title = Gtk.Label(label="…", css_classes=["tm-hero-v2__title"], halign=Gtk.Align.START, xalign=0)
        hero.append(self._hero_title)
        self._hero_sub = Gtk.Label(label="", css_classes=["tm-hero-v2__date"], halign=Gtk.Align.START, xalign=0)
        hero.append(self._hero_sub)
        self.append(hero)

    def _build_kpi(self) -> None:
        strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, css_classes=["tm-today-strip"])
        strip.append(Gtk.Label(label="Статистика", css_classes=["tm-today-strip__label"]))
        self.kpi: dict[str, tuple[Gtk.Label, Gtk.Label]] = {}
        for key, label, tone in [
            ("notes", "заметок", ""),
            ("daily", "daily", "run"),
            ("due", "задач к сроку", "warn"),
            ("done", "выполнено", "ok"),
            ("overdue", "просрочено", "error"),
            ("words", "слов", ""),
        ]:
            chip = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1,
                           css_classes=["tm-kpi-chip"] + ([f"tm-kpi-chip--{tone}"] if tone else []))
            val = Gtk.Label(label="…", css_classes=["tm-kpi-chip__value"])
            lab = Gtk.Label(label=label, css_classes=["tm-kpi-chip__label"])
            chip.append(val)
            chip.append(lab)
            self.kpi[key] = (val, lab)
            strip.append(chip)
        self.append(strip)

    def _build_lists(self) -> None:
        cols = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, hexpand=True, vexpand=True)
        cols.set_margin_start(14)
        cols.set_margin_end(14)

        # заметки
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True, vexpand=True)
        left.append(Gtk.Label(label="Заметки периода", css_classes=["section-title"], halign=Gtk.Align.START, xalign=0))
        self._notes_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sc1 = Gtk.ScrolledWindow(vexpand=True, hexpand=True, css_classes=["card"])
        sc1.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc1.set_child(self._notes_box)
        sc1.set_min_content_height(220)
        left.append(sc1)

        # задачи
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True, vexpand=True)
        right.append(Gtk.Label(label="Задачи периода", css_classes=["section-title"], halign=Gtk.Align.START, xalign=0))
        self._tasks_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sc2 = Gtk.ScrolledWindow(vexpand=True, hexpand=True, css_classes=["card"])
        sc2.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc2.set_child(self._tasks_box)
        sc2.set_min_content_height(220)
        right.append(sc2)

        cols.append(left)
        cols.append(right)
        self.append(cols)

    def _build_actions(self) -> None:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["toolbar"])
        bar.set_margin_start(14)
        bar.set_margin_end(14)
        bar.set_margin_bottom(14)
        self._status_lbl = Gtk.Label(label="", css_classes=["dim-hint"], hexpand=True, halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        bar.append(self._status_lbl)
        self._open_btn = Gtk.Button(label="Открыть обзор", tooltip_text="Открыть существующий файл обзора")
        self._open_btn.connect("clicked", self._on_open_review)
        bar.append(self._open_btn)
        self._create_btn = Gtk.Button(label="Создать обзор", css_classes=["suggested-action", "mod-cta"],
                                      tooltip_text="Создать обзорную заметку для выбранного периода")
        self._create_btn.connect("clicked", self._on_create_review)
        bar.append(self._create_btn)
        self.append(bar)

    # ── логика периода ───────────────────────────────────────────
    def _on_kind_toggled(self, _btn: Gtk.ToggleButton) -> None:
        new_kind = "month" if self._month_btn.get_active() else "week"
        if new_kind == self._kind:
            return
        self._kind = new_kind
        self.reload()

    def _shift(self, delta: int) -> None:
        if self._kind == "month":
            y, m = self._ref.year, self._ref.month
            m += delta
            while m > 12:
                m -= 12
                y += 1
            while m < 1:
                m += 12
                y -= 1
            last = calendar.monthrange(y, m)[1]
            d = min(self._ref.day, last)
            self._ref = datetime.date(y, m, d)
        else:
            self._ref = self._ref + datetime.timedelta(days=7 * delta)
        self.reload()

    def _go_today(self) -> None:
        self._ref = datetime.date.today()
        self.reload()

    # ── данные ───────────────────────────────────────────────────
    def reload(self, force: bool = False) -> None:
        """Пересобрать статистику. heavy-сканы — в фоне."""
        if force:
            try:
                from ..services import vault as _vault

                _vault.invalidate_vault_cache()
            except Exception:
                pass
        start, end = period_range(self._kind, self._ref)
        label = period_label(self._kind, self._ref)
        # hero
        self._period_label.set_text(label)
        if self._kind == "month":
            self._hero_title.set_text(f"Месяц · {label}")
        else:
            wid = iso_week_id(start)
            self._hero_title.set_text(f"Неделя {wid} · {label}")
        self._hero_sub.set_text(f"{start.isoformat()} — {end.isoformat()}  ·  {_period_days(start, end)} дн.")
        # статус
        target = get_review_path(self.settings, self._kind, self._ref)
        exists = target.exists()
        self._open_btn.set_sensitive(exists)
        self._create_btn.set_label("Пересоздать обзор" if exists else "Создать обзор")
        self._status_lbl.set_text(f"Файл: {target.relative_to(Path(str(self.settings.get('vault_root')))) if _is_relative_to(target, Path(str(self.settings.get('vault_root')))) else target.name} {'· уже есть' if exists else '· ещё не создан'}")
        # спинер kpi
        for k in self.kpi:
            self.kpi[k][0].set_text("…")
        # очистить списки, показать плейсхолдер
        self._clear_box(self._notes_box)
        self._clear_box(self._tasks_box)
        self._notes_box.append(Gtk.Label(label="загрузка…", css_classes=["dim-hint"]))
        self._tasks_box.append(Gtk.Label(label="загрузка…", css_classes=["dim-hint"]))
        # фон
        kind = self._kind
        ref = self._ref
        settings_copy = dict(self.settings)

        def work() -> None:
            try:
                data = collect_review_data(settings_copy, kind, ref)
            except Exception:
                data = {"stats": {}, "notes": [], "daily": [], "tasks": {"due_tasks": [], "done_tasks": []}}
            GLib.idle_add(self._apply_data, data)

        threading.Thread(target=work, daemon=True).start()

    def _apply_data(self, data: dict) -> bool:
        # отсечь устаревший ответ (пользователь уже переключил период)
        # проверка по метке периода
        cur_start, cur_end = period_range(self._kind, self._ref)
        if data.get("start") != cur_start or data.get("end") != cur_end:
            return False
        self._data = data
        stats = data.get("stats", {})
        # KPI
        self.kpi["notes"][0].set_text(str(stats.get("notes_count", 0)))
        self.kpi["daily"][0].set_text(str(stats.get("daily_count", 0)))
        self.kpi["due"][0].set_text(str(stats.get("due_period", 0)))
        self.kpi["done"][0].set_text(str(stats.get("done_period", 0)))
        self.kpi["overdue"][0].set_text(str(stats.get("overdue", 0)))
        self.kpi["words"][0].set_text(str(stats.get("words", 0)))
        # заметки
        self._clear_box(self._notes_box)
        notes: list[tuple[Path, float]] = data.get("notes", []) or []
        daily: list[Path] = data.get("daily", []) or []
        if not notes and not daily:
            self._notes_box.append(empty_state("🗒", "Заметок за период нет", hint="Создайте daily или обычные заметки — они появятся здесь"))
        else:
            # daily отдельно компактно
            if daily:
                self._notes_box.append(Gtk.Label(label=f"Daily · {len(daily)}", css_classes=["section-title"], halign=Gtk.Align.START, xalign=0))
                for p in daily[:14]:
                    self._notes_box.append(self._note_row(p, is_daily=True))
                if len(daily) > 14:
                    self._notes_box.append(Gtk.Label(label=f"и ещё {len(daily)-14} daily…", css_classes=["dim-hint"], halign=Gtk.Align.START, xalign=0))
                if notes:
                    self._notes_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            shown = 0
            for p, mt in notes[:20]:
                # не дублировать daily если уже показан
                if p in daily:
                    continue
                self._notes_box.append(self._note_row(p, mtime=mt))
                shown += 1
            if shown == 0 and daily:
                # только daily — уже показали
                pass
            elif len(notes) > shown:
                extra = len(notes) - shown - (len([1 for p,_ in notes if p in daily]))
                if extra > 0:
                    self._notes_box.append(Gtk.Label(label=f"и ещё {extra} заметок…", css_classes=["dim-hint"], halign=Gtk.Align.START, xalign=0))
            if not daily and shown == 0:
                self._notes_box.append(empty_state("🗒", "Заметок за период нет"))

        # задачи
        self._clear_box(self._tasks_box)
        tasks = data.get("tasks", {}) or {}
        due_tasks = tasks.get("due_tasks", []) or []
        done_tasks = tasks.get("done_tasks", []) or []
        if not due_tasks and not done_tasks:
            self._tasks_box.append(empty_state("✅", "Задач на период нет", hint="Задачи с due/scheduled/routine_next в этом интервале появятся здесь"))
        else:
            if due_tasks:
                self._tasks_box.append(Gtk.Label(label=f"К выполнению · {len(due_tasks)}", css_classes=["section-title"], halign=Gtk.Align.START, xalign=0))
                for t in due_tasks[:12]:
                    self._tasks_box.append(self._task_row(t))
                if len(due_tasks) > 12:
                    self._tasks_box.append(Gtk.Label(label=f"и ещё {len(due_tasks)-12}…", css_classes=["dim-hint"], halign=Gtk.Align.START, xalign=0))
            if done_tasks:
                if due_tasks:
                    self._tasks_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
                self._tasks_box.append(Gtk.Label(label=f"Выполнено · {len(done_tasks)}", css_classes=["section-title"], halign=Gtk.Align.START, xalign=0))
                for t in done_tasks[:12]:
                    self._tasks_box.append(self._task_row(t, done=True))
                if len(done_tasks) > 12:
                    self._tasks_box.append(Gtk.Label(label=f"и ещё {len(done_tasks)-12}…", css_classes=["dim-hint"], halign=Gtk.Align.START, xalign=0))
        return False

    def _note_row(self, path: Path, mtime: float | None = None, is_daily: bool = False) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["glass-row"])
        row.set_hexpand(True)
        icon = "📅" if is_daily else "📄"
        row.append(Gtk.Label(label=icon))
        # title из кэша
        try:
            from ..services.vault import note_title_cached as _ntc

            title = _ntc(path)
        except Exception:
            title = path.stem
        lbl = Gtk.Label(label=title, hexpand=True, halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        row.append(lbl)
        if mtime is not None:
            try:
                d = datetime.date.fromtimestamp(mtime)
                row.append(Gtk.Label(label=d.isoformat(), css_classes=["dim-hint"]))
            except Exception:
                pass
        elif is_daily:
            # дата из имени
            try:
                d = datetime.date.fromisoformat(path.stem)
                row.append(Gtk.Label(label=d.isoformat(), css_classes=["dim-hint"]))
            except Exception:
                pass
        btn = Gtk.Button(label="Открыть", css_classes=["link-btn"])
        btn.connect("clicked", lambda *_: self._open_path(path))
        row.append(btn)
        return row

    def _task_row(self, t, done: bool = False) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["glass-row"])
        row.set_hexpand(True)
        icon = "✓" if done else ("🔁" if getattr(t, "is_routine", False) else "☑️")
        row.append(Gtk.Label(label=icon))
        title = getattr(t, "title", None) or str(getattr(t, "path", "?"))
        if callable(title):
            title = t.title
        lbl = Gtk.Label(label=str(title), hexpand=True, halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        row.append(lbl)
        # бейдж due
        try:
            d = t.due_date or t.scheduled_date or t.routine_next_date
            if d:
                row.append(Gtk.Label(label=d.isoformat(), css_classes=["dim-hint"]))
        except Exception:
            pass
        return row

    def _open_path(self, path: Path) -> None:
        if self.on_open is not None:
            try:
                self.on_open(str(path))
                return
            except Exception:
                pass
        try:
            import subprocess

            subprocess.Popen(["xdg-open", str(path)])
        except Exception:
            pass

    # ── создание / открытие обзора ───────────────────────────────
    def _on_create_review(self, _btn: Gtk.Button) -> None:
        try:
            target = create_review_note(self.settings, self._kind, self._ref, overwrite=False)
            # если уже существовал — пересоздаём только по явному overwrite? сейчас не перезаписываем
            # для UX: если уже есть — второй клик должен перезаписать (показать диалог)
            if target.exists():
                # проверим, не только что создан ли (mtime недавний) — иначе предложить перезапись
                age = _file_age_seconds(target)
                if age is not None and age > 5:
                    self._confirm_overwrite()
                    return
            self._toast(f"создан обзор: {target.name}")
            self._open_path(target)
            # обновить состояние кнопок
            self._open_btn.set_sensitive(True)
            self._create_btn.set_label("Пересоздать обзор")
            self._status_lbl.set_text(f"Файл: {target.name} · создан")
        except Exception as exc:  # noqa: BLE001
            self._toast(f"ошибка: {exc}")

    def _confirm_overwrite(self) -> None:
        dlg = Adw.MessageDialog(
            transient_for=self.get_root(),  # type: ignore[arg-type]
            heading="Перезаписать обзор?",
            body=f"Файл уже существует: {get_review_path(self.settings, self._kind, self._ref).name}\nПерезаписать шаблоном?",
        )
        dlg.add_response("cancel", "Отмена")
        dlg.add_response("ok", "Перезаписать")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def _on_resp(d, resp: str) -> None:
            if resp == "ok":
                try:
                    target = create_review_note(self.settings, self._kind, self._ref, overwrite=True)
                    self._toast(f"перезаписан: {target.name}")
                    self._open_path(target)
                except Exception as exc:  # noqa: BLE001
                    self._toast(f"ошибка: {exc}")
            try:
                d.close()
            except Exception:
                pass

        dlg.connect("response", _on_resp)
        try:
            dlg.present()
        except Exception:
            # фолбэк: просто перезаписать
            try:
                target = create_review_note(self.settings, self._kind, self._ref, overwrite=True)
                self._toast(f"перезаписан: {target.name}")
                self._open_path(target)
            except Exception as exc:  # noqa: BLE001
                self._toast(f"ошибка: {exc}")

    def _on_open_review(self, _btn: Gtk.Button) -> None:
        target = get_review_path(self.settings, self._kind, self._ref)
        # также проверить фолбэк без подпапки года (старые файлы)
        if not target.exists():
            # поиск в Review/<weekId>.md
            folder = get_review_folder(self.settings)
            wid = iso_week_id(period_range(self._kind, self._ref)[0])
            alt = folder / f"{wid}.md"
            if alt.exists():
                target = alt
            elif self._kind == "month":
                alt2 = folder / f"{self._ref.year}-{self._ref.month:02d}.md"
                if alt2.exists():
                    target = alt2
        if not target.exists():
            self._toast("файл обзора ещё не создан")
            return
        self._open_path(target)

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

    def _clear_box(self, box: Gtk.Box) -> None:
        while (child := box.get_first_child()) is not None:
            box.remove(child)

    # для внешнего FileMonitor / refresh_all — alias
    def refresh(self) -> None:
        self.reload()

    def reload_if_needed(self) -> None:
        # если дата периода уже не включает сегодня и kind=week/month — не авто-рефетч; но если ref сегодня — обновим
        self.reload()


# ── утилиты ──────────────────────────────────────────────────────────


def _period_days(s: datetime.date, e: datetime.date) -> int:
    return (e - s).days + 1


def _is_relative_to(p: Path, base: Path) -> bool:
    try:
        p.relative_to(base)
        return True
    except Exception:
        return False


def _file_age_seconds(p: Path) -> float | None:
    try:
        return datetime.datetime.now().timestamp() - p.stat().st_mtime
    except Exception:
        return None


__all__ = [
    "ReviewView",
    "week_range",
    "month_range",
    "period_range",
    "period_label",
    "week_label",
    "month_label",
    "iso_week_id",
    "get_review_folder",
    "get_review_path",
    "review_exists",
    "scan_notes_for_period",
    "scan_daily_notes_for_period",
    "collect_tasks_stats",
    "collect_review_data",
    "build_review_markdown",
    "create_review_note",
]
