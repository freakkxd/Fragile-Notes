"""Pomodoro таймер 25/5 — интеграция с задачами (выбор, отсчёт, звук, статистика).

Дизайн: AO Glass (tm-hero-v2, glass-card, pill), 1-сек тик через GLib,
выбор задачи из vault/tasks, звук через Gdk.beep + paplay, статистика
в ~/.config/fragile-notes/pomodoro.json (по дням и по задачам).
Используется как вкладка (PomodoroView) и как виджет в tasks_view (PomodoroWidget).
"""

from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")

from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from ..config import APP_DIR  # noqa: E402
from ..core import tasks as tm  # noqa: E402
from ..paths import resolve_paths  # noqa: E402
from .widgets import status_pill, view_header  # noqa: E402

DEFAULT_WORK_SEC = 25 * 60
DEFAULT_BREAK_SEC = 5 * 60
LONG_BREAK_SEC = 15 * 60

STATS_FILE: Path = APP_DIR / "pomodoro.json"

MODE_WORK = "work"
MODE_BREAK = "break"
MODE_LONG_BREAK = "long_break"

MODE_LABEL = {
    MODE_WORK: "Работа",
    MODE_BREAK: "Перерыв",
    MODE_LONG_BREAK: "Длинный перерыв",
}

MODE_ICON = {
    MODE_WORK: "🍅",
    MODE_BREAK: "☕",
    MODE_LONG_BREAK: "🌿",
}


# ── Утилиты ──────────────────────────────────────────────────

def _today_str() -> str:
    return datetime.date.today().isoformat()


def _fmt(sec: int) -> str:
    sec = max(0, int(sec))
    m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d}"


