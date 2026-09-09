"""LaTeX Live Preview — split view для FragileNotes.

Слева — редактор LaTeX-кода, справа — live preview (WebView + KaTeX) с автообновлением
300 мс debounce (GLib.timeout_add) и incremental рендером (хэш-кэш, пропуск
перерисовки неизменённых данных).

Интеграция:
  * Отдельная вкладка ``latex_live`` в FragileWindow (см. app.py VIEWS)
  * Интеграция в FilesView — кнопка «LaTeX Live» открывает диалог с текущим кодом

API:
  - LatexLiveView(settings, initial_code=None) -> Gtk.Box
  - create_dialog(parent, settings, initial_code) -> Adw.Dialog
  - extract_code_from_text(text) -> str (первый LaTeX блок или весь текст)
  - open_latex_live(parent, settings, code) -> dialog

Зависимости: fragilenotes.ui.latex (build_latex_html, has_webkit, sanitize...)
Фоллбэк без WebKit — PNG через matplotlib/pdflatex или TextView, без падения.
"""

from __future__ import annotations

import hashlib
import html
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# ── Константы ──────────────────────────────────────────────────────────
DEBOUNCE_MS = 300

EXAMPLES: dict[str, str] = {
    "Inline": r"Формула Эйлера: $e^{i\pi} + 1 = 0$ — красиво и просто.",
    "Display": r"$$\int_{-\infty}^{\infty} e^{-x^2}\,dx = \sqrt{\pi}$$",
    "Система": r"$$\begin{cases} x + y = 5 \\ 2x - y = 1 \end{cases}$$",
    "Матрица": r"$$A = \begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix},\quad \det A = -2$$",
    "Дроби": r"$$\frac{1}{1+\frac{1}{1+\frac{1}{2}}} = \frac{3}{5}$$",
    "Сумма": r"$$\sum_{n=1}^{\infty} \frac{1}{n^2} = \frac{\pi^2}{6}$$",
    "Предел": r"$$\lim_{x \to 0} \frac{\sin x}{x} = 1$$",
    "Химия": r"$$\mathrm{H_2O} \quad \ce{H2SO4}$$ — пример с текстом",
    "Греческий": r"$$\alpha + \beta = \gamma \quad \Gamma(z) = \int_0^\infty t^{z-1}e^{-t}dt$$",
    "Пусто": r"$x$",
}

ENGINES = ["katex", "mathjax"]
ENGINE_LABELS = {"katex": "KaTeX", "mathjax": "MathJax"}


def extract_code_from_text(text: str) -> str:
    """Вытащить первый LaTeX-блок из markdown, иначе вернуть текст как есть."""
    if not text:
        return EXAMPLES["Display"]
    try:
        from .latex import extract_all_latex, has_latex  # type: ignore

        if has_latex(text):
            blocks = extract_all_latex(text)
            if blocks:
                # первый display приоритетнее
                for kind, code in blocks:
                    if kind == "display":
                        return f"$${code}$$"
                # иначе inline
                kind, code = blocks[0]
                if kind == "inline":
                    return f"${code}$"
                return code
    except Exception:
        pass
    stripped = text.strip()
    if stripped and "$" in stripped:
        return stripped
    # если текст без $, но не пустой — вернём как display
    if stripped and len(stripped) < 500 and stripped.count("\n") <= 4:
        # короткая строка без $ — обернём в display для демо
        if "$" not in stripped:
            # вернём пример, чтобы preview не был пустым
            return EXAMPLES["Display"]
    return stripped or EXAMPLES["Display"]


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
    log.debug("latex_live: GTK unavailable: %s", _e)


# ── LaTeX helpers (import lazy to avoid circular) ───────────────────
def _sanitize(code: str) -> str:
    try:
        from .latex import sanitize_latex_code  # type: ignore

        return sanitize_latex_code(code)
    except Exception:
        c = (code or "").strip()
        return c if c else r"\text{— пусто —}"


def _escape_for_html(code: str) -> str:
    return html.escape(code, quote=False)


