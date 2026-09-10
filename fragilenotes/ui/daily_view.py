"""Daily — просмотр/создание сегодняшней daily-заметки."""

from __future__ import annotations

import calendar
import datetime
import re
import threading
from pathlib import Path

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from ..paths import resolve_paths
from ..services.ai_summary import AiSummaryService
from .markdown import MarkdownView
from .scale import attach_zoom_keys, editor_zoom
from .scale import subscribe as scale_subscribe
from .widgets import view_header


def _daily_slug(d: datetime.date) -> str:
    return d.isoformat()


def _resolve_template(template: str) -> str | None:
    p = Path(template)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def _apply_template(tpl: str, d: datetime.date) -> str:
    days_ru = [
        "понедельник", "вторник", "среда", "четверг",
        "пятница", "суббота", "воскресенье",
    ]
    months_ru = [
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    ]
    out = tpl
    out = out.replace("{{date:YYYY-MM-DD}}", d.isoformat())
    out = out.replace("{{date:YYYY}}", f"{d.year:04d}")
    out = out.replace("{{date:MM}}", f"{d.month:02d}")
    out = out.replace("{{date:DD}}", f"{d.day:02d}")
    out = out.replace(
        "{{date:dddd, D MMMM}}",
        f"{days_ru[d.weekday()]}, {d.day} {months_ru[d.month - 1]}",
    )
    out = re.sub(r"\{\{date:[^}]*\}\}", d.isoformat(), out)
    return out


def _word_count(text: str) -> int:
    return len([w for w in re.split(r"\s+", text) if w.strip()])


