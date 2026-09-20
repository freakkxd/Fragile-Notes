"""Сегодня — вьюха задач (порт Today/Board-поверхности Task Manager Core).

Поверхность в стиле вашего vault: tm-hero-v2 (дата дня), tm-today-strip с
KPI-чипами (today/overdue/routines), tm-daily-card — секции с акцент-баром
слева, tm-daily-item — строки с баджами типа/приоритета/даты и кнопками
действий (✓ / ⏭ / оплачен), как в добовой панели Task Manager.
"""

from __future__ import annotations

import datetime

from gi.repository import Gtk

from ..core import tasks as tm
from ..paths import resolve_paths
from .widgets import empty_state, view_header

try:
    from .pomodoro_view import PomodoroWidget  # noqa: F401
except Exception:  # pragma: no cover
    PomodoroWidget = None  # type: ignore[assignment]


class TasksView(Gtk.Box):
    def __init__(self, settings: dict, on_action) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self.on_action = on_action
        self.today_count = 0
        self.overdue_count = 0
        self._list: Gtk.Box | None = None
        self._strip: Gtk.Box | None = None

        self.append(view_header("✅", "Сегодня", "Просроченное, задачи дня и ближайшие рутины"))

        # tm-hero-v2: заголовок и дата
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, css_classes=["tm-hero-v2", "tm-hero-v2--dash"])
        hero.append(Gtk.Label(label="Сегодня", css_classes=["tm-hero-v2__eyebrow"], halign=Gtk.Align.START, xalign=0))
        self._hero_title = Gtk.Label(label="План дня", css_classes=["tm-hero-v2__title"], halign=Gtk.Align.START, xalign=0)
        hero.append(self._hero_title)
        self._hero_date = Gtk.Label(label="", css_classes=["tm-hero-v2__date"], halign=Gtk.Align.START, xalign=0)
        hero.append(self._hero_date)
        self.append(hero)

        # tm-today-strip: KPI-чипы
        strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, css_classes=["tm-today-strip"])
        strip.append(Gtk.Label(label="Сводка", css_classes=["tm-today-strip__label"]))
        self._strip = strip
        self.kpi: dict[str, tuple[Gtk.Label, Gtk.Label]] = {}
        for key, label, tone in [
            ("all", "всего", ""),
            ("today", "сегодня", "run"),
            ("overdue", "просрочено", "error"),
            ("upcoming", "рутины 7д", "warn"),
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

        # Pomodoro виджет (25/5, выбор задачи, звук, статистика) — интеграция в tasks_view
        self._pomo: Gtk.Widget | None = None
        if PomodoroWidget is not None:
            try:
                self._pomo = PomodoroWidget(settings, on_done=self._on_pomo_done)
                self.append(self._pomo)
            except Exception:
                self._pomo = None

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scroller.set_child(self._list)
        self.append(scroller)

    def reload(self) -> None:
        assert self._list is not None
        while (child := self._list.get_first_child()) is not None:
            self._list.remove(child)

        paths = resolve_paths(self.settings)
        all_tasks = tm.load_tasks(paths.tm_tasks)
        today = datetime.date.today()

        overdue = [t for t in all_tasks if t.is_overdue(today) and t.is_active]
        due = [t for t in all_tasks if t.is_active and t.is_due_on(today)]
        upcoming = [
            t for t in all_tasks
            if t.is_active and t.is_routine
            and t.routine_next_date and today < t.routine_next_date
            and (t.routine_next_date - today).days <= 7
        ]

        self.kpi["all"][0].set_text(f"{len(all_tasks)}")
        self.kpi["today"][0].set_text(f"{len(due)}")
        self.kpi["overdue"][0].set_text(f"{len(overdue)}")
        self.kpi["upcoming"][0].set_text(f"{len(upcoming)}")
        self._hero_title.set_text("План дня" + (f" · {len(due)} задач" if due else ""))
        self._hero_date.set_text(
            today.strftime("%A, %d %B %Y").capitalize()
        )
        self.today_count = len(due)
        self.overdue_count = len(overdue)

        sections = [
            ("Просрочено", overdue, "overdue", "⏰"),
            ("Сегодня", due, "focus", "🎯"),
            ("Ближайшие рутины (7 дней)", upcoming, "routines", "🔁"),
        ]
        for title, items, tone, icon in sections:
            if not items:
                continue
            self._list.append(self._build_card_group(title, icon, tone, items))

        if not sections_have_items(overdue, due, upcoming):
            self._list.append(empty_state("🎉", "На сегодня задач нет — можно заниматься чем угодно"))

        # обновить список задач в pomodoro (best-effort)
        if self._pomo is not None and hasattr(self._pomo, "reload_tasks"):
            try:
                self._pomo.reload_tasks()
            except Exception:
                pass

    def _build_card_group(self, title: str, icon: str, tone: str, items: list) -> Gtk.Box:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, css_classes=["tm-daily-card", f"tm-daily-card--{tone}"])
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["tm-daily-card__header"])
        head.append(Gtk.Label(label=icon, css_classes=["tm-daily-card__icon"]))
        head_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, hexpand=True)
        head_box.append(Gtk.Label(label=title, css_classes=["tm-daily-card__title"], halign=Gtk.Align.START, xalign=0))
        head_box.append(Gtk.Label(label=f"{len(items)} задач", css_classes=["tm-daily-card__subtitle"], halign=Gtk.Align.START, xalign=0))
        head.append(head_box)
        head.append(Gtk.Label(label=str(len(items)), css_classes=["tm-daily-card__count"]))
        card.append(head)
        for t in items:
            card.append(self._build_item(t))
        return card

    def _build_item(self, t: tm.Task) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["tm-daily-item"])

        def marker_icon():
            if t.is_routine:
                return "🔁"
            if t.is_bill:
                return "🧾"
            return "☑️"
        marker = Gtk.Label(label=marker_icon(), css_classes=["tm-daily-item__marker"])
        row.append(marker)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
        title = Gtk.Label(label=t.title, css_classes=["tm-daily-item__title"], hexpand=True, halign=Gtk.Align.START, xalign=0)
        body.append(title)

        badges: list[Gtk.Widget] = []
        if t.is_routine:
            badges.append(Gtk.Label(label="рутина", css_classes=["tm-daily-badge", "tm-daily-badge--type"]))
        elif t.is_bill:
            badges.append(Gtk.Label(label="счёт", css_classes=["tm-daily-badge", "tm-daily-badge--type"]))
        if t.priority:
            p = t.priority.lower()
            if p in ("critical", "high", "urgent"):
                badges.append(Gtk.Label(label=f"⚠ {t.priority}", css_classes=["tm-daily-badge", "tm-daily-badge--priority"]))
            elif p in ("normal", "medium"):
                badges.append(Gtk.Label(label=t.priority, css_classes=["tm-daily-badge"]))
        if t.is_bill and t.due_date:
            badges.append(Gtk.Label(label=f"оплатить {t.due_date.isoformat()}", css_classes=["tm-daily-badge", "tm-daily-badge--date"]))
        if t.is_routine and t.routine_next_date:
            badges.append(Gtk.Label(label=f"на {t.routine_next_date.isoformat()}", css_classes=["tm-daily-badge", "tm-daily-badge--date"]))
        if t.is_overdue(datetime.date.today()):
            badges.append(Gtk.Label(label="просрочено", css_classes=["tm-daily-badge", "tm-daily-badge--warn"]))

        meta = []
        if t.project:
            meta.append(t.project)
        if t.is_bill and t.due_date:
            meta.append(f"сумма {t.get('amount', '')}".strip() if t.get("amount") else f"до {t.due_date.isoformat()}")
        meta_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for b in badges:
            meta_box.append(b)
        if meta:
            meta_box.append(Gtk.Label(label=" · ".join(meta), css_classes=["dim-label", "task-meta"], halign=Gtk.Align.START, xalign=0))
        body.append(meta_box)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        if t.is_routine:
            done_btn = Gtk.Button(label="✓ Выполнено", css_classes=["suggested-action", "mod-cta"])
            done_btn.connect("clicked", self._on_done, t)
            skip_btn = Gtk.Button(label="⏭ Пропустить")
            skip_btn.connect("clicked", self._on_skip, t)
            btn_row.append(done_btn)
            btn_row.append(skip_btn)
        elif t.is_bill:
            pay_btn = Gtk.Button(label="✓ Оплачен", css_classes=["suggested-action", "mod-cta"])
            pay_btn.connect("clicked", self._on_pay, t)
            btn_row.append(pay_btn)
        else:
            done_btn = Gtk.Button(label="✓ Выполнено", css_classes=["suggested-action", "mod-cta"])
            done_btn.connect("clicked", self._on_done, t)
            btn_row.append(done_btn)
        btn_row.set_valign(Gtk.Align.CENTER)
        body.append(btn_row)
        row.append(body)
        return row

    def _on_done(self, _btn: Gtk.Button, t: tm.Task) -> None:
        if t.is_routine:
            tm.mark_routine_done(t)
        else:
            t.fm["status"] = tm.STATUS_DONE
            t.fm["last_done_date"] = datetime.date.today().isoformat()
        t.save()
        self.on_action("done")

    def _on_skip(self, _btn: Gtk.Button, t: tm.Task) -> None:
        tm.mark_routine_skipped(t)
        t.save()
        self.on_action("skip")

    def _on_pay(self, _btn: Gtk.Button, t: tm.Task) -> None:
        tm.pay_bill(t)
        t.save()
        self.on_action("pay")

    def _on_pomo_done(self, kind: str, task) -> None:
        # проброс как toast через on_action, плюс обновить KPI при следующем reload
        if kind == "work_done":
            self.on_action("done")
        elif kind == "break_done":
            self.on_action("skip")


def sections_have_items(*sections) -> bool:
    return any(bool(s) for s in sections)
