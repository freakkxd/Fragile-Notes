"""AI Чат — боковая панель с LLM (через LlmService), история в памяти."""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from ..services.llm import LlmService  # noqa: E402
from .widgets import view_header  # noqa: E402

AI_CSS = """
.ai-chat {
    background-color: transparent;
}
.ai-history {
    background-color: transparent;
}
.ai-msg-row {
    margin: 4px 8px;
}
.ai-bubble {
    border-radius: 12px;
    padding: 10px 14px;
    border: 1px solid var(--ao-border-subtle);
    background-color: var(--ao-surface-glass);
    box-shadow: inset 0 1px 0 var(--ao-highlight);
    min-width: 120px;
    max-width: 720px;
}
.ai-bubble--user {
    background-color: rgba(130, 168, 255, 0.14);
    border-color: rgba(130, 168, 255, 0.30);
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.06);
}
.ai-bubble--assistant {
    background-color: var(--ao-surface-glass);
    border-color: var(--ao-border-subtle);
}
.ai-bubble--error {
    background-color: rgba(220, 130, 145, 0.10);
    border-color: rgba(220, 130, 145, 0.30);
}
.ai-role {
    font-size: 0.70em;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ao-text-muted);
    margin-bottom: 2px;
}
.ai-role--user {
    color: rgba(130, 168, 255, 0.95);
}
.ai-role--error {
    color: #f87171;
}
.ai-text {
    font-size: 0.94em;
    color: var(--ao-text);
    line-height: 1.48;
}
.ai-text--user {
    color: var(--ao-text);
}
.ai-input-row {
    padding: 10px 12px;
    border-top: 1px solid var(--ao-border-subtle);
    background-color: rgba(255,255,255,0.02);
}
.ai-input {
    border-radius: 999px;
}
.ai-status {
    font-size: 0.78em;
    color: var(--ao-text-muted);
    padding: 2px 12px;
}
.ai-empty {
    padding: 28px 18px;
    color: var(--ao-text-muted);
}
"""

_provider = None