def _build_html(code: str, engine: str = "katex") -> str:
    """HTML-документ с KaTeX/MathJax auto-render и incremental JS-кэшем.

    Incremental: JS хранит _lastHash и пропускает renderMathInElement если
    хэш содержимого не изменился — экономит KaTeX layout на каждом вводе.
    Python-сторона также кэширует хэш и не вызывает load_html при совпадении.
    """
    try:
        from .latex import KATEX_AUTO, KATEX_CSS, KATEX_JS, MATHJAX_CDN  # type: ignore
    except Exception:
        KATEX_CSS = "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css"
        KATEX_JS = "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"
        KATEX_AUTO = "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"
        MATHJAX_CDN = "https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"

    raw = _sanitize(code)
    # Экранируем для вставки в div textContent via JS, но также вставляем
    # как text в #root для SSR-подобного первого кадра.
    esc = _escape_for_html(raw)

    if engine == "mathjax":
        return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
  html,body{{margin:0;padding:16px;background:#06080d;color:#d2dae8;font-family:"JetBrains Mono",monospace}}
  #root{{white-space:pre-wrap;word-break:break-word;line-height:1.6;max-width:100%}}
  .math{{margin:8px 0}}
  a{{color:#8ab4f8}}
</style>
<script>
window.MathJax = {{ tex: {{ inlineMath: [['$', '$'], ['\\\\(', '\\\\)']], displayMath: [['$$','$$'], ['\\\\[','\\\\]']], processEscapes:true }}, chtml: {{ scale:1.0 }} }};
</script>
<script id="MathJax-script" src="{MATHJAX_CDN}"></script>
</head>
<body>
<div id="root">{esc}</div>
<script>
  // incremental cache — MathJax typeset только при изменении
  let _lastHash = "";
  function _hash(s){{ let h=0; for(let i=0;i<s.length;i++) h=((h<<5)-h)+s.charCodeAt(i)|0; return String(h)+":"+s.length; }}
  function _renderIncremental(raw){{
    let h=_hash(raw);
    if(h===_lastHash) return;
    _lastHash=h;
    let el=document.getElementById('root');
    if(!el) return;
    el.textContent = raw;
    if(window.MathJax && window.MathJax.typesetPromise) {{
      MathJax.typesetPromise([el]).catch(()=>{{}});
    }}
  }}
  // initial typeset
  document.addEventListener("DOMContentLoaded", function(){{
    _lastHash = _hash(document.getElementById('root').textContent||"");
    if(window.MathJax && window.MathJax.typesetPromise) MathJax.typesetPromise([document.getElementById('root')]).catch(()=>{{}});
  }});
</script>
</body>
</html>
"""
    # KaTeX — по умолчанию (быстрее, incremental)
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<link rel="stylesheet" href="{KATEX_CSS}"/>
<style>
  html,body{{margin:0;padding:16px;background:#06080d;color:#d2dae8;font-family:"JetBrains Mono",monospace}}
  #root{{white-space:pre-wrap;word-break:break-word;line-height:1.6;max-width:100%}}
  .katex-display{{margin:1em 0}}
  .math{{margin:8px 0}}
</style>
<script src="{KATEX_JS}"></script>
<script src="{KATEX_AUTO}"></script>
</head>
<body>
<div id="root">{esc}</div>
<script>
  let _lastHash = "";
  function _hash(s){{ let h=0; for(let i=0;i<s.length;i++) h=((h<<5)-h)+s.charCodeAt(i)|0; return String(h)+":"+s.length; }}
  function _renderIncremental(raw){{
    let h=_hash(raw);
    if(h===_lastHash) return false;
    _lastHash=h;
    let el=document.getElementById('root');
    if(!el) return false;
    el.textContent = raw;
    if(window.renderMathInElement){{
      renderMathInElement(el, {{
        delimiters: [
          {{left: "$$", right: "$$", display: true}},
          {{left: "$", right: "$", display: false}},
          {{left: "\\\\[", right: "\\\\]", display: true}},
          {{left: "\\\\(", right: "\\\\)", display: false}}
        ],
        throwOnError: false,
        trust: true
      }});
    }}
    return true;
  }}
  document.addEventListener("DOMContentLoaded", function(){{
    let el=document.getElementById('root');
    let raw=el.textContent||"";
    _lastHash = _hash(raw);
    if(window.renderMathInElement){{
      renderMathInElement(el, {{
        delimiters: [
          {{left: "$$", right: "$$", display: true}},
          {{left: "$", right: "$", display: false}},
          {{left: "\\\\[", right: "\\\\]", display: true}},
          {{left: "\\\\(", right: "\\\\)", display: false}}
        ],
        throwOnError: false,
        trust: true
      }});
    }}
  }});
</script>
</body>
</html>
"""


def _has_webkit() -> bool:
    try:
        from .latex import has_webkit  # type: ignore

        return bool(has_webkit())
    except Exception:
        return False


# ── View ───────────────────────────────────────────────────────────────
if _GTK_AVAILABLE:

    class LatexLiveView(Gtk.Box):  # type: ignore[misc]
        """Split view: слева TextView-редактор, справа WebView preview с 300 мс debounce + incremental."""

        def __init__(self, settings: dict | None = None, initial_code: str | None = None) -> None:
            super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True, vexpand=True)
            self.settings = dict(settings or {})
            self._preview_timer: int | None = None
            self._webview: object | None = None
            self._fallback_label: Gtk.Widget | None = None
            self._engine: str = "katex"
            self._alive = True
            self._current_file: Path | None = None
            self._last_hash: str | None = None
            self.connect("destroy", self._on_destroy)
            self._build(initial_code or EXAMPLES["Display"])

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
            from .widgets import view_header  # local import to avoid cycle

            self.append(view_header("∑", "LaTeX Live", "Split view: слева редактор, справа live preview (KaTeX incremental, 300 мс debounce)"))

            toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["toolbar"])
            toolbar.set_margin_start(14)
            toolbar.set_margin_end(14)

            # engine
            toolbar.append(Gtk.Label(label="Движок:", css_classes=["dim-hint"]))
            self._engine_drop = Gtk.DropDown.new_from_strings([ENGINE_LABELS[e] for e in ENGINES])
            try:
                self._engine_drop.set_selected(ENGINES.index(self._engine))
            except ValueError:
                self._engine_drop.set_selected(0)
            self._engine_drop.connect("notify::selected", self._on_engine_changed)
            self._engine_drop.set_tooltip_text("Движок рендера: KaTeX (быстро) / MathJax")
            toolbar.append(self._engine_drop)

            # examples
            toolbar.append(Gtk.Label(label="Пример:", css_classes=["dim-hint"]))
            self._ex_drop = Gtk.DropDown.new_from_strings(list(EXAMPLES.keys()))
            self._ex_drop.set_selected(1)
            toolbar.append(self._ex_drop)
            ex_btn = Gtk.Button(label="Вставить", css_classes=["flat"])
            ex_btn.connect("clicked", self._on_example)
            ex_btn.set_tooltip_text("Вставить выбранный пример в редактор")
            toolbar.append(ex_btn)

            # actions
            copy_btn = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Копировать LaTeX", css_classes=["flat"])
            copy_btn.connect("clicked", self._on_copy)
            toolbar.append(copy_btn)

            export_btn = Gtk.Button(label="Экспорт PNG", icon_name="document-save-symbolic", css_classes=["flat"], tooltip_text="Экспорт в PNG (matplotlib/pdflatex)")
            export_btn.connect("clicked", self._on_export)
            toolbar.append(export_btn)

            open_btn = Gtk.Button(label="Открыть .tex/.md", css_classes=["flat"])
            open_btn.connect("clicked", self._on_open_file)
            toolbar.append(open_btn)

            save_btn = Gtk.Button(label="Сохранить", css_classes=["suggested-action"])
            save_btn.connect("clicked", self._on_save_file)
            toolbar.append(save_btn)
            self._save_btn = save_btn

            # status
            self._status = Gtk.Label(label="", css_classes=["dim-hint"], hexpand=True, halign=Gtk.Align.END, xalign=1, ellipsize=Pango.EllipsizeMode.MIDDLE)
            toolbar.append(self._status)

            self.append(toolbar)

            # Paned split
            paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, vexpand=True, css_classes=["latex-live-paned"])
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
            left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True, vexpand=True)
            left.set_size_request(380, -1)
            left_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            left_hdr.append(Gtk.Label(label="Редактор", css_classes=["dim-hint", "latex-header"], halign=Gtk.Align.START))
            self._line_label = Gtk.Label(label="—", css_classes=["dim-hint"], halign=Gtk.Align.END, hexpand=True, xalign=1)
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
                css_classes=["editor", "latex-editor"],
                top_margin=10,
                bottom_margin=10,
                left_margin=12,
                right_margin=12,
                monospace=True,
            )
            ek = Gtk.EventControllerKey.new()
            ek.connect("key-pressed", self._on_editor_key)
            self.editor.add_controller(ek)

            ed_scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True, css_classes=["editor-frame"])
            ed_scroll.set_child(self.editor)
            left.append(ed_scroll)

            hint = Gtk.Label(
                label="Подсказка: $...$ — inline, $$...$$ — display, \\[ \\] тоже display. Ctrl+Enter — обновить, Ctrl+S — сохранить.",
                css_classes=["dim-hint"],
                halign=Gtk.Align.START,
                wrap=True,
                xalign=0,
            )
            left.append(hint)

            # ── right: preview ────────────────────────────────────
            right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True, vexpand=True)
            right.set_size_request(380, -1)
            right_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            right_hdr.append(Gtk.Label(label="Preview (KaTeX incremental, 300 мс)", css_classes=["dim-hint", "latex-header"], halign=Gtk.Align.START))
            self._preview_status = Gtk.Label(label="", css_classes=["dim-hint"], halign=Gtk.Align.END, hexpand=True, xalign=1)
            right_hdr.append(self._preview_status)
            refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Обновить сейчас", css_classes=["flat"])
            refresh_btn.connect("clicked", lambda *_: self._schedule_preview(force=True))
            right_hdr.append(refresh_btn)
            right.append(right_hdr)

            # preview stack: webview | fallback
            self._preview_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, hexpand=True, vexpand=True)

            # webview container
            self._web_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
            self._web_scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True, css_classes=["editor-frame", "latex-frame"])
            self._web_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            self._web_container.append(self._web_scroll)
            self._preview_stack.add_named(self._web_container, "webview")

            # fallback container
            fb_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True, vexpand=True, css_classes=["latex-fallback"])
            fb_box.set_margin_top(12)
            fb_box.set_margin_start(12)
            fb_box.set_margin_end(12)
            self._fallback_label = Gtk.Label(
                label="WebKit недоступен — установи WebKitGTK 6.0/4.1 для live preview.\nПоказываем PNG (matplotlib) или исходник.",
                wrap=True,
                css_classes=["dim-hint"],
                halign=Gtk.Align.START,
                xalign=0,
            )
            fb_box.append(self._fallback_label)
            self._fallback_view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD, css_classes=["latex-code"])
            self._fallback_view.set_size_request(-1, 160)
            fb_scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True, css_classes=["editor-frame"])
            fb_scroll.set_child(self._fallback_view)
            fb_box.append(fb_scroll)
            # PNG picture placeholder
            self._fallback_image_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            fb_box.append(self._fallback_image_box)
            self._preview_stack.add_named(fb_box, "fallback")
            self._fallback_box = fb_box

            right.append(self._preview_stack)

            paned.set_start_child(left)
            paned.set_end_child(right)
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

        # ── debounce + incremental ────────────────────────────────
        def _on_text_changed(self, *_a) -> None:
            self._update_line_info()
            self._schedule_preview()

        def _update_line_info(self) -> None:
            try:
                start, end = self.buffer.get_bounds()
                text = self.buffer.get_text(start, end, True)
                lines = text.count("\n") + 1 if text else 0
                chars = len(text)
                # подсчёт формул
                try:
                    from .latex import has_latex, extract_all_latex  # type: ignore

                    if has_latex(text):
                        cnt = len(extract_all_latex(text))
                        self._line_label.set_text(f"{lines} строк · {chars} симв. · {cnt} формул")
                    else:
                        self._line_label.set_text(f"{lines} строк · {chars} симв.")
                except Exception:
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
            # --- incremental: hash-кэш (python сторона) ---
            h = hashlib.sha256(f"{code}|{self._engine}".encode()).hexdigest()[:16]
            is_incremental_hit = h == self._last_hash
            if not is_incremental_hit:
                self._last_hash = h

            # update fallback text always
            try:
                if hasattr(self, "_fallback_view"):
                    buf = self._fallback_view.get_buffer()
                    buf.set_text(code)
            except Exception:
                pass

            # webview path — incremental skip if hash совпал и force не нужен
            if _has_webkit() and self._webview is not None:
                if is_incremental_hit and self._preview_stack.get_visible_child_name() == "webview":
                    self._preview_status.set_text("без изменений (incremental ✓)")
                    GLib.timeout_add(1200, self._clear_preview_status)
                    return
                try:
                    html_doc = _build_html(code, engine=self._engine)
                    self._webview.load_html(html_doc, "file:///")  # type: ignore[attr-defined]
                    self._preview_status.set_text("✓ обновлено (KaTeX incremental)")
                    self._preview_stack.set_visible_child_name("webview")
                    # fallback image cleanup
                    try:
                        while (c := self._fallback_image_box.get_first_child()) is not None:
                            self._fallback_image_box.remove(c)
                    except Exception:
                        pass
                    GLib.timeout_add(1200, self._clear_preview_status)
                    return
                except Exception as e:
                    log.debug("preview load_html failed: %s", e)
                    self._preview_status.set_text(f"ошибка: {e}")
                    # fallback к PNG/text
                    self._show_fallback_png(code)
            else:
                # no webkit — показать PNG или текст
                self._show_fallback_png(code)
                self._preview_stack.set_visible_child_name("fallback")
                self._preview_status.set_text("WebKit нет — PNG/текст (incremental кэш)" if is_incremental_hit else "WebKit нет — PNG/текст")
                GLib.timeout_add(1200, self._clear_preview_status)

        def _show_fallback_png(self, code: str) -> None:
            """Попытаться показать PNG через latex export, иначе текст уже есть."""
            try:
                while (c := self._fallback_image_box.get_first_child()) is not None:
                    self._fallback_image_box.remove(c)
            except Exception:
                pass
            # пробуем PNG кэш (только если display-подобная формула)
            try:
                from .latex import export_latex_to_png_cached, has_display_latex  # type: ignore

                is_display = has_display_latex(code) if code else False
                # для live: если в тексте есть $$ — рендерим как display, иначе inline-ish но с display=True для читаемости
                display = is_display or ("$$" in code)
                # берём первый блок если много
                from .latex import extract_all_latex  # type: ignore

                blocks = extract_all_latex(code) if code else []
                render_code = blocks[0][1] if blocks else code
                if not render_code.strip():
                    return
                # кэшированный путь
                png = export_latex_to_png_cached(render_code, display=display)
                if png is not None and png.exists():
                    try:
                        pic = Gtk.Picture.new()
                        from gi.repository import Gio  # type: ignore

                        gfile = Gio.File.new_for_path(str(png))
                        pic.set_file(gfile)
                        pic.set_can_shrink(True)
                        pic.set_content_fit(Gtk.ContentFit.CONTAIN)
                        pic.set_size_request(-1, 120)
                        self._fallback_image_box.append(pic)
                    except Exception:
                        try:
                            img = Gtk.Image.new_from_file(str(png))
                            img.set_hexpand(True)
                            self._fallback_image_box.append(img)
                        except Exception:
                            pass
            except Exception as e:
                log.debug("fallback PNG failed: %s", e)

        def _clear_preview_status(self) -> bool:
            try:
                self._preview_status.set_text("")
            except Exception:
                pass
            return False

        # ── actions ───────────────────────────────────────────────
        def _on_engine_changed(self, drop, _pspec) -> None:
            try:
                idx = int(drop.get_selected())
                if 0 <= idx < len(ENGINES):
                    self._engine = ENGINES[idx]
                    # сброс incremental хэша при смене движка
                    self._last_hash = None
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
            # FileDialog save PNG
            try:
                dlg = Gtk.FileDialog()
                dlg.set_title("Сохранить LaTeX PNG")
                dlg.set_initial_name("formula.png")

                def _on_save(d, res) -> None:
                    try:
                        f = d.save_finish(res)
                        if f is None:
                            return
                        path = Path(f.get_path())
                        # определяем display по наличию $$
                        display = "$$" in code
                        # если много формул — берём первую, иначе весь текст
                        try:
                            from .latex import export_latex_to_png, extract_all_latex  # type: ignore

                            blocks = extract_all_latex(code)
                            target = blocks[0][1] if blocks else code
                            ok = export_latex_to_png(target, path, display=display)
                        except Exception as e:
                            log.debug("export failed: %s", e)
                            ok = False
                        if not ok:
                            alt = path.with_suffix(".tex")
                            alt.write_text(code, encoding="utf-8")
                            self._status.set_text(f"PNG нет — сохранён {alt.name}")
                        else:
                            self._status.set_text(f"PNG сохранён: {path.name}")
                        GLib.timeout_add(2000, lambda: self._status.set_text("") or False)
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
                dlg.set_title("Открыть LaTeX (.tex, .md)")
                filt_tex = Gtk.FileFilter()
                filt_tex.set_name("LaTeX (*.tex, *.md)")
                filt_tex.add_pattern("*.tex")
                filt_tex.add_pattern("*.md")
                filt_all = Gtk.FileFilter()
                filt_all.set_name("Все файлы")
                filt_all.add_pattern("*")
                flist = Gio.ListStore.new(Gtk.FileFilter)
                flist.append(filt_tex)
                flist.append(filt_all)
                dlg.set_filters(flist)
                dlg.set_default_filter(filt_tex)

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
            try:
                dlg = Gtk.FileDialog()
                dlg.set_title("Сохранить LaTeX")
                dlg.set_initial_name((self._current_file.name if self._current_file else "formula.tex"))

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

    class LatexLiveView:  # type: ignore[no-redef]
        """Заглушка для headless/py_compile без GTK — сохраняет API."""

        def __init__(self, *a, **kw) -> None:
            self.settings = kw.get("settings") or (a[0] if a else {})
            self._code = kw.get("initial_code") or EXAMPLES.get("Display", "")

        def set_code(self, code: str) -> None:
            self._code = code

        def get_code(self) -> str:
            return self._code

        def load_file(self, path: Path | str) -> bool:
            return False


