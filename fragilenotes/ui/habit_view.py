"""Habit tracker — отслеживание привычек (Obsidian-стиль).

Хранение: vault/_System/Habits/<slug>.md

Frontmatter привычки:
  title: "Зарядка"
  description: "15 мин утром"
  icon: "💪"
  color: "#88cc88"
  created: "2026-09-04"
  habit_type: "habit"
  completions: ["2026-09-01", "2026-09-03"]
  archived: false

Тело заметки — свободные заметки/журнал (не используется для логики).

Функции:
 - создание привычки (диалог)
 - ежедневная отметка (toggle сегодня/любой день)
 - календарь с heatmap (месячная сетка, интенсивность = доля привычек выполнена)
 - статистика streak: текущий, максимальный, всего, процент за 30 дней
 - хранение в vault/_System/Habits/
 - интеграция как вкладка (HabitView)

Чистые функции вынесены для тестирования без GTK.
"""

from __future__ import annotations

import calendar
import datetime
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from ..vault import parse_frontmatter, serialize_frontmatter  # noqa: E402
from .widgets import empty_state, view_header  # noqa: E402

# ── константы ───────────────────────────────────────────────────────

HABITS_REL = "_System/Habits"
HABIT_TYPE = "habit"

_RU_MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
_RU_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

_HEAT_CSS = {
    0: "cal-day--heat0",
    1: "cal-day--heat1",
    2: "cal-day--heat2",
    3: "cal-day--heat3",
    4: "cal-day--heat4",
}

# палитра иконок по умолчанию
DEFAULT_ICONS = ["🌱", "💪", "🧘", "📚", "💧", "🏃", "🎯", "🧠", "✍️", "🥗", "😴", "🔥"]
DEFAULT_COLOR = "#82a8ff"

_SLUG_RE = re.compile(r"[^a-z0-9а-яё\-_]+", re.IGNORECASE)


# ── пути ────────────────────────────────────────────────────────────

def get_habits_dir(settings: dict) -> Path:
    """Папка habits: vault/_System/Habits/."""
    root = Path(str(settings.get("vault_root") or Path.home() / "desktop"))
    return root / HABITS_REL


def ensure_habits_dir(settings: dict) -> Path:
    p = get_habits_dir(settings)
    p.mkdir(parents=True, exist_ok=True)
    return p


def slugify(name: str) -> str:
    """Слаг для файла привычки: транслит-lite."""
    s = name.strip().lower()
    s = s.replace(" ", "-").replace("_", "-")
    s = _SLUG_RE.sub("-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    # транслит кириллицы в латиницу минимально, но оставляем кириллицу если надо — filesystem utf8
    # если слаг стал пустым — fallback
    if not s:
        s = "habit"
    # ограничим длину
    return s[:64]


def _parse_date(value: Any) -> datetime.date | None:
    if value is None:
        return None
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.datetime):
        return value.date()
    s = str(value).strip()
    if not s:
        return None
    # пробуем iso
    try:
        return datetime.date.fromisoformat(s.split("T")[0].split(" ")[0])
    except Exception:
        return None


def _today() -> datetime.date:
    return datetime.date.today()


# ── модель ──────────────────────────────────────────────────────────

@dataclass
class Habit:
    path: Path
    fm: dict = field(default_factory=dict)
    body: str = ""
    title: str = ""
    description: str = ""
    icon: str = "🌱"
    color: str = DEFAULT_COLOR
    created: datetime.date | None = None
    completions: set[datetime.date] = field(default_factory=set)
    archived: bool = False

    @property
    def slug(self) -> str:
        return self.path.stem