class DailyView(Gtk.Box):
    def __init__(self, settings: dict, on_open=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self.on_open = on_open   # callable(path) для открытия связанной заметки
        self._file: str | None = None

        self.append(view_header("📅", "Daily"))

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["toolbar"])
        toolbar.set_margin_start(14)
        toolbar.set_margin_end(14)
        self.status = Gtk.Label(label="", css_classes=["dim-hint"], hexpand=True, halign=Gtk.Align.START, xalign=0,
                                ellipsize=Pango.EllipsizeMode.END)
        toolbar.append(self.status)
        self.word_chip = self._chip("0 слов")
        self.char_chip = self._chip("0 символов")
        self.tasks_chip = self._chip("—")
        self.zoom_chip = self._chip(self._zoom_text())
        toolbar.append(self.word_chip)
        toolbar.append(self.char_chip)
        toolbar.append(self.tasks_chip)
        toolbar.append(self.zoom_chip)
        # AI саммари дайджеста — кэш + LLM через AiSummaryService
        self.ai_summary_btn = Gtk.Button(label="✨ Саммари", css_classes=["pill-btn"])
        self.ai_summary_btn.set_tooltip_text("AI-саммари последних заметок/дайджеста (кэш 30м)")
        self.ai_summary_btn.connect("clicked", lambda *_: self._on_ai_summary(force=False))
        self.ai_summary_refresh = Gtk.Button(label="↻", css_classes=["link-btn"])
        self.ai_summary_refresh.set_tooltip_text("Перегенерировать саммари (игнор кэша)")
        self.ai_summary_refresh.connect("clicked", lambda *_: self._on_ai_summary(force=True))
        self.ai_spinner = Gtk.Spinner(spinning=False, visible=False)
        toolbar.append(self.ai_summary_btn)
        toolbar.append(self.ai_summary_refresh)
        toolbar.append(self.ai_spinner)
        self._ai_busy = False
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher(stack=self.stack, css_classes=["toolbar-switcher"])
        toolbar.append(switcher)
        self.save_btn = Gtk.Button(label="Сохранить", css_classes=["suggested-action"])
        self.save_btn.connect("clicked", self._on_save)
        toolbar.append(self.save_btn)
        self.append(toolbar)

        self._text = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD,
            top_margin=8, bottom_margin=8, left_margin=10, right_margin=10,
            hexpand=True, vexpand=True, css_classes=["editor", "prose"],
        )
        self._text.get_buffer().connect("changed", self._on_changed)
        self.preview = MarkdownView()
        self.preview.on_wikilink = self._on_wikilink
        self.stack.add_named(self._scroll(self._text), "Редактор")
        self.stack.add_named(self._scroll(self.preview, cls=("md-read",)), "Просмотр")
        self.stack.set_visible_child_name("Просмотр")
        self.stack.connect("notify::visible-child-name", self._on_switch)
        center = Gtk.CenterBox(hexpand=True, vexpand=True)
        center.set_center_widget(self.stack)
        center.set_margin_start(14)
        center.set_margin_end(14)
        center.set_margin_bottom(14)
        self.append(center)

        self._today = None
        self._loading = False
        self._unsub_scale = None
        attach_zoom_keys(self)
        save_key = Gtk.EventControllerKey.new()
        save_key.connect("key-pressed", self._on_key_save)
        self.add_controller(save_key)
        ed_key = Gtk.EventControllerKey.new()
        ed_key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        ed_key.connect("key-pressed", self._on_editor_key)
        self._text.add_controller(ed_key)
        self._unsub_scale = scale_subscribe(self._on_zoom)
        self.connect("destroy", self._on_destroy)
        self.reload()

    def _on_destroy(self, _w) -> None:
        if self._unsub_scale:
            self._unsub_scale()

    def duplicate_line(self) -> bool:
        """Ctrl+D — дублировать строку в daily-редакторе."""
        buf = self._text.get_buffer()
        if buf is None:
            return False
        insert = buf.get_insert()
        it = buf.get_iter_at_mark(insert)
        line = it.get_line()
        line_offset = it.get_line_offset()
        start = buf.get_iter_at_line(line)
        if line + 1 < buf.get_line_count():
            end = buf.get_iter_at_line(line + 1)
            text = buf.get_text(start, end, True)
            buf.insert(end, text)
        else:
            end = buf.get_end_iter()
            text = buf.get_text(start, end, True)
            if text:
                buf.insert(end, "\n" + text)
            else:
                buf.insert(end, "\n")
        new_line = line + 1
        if new_line < buf.get_line_count():
            try:
                nit = buf.get_iter_at_line_offset(new_line, min(line_offset, 9999))
            except Exception:  # noqa: BLE001
                nit = buf.get_iter_at_line(new_line)
            buf.place_cursor(nit)
            self._text.scroll_to_iter(nit, 0.0, False, 0, 0)
        return True

    def _on_editor_key(self, _ctrl, keyval, _keycode, state) -> bool:
        if state & Gdk.ModifierType.CONTROL_MASK and keyval in (Gdk.KEY_d, Gdk.KEY_D):
            if not (state & Gdk.ModifierType.SHIFT_MASK):
                return self.duplicate_line()
        return False

    def _on_key_save(self, _ctrl, keyval, _keycode, state) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if ctrl and keyval in (Gdk.KEY_s, Gdk.KEY_S) and not shift:
            self._on_save(None)
            return True
        if ctrl and keyval in (Gdk.KEY_d, Gdk.KEY_D) and not shift:
            return self.duplicate_line()
        return False

    def _on_wikilink(self, target: str) -> None:
        from ..services import vault
        p = vault.resolve_wikilink(self.settings, target)
        if p is not None and self.on_open is not None:
            self.on_open(str(p))

    def _zoom_text(self) -> str:
        return f"{editor_zoom() * 100:.0f}%"

    def _on_zoom(self) -> None:
        self.zoom_chip.set_text(self._zoom_text())

    def _scroll(self, child: Gtk.Widget, cls: tuple[str, ...] = ()):
        sc = Gtk.ScrolledWindow(
            hexpand=True, vexpand=True, css_classes=list(cls) + ["editor-frame"],
        )
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc.set_child(child)
        clamp = Adw.Clamp(maximum_size=920, tightening_threshold=720)
        clamp.set_child(sc)
        return clamp

    def _on_switch(self, _stack: Gtk.Stack, _pspec) -> None:
        if self.stack.get_visible_child_name() == "Просмотр":
            self._render_preview()

    def _chip(self, text: str) -> Gtk.Label:
        lbl = Gtk.Label(label=text, css_classes=["glass-card__count"])
        return lbl

    def _refresh_counts(self, text: str) -> None:
        self.word_chip.set_text(f"{_word_count(text)} слов")
        self.char_chip.set_text(f"{len(text)} символов")

    def _refresh_tasks_chip(self) -> None:
        try:
            from ..core import tasks as tm
            from ..paths import resolve_paths
            all_tasks = tm.load_tasks(resolve_paths(self.settings).tm_tasks)
            today = datetime.date.today()
            n = len([t for t in all_tasks if t.is_active and (t.is_due_on(today) or t.is_overdue(today))])
            self.tasks_chip.set_text(f"{n} задач сегодня")
        except Exception:  # noqa: BLE001
            self.tasks_chip.set_text("—")

    def reload_if_needed(self) -> None:
        if self._today != datetime.date.today():
            self.reload()

    def reload(self) -> None:
        paths = resolve_paths(self.settings)
        today = datetime.date.today()
        full = paths.daily / f"{_daily_slug(today)}.md"
        self._file = str(full)
        self._today = today

        weekday = calendar.day_name[today.weekday()]
        base = f"Заметка: {today.isoformat()} ({weekday})"

        if full.exists():
            content = full.read_text(encoding="utf-8")
        else:
            tpl_path = str(
                self.settings.get("daily_template")
                or paths.tm_templates / "Daily note.md"
            )
            tpl = _resolve_template(tpl_path)
            content = _apply_template(tpl or "# {{date:YYYY-MM-DD}}\n", today)

        self._loading = True
        self._text.get_buffer().set_text(content)
        self._loading = False

        self.save_btn.set_sensitive(full.exists())
        self.status.set_label(
            f"{base} · существующая" if full.exists()
            else f"{base} · черновик (не сохранён)"
        )
        self._refresh_counts(content)
        self._refresh_tasks_chip()
        if self.stack.get_visible_child_name() == "Просмотр":
            self._render_preview()

    def _render_preview(self) -> None:
        buf = self._text.get_buffer()
        start, end = buf.get_bounds()
        self.preview.set_markdown(buf.get_text(start, end, True))

    def _on_changed(self, buf: Gtk.TextBuffer) -> None:
        if self._loading:
            return
        if self._file:
            self.save_btn.set_sensitive(True)
            self.status.set_label(f"{self.status.get_label().split('·')[0].strip()} · не сохранено")
        start, end = buf.get_bounds()
        self._refresh_counts(buf.get_text(start, end, True))

    def _on_save(self, _btn: Gtk.Button) -> None:
        if not self._file:
            return
        p = Path(self._file)
        p.parent.mkdir(parents=True, exist_ok=True)
        buf = self._text.get_buffer()
        start, end = buf.get_bounds()
        text = buf.get_text(start, end, True)
        p.write_text(text, encoding="utf-8")
        self.save_btn.set_sensitive(False)
        self.status.set_label(f"сохранено ✓ · {len(text)} символов · {_word_count(text)} слов")
        self._refresh_counts(text)

    # ── AI саммари (через AiSummaryService, кэш) ─────────────
    def _set_ai_busy(self, busy: bool) -> None:
        self._ai_busy = busy
        self.ai_summary_btn.set_sensitive(not busy)
        self.ai_summary_refresh.set_sensitive(not busy)
        self.ai_spinner.set_visible(busy)
        self.ai_spinner.set_spinning(busy)

    def _on_ai_summary(self, force: bool = False) -> None:
        if getattr(self, "_ai_busy", False):
            return
        self._set_ai_busy(True)
        self.status.set_label("⏳ Генерация AI-саммари…")
        settings_copy = dict(self.settings)
        llm = None
        try:
            root = self.get_root()
            if root is not None and hasattr(root, "llm"):
                llm = root.llm  # type: ignore[attr-defined]
        except Exception:
            llm = None

        def work() -> None:
            try:
                svc = AiSummaryService(settings=settings_copy, llm=llm)
                res = svc.generate(force=force)
                if res.ok:
                    GLib.idle_add(lambda: self._on_ai_summary_done(True, res.text, cached=res.cached) or False)
                else:
                    msg = res.error or "LLM недоступен"
                    GLib.idle_add(lambda: self._on_ai_summary_done(False, msg) or False)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(lambda exc=exc: self._on_ai_summary_done(False, str(exc)) or False)

        threading.Thread(target=work, daemon=True).start()

    def _on_ai_summary_done(self, ok: bool, text: str, cached: bool = False) -> bool:
        self._set_ai_busy(False)
        if not ok:
            self.status.set_label(f"AI саммари: ошибка — {text[:120]}")
            self._show_summary_dialog(False, text)
            return False
        self.status.set_label(f"AI саммари готово {'(кэш)' if cached else '(LLM)'} ✓")
        self._show_summary_dialog(True, text, cached=cached)
        return False

    def _show_summary_dialog(self, ok: bool, text: str, cached: bool = False) -> None:
        title = "✨ AI Саммари" + (" (кэш)" if cached else "")
        dialog = Adw.Dialog(title=title)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, css_classes=["dialog-body"], margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
        head = Gtk.Label(label=title, css_classes=["task-title"], halign=Gtk.Align.START, xalign=0)
        body.append(head)
        if not ok:
            err = Gtk.Label(label=text or "Ошибка генерации", wrap=True, halign=Gtk.Align.START, xalign=0, selectable=True, css_classes=["dim-hint"])
            err.set_max_width_chars(80)
            body.append(err)
        else:
            sc = Gtk.ScrolledWindow(vexpand=True, hexpand=True, css_classes=["editor-frame"])
            sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            sc.set_min_content_height(220)
            sc.set_min_content_width(480)
            lbl = Gtk.Label(label=text.strip(), wrap=True, halign=Gtk.Align.START, xalign=0, selectable=True)
            lbl.set_max_width_chars(80)
            lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            sc.set_child(lbl)
            body.append(sc)
            # вставить в daily-заметку
            insert_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            insert_row.set_halign(Gtk.Align.END)
            copy_btn = Gtk.Button(label="Копировать", css_classes=["mod-neutral"])
            insert_btn = Gtk.Button(label="Вставить в заметку", css_classes=["suggested-action"])
            def _do_copy(*_):
                try:
                    disp = self.get_display()
                    if disp is not None:
                        disp.get_clipboard().set(text.strip())
                    self.status.set_label("саммари скопировано в буфер ✓")
                except Exception:
                    pass
                dialog.close()
            def _do_insert(*_):
                try:
                    buf = self._text.get_buffer()
                    end = buf.get_end_iter()
                    ins = "\n\n## ✨ AI Саммари\n\n" + text.strip() + "\n"
                    buf.insert(end, ins)
                    self.status.set_label("саммари вставлено в заметку · не забудь сохранить")
                    self.save_btn.set_sensitive(True)
                except Exception:
                    pass
                dialog.close()
            copy_btn.connect("clicked", _do_copy)
            insert_btn.connect("clicked", _do_insert)
            insert_row.append(copy_btn)
            insert_row.append(insert_btn)
            body.append(insert_row)
        # footer close
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        close = Gtk.Button(label="Закрыть", css_classes=["mod-neutral"])
        close.connect("clicked", lambda *_: dialog.close())
        footer.append(close)
        body.append(footer)
        clamp = Adw.Clamp(maximum_size=640, tightening_threshold=480)
        clamp.set_child(body)
        dialog.set_child(clamp)
        dialog.set_content_width(560)
        dialog.set_content_height(420)
        dialog.present(self.get_root() if self.get_root() else self)
