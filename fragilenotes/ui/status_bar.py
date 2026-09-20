"""Статус-бар внизу окна: текущая операция, LLM, enrich, задачи."""

from __future__ import annotations

from gi.repository import Gtk, Pango

from ..core import runner as model


def _chip(text: str, tone: str = "idle") -> Gtk.Widget:
    lbl = Gtk.Label(
        label=text, css_classes=["sb-chip", f"sb-chip-{tone}"],
        margin_start=2, margin_end=2,
    )
    return lbl


class StatusBar(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.add_css_class("sb")
        self.set_valign(Gtk.Align.CENTER)
        self.set_tooltip_text(
            "Горячие клавиши: Ctrl+O/P/K — быстрый переход • "
            "Ctrl+Shift+P — палитра команд • "
            "Ctrl+Shift+F — глобальный поиск • "
            "Ctrl+D — дублировать строку • "
            "Ctrl+E / Ctrl+Shift+E / Ctrl+Enter — предпросмотр • "
            "Ctrl+S — сохранить • Ctrl+N — новая заметка • Ctrl+B — сайдбар"
        )
        self.op_label = Gtk.Label(
            label="", halign=Gtk.Align.START, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE,
        )
        self.op_label.add_css_class("sb-op")
        self.append(self.op_label)

        self.chips: list[Gtk.Widget] = []
        self.chip_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.append(self.chip_box)

    def _set_chips(self, texts: list[tuple[str, str]]) -> None:
        for w in self.chips:
            self.chip_box.remove(w)
        self.chips = []
        for text, tone in texts:
            w = _chip(text, tone)
            self.chips.append(w)
            self.chip_box.append(w)

    def refresh(
        self,
        controller,
        llm,
        enrich,
        due_today: int,
        overdue: int,
        log: str,
        git_sync=None,
    ) -> None:
        st = controller.status
        if st.state == model.RUNNING:
            progress = ""
            if st.progress_total and st.progress_total > 0:
                progress = f"  {st.progress_current}/{st.progress_total}"
            op_text = f"▶ {st.current_command}{progress} …"
        elif st.state == model.ERROR:
            op_text = f"✗ {st.last_error or 'ошибка'}"
        elif st.state == model.DONE:
            op_text = "✓ готово"
        else:
            op_text = "—"
        if self.op_label.get_label() != op_text:
            self.op_label.set_label(op_text)

        day = llm.get_status("day")
        arch = llm.get_status("archive")
        texts: list[tuple[str, str]] = [
            ("💎 day " + ("ВКЛ" if day.online else "ВЫКЛ"), "ok" if day.online else ("error" if day.online is False else "idle")),
            ("🌙 arch " + ("ВКЛ" if arch.online else "ВЫКЛ"), "ok" if arch.online else ("error" if arch.online is False else "idle")),
            (enrich.summary(), "run" if enrich.state == "running" else "idle"),
            (f"✅ сегодня {due_today}", "warn" if due_today else "idle"),
        ]
        if overdue:
            texts.append((f"⚠ просрочено {overdue}", "error"))
        # git auto-sync статус
        if git_sync is not None:
            try:
                label, tone = git_sync.status_text() if hasattr(git_sync, "status_text") else (git_sync.get_status().label(), git_sync.get_status().tone())  # type: ignore
                # не показываем "нет репо" как ошибку — idle тон
                texts.append((label, tone))
            except Exception:
                pass
        # пересобираем чипы только при реальных изменениях
        if texts != getattr(self, "_sig", None):
            self._sig = texts
            self._set_chips(texts)