def _habit_from_file(path: Path) -> Habit | None:
    """Загрузить Habit из .md файла."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm, body = parse_frontmatter(text)
    # только habit_type habit (или без типа но в папке habits считаем привычкой)
    # если файл не habit — всё равно грузим если лежит в habits папке
    title = str(fm.get("title") or path.stem).strip() or path.stem
    desc = str(fm.get("description") or fm.get("desc") or "").strip()
    icon = str(fm.get("icon") or "🌱").strip() or "🌱"
    # взять первый символ/эмодзи
    if len(icon) > 4:
        icon = icon[:2]
    color = str(fm.get("color") or DEFAULT_COLOR).strip() or DEFAULT_COLOR
    created = _parse_date(fm.get("created"))
    raw_compl = fm.get("completions") or fm.get("log") or fm.get("dates") or []
    completions: set[datetime.date] = set()
    if isinstance(raw_compl, list):
        for v in raw_compl:
            d = _parse_date(v)
            if d is not None:
                completions.add(d)
    elif isinstance(raw_compl, dict):
        for k, v in raw_compl.items():
            if v:
                d = _parse_date(k)
                if d is not None:
                    completions.add(d)
    elif isinstance(raw_compl, str):
        d = _parse_date(raw_compl)
        if d is not None:
            completions.add(d)
    archived = bool(fm.get("archived"))
    return Habit(
        path=path,
        fm=dict(fm),
        body=body,
        title=title,
        description=desc,
        icon=icon,
        color=color,
        created=created,
        completions=completions,
        archived=archived,
    )


def load_habits(settings: dict) -> list[Habit]:
    """Все привычки из vault/_System/Habits/ (отсортированы по title)."""
    d = get_habits_dir(settings)
    if not d.is_dir():
        return []
    out: list[Habit] = []
    try:
        for p in d.glob("*.md"):
            if not p.is_file():
                continue
            h = _habit_from_file(p)
            if h is not None and not h.archived:
                out.append(h)
            elif h is not None and h.archived:
                # archived тоже показываем но в конце? по умолчанию скрываем
                # пока скрываем, но оставляем возможность load_habits(include_archived=True) позже
                continue
    except OSError:
        return []
    out.sort(key=lambda h: h.title.lower())
    return out


def load_all_habits(settings: dict, include_archived: bool = False) -> list[Habit]:
    d = get_habits_dir(settings)
    if not d.is_dir():
        return []
    out: list[Habit] = []
    for p in d.glob("*.md"):
        h = _habit_from_file(p)
        if h is None:
            continue
        if not include_archived and h.archived:
            continue
        out.append(h)
    out.sort(key=lambda h: h.title.lower())
    return out


def save_habit(habit: Habit) -> None:
    """Сохранить Habit обратно в файл (обновляет frontmatter)."""
    fm = dict(habit.fm)
    fm["title"] = habit.title
    if habit.description:
        fm["description"] = habit.description
    fm["icon"] = habit.icon
    fm["color"] = habit.color
    if habit.created is not None:
        fm["created"] = habit.created.isoformat()
    else:
        fm.setdefault("created", _today().isoformat())
    fm["habit_type"] = HABIT_TYPE
    # completions как отсортированный список iso
    fm["completions"] = sorted(d.isoformat() for d in habit.completions)
    fm["archived"] = bool(habit.archived)
    # тело — description как заголовок + существующее тело
    body = habit.body
    # если тело пустое — подставим описание
    if not body.strip() and habit.description:
        body = habit.description + "\n"
    content = serialize_frontmatter(fm) + body.lstrip("\n")
    habit.path.parent.mkdir(parents=True, exist_ok=True)
    habit.path.write_text(content, encoding="utf-8")
    habit.fm = fm
    try:
        from ..services import vault as _vault
        _vault.invalidate_vault_cache()
    except Exception:
        pass


def create_habit(
    settings: dict,
    title: str,
    description: str = "",
    icon: str = "🌱",
    color: str = DEFAULT_COLOR,
) -> Habit:
    """Создать файл привычки. Возвращает Habit. Бросает ValueError если уже есть."""
    title = title.strip()
    if not title:
        raise ValueError("Название привычки пустое")
    slug = slugify(title)
    d = ensure_habits_dir(settings)
    # уникальность слага
    base = slug
    idx = 1
    while (d / f"{slug}.md").exists():
        idx += 1
        slug = f"{base}-{idx}"
    path = d / f"{slug}.md"
    icon = (icon or "🌱").strip() or "🌱"
    color = (color or DEFAULT_COLOR).strip() or DEFAULT_COLOR
    habit = Habit(
        path=path,
        fm={},
        body=f"# {title}\n\n{description}\n" if description else f"# {title}\n",
        title=title,
        description=description.strip(),
        icon=icon,
        color=color,
        created=_today(),
        completions=set(),
        archived=False,
    )
    save_habit(habit)
    return habit


def delete_habit(habit: Habit) -> None:
    try:
        habit.path.unlink()
    except OSError:
        pass
    try:
        from ..services import vault as _vault
        _vault.invalidate_vault_cache()
    except Exception:
        pass


def toggle_completion(habit: Habit, day: datetime.date, done: bool | None = None) -> bool:
    """Переключить отметку дня. done=None — инвертировать. Возвращает новое состояние (True если отмечено)."""
    if done is None:
        done = day not in habit.completions
    if done:
        habit.completions.add(day)
    else:
        habit.completions.discard(day)
    save_habit(habit)
    return done


def set_completion(habit: Habit, day: datetime.date, done: bool) -> None:
    if done:
        habit.completions.add(day)
    else:
        habit.completions.discard(day)
    save_habit(habit)


# ── статистика streak ───────────────────────────────────────────────

def calc_streak(completions: set[datetime.date], today: datetime.date | None = None) -> dict:
    """Посчитать streak.

    Возвращает dict:
      current: серия подряд до today (если today не выполнен — 0)
      current_including_yesterday: серия до вчера если сегодня пропущен (для UI)
      longest: максимальная серия
      total: всего отметок
      last_date: последняя дата отметки или None
    """
    if today is None:
        today = _today()
    if not completions:
        return {"current": 0, "current_with_gap": 0, "longest": 0, "total": 0, "last_date": None}
    s = set(completions)
    total = len(s)
    last_date = max(s) if s else None
    # current streak ending today
    cur = 0
    d = today
    while d in s:
        cur += 1
        d -= datetime.timedelta(days=1)
    # current with one-day gap tolerance (если сегодня не выполнен — считаем до вчера)
    cur_gap = cur
    if cur == 0:
        # пробуем от вчера
        d = today - datetime.timedelta(days=1)
        g = 0
        while d in s:
            g += 1
            d -= datetime.timedelta(days=1)
        cur_gap = g
    # longest
    if not s:
        longest = 0
    else:
        sorted_dates = sorted(s)
        longest = 1
        run = 1
        for i in range(1, len(sorted_dates)):
            if sorted_dates[i] - sorted_dates[i - 1] == datetime.timedelta(days=1):
                run += 1
                longest = max(longest, run)
            else:
                run = 1
        # если только одна дата — longest 1
    return {"current": cur, "current_with_gap": cur_gap, "longest": longest, "total": total, "last_date": last_date}


def calc_stats(habit: Habit, today: datetime.date | None = None) -> dict:
    """Расширенная статистика привычки."""
    if today is None:
        today = _today()
    streak = calc_streak(habit.completions, today)
    total = streak["total"]
    # процент за последние 30 дней
    start30 = today - datetime.timedelta(days=29)
    cnt30 = sum(1 for d in habit.completions if start30 <= d <= today)
    pct30 = round(cnt30 / 30 * 100) if 30 else 0
    # за 7 дней
    start7 = today - datetime.timedelta(days=6)
    cnt7 = sum(1 for d in habit.completions if start7 <= d <= today)
    pct7 = round(cnt7 / 7 * 100) if 7 else 0
    # дни с создания (учитываем первую отметку если она раньше created)
    created = habit.created or today
    if habit.completions:
        first = min(habit.completions)
        if first < created:
            created = first
    days_since = (today - created).days + 1
    if days_since < 1:
        days_since = 1
    overall_pct = round(total / days_since * 100) if days_since else 0
    if overall_pct > 100:
        overall_pct = 100
    return {
        **streak,
        "count_30": cnt30,
        "pct_30": pct30,
        "count_7": cnt7,
        "pct_7": pct7,
        "days_since": days_since,
        "overall_pct": overall_pct,
    }


def aggregated_heatmap(habits: list[Habit]) -> dict[datetime.date, int]:
    """Для каждой даты — сколько привычек выполнено (для overall heatmap)."""
    out: dict[datetime.date, int] = {}
    for h in habits:
        for d in h.completions:
            out[d] = out.get(d, 0) + 1
    return out


def heatmap_level(count: int, total: int) -> int:
    """Уровень 0..4 для heatmap ячейки."""
    if total <= 0 or count <= 0:
        return 0
    if total == 1:
        return 4 if count >= 1 else 0
    ratio = count / total
    if ratio >= 1.0:
        return 4
    if ratio >= 0.75:
        return 3
    if ratio >= 0.5:
        return 2
    if ratio >= 0.25:
        return 1
    return 1 if count > 0 else 0


# ── UI ───────────────────────────────────────────────────────────────

class HabitView(Gtk.Box):
    """Вкладка Привычки: создание, ежедневная отметка, календарь heatmap, статистика streak."""

    def __init__(self, settings: dict, on_open=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self.on_open = on_open
        self._habits: list[Habit] = []
        self._selected: Habit | None = None
        self._cal_current = _today().replace(day=1)
        self._cal_selected = _today()
        self._cal_cells: list[Gtk.Button] = []
        self._habit_cards: dict[str, Gtk.Box] = {}

        self.append(view_header("🌱", "Привычки", "Ежедневные ритуалы · отметка · календарь heatmap · серия (streak)"))

        self._build_toolbar()
        self._build_hero()
        self._build_kpi()
        self._build_calendar()
        self._build_habit_list()

        self.reload()

    # ── toolbar ──────────────────────────────────────────────────
    def _build_toolbar(self) -> None:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["toolbar", "cal-toolbar"])
        bar.set_margin_start(14)
        bar.set_margin_end(14)

        new_btn = Gtk.Button(label="＋ Новая привычка", css_classes=["suggested-action", "mod-cta"])
        new_btn.set_tooltip_text("Создать новую привычку")
        new_btn.connect("clicked", lambda *_: self._open_create_dialog())
        bar.append(new_btn)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Обновить")
        refresh.connect("clicked", lambda *_: self.reload(force=True))
        bar.append(refresh)

        bar.append(Gtk.Box(hexpand=True))

        self._today_btn = Gtk.Button(label="Сегодня", css_classes=["pill"])
        self._today_btn.set_tooltip_text("Отметить все привычки за сегодня / перейти к сегодня")
        self._today_btn.connect("clicked", self._on_today_clicked)
        bar.append(self._today_btn)

        self.append(bar)

    def _build_hero(self) -> None:
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, css_classes=["tm-hero-v2", "tm-hero-v2--dash"])
        hero.append(Gtk.Label(label="Привычки", css_classes=["tm-hero-v2__eyebrow"], halign=Gtk.Align.START, xalign=0))
        self._hero_title = Gtk.Label(label="Отслеживай ежедневные ритуалы", css_classes=["tm-hero-v2__title"], halign=Gtk.Align.START, xalign=0)
        hero.append(self._hero_title)
        self._hero_sub = Gtk.Label(label="", css_classes=["tm-hero-v2__date"], halign=Gtk.Align.START, xalign=0)
        hero.append(self._hero_sub)
        self.append(hero)

    def _build_kpi(self) -> None:
        strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, css_classes=["tm-today-strip"])
        strip.append(Gtk.Label(label="Сводка", css_classes=["tm-today-strip__label"]))
        self.kpi: dict[str, tuple[Gtk.Label, Gtk.Label]] = {}
        for key, label, tone in [
            ("total", "всего", ""),
            ("done_today", "выполнено сегодня", "ok"),
            ("streak_max", "макс серия", "run"),
            ("pct_7", "7 дней %", "warn"),
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

    def _build_calendar(self) -> None:
        """Месячная сетка heatmap + навигация."""
        # toolbar календаря
        cal_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["toolbar", "cal-toolbar"])
        cal_bar.set_margin_start(14)
        cal_bar.set_margin_end(14)

        self._cal_prev = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Предыдущий месяц")
        self._cal_prev.connect("clicked", lambda *_: self._shift_month(-1))
        cal_bar.append(self._cal_prev)

        self._cal_next = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Следующий месяц")
        self._cal_next.connect("clicked", lambda *_: self._shift_month(1))
        cal_bar.append(self._cal_next)

        today_cal = Gtk.Button(label="Сегодня", css_classes=["pill"])
        today_cal.connect("clicked", lambda *_: self._go_today_cal())
        cal_bar.append(today_cal)

        self._cal_month_label = Gtk.Label(label="", css_classes=["cal-month"], halign=Gtk.Align.START, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        cal_bar.append(self._cal_month_label)

        self._cal_detail = Gtk.Label(label="", css_classes=["dim-hint", "cal-detail__label"], halign=Gtk.Align.END, xalign=1, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        cal_bar.append(self._cal_detail)

        self.append(cal_bar)

        # grid
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

        for col, wd in enumerate(_RU_WEEKDAYS):
            lbl = Gtk.Label(label=wd, css_classes=["cal-weekday"])
            if col >= 5:
                lbl.add_css_class("cal-weekend")
            grid.attach(lbl, col, 0, 1, 1)

        for idx in range(42):
            row = 1 + idx // 7
            col = idx % 7
            btn = Gtk.Button(css_classes=["cal-day", "cal-day--heat0"])
            btn.set_can_focus(True)
            btn.set_focusable(True)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            num = Gtk.Label(label="", css_classes=["cal-day__num"])
            dot = Gtk.Box(css_classes=["cal-day__dot"])
            dot.set_size_request(6, 6)
            box.append(num)
            box.append(dot)
            btn.set_child(box)
            btn._cal_num = num  # type: ignore[attr-defined]
            btn._cal_dot = dot  # type: ignore[attr-defined]
            btn._cal_date: datetime.date | None = None  # type: ignore[attr-defined]
            btn.connect("clicked", self._on_cal_day_clicked)
            grid.attach(btn, col, row, 1, 1)
            self._cal_cells.append(btn)

        frame.append(grid)
        clamp.set_child(frame)
        self._cal_grid = grid
        self.append(clamp)

        # legend
        leg = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["cal-legend"])
        leg.set_margin_start(14)
        leg.set_margin_end(14)
        leg.append(Gtk.Label(label="Выполнение:", css_classes=["dim-label"]))
        for lvl in range(5):
            sw = Gtk.Box(css_classes=["cal-legend__swatch", _HEAT_CSS[lvl]])
            sw.set_size_request(14, 14)
            sw.set_tooltip_text(f"уровень {lvl}")
            leg.append(sw)
        leg.append(Gtk.Label(label="меньше", css_classes=["dim-label", "cal-legend__edge"]))
        leg.append(Gtk.Label(label="больше", css_classes=["dim-label", "cal-legend__edge"]))
        self._cal_legend_streak = Gtk.Label(label="", css_classes=["dim-hint", "cal-streak"], halign=Gtk.Align.END, xalign=1, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        leg.append(self._cal_legend_streak)
        self.append(leg)

    def _build_habit_list(self) -> None:
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scroller.set_child(self._list)
        scroller.set_margin_start(14)
        scroller.set_margin_end(14)
        scroller.set_margin_bottom(14)
        self.append(scroller)

    # ── reload ───────────────────────────────────────────────────
    def reload(self, force: bool = False) -> None:
        if force:
            try:
                from ..services import vault as _vault
                _vault.invalidate_vault_cache()
            except Exception:
                pass
        # фоновая загрузка habits (диск)
        settings_copy = dict(self.settings)

        def work() -> None:
            try:
                habits = load_habits(settings_copy)
            except Exception:
                habits = []
            GLib.idle_add(lambda: self._apply_habits(habits) or False)

        threading.Thread(target=work, daemon=True).start()
        # пока грузится — показываем спинер
        self._render_kpi_loading()
        self._render_calendar_loading()

    def _apply_habits(self, habits: list[Habit]) -> bool:
        self._habits = habits
        self._render_kpi()
        self._render_hero()
        self._render_calendar()
        self._render_list()
        return False

    def _render_kpi_loading(self) -> None:
        for k in self.kpi:
            self.kpi[k][0].set_text("…")

    def _render_calendar_loading(self) -> None:
        pass

    def _render_hero(self) -> None:
        n = len(self._habits)
        today = _today()
        done_today = sum(1 for h in self._habits if today in h.completions)
        self._hero_title.set_text(f"{n} привычек" if n else "Нет привычек — создай первую")
        if n == 0:
            self._hero_sub.set_text("Нажми «＋ Новая привычка» чтобы начать отслеживать")
        else:
            # лучший streak
            best = 0
            for h in self._habits:
                s = calc_streak(h.completions, today)
                best = max(best, s["current_with_gap"] if s["current"] == 0 else s["current"])
                best = max(best, s["longest"])
            self._hero_sub.set_text(f"Сегодня выполнено {done_today}/{n} · лучшая серия {best} дн. · хранение в vault/_System/Habits/")

    def _render_kpi(self) -> None:
        n = len(self._habits)
        today = _today()
        done_today = sum(1 for h in self._habits if today in h.completions)
        # макс серия среди привычек
        max_streak = 0
        for h in self._habits:
            s = calc_streak(h.completions, today)
            cur = s["current"] if s["current"] > 0 else s["current_with_gap"]
            max_streak = max(max_streak, cur, s["longest"])
        # средний % за 7 дней
        if n:
            avg7 = sum(calc_stats(h, today)["pct_7"] for h in self._habits) / n
        else:
            avg7 = 0
        self.kpi["total"][0].set_text(str(n))
        self.kpi["done_today"][0].set_text(f"{done_today}/{n}" if n else "0")
        self.kpi["streak_max"][0].set_text(str(max_streak) if n else "0")
        self.kpi["pct_7"][0].set_text(f"{avg7:.0f}%" if n else "—")

    # ── calendar ─────────────────────────────────────────────────
    def _shift_month(self, delta: int) -> None:
        y, m = self._cal_current.year, self._cal_current.month
        m += delta
        while m > 12:
            m -= 12
            y += 1
        while m < 1:
            m += 12
            y -= 1
        self._cal_current = datetime.date(y, m, 1)
        self._render_calendar()

    def _go_today_cal(self) -> None:
        self._cal_current = _today().replace(day=1)
        self._cal_selected = _today()
        self._render_calendar()

    def _month_cells(self) -> list[datetime.date]:
        first = self._cal_current
        cal = calendar.Calendar(firstweekday=0)
        weeks = cal.monthdatescalendar(first.year, first.month)
        flat: list[datetime.date] = [d for w in weeks for d in w]
        while len(flat) < 42:
            last = flat[-1]
            nxt = [last + datetime.timedelta(days=i) for i in range(1, 8)]
            flat.extend(nxt)
        return flat[:42]

    def _render_calendar(self) -> None:
        if not hasattr(self, "_cal_month_label"):
            return
        y, m = self._cal_current.year, self._cal_current.month
        self._cal_month_label.set_text(f"{_RU_MONTHS[m-1]} {y}")
        today = _today()
        n = len(self._habits)
        heat = aggregated_heatmap(self._habits) if n else {}
        # streak для легенды — max current
        best_cur = 0
        for h in self._habits:
            s = calc_streak(h.completions, today)
            cur = s["current"] if s["current"] > 0 else s["current_with_gap"]
            best_cur = max(best_cur, cur)
        if best_cur:
            self._cal_legend_streak.set_text(f"🔥 серия {best_cur} дн.")
        else:
            self._cal_legend_streak.set_text("")
        cells = self._month_cells()
        for btn, d in zip(self._cal_cells, cells):
            btn._cal_date = d  # type: ignore[attr-defined]
            num: Gtk.Label = btn._cal_num  # type: ignore[attr-defined]
            num.set_text(str(d.day))
            for lvl_cls in _HEAT_CSS.values():
                btn.remove_css_class(lvl_cls)
            btn.remove_css_class("cal-day--outside")
            btn.remove_css_class("cal-day--today")
            btn.remove_css_class("cal-day--selected")
            btn.remove_css_class("cal-day--weekend")
            dot: Gtk.Box = btn._cal_dot  # type: ignore[attr-defined]
            if d.month != self._cal_current.month:
                btn.add_css_class("cal-day--outside")
            if d.weekday() >= 5:
                btn.add_css_class("cal-day--weekend")
            cnt = heat.get(d, 0) if n else 0
            lvl = heatmap_level(cnt, n) if n else 0
            btn.add_css_class(_HEAT_CSS.get(lvl, "cal-day--heat0"))
            if lvl > 0:
                dot.set_visible(True)
                for c in list(dot.get_css_classes()):
                    if c.startswith("cal-dot--"):
                        dot.remove_css_class(c)
                dot.add_css_class(f"cal-dot--{lvl}")
            else:
                dot.set_visible(False)
            if d == today:
                btn.add_css_class("cal-day--today")
            if d == self._cal_selected:
                btn.add_css_class("cal-day--selected")
            # tooltip
            try:
                tip = d.isoformat()
                if n:
                    tip += f" · {cnt}/{n} выполнено"
                    if cnt:
                        # список что выполнено
                        done_names = [h.title for h in self._habits if d in h.completions][:3]
                        if done_names:
                            tip += " · " + ", ".join(done_names)
                            if cnt > 3:
                                tip += f" +{cnt-3}"
                if d == today:
                    tip += " · сегодня"
                btn.set_tooltip_text(tip)
                btn.update_property(Gtk.AccessibleProperty.LABEL, tip)
            except Exception:
                pass
        # detail bar
        lvl_sel = heatmap_level(heat.get(self._cal_selected, 0), n) if n else 0
        cnt_sel = heat.get(self._cal_selected, 0) if n else 0
        self._cal_detail.set_text(f"{_RU_WEEKDAYS[self._cal_selected.weekday()]}, {self._cal_selected.day} {_RU_MONTHS[self._cal_selected.month-1].lower()} {self._cal_selected.year} · {cnt_sel}/{n} · уровень {lvl_sel}" if n else f"{self._cal_selected.isoformat()} · нет привычек")

    def _on_cal_day_clicked(self, btn: Gtk.Button) -> None:
        d: datetime.date | None = getattr(btn, "_cal_date", None)
        if d is None:
            return
        self._cal_selected = d
        # клик по дате — показать статус + предложить отметить привычки
        # если выбранная дата — сегодня, переключаем? нет, просто выбираем и обновляем список подсветку
        self._render_calendar()
        # подсветить карточки habit где этот день выполнен/нет (опционально — обновить list)
        self._render_list(highlight_date=d)

    # ── habit list ───────────────────────────────────────────────
    def _render_list(self, highlight_date: datetime.date | None = None) -> None:
        assert hasattr(self, "_list")
        while (child := self._list.get_first_child()) is not None:
            self._list.remove(child)
        self._habit_cards.clear()
        if not self._habits:
            self._list.append(empty_state("🌱", "Пока нет привычек", hint="Создай первую привычку — нажми «＋ Новая привычка». Хранение в vault/_System/Habits/", action_label="Создать привычку", on_action=self._open_create_dialog))
            return
        today = _today()
        for habit in self._habits:
            card = self._build_habit_card(habit, highlight_date=highlight_date, today=today)
            self._list.append(card)
            self._habit_cards[habit.path.stem] = card

    def _build_habit_card(self, habit: Habit, highlight_date: datetime.date | None = None, today: datetime.date | None = None) -> Gtk.Box:
        if today is None:
            today = _today()
        stats = calc_stats(habit, today)
        streak = stats["current"] if stats["current"] > 0 else stats["current_with_gap"]
        # карточка
        tone = "focus" if today in habit.completions else ("overdue" if streak == 0 and stats["total"] > 0 else "routines")
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, css_classes=["tm-daily-card", f"tm-daily-card--{tone}"])
        # header
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["tm-daily-card__header"])
        head.append(Gtk.Label(label=habit.icon or "🌱", css_classes=["tm-daily-card__icon"]))
        head_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, hexpand=True)
        head_box.append(Gtk.Label(label=habit.title, css_classes=["tm-daily-card__title"], halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END))
        subtitle = habit.description or f"создана {habit.created.isoformat() if habit.created else '—'}"
        head_box.append(Gtk.Label(label=subtitle, css_classes=["tm-daily-card__subtitle"], halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END))
        head.append(head_box)
        # streak badge
        if streak > 0:
            head.append(Gtk.Label(label=f"🔥 {streak}", css_classes=["tm-daily-badge", "tm-daily-badge--warn"], valign=Gtk.Align.CENTER))
        elif stats["total"] == 0:
            head.append(Gtk.Label(label="новая", css_classes=["tm-daily-badge"], valign=Gtk.Align.CENTER))
        # count
        head.append(Gtk.Label(label=str(stats["total"]), css_classes=["tm-daily-card__count"], valign=Gtk.Align.CENTER))
        card.append(head)

        # stats row
        stats_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10, css_classes=["tm-today-strip"])
        stats_row.set_margin_top(8)
        stats_row.set_margin_start(10)
        stats_row.set_margin_end(10)
        for key, label, val in [
            ("cur", "серия", f"{streak}"),
            ("long", "макс", f"{stats['longest']}"),
            ("pct7", "7д", f"{stats['pct_7']}%"),
            ("pct30", "30д", f"{stats['pct_30']}%"),
        ]:
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, css_classes=["tm-kpi-chip"])
            col.append(Gtk.Label(label=val, css_classes=["tm-kpi-chip__value"]))
            col.append(Gtk.Label(label=label, css_classes=["tm-kpi-chip__label"]))
            stats_row.append(col)
        card.append(stats_row)

        # 7-day toggle row
        week_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        week_row.set_margin_top(8)
        week_row.set_margin_start(10)
        week_row.set_margin_end(10)
        # показываем последние 7 дней включая сегодня
        for offset in range(6, -1, -1):
            d = today - datetime.timedelta(days=offset)
            done = d in habit.completions
            is_today = d == today
            is_highlight = highlight_date is not None and d == highlight_date
            btn = Gtk.ToggleButton(label=f"{d.day:02d}", css_classes=["pill"] + (["pill-ok"] if done else []))
            if is_today:
                btn.add_css_class("pill-run")
            if is_highlight:
                btn.add_css_class("pill-warn")
            btn.set_active(done)
            btn.set_tooltip_text(f"{d.isoformat()} · {'✓ выполнено' if done else 'не выполнено'}" + (" · сегодня" if is_today else ""))
            # замыкание
            def _make_toggle(h=habit, date=d, b=btn):
                def _on_toggle(_btn: Gtk.ToggleButton) -> None:
                    new_state = _btn.get_active()
                    # сохранить
                    try:
                        set_completion(h, date, new_state)
                    except Exception as exc:  # noqa: BLE001
                        self._toast(f"ошибка: {exc}")
                        _btn.set_active(not new_state)
                        return
                    # обновить UI
                    if new_state:
                        _btn.add_css_class("pill-ok")
                    else:
                        _btn.remove_css_class("pill-ok")
                    self._render_kpi()
                    self._render_hero()
                    self._render_calendar()
                    # обновить эту карточку без полного перерендера — но проще перерендерить список
                    # debounce: не дергаем весь список на каждый клик, только kpi/calendar
                    self._toast(f"{'✓' if new_state else '○'} {h.title} · {date.isoformat()}")
                return _on_toggle
            btn.connect("toggled", _make_toggle())
            # подпись дня недели под кнопкой
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            wd_lbl = Gtk.Label(label=_RU_WEEKDAYS[d.weekday()], css_classes=["dim-label"])
            wd_lbl.set_halign(Gtk.Align.CENTER)
            col.append(wd_lbl)
            col.append(btn)
            week_row.append(col)
        card.append(week_row)

        # today big toggle + actions
        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_row.set_margin_top(8)
        action_row.set_margin_start(10)
        action_row.set_margin_end(10)
        action_row.set_margin_bottom(10)
        done_today = today in habit.completions
        check = Gtk.ToggleButton(label="✓ Отметить сегодня" if not done_today else "✓ Выполнено сегодня", css_classes=["suggested-action", "mod-cta"] if not done_today else ["pill", "pill-ok"])
        check.set_active(done_today)
        def _on_today_toggle(_btn: Gtk.ToggleButton, h=habit) -> None:
            new_state = _btn.get_active()
            try:
                set_completion(h, today, new_state)
            except Exception as exc:  # noqa: BLE001
                self._toast(f"ошибка: {exc}")
                _btn.set_active(not new_state)
                return
            _btn.set_label("✓ Выполнено сегодня" if new_state else "✓ Отметить сегодня")
            if new_state:
                _btn.remove_css_class("suggested-action")
                _btn.remove_css_class("mod-cta")
                _btn.add_css_class("pill-ok")
            else:
                _btn.add_css_class("suggested-action")
                _btn.add_css_class("mod-cta")
                _btn.remove_css_class("pill-ok")
            self._render_kpi()
            self._render_hero()
            self._render_calendar()
            self._render_list(highlight_date=self._cal_selected)
            self._toast(f"{'✓' if new_state else '○'} {h.title} · {today.isoformat()}")
        check.connect("toggled", _on_today_toggle)
        action_row.append(check)

        edit_btn = Gtk.Button(label="✎", tooltip_text="Редактировать")
        edit_btn.connect("clicked", lambda *_: self._open_edit_dialog(habit))
        action_row.append(edit_btn)

        del_btn = Gtk.Button(label="🗑", tooltip_text="Удалить привычку", css_classes=["destructive-action"])
        del_btn.connect("clicked", lambda *_: self._confirm_delete(habit))
        action_row.append(del_btn)

        # открыть файл
        open_btn = Gtk.Button(label="Открыть", css_classes=["link-btn"])
        open_btn.connect("clicked", lambda *_: self._open_habit_file(habit))
        action_row.append(open_btn)

        # heatmap mini: последние 21 день как точки
        heat_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        heat_row.set_margin_top(4)
        heat_row.set_halign(Gtk.Align.START)
        for offset in range(20, -1, -1):
            d = today - datetime.timedelta(days=offset)
            done = d in habit.completions
            dot = Gtk.Box(css_classes=["cal-day__dot"] + (["cal-dot--4"] if done else []))
            dot.set_size_request(8, 8)
            if not done:
                dot.set_opacity(0.18)
            dot.set_tooltip_text(f"{d.isoformat()} · {'✓' if done else '○'}")
            heat_row.append(dot)
        action_row.append(Gtk.Box(hexpand=True))
        # небольшой hint
        action_row.append(heat_row)

        card.append(action_row)

        return card

    # ── dialogs ──────────────────────────────────────────────────
    def _open_create_dialog(self) -> None:
        self._open_habit_dialog(None)

    def _open_edit_dialog(self, habit: Habit) -> None:
        self._open_habit_dialog(habit)

    def _open_habit_dialog(self, habit: Habit | None) -> None:
        is_edit = habit is not None
        dlg = Adw.Dialog(title="Редактировать привычку" if is_edit else "Новая привычка")
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, css_classes=["dialog-body"],
                       margin_top=8, margin_bottom=8, margin_start=24, margin_end=24)
        body.set_size_request(380, -1)

        # title
        title_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title_row.append(Gtk.Label(label="Название *", halign=Gtk.Align.START, xalign=0, css_classes=["settings-label"]))
        title_entry = Gtk.Entry(placeholder_text="Например: Зарядка, Чтение, Медитация", hexpand=True)
        if is_edit:
            title_entry.set_text(habit.title)  # type: ignore[union-attr]
        title_row.append(title_entry)
        body.append(title_row)

        # description
        desc_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        desc_row.append(Gtk.Label(label="Описание", halign=Gtk.Align.START, xalign=0, css_classes=["settings-label"]))
        desc_entry = Gtk.Entry(placeholder_text="Краткое описание (необязательно)", hexpand=True)
        if is_edit and habit.description:  # type: ignore[union-attr]
            desc_entry.set_text(habit.description)  # type: ignore[union-attr]
        desc_row.append(desc_entry)
        body.append(desc_row)

        # icon
        icon_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        icon_row.append(Gtk.Label(label="Иконка", halign=Gtk.Align.START, xalign=0, css_classes=["settings-label"]))
        icon_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon_entry = Gtk.Entry(placeholder_text="эмодзи, напр. 🌱", hexpand=False)
        icon_entry.set_width_chars(4)
        icon_entry.set_max_width_chars(4)
        cur_icon = (habit.icon if is_edit else "🌱")  # type: ignore[union-attr]
        icon_entry.set_text(cur_icon or "🌱")
        icon_box.append(icon_entry)
        # quick icons
        for ic in DEFAULT_ICONS[:8]:
            b = Gtk.Button(label=ic, css_classes=["flat"])
            b.set_size_request(32, 32)
            def _on_pick(_btn, val=ic):
                icon_entry.set_text(val)
            b.connect("clicked", _on_pick)
            icon_box.append(b)
        icon_row.append(icon_box)
        body.append(icon_row)

        # color
        color_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        color_row.append(Gtk.Label(label="Цвет", halign=Gtk.Align.START, xalign=0, css_classes=["settings-label"]))
        color_entry = Gtk.Entry(placeholder_text="#82a8ff", hexpand=True)
        color_entry.set_text((habit.color if is_edit else DEFAULT_COLOR) or DEFAULT_COLOR)  # type: ignore[union-attr]
        color_row.append(color_entry)
        body.append(color_row)

        hint = Gtk.Label(label="Хранение: vault/_System/Habits/<slug>.md · completions в frontmatter", halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"], wrap=True)
        body.append(hint)

        # buttons
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["btn-row"])
        btn_row.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Отмена", css_classes=["mod-neutral"])
        cancel.connect("clicked", lambda *_: dlg.close())
        btn_row.append(cancel)
        if is_edit:
            save = Gtk.Button(label="Сохранить", css_classes=["suggested-action", "mod-cta"])
            def _do_save(*_):
                title = title_entry.get_text().strip()
                if not title:
                    self._toast("Укажи название")
                    return
                desc = desc_entry.get_text().strip()
                icon = icon_entry.get_text().strip() or "🌱"
                color = color_entry.get_text().strip() or DEFAULT_COLOR
                # если title изменился — slug не меняем (путь остаётся), только title
                habit.title = title  # type: ignore[union-attr]
                habit.description = desc  # type: ignore[union-attr]
                habit.icon = icon  # type: ignore[union-attr]
                habit.color = color  # type: ignore[union-attr]
                try:
                    save_habit(habit)  # type: ignore[arg-type]
                except Exception as exc:  # noqa: BLE001
                    self._toast(f"ошибка: {exc}")
                    return
                dlg.close()
                self.reload()
                self._toast(f"обновлено: {title}")
            save.connect("clicked", _do_save)
            btn_row.append(save)
        else:
            create = Gtk.Button(label="Создать", css_classes=["suggested-action", "mod-cta"])
            def _do_create(*_):
                title = title_entry.get_text().strip()
                if not title:
                    self._toast("Укажи название")
                    return
                desc = desc_entry.get_text().strip()
                icon = icon_entry.get_text().strip() or "🌱"
                color = color_entry.get_text().strip() or DEFAULT_COLOR
                try:
                    h = create_habit(self.settings, title, desc, icon, color)
                except Exception as exc:  # noqa: BLE001
                    self._toast(f"ошибка: {exc}")
                    return
                dlg.close()
                self.reload()
                self._toast(f"создана: {h.title}")
            create.connect("clicked", _do_create)
            title_entry.connect("activate", _do_create)
            btn_row.append(create)
        body.append(btn_row)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(body)
        dlg.set_child(content)
        dlg.present(self.get_root() if self.get_root() else None)  # type: ignore[arg-type]
        title_entry.grab_focus()

    def _confirm_delete(self, habit: Habit) -> None:
        dlg = Adw.MessageDialog(
            transient_for=self.get_root(),  # type: ignore[arg-type]
            heading="Удалить привычку?",
            body=f"«{habit.title}» будет удалён файл {habit.path.name}. Отметки пропадут. Отменить нельзя.",
        )
        dlg.add_response("cancel", "Отмена")
        dlg.add_response("ok", "Удалить")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def _on_resp(d, resp: str) -> None:
            if resp == "ok":
                try:
                    delete_habit(habit)
                    self._toast(f"удалено: {habit.title}")
                    self.reload()
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
            try:
                delete_habit(habit)
                self.reload()
            except Exception:
                pass

    def _open_habit_file(self, habit: Habit) -> None:
        p = habit.path
        if not p.is_file():
            self._toast("файл не найден")
            return
        if self.on_open is not None:
            try:
                self.on_open(str(p))
                return
            except Exception:
                pass
        try:
            from gi.repository import Gio
            Gio.AppInfo.launch_default_for_uri(p.as_uri(), None)
        except Exception:
            pass

    def _on_today_clicked(self, _btn: Gtk.Button) -> None:
        # если сегодня не все выполнены — отметить все, иначе снять? для UX — отметить незавершённые
        today = _today()
        undone = [h for h in self._habits if today not in h.completions]
        if undone:
            for h in undone:
                try:
                    set_completion(h, today, True)
                except Exception:
                    pass
            self._toast(f"отмечено {len(undone)} привычек на сегодня")
        else:
            # все выполнены — перейти к календарю сегодня
            self._go_today_cal()
            self._toast("все привычки уже выполнены сегодня ✓")
        self.reload()

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

    # для внешнего FileMonitor / refresh_all — alias
    def refresh(self) -> None:
        self.reload()

    def reload_if_needed(self) -> None:
        self.reload()


__all__ = [
    "Habit",
    "HabitView",
    "get_habits_dir",
    "ensure_habits_dir",
    "slugify",
    "load_habits",
    "load_all_habits",
    "save_habit",
    "create_habit",
    "delete_habit",
    "toggle_completion",
    "set_completion",
    "calc_streak",
    "calc_stats",
    "aggregated_heatmap",
    "heatmap_level",
    "HABITS_REL",
]