# ── Dialog helper (for FilesView integration) ──────────────────────────
def create_dialog(parent: object | None, settings: dict | None, initial_code: str | None = None) -> object | None:
    """Создать Adw.Dialog с LatexLiveView. Возвращает диалог или None без GTK."""
    if not _GTK_AVAILABLE:
        return None
    try:
        dlg = Adw.Dialog(title="LaTeX Live — split preview (KaTeX incremental)")
        dlg.set_content_width(1100)
        dlg.set_content_height(700)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        header = Adw.HeaderBar(css_classes=["latex-live-header"])
        box.append(header)
        view = LatexLiveView(settings or {}, initial_code=initial_code)
        box.append(view)
        dlg.set_child(box)
        if parent is not None and hasattr(parent, "get_root"):
            try:
                root = parent.get_root()
                if root is not None:
                    dlg.present(root)
                    return dlg
            except Exception:
                pass
        try:
            if isinstance(parent, Gtk.Widget):
                dlg.present(parent)
                return dlg
        except Exception:
            pass
        return dlg
    except Exception as e:
        log.debug("create_dialog failed: %s", e)
        return None


def open_latex_live(parent: Gtk.Widget | None, settings: dict | None, code: str | None = None) -> object | None:
    """Удобный хелпер для FilesView: открыть live-диалог с кодом."""
    c = _sanitize(code or EXAMPLES["Display"])
    return create_dialog(parent, settings or {}, initial_code=c)


__all__ = ["LatexLiveView", "create_dialog", "open_latex_live", "extract_code_from_text", "EXAMPLES", "DEBOUNCE_MS", "ENGINES"]