def _ensure_ai_css() -> None:
    global _provider
    if _provider is not None:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    p = Gtk.CssProvider()
    try:
        p.load_from_string(AI_CSS)
        Gtk.StyleContext.add_provider_for_display(
            display, p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        _provider = p
    except Exception:
        pass


class AiChatView(Gtk.Box):
    """Простой чат с LLM: история в памяти, поле ввода, кнопка отправки."""

    def __init__(self, llm: LlmService, settings: dict) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, css_classes=["ai-chat"])
        self.llm = llm
        self.settings = settings
        self.history: list[dict] = []
        self._busy = False
        _ensure_ai_css()
        self._build()

    # ── UI ───────────────────────────────────────────────────
    def _build(self) -> None:
        self.append(view_header("🤖", "AI Чат", "Локальный LLM · история в памяти"))

        # статус-строка (online/offline)
        self.status_lbl = Gtk.Label(
            label=self._status_text(), halign=Gtk.Align.START, xalign=0,
            css_classes=["ai-status"], wrap=True,
        )
        self.append(self.status_lbl)

        # история
        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True, css_classes=["ai-history"])
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.history_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.history_box.set_margin_top(10)
        self.history_box.set_margin_bottom(10)
        self.history_box.set_margin_start(8)
        self.history_box.set_margin_end(8)
        scroller.set_child(self.history_box)
        self.scroller = scroller
        self.append(scroller)

        self._empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["ai-empty"])
        self._empty.set_halign(Gtk.Align.CENTER)
        self._empty.set_valign(Gtk.Align.CENTER)
        self._empty.append(Gtk.Label(label="💬", css_classes=["empty-icon"], halign=Gtk.Align.CENTER))
        self._empty.append(Gtk.Label(label="Задай вопрос LLM", css_classes=["empty-text"], halign=Gtk.Align.CENTER))
        self._empty.append(Gtk.Label(label="История хранится в памяти до закрытия вкладки", css_classes=["dim-hint", "empty-hint"], halign=Gtk.Align.CENTER, wrap=True, justify=Gtk.Justification.CENTER))
        self.history_box.append(self._empty)

        # поле ввода
        input_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["ai-input-row"])
        input_row.set_margin_top(2)
        self.entry = Gtk.Entry(placeholder_text="Спроси что-нибудь…", hexpand=True, css_classes=["ai-input"])
        self.entry.connect("activate", lambda *_: self._on_send())
        input_row.append(self.entry)

        self.send_btn = Gtk.Button(label="Отправить", css_classes=["suggested-action", "mod-cta"])
        self.send_btn.connect("clicked", lambda *_: self._on_send())
        input_row.append(self.send_btn)

        self.clear_btn = Gtk.Button(label="Очистить", css_classes=["mod-neutral"])
        self.clear_btn.connect("clicked", lambda *_: self._on_clear())
        input_row.append(self.clear_btn)

        self.spinner = Gtk.Spinner(spinning=False, visible=False)
        input_row.append(self.spinner)

        self.append(input_row)

        # профиль выбора (day/archive)
        profile_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        profile_row.set_margin_start(12)
        profile_row.set_margin_end(12)
        profile_row.set_margin_bottom(6)
        profile_row.append(Gtk.Label(label="Профиль:", css_classes=["dim-hint"]))
        self.profile_drop = Gtk.DropDown.new_from_strings(["day — Qwen3-14B", "archive — Gemma-4-26B"])
        self.profile_drop.set_selected(0)
        # выбрать по online статусу
        try:
            if not self.llm.get_status("day").online and self.llm.get_status("archive").online:
                self.profile_drop.set_selected(1)
        except Exception:
            pass
        profile_row.append(self.profile_drop)
        # вставим перед input_row: найдём индекс и вставим
        # проще: append и reorder — GTK4 Box без insert; пересоберём: удалим input_row и добавим profile_row затем input_row
        self.remove(input_row)
        self.append(profile_row)
        self.append(input_row)

        self._refresh_status()

    def _status_text(self) -> str:
        try:
            day = self.llm.get_status("day")
            arch = self.llm.get_status("archive")
            parts = []
            parts.append(f"day: {'ВКЛ' if day.online else 'ВЫКЛ' if day.online is False else '—'}")
            parts.append(f"archive: {'ВКЛ' if arch.online else 'ВЫКЛ' if arch.online is False else '—'}")
            if day.online or arch.online:
                parts.append("· готов к чату")
            else:
                parts.append("· LLM офлайн — проверь manage-llm / порт")
            return "  ·  ".join(parts)
        except Exception:
            return "LLM статус неизвестен"

    def _refresh_status(self) -> None:
        self.status_lbl.set_text(self._status_text())

    def _profile(self) -> str:
        sel = self.profile_drop.get_selected()
        return "archive" if sel == 1 else "day"

    # ── Сообщения ───────────────────────────────────────────
    def _create_bubble(self, role: str, text: str) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0, css_classes=["ai-msg-row"])
        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["ai-bubble", f"ai-bubble--{role}"])
        role_lbl = Gtk.Label(
            label="Вы" if role == "user" else ("Ошибка" if role == "error" else "Ассистент"),
            halign=Gtk.Align.START, xalign=0,
            css_classes=["ai-role", f"ai-role--{role}"],
        )
        bubble.append(role_lbl)
        txt = Gtk.Label(
            label=text, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
            halign=Gtk.Align.START, xalign=0, selectable=True,
            css_classes=["ai-text", f"ai-text--{role}"],
        )
        txt.set_max_width_chars(72)
        bubble.append(txt)
        # выравнивание: user — справа, assistant — слева
        if role == "user":
            row.set_halign(Gtk.Align.END)
            row.set_hexpand(True)
            bubble.set_halign(Gtk.Align.END)
            bubble.set_hexpand(False)
        else:
            row.set_halign(Gtk.Align.START)
            row.set_hexpand(True)
            bubble.set_halign(Gtk.Align.START)
        row.append(bubble)
        return row

    def _append_message(self, role: str, content: str) -> None:
        if getattr(self, "_empty", None) is not None and self._empty.get_parent() is not None:
            self.history_box.remove(self._empty)
        row = self._create_bubble(role, content)
        self.history_box.append(row)
        GLib.idle_add(self._scroll_to_end)

    def _scroll_to_end(self) -> bool:
        adj = self.scroller.get_vadjustment()
        if adj is not None:
            adj.set_value(adj.get_upper() - adj.get_page_size())
        return False

    # ── Действия ─────────────────────────────────────────────
    def _on_clear(self) -> None:
        self.history.clear()
        while (child := self.history_box.get_first_child()) is not None:
            self.history_box.remove(child)
        self.history_box.append(self._empty)
        self._empty.set_visible(True)

    def _on_send(self) -> None:
        if self._busy:
            return
        text = self.entry.get_text().strip()
        if not text:
            return
        self.entry.set_text("")
        self.history.append({"role": "user", "content": text})
        self._append_message("user", text)
        self._set_busy(True)
        # снимок истории для потока
        messages = list(self.history)
        profile = self._profile()

        def work() -> None:
            ok, reply = self.llm.chat(messages, profile=profile, timeout=120.0)
            GLib.idle_add(lambda: self._on_llm_done(ok, reply))

        threading.Thread(target=work, daemon=True).start()

    def _on_llm_done(self, ok: bool, reply: str) -> bool:
        self._set_busy(False)
        self._refresh_status()
        reply = (reply or "").strip()
        if not ok:
            msg = reply or "Ошибка LLM"
            self._append_message("error", msg)
            return False
        if not reply:
            reply = "(пустой ответ)"
        self.history.append({"role": "assistant", "content": reply})
        self._append_message("assistant", reply)
        return False

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.send_btn.set_sensitive(not busy)
        self.entry.set_sensitive(not busy)
        self.spinner.set_visible(busy)
        self.spinner.set_spinning(busy)
        if not busy:
            self.entry.grab_focus()

    # ── Обновление извне (если app меняет llm) ─────────────
    def refresh_llm(self, llm: LlmService) -> None:
        self.llm = llm
        self._refresh_status()
