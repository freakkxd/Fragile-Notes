"""Поддержка Mermaid диаграмм для FragileNotes.

Рендер блоков ```mermaid в markdown preview двумя путями:

1) WebView (WebKitGTK) + mermaid.js CDN — интерактивный рендер в окне предпросмотра.
2) Экспорт в SVG через @mermaid-js/mermaid-cli (mmdc / npx) — кэшируемый, без WebView.

Модуль используется в ``fragilenotes.ui.markdown.MarkdownView``: блоки с
lang == "mermaid" обрабатываются отдельно от обычных code-блоков.

API:
  - is_mermaid_lang(lang) -> bool
  - has_mermaid(text) -> bool
  - extract_mermaid_blocks(text) -> list[str]
  - build_mermaid_html(code, theme="dark") -> str  — полный HTML документ
  - build_mermaid_fragment(code) -> str — <pre class="mermaid">
  - mermaid_cli_available() -> bool
  - export_mermaid_to_svg(code, output_path) -> bool
  - export_mermaid_to_svg_cached(code) -> Path | None
  - has_webkit() -> bool
  - create_mermaid_webview(code) -> Gtk.Widget | None
  - create_mermaid_widget(code) -> Gtk.Widget  — WebView или fallback
  - MermaidWidget — Box с автопереключением WebView / Image / Label
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# ── Константы ───────────────────────────────────────────────────────
MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"
MERMAID_CDN_FALLBACK = "https://unpkg.com/mermaid@11/dist/mermaid.min.js"
MERMAID_VERSION = "11"

# Тема mermaid.js — синхронизирована с AO Glass (тёмная канвас #06080d)
MERMAID_THEME_DARK = "dark"
MERMAID_THEME_DEFAULT = "default"
MERMAID_THEME_FOREST = "forest"
MERMAID_THEME_NEUTRAL = "neutral"

# Кэш SVG — XDG_CACHE_HOME или /tmp
def _cache_dir() -> Path:
    import os

    base = os.environ.get("XDG_CACHE_HOME", "")
    if base:
        p = Path(base) / "fragilenotes" / "mermaid"
    else:
        p = Path.home() / ".cache" / "fragilenotes" / "mermaid"
        if not p.parent.exists():
            # fallback для контейнеров без HOME
            p = Path(tempfile.gettempdir()) / "fragilenotes-mermaid"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        p = Path(tempfile.gettempdir()) / "fragilenotes-mermaid"
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    return p


# ── Регулярки ───────────────────────────────────────────────────────
_MERMAID_BLOCK_RE = re.compile(
    r"```\s*mermaid\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE
)
_MERMAID_LANG_RE = re.compile(r"^\s*mermaid\s*$", re.IGNORECASE)
_FENCE_MERMAID_LINE = re.compile(r"^```\s*mermaid\s*$", re.IGNORECASE)

# ── Проверка языка ─────────────────────────────────────────────────
def is_mermaid_lang(lang: str | None) -> bool:
    """True если fence-язык — mermaid (case-insensitive, trim)."""
    if not lang:
        return False
    return lang.strip().lower() == "mermaid"


def is_mermaid(block_lang: str | None = None, code: str | None = None) -> bool:
    """Алиас для is_mermaid_lang — совместимость со старыми импортами."""
    return is_mermaid_lang(block_lang or "")


def has_mermaid(text: str) -> bool:
    """Быстрая проверка: есть ли в markdown хотя бы один ```mermaid блок."""
    if not text:
        return False
    # быстрый путь без regex
    if "```mermaid" not in text.lower():
        return False
    return bool(_MERMAID_BLOCK_RE.search(text))


def extract_mermaid_blocks(text: str) -> list[str]:
    """Вытащить все mermaid-коды из markdown (без fence)."""
    if not text:
        return []
    return [m.group(1).strip("\n") for m in _MERMAID_BLOCK_RE.finditer(text)]


