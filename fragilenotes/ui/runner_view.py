"""AO Task Manager view — нативный порт AoRunnerPanelView из плагина ao-runner."""

from __future__ import annotations

import datetime

from gi.repository import Adw, Gtk

from ..core import runner as model
from ..core.runner_controller import GROUP_LABELS, GROUP_ORDER, RunnerController, TaskDef
from .widgets import set_dot_tone, status_dot, status_pill, view_header

STATE_TEXT = {
    model.IDLE: "idle",
    model.RUNNING: "выполняется",
    model.DONE: "готово",
    model.ERROR: "ошибка",
}

STATE_TONE = {
    model.IDLE: "idle",
    model.RUNNING: "run",
    model.DONE: "ok",
    model.ERROR: "error",
}


def _time(ms: float) -> str:
    dt = datetime.datetime.fromtimestamp(ms / 1000.0)
    return dt.strftime("%d.%m %H:%M")


class RunnerView(Gtk.Box):
    """Колонка задач: овервью + карточки + лог."""

    def __init__(self, controller: RunnerController, on_action) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.controller = controller
        self.on_action = on_action  # callable(task_id, action)
        self._buttons: dict[tuple[str, str], Gtk.Button] = {}
        self._cards: dict[str, Gtk.Box] = {}
        self._list: Gtk.Box | None = None

        self.append(view_header("🛠", "AO Task Manager", "Пул команд движка и LLM — задачи-карточки"))

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body = self._build_body()
        content = Adw.Clamp(maximum_size=980, tightening_threshold=640)
        content.set_child(body)
        scroller.set_child(content)
        self.append(scroller)

    # ── Сборка ───────────────────────────────────────────────
    def _build_body(self) -> Gtk.Box:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.set_margin_start(14)
        body.set_margin_end(14)
        body.set_margin_bottom(14)
        self.overview = self._build_overview()
        body.append(self.overview)

        self._list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._rebuild_list()
        body.append(self._list)

        self.log_label = Gtk.Label(
            label="(пусто)", halign=Gtk.Align.START, wrap=True, xalign=0, css_classes=["dim-label"]
        )
        self.log_label.set_margin_top(6)
        self.log_label.set_margin_start(4)
        log_exp = Gtk.Expander(label="Лог операций")
        log_exp.set_child(self.log_label)
        body.append(log_exp)
        return body

    def _build_overview(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("rv-overview")
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.append(Gtk.Label(label="AO Pipeline", css_classes=["rv-overview-title"]))
        self.chip_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        head.append(self.chip_box)
        box.append(head)
        self.progress = Gtk.ProgressBar(show_text=True, css_classes=["rv-progress"])
        self.progress.set_visible(False)
        box.append(self.progress)
        self.enrich_progress = Gtk.ProgressBar(show_text=True, css_classes=["rv-progress", "live-bar"])
        self.enrich_progress.set_visible(False)
        box.append(self.enrich_progress)
        return box

    def _chip(self, text: str, tone: str = "idle") -> Gtk.Widget:
        return status_pill(text, tone)

    def _rebuild_list(self) -> None:
        assert self._list is not None
        while (child := self._list.get_first_child()) is not None:
            self._list.remove(child)
        for group in GROUP_ORDER:
            defs = [d for d in self.controller.defs if d.group == group]
            if not defs:
                continue
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            from .widgets import section_title
            box.append(section_title(GROUP_LABELS[group], len(defs)))
            box.set_margin_start(4)
            box.set_margin_top(2)
            for d in defs:
                card = self._build_card(d)
                self._cards[d.id] = card
                box.append(card)
            self._list.append(box)

    def _build_card(self, d: TaskDef) -> Gtk.Box:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.add_css_class("card")

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = Gtk.Label(label=d.icon, css_classes=["rv-card-icon"])
        title = Gtk.Label(label=d.title, css_classes=["rv-card-title"], hexpand=True, halign=Gtk.Align.START, xalign=0)
        dot = status_dot("idle", 10)
        badge = status_pill("idle", "idle")
        head.append(icon)
        head.append(title)
        head.append(dot)
        head.append(badge)
        card._dot = dot
        card._badge = badge

        help_lbl = Gtk.Label(label=d.help, css_classes=["dim-label", "rv-card-help"], halign=Gtk.Align.START, xalign=0, wrap=True)
        help_lbl.set_margin_start(24)

        progress = Gtk.ProgressBar(show_text=True)
        progress.set_visible(False)
        card._progress = progress

        error_lbl = Gtk.Label(label="", halign=Gtk.Align.START, xalign=0, wrap=True, css_classes=["rv-error-box"])
        error_lbl.set_visible(False)
        error_lbl.set_margin_start(24)
        card._error = error_lbl

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_row.set_margin_start(20)
        btn_row.set_margin_top(2)
        for a in d.actions:
            btn = Gtk.Button(label=a.label)
            if a.kind == "stop":
                btn.add_css_class("destructive-action")
            elif a.kind == "ping":
                btn.add_css_class("flat")
            elif a.primary:
                btn.add_css_class("suggested-action")
            btn.connect("clicked", self._on_click, d.id, a.kind)
            self._buttons[(d.id, a.kind)] = btn
            btn_row.append(btn)
        card._btn_row = btn_row

        history = Gtk.Expander(label="История")
        hist_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        history.set_child(hist_box)
        card._hist_box = hist_box

        card.append(head)
        card.append(help_lbl)
        card.append(progress)
        card.append(error_lbl)
        card.append(btn_row)
        card.append(history)
        return card

    def _on_click(self, _btn: Gtk.Button, task_id: str, action: str) -> None:
        self.on_action(task_id, action)

    def _build_hist_row(self, r: model.TaskRunRecord) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_start(20)
        time_lbl = Gtk.Label(label=_time(r.at), css_classes=["dim-label", "rv-hist-time"])
        tag = status_pill("✓" if r.state == model.DONE else "✗", "ok" if r.state == model.DONE else "error")
        detail = Gtk.Label(label=r.detail, css_classes=["dim-label"], hexpand=True, halign=Gtk.Align.START, xalign=0, wrap=True)
        row.append(time_lbl)
        row.append(tag)
        row.append(detail)
        return row

    # ── Обновление ───────────────────────────────────────────
    def refresh(self, busy: bool, log: str, enrich, inbox_undecided: int) -> None:
        ctrl = self.controller
        st = ctrl.status
        day = ctrl.llm.get_status("day")
        arch = ctrl.llm.get_status("archive")
        chips: list[tuple[str, str]] = []
        if day.online:
            chips.append((f"💎 day ВКЛ · ctx {day.ctx_size or '?'}", "ok"))
        elif day.online is False:
            chips.append(("💎 day ВЫКЛ", "error"))
        else:
            chips.append(("💎 day ?", "idle"))
        if arch.online:
            chips.append((f"🌙 arch ВКЛ · ctx {arch.ctx_size or '?'}", "ok"))
        elif arch.online is False:
            chips.append(("🌙 arch ВЫКЛ", "error"))

        if enrich.online:
            chips.append((enrich.summary(), "run" if enrich.state == "running" else "idle"))
        else:
            chips.append(("🔄 enrich недоступен", "error"))

        if inbox_undecided:
            chips.append((f"📥 {inbox_undecided} неразобрано", "warn"))
        elif inbox_undecided == 0:
            chips.append(("📥 всё разобрано", "ok"))

        if st.state == model.RUNNING:
            chips.append((f"⏱ {st.current_command}", "run"))

        # пересобираем чипы только при изменениях
        if chips != getattr(self, "_chips_sig", None):
            self._chips_sig = chips
            while (c := self.chip_box.get_first_child()) is not None:
                self.chip_box.remove(c)
            for text, tone in chips:
                self.chip_box.append(self._chip(text, tone))

        if st.state == model.RUNNING and st.progress_total and st.progress_total > 0:
            self.progress.set_fraction(min(1.0, st.progress_current / st.progress_total))
            self.progress.set_text(f"{st.progress_current}/{st.progress_total}")
            self.progress.set_visible(True)
        else:
            self.progress.set_visible(False)

        if enrich.online and enrich.state == "running" and enrich.total:
            self.enrich_progress.set_fraction(min(1.0, enrich.scanned / enrich.total))
            self.enrich_progress.set_text(f"enrich {enrich.scanned}/{enrich.total}")
            self.enrich_progress.set_visible(True)
        else:
            self.enrich_progress.set_visible(False)

        for d in ctrl.defs:
            card_state = ctrl.task_states[d.id]
            self._refresh_card(d, card_state, busy)

        self.log_label.set_text(log or "(пусто)")

    def _refresh_card(self, d: TaskDef, s: model.TaskViewState, busy: bool) -> None:
        card = self._cards[d.id]
        tone = STATE_TONE[s.state]
        if getattr(card, "_tone", None) != tone:
            card._tone = tone
            set_dot_tone(card._dot, tone)
            card._badge.set_text(STATE_TEXT[s.state])
            card._badge.remove_css_class(f"pill-{STATE_TONE.get(s.last_result_state or s.state)}")
            card._badge.add_css_class(f"pill-{tone}")

        if s.state == model.RUNNING and s.progress_total and s.progress_total > 0:
            card._progress.set_fraction(min(1.0, s.progress_current / s.progress_total))
            card._progress.set_text(f"{s.progress_current}/{s.progress_total}")
            card._progress.set_visible(True)
        else:
            card._progress.set_visible(False)

        if s.state == model.ERROR and s.last_result:
            card._error.set_text(s.last_result[:240])
            card._error.set_visible(True)
        else:
            card._error.set_visible(False)

        for a in d.actions:
            btn = self._buttons.get((d.id, a.kind))
            if btn is None:
                continue
            if a.kind == "stop":
                btn.set_sensitive(not busy and (self.controller.llm.get_status("day").online or self.controller.llm.get_status("archive").online))
            else:
                disabled = busy or (d.requires_llm and not (self.controller.llm.get_status("day").online or self.controller.llm.get_status("archive").online))
                btn.set_sensitive(not disabled)

        hist_sig = tuple((r.at, r.state, r.detail) for r in s.history[:5])
        if getattr(card, "_hist_sig", None) != hist_sig:
            card._hist_sig = hist_sig
            while (w := card._hist_box.get_first_child()) is not None:
                card._hist_box.remove(w)
            for r in s.history[:5]:
                card._hist_box.append(self._build_hist_row(r))