def _play_sound() -> None:
    """Звук окончания интервала: Gdk.beep + системный ogg (best-effort)."""
    try:
        display = Gdk.Display.get_default()
        if display is not None:
            display.beep()
    except Exception:
        pass
    for ogg in (
        "/usr/share/sounds/freedesktop/stereo/complete.oga",
        "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
        "/usr/share/sounds/freedesktop/stereo/message.oga",
    ):
        p = Path(ogg)
        if p.is_file():
            try:
                subprocess.Popen(
                    ["paplay", str(p)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except Exception:
                continue
    try:
        subprocess.Popen(
            ["sh", "-c", "printf '\\a'"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _notify(title: str, body: str) -> None:
    """Системное уведомление (Gio.Notification если есть app)."""
    try:
        app = Gio.Application.get_default()
        if app is not None:
            notif = Gio.Notification.new(title)
            notif.set_body(body)
            try:
                app.send_notification("pomodoro", notif)
                return
            except Exception:
                pass
    except Exception:
        pass


# ── Статистика ───────────────────────────────────────────────

def _load_stats() -> dict[str, Any]:
    if not STATS_FILE.is_file():
        return {"history": {}, "per_task": {}, "total_completed": 0, "streak": 0}
    try:
        data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"history": {}, "per_task": {}, "total_completed": 0}
        data.setdefault("history", {})
        data.setdefault("per_task", {})
        data.setdefault("total_completed", 0)
        return data
    except Exception:
        return {"history": {}, "per_task": {}, "total_completed": 0}


def _save_stats(data: dict[str, Any]) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        STATS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def record_work_done(task_key: str | None = None, minutes: int = 25) -> dict[str, Any]:
    """Записать завершённый Pomodoro (work). Возвращает обновлённые данные."""
    data = _load_stats()
    today = _today_str()
    hist = data.get("history", {})
    entry = hist.get(today)
    if not isinstance(entry, dict):
        entry = {"work": 0, "break": 0, "total_minutes": 0}
        hist[today] = entry
    entry["work"] = int(entry.get("work", 0)) + 1
    entry["total_minutes"] = int(entry.get("total_minutes", 0)) + int(minutes)
    data["total_completed"] = int(data.get("total_completed", 0)) + 1
    if task_key:
        pt = data.setdefault("per_task", {})
        pt[task_key] = int(pt.get(task_key, 0)) + 1
    try:
        dates = sorted(hist.keys())
        streak = 0
        cur = datetime.date.today()
        for _ in range(60):
            k = cur.isoformat()
            e = hist.get(k)
            if isinstance(e, dict) and int(e.get("work", 0)) > 0:
                streak += 1
                cur -= datetime.timedelta(days=1)
            else:
                break
        data["streak"] = streak
    except Exception:
        pass
    _save_stats(data)
    return data


def record_break_done() -> None:
    data = _load_stats()
    today = _today_str()
    hist = data.get("history", {})
    entry = hist.get(today)
    if not isinstance(entry, dict):
        entry = {"work": 0, "break": 0, "total_minutes": 0}
        hist[today] = entry
    entry["break"] = int(entry.get("break", 0)) + 1
    _save_stats(data)


def get_today_stats() -> dict[str, int]:
    data = _load_stats()
    entry = data.get("history", {}).get(_today_str(), {})
    if not isinstance(entry, dict):
        return {"work": 0, "break": 0, "total_minutes": 0}
    return {
        "work": int(entry.get("work", 0)),
        "break": int(entry.get("break", 0)),
        "total_minutes": int(entry.get("total_minutes", 0)),
    }


def get_week_stats(days: int = 7) -> list[tuple[str, int]]:
    data = _load_stats()
    hist = data.get("history", {})
    out: list[tuple[str, int]] = []
    today = datetime.date.today()
    for i in range(days):
        d = (today - datetime.timedelta(days=i)).isoformat()
        e = hist.get(d, {})
        work = int(e.get("work", 0)) if isinstance(e, dict) else 0
        out.append((d, work))
    out.reverse()
    return out


# ── Базовый движок (логика без GTK) ──────────────────────────

class PomodoroEngine:
    """Чистая логика таймера — тестируема без GTK."""

    def __init__(
        self,
        work_sec: int = DEFAULT_WORK_SEC,
        break_sec: int = DEFAULT_BREAK_SEC,
        long_break_sec: int = LONG_BREAK_SEC,
    ) -> None:
        self.work_sec = max(60, int(work_sec))
        self.break_sec = max(60, int(break_sec))
        self.long_break_sec = max(60, int(long_break_sec))
        self.mode: str = MODE_WORK
        self.remaining: int = self.work_sec
        self.running: bool = False
        self.completed_work: int = 0

    def start(self) -> None:
        self.running = True

    def pause(self) -> None:
        self.running = False

    def reset(self) -> None:
        self.running = False
        self.mode = MODE_WORK
        self.remaining = self.work_sec

    def tick(self) -> str | None:
        """Сдвинуть на 1 сек; вернуть 'work_done'/'break_done' при переходе или None."""
        if not self.running:
            return None
        self.remaining -= 1
        if self.remaining > 0:
            return None
        if self.mode == MODE_WORK:
            self.completed_work += 1
            if self.completed_work % 4 == 0:
                self.mode = MODE_LONG_BREAK
                self.remaining = self.long_break_sec
            else:
                self.mode = MODE_BREAK
                self.remaining = self.break_sec
            self.running = False
            return "work_done"
        else:
            self.mode = MODE_WORK
            self.remaining = self.work_sec
            self.running = False
            return "break_done"

    def skip(self) -> None:
        """Пропустить текущий интервал."""
        self.running = False
        if self.mode == MODE_WORK:
            self.mode = MODE_BREAK
            self.remaining = self.break_sec
        else:
            self.mode = MODE_WORK
            self.remaining = self.work_sec

    def fraction(self) -> float:
        total = self.work_sec if self.mode == MODE_WORK else (self.long_break_sec if self.mode == MODE_LONG_BREAK else self.break_sec)
        if total <= 0:
            return 0.0
        return max(0.0, min(1.0, 1.0 - self.remaining / total))


# ── Виджет (компакт) для tasks_view ──────────────────────────
# Важно: избегаем Gtk.DropDown/ComboBox/SpinButton/MenuButton — их внутренние
# ToggleButton с get_label()==None ломают test_tasks::collect_labels.
# Используем только Box+Label+Button+Popover+ListBox.

class PomodoroWidget(Gtk.Box):
    """Компактный виджет для встраивания в tasks_view (выбор задачи + таймер + звук + статистика)."""

    def __init__(self, settings: dict, on_done=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["glass-card", "pomo-widget"])
        self.settings = settings
        self.on_done = on_done
        self.engine = PomodoroEngine()
        self._timer_id: int | None = None
        self._tasks: list[tm.Task] = []
        self._selected_key: str | None = None
        self._work_opts = [5, 10, 15, 20, 25, 30, 45, 60]
        self._break_opts = [1, 3, 5, 10, 15, 20]
        self._work_idx = self._work_opts.index(25)
        self._break_idx = self._break_opts.index(5)
        self._task_popover: Gtk.Popover | None = None

        # header
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["glass-card__head"])
        head.append(Gtk.Label(label="🍅 Pomodoro", css_classes=["glass-card__title"]))
        self.mode_pill = status_pill(MODE_LABEL[MODE_WORK], "run")
        head.append(self.mode_pill)
        head.append(Gtk.Box(hexpand=True))
        self.stats_lbl = Gtk.Label(label="сегодня 0", css_classes=["dim-label"])
        head.append(self.stats_lbl)
        self.append(head)

        # body
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_margin_start(12)
        body.set_margin_end(12)
        body.set_margin_top(8)
        body.set_margin_bottom(10)
        self.append(body)

        # выбор задачи — Button + Popover (без DropDown)
        task_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        task_row.append(Gtk.Label(label="Задача", css_classes=["dim-label"]))
        self.task_btn = Gtk.Button(label="— без задачи —", hexpand=True)
        self.task_btn.set_halign(Gtk.Align.FILL)
        self.task_btn.connect("clicked", self._open_task_popover)
        task_row.append(self.task_btn)
        refresh_btn = Gtk.Button(label="↻", css_classes=["flat"])
        refresh_btn.set_tooltip_text("Обновить список задач")
        refresh_btn.connect("clicked", lambda *_: self.reload_tasks())
        task_row.append(refresh_btn)
        body.append(task_row)

        # таймер + прогресс
        timer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["pomo-hero"])
        self.timer_lbl = Gtk.Label(label=_fmt(self.engine.remaining), css_classes=["pomo-timer"])
        self.timer_lbl.set_halign(Gtk.Align.CENTER)
        timer_box.append(self.timer_lbl)
        self.progress = Gtk.ProgressBar(show_text=False, css_classes=["pomo-progress"])
        self.progress.set_hexpand(True)
        timer_box.append(self.progress)
        body.append(timer_box)

        # кнопки управления
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.CENTER)
        self.start_btn = Gtk.Button(label="▶ Старт", css_classes=["suggested-action", "mod-cta"])
        self.start_btn.connect("clicked", self._on_start_pause)
        btn_row.append(self.start_btn)
        self.reset_btn = Gtk.Button(label="↺ Сброс")
        self.reset_btn.connect("clicked", self._on_reset)
        btn_row.append(self.reset_btn)
        self.skip_btn = Gtk.Button(label="⏭ Пропустить")
        self.skip_btn.connect("clicked", self._on_skip)
        btn_row.append(self.skip_btn)
        body.append(btn_row)

        # длительности — кнопки +/- без SpinButton/DropDown
        dur_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        dur_row.set_halign(Gtk.Align.CENTER)
        # работа
        work_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        work_box.append(Gtk.Label(label="Работа", css_classes=["dim-label"]))
        self._work_minus = Gtk.Button(label="−", css_classes=["flat"])
        self._work_minus.connect("clicked", self._on_work_minus)
        work_box.append(self._work_minus)
        self._work_val = Gtk.Label(label="25 мин", css_classes=["pomo-week-val"])
        work_box.append(self._work_val)
        self._work_plus = Gtk.Button(label="+", css_classes=["flat"])
        self._work_plus.connect("clicked", self._on_work_plus)
        work_box.append(self._work_plus)
        dur_row.append(work_box)
        # перерыв
        break_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        break_box.append(Gtk.Label(label="Перерыв", css_classes=["dim-label"]))
        self._break_minus = Gtk.Button(label="−", css_classes=["flat"])
        self._break_minus.connect("clicked", self._on_break_minus)
        break_box.append(self._break_minus)
        self._break_val = Gtk.Label(label="5 мин", css_classes=["pomo-week-val"])
        break_box.append(self._break_val)
        self._break_plus = Gtk.Button(label="+", css_classes=["flat"])
        self._break_plus.connect("clicked", self._on_break_plus)
        break_box.append(self._break_plus)
        dur_row.append(break_box)
        body.append(dur_row)

        self.reload_tasks()
        self._refresh_stats()
        self._update_ui()

    # ── Задачи ───────────────────────────────────────────────
    def reload_tasks(self) -> None:
        try:
            paths = resolve_paths(self.settings)
            self._tasks = tm.load_tasks(paths.tm_tasks)
        except Exception:
            self._tasks = []
        active = [t for t in self._tasks if getattr(t, "is_active", True)]
        today = datetime.date.today()
        def rank(t: tm.Task) -> int:
            if t.is_overdue(today):
                return 0
            if t.is_due_on(today):
                return 1
            return 2
        active.sort(key=rank)
        self._tasks = active
        # обновить label кнопки если уже выбран
        if self._selected_key is not None:
            found = None
            for t in active:
                if t.path.stem == self._selected_key or str(t.path) == self._selected_key:
                    found = t
                    break
            if found is not None:
                today_mark = "⏰ " if found.is_overdue(today) else ("🎯 " if found.is_due_on(today) else "")
                self.task_btn.set_label(f"{today_mark}{found.title}")
            else:
                self._selected_key = None
                self.task_btn.set_label("— без задачи —")
        else:
            self.task_btn.set_label("— без задачи —")

    def _open_task_popover(self, btn: Gtk.Button) -> None:
        # закрыть старый
        if self._task_popover is not None:
            try:
                self._task_popover.popdown()
            except Exception:
                pass
            self._task_popover = None
        pop = Gtk.Popover()
        pop.set_parent(btn)
        # has_arrow requires Gtk 4.12+, fallback
        try:
            pop.set_has_arrow(True)
        except Exception:
            pass
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        # заголовок
        box.append(Gtk.Label(label="Выберите задачу", css_classes=["dim-label"], halign=Gtk.Align.START, xalign=0))
        # список
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        scroller.set_size_request(-1, 220)
        scroller.set_max_content_height(320)
        listbox = Gtk.ListBox(css_classes=["pomo-task-list"])
        listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        # пункт без задачи
        row0 = Gtk.ListBoxRow()
        row0._key = None  # type: ignore[attr-defined]
        row0.set_child(Gtk.Label(label="— без задачи —", halign=Gtk.Align.START, xalign=0))
        listbox.append(row0)
        today = datetime.date.today()
        for t in self._tasks:
            row = Gtk.ListBoxRow()
            row._key = t.path.stem  # type: ignore[attr-defined]
            row._task = t  # type: ignore[attr-defined]
            prefix = "⏰ " if t.is_overdue(today) else ("🎯 " if t.is_due_on(today) else "• ")
            lbl = Gtk.Label(label=f"{prefix}{t.title}", halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            row.set_child(lbl)
            listbox.append(row)
        def on_row_activated(_lb, r):
            key = getattr(r, "_key", None)
            self._selected_key = key
            if key is None:
                self.task_btn.set_label("— без задачи —")
            else:
                task = getattr(r, "_task", None)
                title = task.title if task is not None else key
                prefix = ""
                if task is not None:
                    prefix = "⏰ " if task.is_overdue(today) else ("🎯 " if task.is_due_on(today) else "")
                self.task_btn.set_label(f"{prefix}{title}")
            try:
                pop.popdown()
            except Exception:
                pass
        listbox.connect("row-activated", on_row_activated)
        scroller.set_child(listbox)
        box.append(scroller)
        pop.set_child(box)
        pop.connect("closed", lambda *_: pop.unparent() if pop.get_parent() else None)
        self._task_popover = pop
        pop.popup()

    def selected_task(self) -> tm.Task | None:
        if self._selected_key is None:
            return None
        for t in self._tasks:
            if t.path.stem == self._selected_key or str(t.path) == self._selected_key:
                return t
        return None

    # ── Длительности +/- ─────────────────────────────────────
    def _apply_work_idx(self) -> None:
        self._work_val.set_text(f"{self._work_opts[self._work_idx]} мин")
        sec = self._work_opts[self._work_idx] * 60
        self.engine.work_sec = sec
        if self.engine.mode == MODE_WORK and not self.engine.running:
            self.engine.remaining = sec
        self._update_ui()

    def _apply_break_idx(self) -> None:
        self._break_val.set_text(f"{self._break_opts[self._break_idx]} мин")
        sec = self._break_opts[self._break_idx] * 60
        self.engine.break_sec = sec
        if self.engine.mode != MODE_WORK and not self.engine.running:
            self.engine.remaining = sec
        self._update_ui()

    def _on_work_minus(self, *_args) -> None:
        if self._work_idx > 0:
            self._work_idx -= 1
            self._apply_work_idx()

    def _on_work_plus(self, *_args) -> None:
        if self._work_idx < len(self._work_opts) - 1:
            self._work_idx += 1
            self._apply_work_idx()

    def _on_break_minus(self, *_args) -> None:
        if self._break_idx > 0:
            self._break_idx -= 1
            self._apply_break_idx()

    def _on_break_plus(self, *_args) -> None:
        if self._break_idx < len(self._break_opts) - 1:
            self._break_idx += 1
            self._apply_break_idx()

    # legacy dropdown handler kept for compat
    def _on_duration_drop_changed(self, *a, **kw) -> None:
        pass

    def _on_duration_changed(self, *a, **kw) -> None:
        pass

    # ── Таймер ───────────────────────────────────────────────
    def _on_start_pause(self, *_args) -> None:
        if self.engine.running:
            self._stop_tick()
            self.engine.pause()
        else:
            self.engine.start()
            self._start_tick()
        self._update_ui()

    def _on_reset(self, *_args) -> None:
        self._stop_tick()
        self.engine.reset()
        try:
            self.engine.work_sec = self._work_opts[self._work_idx] * 60
            self.engine.break_sec = self._break_opts[self._break_idx] * 60
            self.engine.remaining = self.engine.work_sec
        except Exception:
            pass
        self._update_ui()

    def _on_skip(self, *_args) -> None:
        self._stop_tick()
        self.engine.skip()
        self._update_ui()

    def _start_tick(self) -> None:
        if self._timer_id is not None:
            return
        self._timer_id = GLib.timeout_add_seconds(1, self._tick)

    def _stop_tick(self) -> None:
        if self._timer_id is not None:
            try:
                GLib.source_remove(self._timer_id)
            except Exception:
                pass
            self._timer_id = None

    def _tick(self) -> bool:
        result = self.engine.tick()
        self._update_ui()
        if result == "work_done":
            _play_sound()
            task = self.selected_task()
            key = task.path.stem if task is not None else self._selected_key
            mins = max(1, self.engine.work_sec // 60)
            record_work_done(key, mins)
            self._refresh_stats()
            _notify("Pomodoro завершён", f"{MODE_LABEL[MODE_WORK]} {mins} мин — время перерыва ☕")
            if self.on_done is not None:
                try:
                    self.on_done("work_done", task)
                except Exception:
                    pass
            self._stop_tick()
            self._update_ui()
            return False
        if result == "break_done":
            _play_sound()
            record_break_done()
            self._refresh_stats()
            _notify("Перерыв окончен", "Пора возвращаться к работе 🍅")
            if self.on_done is not None:
                try:
                    self.on_done("break_done", self.selected_task())
                except Exception:
                    pass
            self._stop_tick()
            self._update_ui()
            return False
        if not self.engine.running:
            self._stop_tick()
            return False
        return True

    def _update_ui(self) -> None:
        self.timer_lbl.set_text(_fmt(self.engine.remaining))
        self.progress.set_fraction(self.engine.fraction())
        mode = self.engine.mode
        label = MODE_LABEL.get(mode, mode)
        icon = MODE_ICON.get(mode, "🍅")
        self.mode_pill.set_text(f"{icon} {label}")
        for cls in ("pill-ok", "pill-run", "pill-warn", "pill-idle", "pill-error"):
            self.mode_pill.remove_css_class(cls)
        tone = "run" if mode == MODE_WORK else ("warn" if mode == MODE_BREAK else "ok")
        self.mode_pill.add_css_class(f"pill-{tone}")
        self.start_btn.set_label("⏸ Пауза" if self.engine.running else "▶ Старт")
        self.skip_btn.set_sensitive(True)

    def _refresh_stats(self) -> None:
        s = get_today_stats()
        data = _load_stats()
        total = int(data.get("total_completed", 0))
        streak = int(data.get("streak", 0))
        self.stats_lbl.set_text(f"сегодня {s['work']} · всего {total}" + (f" · 🔥{streak}" if streak else ""))

    def destroy(self) -> None:
        self._stop_tick()
        super().destroy() if hasattr(super(), "destroy") else None


# ── Полноценная вкладка ─────────────────────────────────────

class PomodoroView(Gtk.Box):
    """Вкладка Pomodoro: герой-таймер, выбор задачи, статистика, история."""

    def __init__(self, settings: dict, on_action=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self.on_action = on_action

        self.append(view_header("🍅", "Pomodoro", "25 мин фокуса · 5 мин перерыв · звук и статистика"))

        # hero
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, css_classes=["tm-hero-v2", "tm-hero-v2--dash"])
        hero.append(Gtk.Label(label="Pomodoro", css_classes=["tm-hero-v2__eyebrow"], halign=Gtk.Align.START, xalign=0))
        hero.append(Gtk.Label(label="Фокус-таймер", css_classes=["tm-hero-v2__title"], halign=Gtk.Align.START, xalign=0))
        hero.append(Gtk.Label(label="Выберите задачу — запустите отсчёт — услышите сигнал", css_classes=["tm-hero-v2__date"], halign=Gtk.Align.START, xalign=0))
        self.append(hero)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_start(14)
        body.set_margin_end(14)
        body.set_margin_bottom(16)
        body.set_margin_top(4)
        scroller.set_child(body)
        self.append(scroller)

        # виджет таймера (переиспользуем)
        self.widget = PomodoroWidget(settings, on_done=self._on_pomo_done)
        body.append(self.widget)

        # статистика: KPI-чипы + неделя
        self.stats_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["tm-daily-card", "tm-daily-card--focus"])
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["tm-daily-card__header"])
        head.append(Gtk.Label(label="📊", css_classes=["tm-daily-card__icon"]))
        head_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, hexpand=True)
        head_box.append(Gtk.Label(label="Статистика", css_classes=["tm-daily-card__title"], halign=Gtk.Align.START, xalign=0))
        self.stats_sub = Gtk.Label(label="сегодня · неделя", css_classes=["tm-daily-card__subtitle"], halign=Gtk.Align.START, xalign=0)
        head_box.append(self.stats_sub)
        head.append(head_box)
        self.stats_card.append(head)

        # KPI
        kpi_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10, css_classes=["tm-today-strip"])
        kpi_row.set_margin_start(10)
        kpi_row.set_margin_end(10)
        kpi_row.set_margin_top(6)
        self.kpi_labels: dict[str, Gtk.Label] = {}
        for key, caption in [("today_work", "сегодня"), ("week", "неделя"), ("total", "всего"), ("streak", "серия")]:
            chip = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, css_classes=["tm-kpi-chip"])
            val = Gtk.Label(label="0", css_classes=["tm-kpi-chip__value"])
            lab = Gtk.Label(label=caption, css_classes=["tm-kpi-chip__label"])
            chip.append(val)
            chip.append(lab)
            kpi_row.append(chip)
            self.kpi_labels[key] = val
        self.stats_card.append(kpi_row)

        # неделя (7 дней)
        self.week_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.week_box.set_margin_start(10)
        self.week_box.set_margin_end(10)
        self.week_box.set_margin_bottom(8)
        self.stats_card.append(self.week_box)

        # топ задач
        self.top_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.top_box.set_margin_start(10)
        self.top_box.set_margin_end(10)
        self.top_box.set_margin_bottom(10)
        self.stats_card.append(self.top_box)

        body.append(self.stats_card)

        # подсказка
        hint = Gtk.Label(
            label="Подсказка: 4 Pomodoro подряд → длинный перерыв 15 мин. Статистика хранится в ~/.config/fragile-notes/pomodoro.json",
            css_classes=["dim-label"],
            wrap=True,
            halign=Gtk.Align.START,
            xalign=0,
        )
        hint.set_margin_start(6)
        body.append(hint)

        self._refresh_stats_full()
        GLib.timeout_add_seconds(30, self._poll_stats)

    def reload(self) -> None:
        try:
            self.widget.reload_tasks()
        except Exception:
            pass
        self._refresh_stats_full()

    def _on_pomo_done(self, kind: str, task: tm.Task | None) -> None:
        if self.on_action is not None:
            try:
                self.on_action(kind, task)
            except Exception:
                pass
        self._refresh_stats_full()

    def _poll_stats(self) -> bool:
        self._refresh_stats_full()
        return True

    def _refresh_stats_full(self) -> None:
        data = _load_stats()
        today = get_today_stats()
        week = get_week_stats(7)
        week_total = sum(v for _, v in week)
        total = int(data.get("total_completed", 0))
        streak = int(data.get("streak", 0))
        try:
            self.kpi_labels["today_work"].set_text(str(today["work"]))
            self.kpi_labels["week"].set_text(str(week_total))
            self.kpi_labels["total"].set_text(str(total))
            self.kpi_labels["streak"].set_text(f"🔥 {streak}" if streak else "0")
        except Exception:
            pass
        try:
            while (c := self.week_box.get_first_child()) is not None:
                self.week_box.remove(c)
            max_w = max((v for _, v in week), default=1)
            for d, v in week:
                col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, css_classes=["pomo-week-col"])
                bar = Gtk.Box(css_classes=["pomo-week-bar"])
                h = 4 + int(28 * (v / max_w)) if max_w else 4
                bar.set_size_request(22, h)
                if v == 0:
                    bar.add_css_class("pomo-week-bar--empty")
                elif v >= max_w and max_w > 2:
                    bar.add_css_class("pomo-week-bar--max")
                col.append(bar)
                col.append(Gtk.Label(label=str(v), css_classes=["pomo-week-val"]))
                try:
                    dt = datetime.date.fromisoformat(d)
                    lab = dt.strftime("%d.%m")
                except Exception:
                    lab = d[5:]
                col.append(Gtk.Label(label=lab, css_classes=["pomo-week-date"]))
                self.week_box.append(col)
        except Exception:
            pass
        try:
            while (c := self.top_box.get_first_child()) is not None:
                self.top_box.remove(c)
            per = data.get("per_task", {})
            if not isinstance(per, dict) or not per:
                self.top_box.append(Gtk.Label(label="Пока нет завершённых Pomodoro для задач", css_classes=["dim-label"], halign=Gtk.Align.START, xalign=0))
            else:
                items = sorted(per.items(), key=lambda kv: int(kv[1]), reverse=True)[:6]
                self.top_box.append(Gtk.Label(label="Топ задач", css_classes=["tm-daily-card__title"], halign=Gtk.Align.START, xalign=0))
                for key, cnt in items:
                    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["tm-daily-item"])
                    title = key
                    try:
                        for t in self.widget._tasks:  # type: ignore[attr-defined]
                            if t.path.stem == key:
                                title = t.title
                                break
                    except Exception:
                        pass
                    row.append(Gtk.Label(label="🍅", css_classes=["tm-daily-item__marker"]))
                    row.append(Gtk.Label(label=title, css_classes=["tm-daily-item__title"], hexpand=True, halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END))
                    row.append(Gtk.Label(label=f"{cnt} ×", css_classes=["tm-daily-badge", "tm-daily-badge--type"]))
                    self.top_box.append(row)
            self.stats_sub.set_text(f"сегодня {today['work']} · {today['total_minutes']} мин · неделя {week_total}")
        except Exception:
            pass
