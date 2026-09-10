"""Mermaid Live Preview — split view для FragileNotes.

Слева — редактор mermaid-кода, справа — live preview (WebView) с автообновлением
300 мс debounce (GLib.timeout_add). Интеграция:

  * Отдельная вкладка ``mermaid_live`` в FragileWindow (см. app.py VIEWS)
  * Интеграция в FilesView — кнопка «Mermaid Live» открывает диалог с текущим кодом

API:
  - MermaidLiveView(settings, initial_code=None) -> Gtk.Box
  - create_dialog(parent, settings, initial_code) -> Adw.Dialog
  - extract_code_from_text(text) -> str (первый mermaid блок или весь текст)

Зависимости: fragilenotes.ui.mermaid (build_mermaid_html, has_webkit, sanitize...)
Фоллбэк без WebKit — показывает подсказку + исходник, без падения.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# ── Константы ──────────────────────────────────────────────────────────
DEBOUNCE_MS = 300

EXAMPLES: dict[str, str] = {
    "Flowchart": "graph TD\n    A[Начало] --> B{Решение?}\n    B -- Да --> C[Успех]\n    B -- Нет --> D[Повтор]\n    D --> B",
    "Sequence": "sequenceDiagram\n    participant A as Клиент\n    participant B as Сервер\n    A->>B: Запрос\n    B-->>A: Ответ\n    Note over A,B: Диаграмма последовательности",
    "Class": "classDiagram\n    class Note {\n        +String title\n        +String body\n        +save()\n    }\n    Note <|-- DailyNote",
    "State": "stateDiagram-v2\n    [*] --> Idle\n    Idle --> Processing : событие\n    Processing --> Idle : готово\n    Processing --> Error : ошибка\n    Error --> Idle : retry",
    "Gantt": "gantt\n    title План релиза\n    dateFormat  YYYY-MM-DD\n    section Разработка\n    Дизайн       :a1, 2026-01-01, 7d\n    Кодинг       :after a1, 10d\n    section Тест\n    QA           :after a1, 12d",
    "Pie": 'pie title Распределение\n    "Код" : 42\n    "Тесты" : 30\n    "Док" : 15\n    "Ревью" : 13',
    "ER": "erDiagram\n    VAULT ||--o{ NOTE : содержит\n    NOTE ||--o{ TAG : имеет\n    NOTE }o--|| USER : автор",
    "Mindmap": "mindmap\n  root((FragileNotes))\n    Vault\n      Заметки\n      Шаблоны\n    Граф\n    Mermaid",
    "Пусто": "graph TD\n    A[Пусто — введите диаграмму]",
}

THEMES = ["dark", "default", "forest", "neutral"]


def extract_code_from_text(text: str) -> str:
    """Вытащить первый mermaid-блок из markdown, иначе вернуть сам текст если это mermaid."""
    if not text:
        return EXAMPLES["Flowchart"]
    try:
        from .mermaid import extract_mermaid_blocks, has_mermaid

        if has_mermaid(text):
            blocks = extract_mermaid_blocks(text)
            if blocks:
                return blocks[0]
    except Exception:
        pass
    # эвристика: если текст похож на mermaid (начинается с graph/sequence/class/...)
    stripped = text.strip()
    if stripped and not stripped.startswith("#") and "```" not in stripped:
        # считаем что это уже mermaid-код
        # но если строка короткая и без ключевых слов — вернём пример
        keywords = (
            "graph",
            "sequenceDiagram",
            "classDiagram",
            "stateDiagram",
            "gantt",
            "pie",
            "erDiagram",
            "mindmap",
            "flowchart",
            "journey",
        )
        low = stripped.lower()
        if any(k.lower() in low for k in keywords):
            return stripped
    # fallback — первый блок или пример
    return EXAMPLES["Flowchart"]


# ── GTK imports (lazy, tolerate headless) ─────────────────────────────
try:
    import gi  # type: ignore

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

    _GTK_AVAILABLE = True
except Exception as _e:  # pragma: no cover
    _GTK_AVAILABLE = False
    Adw = Gio = GLib = Gtk = Gdk = Pango = None  # type: ignore
    log.debug("mermaid_live: GTK unavailable: %s", _e)


# ── Mermaid helpers (import lazy to avoid circular) ───────────────────
def _sanitize(code: str) -> str:
    try:
        from .mermaid import sanitize_mermaid_code

        return sanitize_mermaid_code(code)
    except Exception:
        c = (code or "").strip()
        return c if c else "graph TD\n    A[Пусто]"


def _build_html(code: str, theme: str = "dark") -> str:
    try:
        from .mermaid import build_mermaid_html

        return build_mermaid_html(code, theme=theme)
    except Exception as e:
        log.debug("build_mermaid_html fallback: %s", e)
        # minimal inline fallback
        import html as _html

        esc = _html.escape(_sanitize(code))
        return f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>body{{margin:0;padding:16px;background:#06080d;color:#d2dae8;font-family:monospace}}pre{{white-space:pre-wrap}}</style></head><body><pre>{esc}</pre></body></html>"


def _has_webkit() -> bool:
    try:
        from .mermaid import has_webkit

        return bool(has_webkit())
    except Exception:
        return False


# ── View ───────────────────────────────────────────────────────────────
if _GTK_AVAILABLE:

    class MermaidLiveView(Gtk.Box):  # type: ignore[misc]
        """Split view: слева TextView-редактор, справа WebView preview с 300 мс debounce."""

        def __init__(self, settings: dict | None = None, initial_code: str | None = None) -> None:
            super().__init__(
                orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True, vexpand=True
            )
            self.settings = dict(settings or {})
            self._preview_timer: int | None = None
            self._webview: object | None = None
            self._fallback_label: Gtk.Widget | None = None
            self._theme: str = "dark"
            self._alive = True
            self._current_file: Path | None = None
            self.connect("destroy", self._on_destroy)
            self._build(initial_code or EXAMPLES["Flowchart"])

        # ── lifecycle ──────────────────────────────────────────────
        def _on_destroy(self, *_a) -> None:
            self._alive = False
            if self._preview_timer is not None:
                try:
                    GLib.source_remove(self._preview_timer)
                except Exception:
                    pass
                self._preview_timer = None

        # ── build ──────────────────────────────────────────────────
        def _build(self, initial_code: str) -> None:
            # Header
            from .widgets import view_header  # local import to avoid cycle

            self.append(
                view_header(
                    "🧜",
                    "Mermaid Live",
                    "Split view: слева редактор, справа live preview (300 мс debounce)",
                )
            )

            toolbar = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["toolbar"]
            )
            toolbar.set_margin_start(14)
            toolbar.set_margin_end(14)

            # theme
            toolbar.append(Gtk.Label(label="Тема:", css_classes=["dim-hint"]))
            self._theme_drop = Gtk.DropDown.new_from_strings(THEMES)
            try:
                self._theme_drop.set_selected(THEMES.index(self._theme))
            except ValueError:
                self._theme_drop.set_selected(0)
            self._theme_drop.connect("notify::selected", self._on_theme_changed)
            self._theme_drop.set_tooltip_text("Тема mermaid.js")
            toolbar.append(self._theme_drop)

            # examples
            toolbar.append(Gtk.Label(label="Пример:", css_classes=["dim-hint"]))
            self._ex_drop = Gtk.DropDown.new_from_strings(list(EXAMPLES.keys()))
            self._ex_drop.set_selected(0)
            toolbar.append(self._ex_drop)
            ex_btn = Gtk.Button(label="Вставить", css_classes=["flat"])
            ex_btn.connect("clicked", self._on_example)
            ex_btn.set_tooltip_text("Вставить выбранный пример в редактор")
            toolbar.append(ex_btn)

            # actions
            copy_btn = Gtk.Button(
                icon_name="edit-copy-symbolic", tooltip_text="Копировать код", css_classes=["flat"]
            )
            copy_btn.connect("clicked", self._on_copy)
            toolbar.append(copy_btn)

            export_btn = Gtk.Button(
                label="Экспорт SVG",
                icon_name="document-save-symbolic",
                css_classes=["flat"],
                tooltip_text="Экспорт в SVG (mermaid-cli)",
            )
            export_btn.connect("clicked", self._on_export)
            toolbar.append(export_btn)

            open_btn = Gtk.Button(label="Открыть .mmd/.md", css_classes=["flat"])
            open_btn.connect("clicked", self._on_open_file)
            toolbar.append(open_btn)

            save_btn = Gtk.Button(label="Сохранить", css_classes=["suggested-action"])
            save_btn.connect("clicked", self._on_save_file)
            toolbar.append(save_btn)
            self._save_btn = save_btn

            # status
            self._status = Gtk.Label(
                label="",
                css_classes=["dim-hint"],
                hexpand=True,
                halign=Gtk.Align.END,
                xalign=1,
                ellipsize=Pango.EllipsizeMode.MIDDLE,
            )
            toolbar.append(self._status)

            self.append(toolbar)

            # Paned split
            paned = Gtk.Paned(
                orientation=Gtk.Orientation.HORIZONTAL,
                hexpand=True,
                vexpand=True,
                css_classes=["mermaid-live-paned"],
            )
            paned.set_margin_start(14)
            paned.set_margin_end(14)
            paned.set_margin_bottom(14)
            paned.set_shrink_start_child(False)
            paned.set_shrink_end_child(False)
            paned.set_resize_start_child(True)
            paned.set_resize_end_child(True)
            try:
                paned.set_wide_handle(True)
            except Exception:
                pass

            # ── left: editor ──────────────────────────────────────
            left = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True, vexpand=True
            )
            left.set_size_request(380, -1)
            left_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            left_hdr.append(
                Gtk.Label(
                    label="Редактор",
                    css_classes=["dim-hint", "mermaid-header"],
                    halign=Gtk.Align.START,
                )
            )
            self._line_label = Gtk.Label(
                label="—", css_classes=["dim-hint"], halign=Gtk.Align.END, hexpand=True, xalign=1
            )
            left_hdr.append(self._line_label)
            clear_btn = Gtk.Button(label="Очистить", css_classes=["flat", "pill"])
            clear_btn.connect("clicked", self._on_clear)
            left_hdr.append(clear_btn)
            left.append(left_hdr)

            self.buffer = Gtk.TextBuffer()
            self.buffer.set_text(_sanitize(initial_code))
            self.buffer.connect("changed", self._on_text_changed)
            self.editor = Gtk.TextView(
                buffer=self.buffer,
                wrap_mode=Gtk.WrapMode.WORD,
                hexpand=True,
                vexpand=True,
                css_classes=["editor", "mermaid-editor"],
                top_margin=10,
                bottom_margin=10,
                left_margin=12,
                right_margin=12,
                monospace=True,
            )
            # key: Ctrl+Enter -> force refresh, Ctrl+S -> save
            ek = Gtk.EventControllerKey.new()
            ek.connect("key-pressed", self._on_editor_key)
            self.editor.add_controller(ek)

            ed_scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True, css_classes=["editor-frame"])
            ed_scroll.set_child(self.editor)
            left.append(ed_scroll)

            hint = Gtk.Label(
                label="Подсказка: graph TD, sequenceDiagram, classDiagram, stateDiagram, gantt, pie, erDiagram, mindmap…",
                css_classes=["dim-hint"],
                halign=Gtk.Align.START,
                wrap=True,
                xalign=0,
            )
            left.append(hint)

            # ── right: preview ────────────────────────────────────
            right = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True, vexpand=True
            )
            right.set_size_request(380, -1)
            right_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            right_hdr.append(
                Gtk.Label(
                    label="Preview (live, 300 мс)",
                    css_classes=["dim-hint", "mermaid-header"],
                    halign=Gtk.Align.START,
                )
            )
            self._preview_status = Gtk.Label(
                label="", css_classes=["dim-hint"], halign=Gtk.Align.END, hexpand=True, xalign=1
            )
            right_hdr.append(self._preview_status)
            refresh_btn = Gtk.Button(
                icon_name="view-refresh-symbolic",
                tooltip_text="Обновить сейчас",
                css_classes=["flat"],
            )
            refresh_btn.connect("clicked", lambda *_: self._schedule_preview(force=True))
            right_hdr.append(refresh_btn)
            right.append(right_hdr)

            # preview stack: webview | fallback
            self._preview_stack = Gtk.Stack(
                transition_type=Gtk.StackTransitionType.CROSSFADE, hexpand=True, vexpand=True
            )

            # webview container
            self._web_container = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True
            )
            self._web_scroll = Gtk.ScrolledWindow(
                hexpand=True, vexpand=True, css_classes=["editor-frame", "mermaid-frame"]
            )
            self._web_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            # WebView will be placed inside _web_scroll
            self._web_container.append(self._web_scroll)
            self._preview_stack.add_named(self._web_container, "webview")

            # fallback container
            fb_box = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=8,
                hexpand=True,
                vexpand=True,
                css_classes=["mermaid-fallback"],
            )
            fb_box.set_margin_top(12)
            fb_box.set_margin_start(12)
            fb_box.set_margin_end(12)
            self._fallback_label = Gtk.Label(
                label="WebKit недоступен — установи WebKitGTK 6.0/4.1 для live preview.\nКод отображается как текст.",
                wrap=True,
                css_classes=["dim-hint"],
                halign=Gtk.Align.START,
                xalign=0,
            )
            fb_box.append(self._fallback_label)
            self._fallback_view = Gtk.TextView(
                editable=False,
                cursor_visible=False,
                wrap_mode=Gtk.WrapMode.WORD,
                css_classes=["mermaid-code"],
            )
            self._fallback_view.set_size_request(-1, 200)
            fb_scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True, css_classes=["editor-frame"])
            fb_scroll.set_child(self._fallback_view)
            fb_box.append(fb_scroll)
            self._preview_stack.add_named(fb_box, "fallback")
            self._fallback_box = fb_box

            right.append(self._preview_stack)

            paned.set_start_child(left)
            paned.set_end_child(right)
            # initial position 50%
            try:
                paned.set_position(560)
            except Exception:
                pass

            self.append(paned)
            self._paned = paned

            # init webview and first preview
            self._ensure_webview()
            self._update_preview()

        # ── webview ───────────────────────────────────────────────
        def _ensure_webview(self) -> None:
            if not _has_webkit():
                self._preview_stack.set_visible_child_name("fallback")
                return
            if self._webview is not None:
                return
            try:
                from gi.repository import WebKit  # type: ignore

                # ensure version already required in has_webkit
                view = WebKit.WebView()
                view.set_hexpand(True)
                view.set_vexpand(True)
                try:
                    view.set_background_color(None)  # type: ignore
                except Exception:
                    pass
                self._web_scroll.set_child(view)
                self._webview = view
                self._preview_stack.set_visible_child_name("webview")
            except Exception as e:
                log.debug("WebView create failed: %s", e)
                self._preview_stack.set_visible_child_name("fallback")
                self._webview = None

        # ── debounce ──────────────────────────────────────────────
        def _on_text_changed(self, *_a) -> None:
            self._update_line_info()
            self._schedule_preview()

        def _update_line_info(self) -> None:
            try:
                start, end = self.buffer.get_bounds()
                text = self.buffer.get_text(start, end, True)
                lines = text.count("\n") + 1 if text else 0
                chars = len(text)
                self._line_label.set_text(f"{lines} строк · {chars} симв.")
            except Exception:
                pass

        def _schedule_preview(self, force: bool = False) -> None:
            if self._preview_timer is not None:
                try:
                    GLib.source_remove(self._preview_timer)
                except Exception:
                    pass
                self._preview_timer = None
            if force:
                self._preview_timer = None
                self._update_preview()
                return
            self._preview_status.set_text("…")
            self._preview_timer = GLib.timeout_add(DEBOUNCE_MS, self._do_preview)

        def _do_preview(self) -> bool:
            self._preview_timer = None
            self._update_preview()
            return False  # GLib.SOURCE_REMOVE

        def _update_preview(self) -> None:
            if not self._alive:
                return
            try:
                start, end = self.buffer.get_bounds()
                code = self.buffer.get_text(start, end, True)
            except Exception:
                code = ""
            code = _sanitize(code)
            # update fallback text always
            try:
                if hasattr(self, "_fallback_view"):
                    buf = self._fallback_view.get_buffer()
                    buf.set_text(code)
            except Exception:
                pass
            # webview path
            if _has_webkit() and self._webview is not None:
                try:
                    html_doc = _build_html(code, theme=self._theme)
                    # WebKit.WebView.load_html
                    self._webview.load_html(html_doc, "file:///")  # type: ignore[attr-defined]
                    self._preview_status.set_text("✓ обновлено")
                    self._preview_stack.set_visible_child_name("webview")
                    GLib.timeout_add(1200, self._clear_preview_status)
                    return
                except Exception as e:
                    log.debug("preview load_html failed: %s", e)
                    self._preview_status.set_text(f"ошибка: {e}")
            else:
                # fallback visible
                self._preview_stack.set_visible_child_name("fallback")
                self._preview_status.set_text("WebKit нет — текст")
                GLib.timeout_add(1200, self._clear_preview_status)

        def _clear_preview_status(self) -> bool:
            try:
                self._preview_status.set_text("")
            except Exception:
                pass
            return False

        # ── actions ───────────────────────────────────────────────
        def _on_theme_changed(self, drop, _pspec) -> None:
            try:
                idx = int(drop.get_selected())
                if 0 <= idx < len(THEMES):
                    self._theme = THEMES[idx]
                    self._schedule_preview(force=True)
            except Exception:
                pass

        def _on_example(self, _btn) -> None:
            try:
                idx = int(self._ex_drop.get_selected())
                keys = list(EXAMPLES.keys())
                if 0 <= idx < len(keys):
                    code = EXAMPLES[keys[idx]]
                    self.set_code(code)
            except Exception as e:
                log.debug("example failed: %s", e)

        def _on_copy(self, _btn) -> None:
            try:
                start, end = self.buffer.get_bounds()
                code = self.buffer.get_text(start, end, True)
                disp = Gdk.Display.get_default()
                if disp is None:
                    return
                cb = disp.get_clipboard()
                cb.set(code)
                self._status.set_text("скопировано ✓")
                GLib.timeout_add(1500, lambda: self._status.set_text("") or False)
            except Exception as e:
                log.debug("copy failed: %s", e)

        def _on_export(self, _btn) -> None:
            try:
                start, end = self.buffer.get_bounds()
                code = self.buffer.get_text(start, end, True)
            except Exception:
                code = ""
            code = _sanitize(code)
            # FileDialog save
            try:
                dlg = Gtk.FileDialog()
                dlg.set_title("Сохранить Mermaid SVG")
                dlg.set_initial_name("diagram.svg")

                def _on_save(d, res) -> None:
                    try:
                        f = d.save_finish(res)
                        if f is None:
                            return
                        path = Path(f.get_path())
                        # try export via mermaid-cli
                        try:
                            from .mermaid import export_mermaid_to_svg

                            ok = export_mermaid_to_svg(code, path, theme=self._theme)
                        except Exception as e:
                            log.debug("export_mermaid_to_svg failed: %s", e)
                            ok = False
                        if not ok:
                            # fallback: save raw .mmd
                            alt = path.with_suffix(".mmd")
                            alt.write_text(code, encoding="utf-8")
                            self._status.set_text(f"CLI нет — сохранён {alt.name}")
                        else:
                            self._status.set_text(f"SVG сохранён: {path.name}")
                        GLib.timeout_add(2000, lambda: self._status.set_text("") or False)
                        # toast
                        try:
                            win = self.get_root()
                            if win is not None and hasattr(win, "toast_overlay"):
                                t = Adw.Toast.new(self._status.get_text())
                                t.set_timeout(3)
                                win.toast_overlay.add_toast(t)  # type: ignore
                        except Exception:
                            pass
                    except Exception as e:
                        log.debug("save finish failed: %s", e)

                dlg.save(self.get_root(), None, _on_save)  # type: ignore[arg-type]
            except Exception as e:
                log.debug("export dialog failed: %s", e)
                self._status.set_text(f"ошибка: {e}")

        def _on_open_file(self, _btn) -> None:
            try:
                dlg = Gtk.FileDialog()
                dlg.set_title("Открыть mermaid (.mmd, .md)")
                filt_mmd = Gtk.FileFilter()
                filt_mmd.set_name("Mermaid (*.mmd, *.md)")
                filt_mmd.add_pattern("*.mmd")
                filt_mmd.add_pattern("*.md")
                filt_all = Gtk.FileFilter()
                filt_all.set_name("Все файлы")
                filt_all.add_pattern("*")
                flist = Gio.ListStore.new(Gtk.FileFilter)
                flist.append(filt_mmd)
                flist.append(filt_all)
                dlg.set_filters(flist)
                dlg.set_default_filter(filt_mmd)

                def _on_open(d, res) -> None:
                    try:
                        f = d.open_finish(res)
                        if f is None:
                            return
                        path = Path(f.get_path())
                        try:
                            text = path.read_text(encoding="utf-8")
                        except Exception as e:
                            self._status.set_text(f"ошибка чтения: {e}")
                            return
                        # if .md — extract mermaid, else raw
                        if path.suffix.lower() == ".md":
                            code = extract_code_from_text(text)
                        else:
                            code = text
                        self._current_file = path
                        self.set_code(code)
                        self._status.set_text(f"открыт: {path.name}")
                        GLib.timeout_add(2000, lambda: self._status.set_text("") or False)
                    except Exception as e:
                        log.debug("open finish failed: %s", e)

                dlg.open(self.get_root(), None, _on_open)  # type: ignore[arg-type]
            except Exception as e:
                log.debug("open dialog failed: %s", e)

        def _on_save_file(self, _btn) -> None:
            # if we have current file — save directly, else dialog
            if self._current_file is not None and self._current_file.exists():
                try:
                    start, end = self.buffer.get_bounds()
                    code = self.buffer.get_text(start, end, True)
                    self._current_file.write_text(code, encoding="utf-8")
                    self._status.set_text(f"сохранено: {self._current_file.name} ✓")
                    GLib.timeout_add(1500, lambda: self._status.set_text("") or False)
                    return
                except Exception as e:
                    self._status.set_text(f"ошибка: {e}")
                    # fallback to dialog
            # dialog
            try:
                dlg = Gtk.FileDialog()
                dlg.set_title("Сохранить mermaid")
                dlg.set_initial_name(
                    self._current_file.name if self._current_file else "diagram.mmd"
                )

                def _on_save(d, res) -> None:
                    try:
                        f = d.save_finish(res)
                        if f is None:
                            return
                        path = Path(f.get_path())
                        start, end = self.buffer.get_bounds()
                        code = self.buffer.get_text(start, end, True)
                        path.write_text(code, encoding="utf-8")
                        self._current_file = path
                        self._status.set_text(f"сохранено: {path.name} ✓")
                        GLib.timeout_add(1500, lambda: self._status.set_text("") or False)
                    except Exception as e:
                        log.debug("save finish failed: %s", e)

                dlg.save(self.get_root(), None, _on_save)  # type: ignore[arg-type]
            except Exception as e:
                log.debug("save dialog failed: %s", e)

        def _on_clear(self, _btn) -> None:
            self.set_code("")

        def _on_editor_key(self, _ctrl, keyval, _keycode, state) -> bool:
            ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
            if ctrl and keyval in (Gdk.KEY_s, Gdk.KEY_S):
                self._on_save_file(None)
                return True
            if ctrl and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                self._schedule_preview(force=True)
                return True
            return False

        # ── public API ────────────────────────────────────────────
        def set_code(self, code: str) -> None:
            self.buffer.set_text(_sanitize(code))
            # buffer changed will schedule preview; force immediate for set_code
            self._schedule_preview(force=True)

        def get_code(self) -> str:
            try:
                s, e = self.buffer.get_bounds()
                return self.buffer.get_text(s, e, True)
            except Exception:
                return ""

        def load_file(self, path: Path | str) -> bool:
            p = Path(path)
            if not p.is_file():
                return False
            try:
                text = p.read_text(encoding="utf-8")
                code = extract_code_from_text(text) if p.suffix.lower() == ".md" else text
                self._current_file = p
                self.set_code(code)
                return True
            except Exception as e:
                log.debug("load_file failed: %s", e)
                return False

else:  # headless fallback

    class MermaidLiveView:  # type: ignore[no-redef]
        """Заглушка для headless/py_compile без GTK."""

        def __init__(self, *a, **kw) -> None:
            self.settings = kw.get("settings") or (a[0] if a else {})
            self._code = kw.get("initial_code") or EXAMPLES.get("Flowchart", "")

        def set_code(self, code: str) -> None:
            self._code = code

        def get_code(self) -> str:
            return self._code

        def load_file(self, path: Path | str) -> bool:
            return False


# ── Dialog helper (for FilesView integration) ──────────────────────────
def create_dialog(
    parent: object | None, settings: dict | None, initial_code: str | None = None
) -> object | None:
    """Создать Adw.Dialog с MermaidLiveView. Возвращает диалог или None без GTK."""
    if not _GTK_AVAILABLE:
        return None
    try:
        dlg = Adw.Dialog(title="Mermaid Live — split preview")
        dlg.set_content_width(1100)
        dlg.set_content_height(700)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        # header bar inside dialog
        header = Adw.HeaderBar(css_classes=["mermaid-live-header"])
        box.append(header)
        view = MermaidLiveView(settings or {}, initial_code=initial_code)
        box.append(view)
        dlg.set_child(box)
        # present if parent provides root
        if parent is not None and hasattr(parent, "get_root"):
            try:
                root = parent.get_root()
                if root is not None:
                    dlg.present(root)
                    return dlg
            except Exception:
                pass
        try:
            # fallback: try present with parent as widget
            if isinstance(parent, Gtk.Widget):
                dlg.present(parent)
                return dlg
        except Exception:
            pass
        return dlg
    except Exception as e:
        log.debug("create_dialog failed: %s", e)
        return None


def open_mermaid_live(
    parent: Gtk.Widget | None, settings: dict | None, code: str | None = None
) -> object | None:
    """Удобный хелпер для FilesView: открыть live-диалог с кодом."""
    c = _sanitize(code or EXAMPLES["Flowchart"])
    return create_dialog(parent, settings or {}, initial_code=c)


__all__ = [
    "MermaidLiveView",
    "create_dialog",
    "open_mermaid_live",
    "extract_code_from_text",
    "EXAMPLES",
    "DEBOUNCE_MS",
]