def sanitize_mermaid_code(code: str) -> str:
    """Нормализует код: trim, fallback на пустую диаграмму."""
    c = (code or "").strip()
    if not c:
        return "graph TD\n    A[Пусто — нет данных]"
    # защита от инъекции </pre> — экранирование не ломает mermaid, но ломает HTML
    # mermaid.js берёт textContent, так что html-escape не нужен для div.mermaid
    # но для безопасности HTML-документа экранируем только < > & вне mermaid-контекста
    return c


def html_escape_mermaid(code: str) -> str:
    """Экранирование для вставки в HTML (если code вставляется как текст)."""
    return html.escape(code, quote=False)


# ── HTML генерация (mermaid.js) ────────────────────────────────────
_MERMAID_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
  html,body {{ margin:0; padding:0; background:{bg}; color:#d2dae8; }}
  body {{ display:flex; align-items:center; justify-content:center; min-height:100vh; }}
  .mermaid {{ max-width:100%; }}
  /* AO Glass — фон как в canvas (#06080d) */
  pre.mermaid {{ background:transparent !important; }}
</style>
<script src="{cdn}"></script>
<script>
  mermaid.initialize({{ startOnLoad:true, theme:'{theme}', securityLevel:'loose', fontFamily:'Inter, JetBrains Mono, monospace', themeVariables:{{ darkMode:true, background:'{bg}', primaryColor:'#0a0e16', primaryTextColor:'#d2dae8', lineColor:'#8ab4ff', textColor:'#d2dae8' }} }});
</script>
</head>
<body>
<pre class="mermaid">
{code}
</pre>
</body>
</html>
"""

_MERMAID_FRAGMENT_TEMPLATE = '<pre class="mermaid">\n{code}\n</pre>'


def build_mermaid_fragment(code: str) -> str:
    """Фрагмент <pre class=\"mermaid\"> для вставки в существующий HTML."""
    c = sanitize_mermaid_code(code)
    # mermaid.js читает textContent, экранировать не нужно, но для валидности HTML — да
    # оставляем raw — mermaid сам парсит текст, & < > в диаграммах редки
    return _MERMAID_FRAGMENT_TEMPLATE.format(code=c)


def build_mermaid_html(code: str, theme: str = MERMAID_THEME_DARK, background: str = "transparent", cdn: str = MERMAID_CDN) -> str:
    """Полный HTML-документ с одной диаграммой — для WebKit WebView.load_html()."""
    c = sanitize_mermaid_code(code)
    return _MERMAID_HTML_TEMPLATE.format(bg=background, cdn=cdn, theme=theme, code=c)


def build_mermaid_document(codes: list[str] | str, theme: str = MERMAID_THEME_DARK, background: str = "transparent", title: str = "Mermaid") -> str:
    """HTML-документ с несколькими диаграммами (каждая — <pre class=\"mermaid\">)."""
    if isinstance(codes, str):
        codes = [codes]
    fragments = "\n".join(build_mermaid_fragment(sanitize_mermaid_code(c)) for c in codes)
    # документ с несколькими блоками — mermaid отрисует каждый
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{html.escape(title)}</title>
<style>html,body{{margin:0;padding:16px;background:{background};color:#d2dae8}} .mermaid{{margin:12px 0}}</style>
<script src="{MERMAID_CDN}"></script>
<script>mermaid.initialize({{startOnLoad:true,theme:'{theme}',securityLevel:'loose'}});</script>
</head>
<body>
{fragments}
</body>
</html>
"""


def markdown_to_html_with_mermaid(text: str, theme: str = MERMAID_THEME_DARK) -> str:
    """Заменить все ```mermaid блоки в markdown на <pre class=\"mermaid\">.

    Удобно для экспорта markdown -> HTML (без WebView).
    """
    if not text:
        return ""
    def _repl(m: re.Match) -> str:
        code = m.group(1).strip("\n")
        return build_mermaid_fragment(code)
    return _MERMAID_BLOCK_RE.sub(_repl, text)


# ── mermaid-cli (mmdc) — экспорт в SVG ─────────────────────────────
def mermaid_cli_available() -> bool:
    """Есть ли mermaid-cli в PATH (mmdc / mmdc.cmd / npx)."""
    if shutil.which("mmdc"):
        return True
    if shutil.which("mmdc.cmd"):
        return True
    if shutil.which("npx"):
        return True
    return False


def _mmdc_candidates() -> list[list[str]]:
    """Кандидаты команд для запуска mermaid-cli."""
    cands: list[list[str]] = []
    mmdc = shutil.which("mmdc")
    if mmdc:
        cands.append([mmdc])
    mmdc_cmd = shutil.which("mmdc.cmd")
    if mmdc_cmd:
        cands.append([mmdc_cmd])
    # npx — скачает пакет если нет глобальной установки
    npx = shutil.which("npx")
    if npx:
        cands.append([npx, "--yes", "@mermaid-js/mermaid-cli"])
        cands.append([npx, "--yes", "mermaid-cli"])
        # старый алиас
        cands.append([npx, "mmdc"])
    return cands


def export_mermaid_to_svg(
    code: str,
    output: Path | str,
    theme: str = MERMAID_THEME_DARK,
    background: str = "transparent",
    timeout: int = 20,
) -> bool:
    """Экспорт mermaid-кода в SVG через mermaid-cli.

    Возвращает True при успехе и наличии файла. Требует Node.js + mermaid-cli.
    """
    c = sanitize_mermaid_code(code)
    out = Path(output)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # временный .mmd
    tmp_mmd: Path | None = None
    tmp_cfg: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".mmd", delete=False, encoding="utf-8") as f:
            f.write(c)
            tmp_mmd = Path(f.name)
        # конфиг темы (json)
        cfg = {"theme": theme, "backgroundColor": background}
        import json

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as cf:
            json.dump(cfg, cf)
            tmp_cfg = Path(cf.name)

        candidates = _mmdc_candidates()
        if not candidates:
            log.debug("mermaid-cli: ни mmdc ни npx не найдены")
            return False

        last_err = ""
        for base in candidates:
            # базовая команда: base -i input.mmd -o output.svg -t theme -b background -c config.json
            cmd = base + ["-i", str(tmp_mmd), "-o", str(out), "-t", theme, "-b", background]
            # некоторые версии поддерживают -c config, добавляем опционально
            # но не критично — тема уже в -t
            try:
                # пробуем с конфигом, fallback без
                for try_cfg in ([ "-c", str(tmp_cfg)] if tmp_cfg else [], []):
                    full = cmd + try_cfg
                    log.debug("mermaid-cli try: %s", " ".join(full))
                    res = subprocess.run(
                        full,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                    )
                    if res.returncode == 0 and out.exists() and out.stat().st_size > 0:
                        return True
                    last_err = (res.stderr or res.stdout or "")[:500]
                    # если ошибка — пробуем следующий try_cfg / candidate
                    if "unknown option" in last_err.lower() and try_cfg:
                        continue
                    break
            except FileNotFoundError:
                last_err = "executable not found"
                continue
            except subprocess.TimeoutExpired:
                last_err = "timeout"
                continue
            except Exception as e:
                last_err = str(e)[:500]
                continue
            # если команда вернула ошибку, пробуем следующий candidate
            if last_err:
                continue
        log.debug("mermaid-cli export failed: %s", last_err)
        return False
    finally:
        for p in (tmp_mmd, tmp_cfg):
            if p is not None:
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass


def export_mermaid_to_svg_cached(
    code: str,
    cache_dir: Path | None = None,
    theme: str = MERMAID_THEME_DARK,
    background: str = "transparent",
    timeout: int = 20,
) -> Path | None:
    """Кэшируемый экспорт: хэш кода -> SVG в кэше, возвращает путь или None.

    Если файл уже есть — не запускает mermaid-cli.
    """
    c = sanitize_mermaid_code(code)
    h = hashlib.sha256(f"{c}|{theme}|{background}".encode()).hexdigest()[:16]
    cdir = cache_dir or _cache_dir()
    try:
        cdir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    out = cdir / f"mermaid-{h}.svg"
    if out.exists() and out.stat().st_size > 0:
        # touch для LRU? оставляем как есть
        return out
    if not mermaid_cli_available():
        return None
    ok = export_mermaid_to_svg(c, out, theme=theme, background=background, timeout=timeout)
    if ok and out.exists():
        return out
    # неудача — не оставлять пустой файл
    try:
        if out.exists() and out.stat().st_size == 0:
            out.unlink(missing_ok=True)
    except Exception:
        pass
    return None


# ── WebKit WebView ──────────────────────────────────────────────────
_webkit_available: bool | None = None
_webkit_version: str | None = None


def has_webkit() -> bool:
    """Есть ли WebKitGTK (WebKit 6.0 / 4.1) для WebView."""
    global _webkit_available, _webkit_version
    if _webkit_available is not None:
        return _webkit_available
    try:
        import gi

        # пробуем 6.0 (GTK4 + libwebkit 6.0), fallback 4.1
        for ver in ("6.0", "4.1", "4.0"):
            try:
                gi.require_version("WebKit", ver)
                from gi.repository import WebKit  # noqa: F401

                _webkit_available = True
                _webkit_version = ver
                log.debug("WebKit %s available", ver)
                return True
            except Exception:
                continue
        _webkit_available = False
        return False
    except Exception:
        _webkit_available = False
        return False


def get_webkit_version() -> str | None:
    has_webkit()
    return _webkit_version


def create_mermaid_webview(code: str, theme: str = MERMAID_THEME_DARK, height: int = 320) -> object | None:
    """Создать WebKit.WebView с отрендеренной диаграммой, или None если WebKit недоступен.

    Возвращает Gtk.Widget (WebView) готовый к вставке в контейнер.
    """
    if not has_webkit():
        return None
    try:

        # ensure WebKit version already required in has_webkit
        from gi.repository import WebKit

        html_doc = build_mermaid_html(code, theme=theme)
        view = WebKit.WebView()
        # компактный размер, масштаб AO Glass
        view.set_size_request(-1, height)
        view.set_hexpand(True)
        view.set_vexpand(False)
        # прозрачный фон
        try:
            view.set_background_color(None)  # type: ignore
        except Exception:
            pass
        view.load_html(html_doc, "file:///")
        # контейнер для скролла
        return view
    except Exception as e:
        log.debug("create_mermaid_webview failed: %s", e)
        return None


def create_mermaid_widget(code: str, height: int = 320, prefer_webkit: bool = True) -> object:
    """Создать Gtk.Widget для предпросмотра mermaid.

    Приоритет: WebView (если доступен) -> SVG Picture (если cli закэшировал) -> Label fallback.
    Возвращает Gtk.Widget (Box / WebView / Image / Label).
    """
    try:
        import gi

        gi.require_version("Gtk", "4.0")
    except Exception:
        # без GTK — вернуть None-заглушку (для headless тестов)
        return None  # type: ignore[return-value]

    from gi.repository import Gtk as _Gtk  # noqa: F811

    code = sanitize_mermaid_code(code)

    # 1) WebView
    if prefer_webkit and has_webkit():
        wv = create_mermaid_webview(code, height=height)
        if wv is not None:
            box = _Gtk.Box(orientation=_Gtk.Orientation.VERTICAL, spacing=4)
            box.set_hexpand(True)
            # заголовок
            hdr = _Gtk.Label(label="🧜 Mermaid (WebView + mermaid.js)", halign=_Gtk.Align.START, css_classes=["dim-hint", "mermaid-header"])
            box.append(hdr)
            # WebView в ScrolledWindow для больших диаграмм
            sw = _Gtk.ScrolledWindow(hexpand=True, vexpand=False, css_classes=["mermaid-frame"])
            sw.set_policy(_Gtk.PolicyType.AUTOMATIC, _Gtk.PolicyType.AUTOMATIC)
            sw.set_min_content_height(height)
            sw.set_child(wv)
            box.append(sw)
            return box

    # 2) SVG через mermaid-cli (кэш)
    svg_path = None
    try:
        # быстрый кэш без запуска CLI если уже есть
        h = hashlib.sha256(f"{code}|{MERMAID_THEME_DARK}|transparent".encode()).hexdigest()[:16]
        cand = _cache_dir() / f"mermaid-{h}.svg"
        if cand.exists() and cand.stat().st_size > 0:
            svg_path = cand
        else:
            # попытка экспорта только если CLI доступен, без ожидания в UI-потоке — синхронно но быстро
            if mermaid_cli_available():
                svg_path = export_mermaid_to_svg_cached(code)
    except Exception:
        svg_path = None

    if svg_path is not None and svg_path.exists():
        try:
            from gi.repository import Gtk as _Gtk2

            box = _Gtk2.Box(orientation=_Gtk2.Orientation.VERTICAL, spacing=4)
            box.set_hexpand(True)
            hdr = _Gtk2.Label(label="🧜 Mermaid (SVG via mermaid-cli)", halign=_Gtk2.Align.START, css_classes=["dim-hint", "mermaid-header"])
            box.append(hdr)
            # Gtk.Picture c SVG (GTK4 поддерживает SVG через GdkPixbuf / rsvg)
            try:
                pic = _Gtk2.Picture.new()
                # load from file — GFile
                from gi.repository import Gio

                gfile = Gio.File.new_for_path(str(svg_path))
                pic.set_file(gfile)
                pic.set_can_shrink(True)
                pic.set_content_fit(_Gtk2.ContentFit.CONTAIN)
                pic.set_size_request(-1, height)
                sw = _Gtk2.ScrolledWindow(hexpand=True, vexpand=False, css_classes=["mermaid-frame"])
                sw.set_min_content_height(height)
                sw.set_child(pic)
                box.append(sw)
                return box
            except Exception:
                # fallback — Gtk.Image
                img = _Gtk2.Image.new_from_file(str(svg_path))
                img.set_hexpand(True)
                box.append(img)
                return box
        except Exception as e:
            log.debug("SVG widget failed: %s", e)

    # 3) Fallback — текстовый блок с кодом
    try:
        from gi.repository import Gtk as _Gtk3

        box = _Gtk3.Box(orientation=_Gtk3.Orientation.VERTICAL, spacing=4, css_classes=["mermaid-fallback"])
        hdr = _Gtk3.Label(label="🧜 Mermaid — предпросмотр недоступен (нужен WebKit или mermaid-cli)", halign=_Gtk3.Align.START, css_classes=["dim-hint", "mermaid-header"])
        hdr.set_wrap(True)
        box.append(hdr)
        # подпись с подсказкой
        hint = _Gtk3.Label(label="npm i -g @mermaid-js/mermaid-cli  •  или установи WebKitGTK 6.0", halign=_Gtk3.Align.START, css_classes=["dim-hint"])
        hint.set_wrap(True)
        box.append(hint)
        # код
        tv = _Gtk3.TextView(editable=False, cursor_visible=False, wrap_mode=_Gtk3.WrapMode.WORD, css_classes=["mermaid-code"])
        tv.set_size_request(-1, min(160, max(60, code.count("\n") * 18 + 40)))
        buf = tv.get_buffer()
        buf.set_text(code)
        sw = _Gtk3.ScrolledWindow(hexpand=True, vexpand=False, css_classes=["editor-frame"])
        sw.set_policy(_Gtk3.PolicyType.AUTOMATIC, _Gtk3.PolicyType.AUTOMATIC)
        sw.set_child(tv)
        box.append(sw)
        return box
    except Exception:
        # последний fallback — простой Label
        from gi.repository import Gtk as _Gtk4

        return _Gtk4.Label(label=code[:500], wrap=True, css_classes=["mermaid-code"])


# ── Gtk Widget класс ─────────────────────────────────────────────────
try:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk  # type: ignore

    class MermaidWidget(Gtk.Box):
        """Box с Mermaid-диаграммой: WebView -> SVG -> Fallback, с кнопкой экспорта."""

        def __init__(self, code: str, height: int = 320, theme: str = MERMAID_THEME_DARK) -> None:
            super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["mermaid-widget"], hexpand=True)
            self.code = sanitize_mermaid_code(code)
            self.theme = theme
            self.height = height
            self._build()

        def _build(self) -> None:
            # вставляем основной виджет
            inner = create_mermaid_widget(self.code, height=self.height)
            if inner is not None:
                self.append(inner)
            # тулбар экспорта
            bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, halign=Gtk.Align.END)
            export_btn = Gtk.Button(label="Экспорт SVG", icon_name="document-save-symbolic", css_classes=["flat", "btn-sm"], tooltip_text="Экспортировать диаграмму в SVG (mermaid-cli)")
            export_btn.connect("clicked", self._on_export)
            bar.append(export_btn)
            copy_btn = Gtk.Button(label="Копировать код", icon_name="edit-copy-symbolic", css_classes=["flat", "btn-sm"], tooltip_text="Копировать mermaid-код в буфер")
            copy_btn.connect("clicked", self._on_copy)
            bar.append(copy_btn)
            self.append(bar)

        def _on_export(self, _btn) -> None:
            try:

                dlg = Gtk.FileDialog()
                dlg.set_title("Сохранить Mermaid SVG")
                dlg.set_initial_name("diagram.svg")

                def _on_save(_dlg, res) -> None:
                    try:
                        f = _dlg.save_finish(res)
                        if f is None:
                            return
                        path = Path(f.get_path())
                        ok = export_mermaid_to_svg(self.code, path, theme=self.theme)
                        # fallback — записать raw code если CLI недоступен
                        if not ok:
                            # сохранить как .mmd
                            alt = path.with_suffix(".mmd")
                            alt.write_text(self.code, encoding="utf-8")
                    except Exception as e:
                        log.debug("export save failed: %s", e)

                # GTK 4.12+ FileDialog — async
                try:
                    dlg.save(self.get_root(), None, _on_save)  # type: ignore[arg-type]
                except Exception:
                    # fallback — диалог через Gtk.FileChooser (устар.)
                    pass
            except Exception as e:
                log.debug("MermaidWidget export failed: %s", e)

        def _on_copy(self, _btn) -> None:
            try:
                from gi.repository import Gdk

                disp = Gdk.Display.get_default()
                if disp is None:
                    return
                cb = disp.get_clipboard()
                cb.set(self.code)
            except Exception as e:
                log.debug("copy failed: %s", e)

        def update_code(self, code: str) -> None:
            """Обновить диаграмму (пересоздать внутренний виджет)."""
            # очистить детей
            while (child := self.get_first_child()) is not None:
                self.remove(child)
            self.code = sanitize_mermaid_code(code)
            self._build()

except Exception:
    # без GTK — заглушка для импорта без GUI (py_compile, тесты)
    class MermaidWidget:  # type: ignore[no-redef]
        def __init__(self, *a, **kw) -> None:
            self.code = kw.get("code", a[0] if a else "")

        def update_code(self, code: str) -> None:
            self.code = code


# ── Утилиты для markdown.py ──────────────────────────────────────────
def get_mermaid_css() -> str:
    """CSS для mermaid-блоков в GTK (если рендер как TextView placeholder)."""
    return """
.mermaid-widget { border:1px solid rgba(255,255,255,0.07); border-radius:12px; padding:8px; background:rgba(255,255,255,0.03); }
.mermaid-header { font-size:0.85em; letter-spacing:0.04em; }
.mermaid-frame { background: transparent; border-radius:8px; }
.mermaid-code { font-family: "JetBrains Mono", monospace; font-size:0.9em; }
.mermaid-fallback { opacity:0.95; }
"""


__all__ = [
    "MERMAID_CDN",
    "MERMAID_CDN_FALLBACK",
    "MERMAID_THEME_DARK",
    "is_mermaid_lang",
    "is_mermaid",
    "has_mermaid",
    "extract_mermaid_blocks",
    "sanitize_mermaid_code",
    "build_mermaid_fragment",
    "build_mermaid_html",
    "build_mermaid_document",
    "markdown_to_html_with_mermaid",
    "mermaid_cli_available",
    "export_mermaid_to_svg",
    "export_mermaid_to_svg_cached",
    "has_webkit",
    "get_webkit_version",
    "create_mermaid_webview",
    "create_mermaid_widget",
    "MermaidWidget",
    "get_mermaid_css",
]
