"""Поддержка LaTeX формул для FragileNotes.

Рендер ``$...$`` (inline) и ``$$...$$`` (display) в markdown preview двумя путями:

1) WebView (WebKitGTK) + KaTeX / MathJax CDN — интерактивный рендер в окне предпросмотра.
2) Экспорт в PNG через matplotlib (mathtext) или LaTeX CLI (pdflatex + dvipng) — кэшируемый, без WebView.

Модуль используется в ``fragilenotes.ui.markdown.MarkdownView``: display-блоки
``$$...$$`` обрабатываются как отдельные блоки, inline ``$...$`` — как inline-сегменты.
Для экспорта markdown -> HTML есть ``markdown_to_html_with_latex``.

API:
  - has_latex(text) -> bool
  - has_inline_latex(text) -> bool
  - has_display_latex(text) -> bool
  - extract_latex_blocks(text) -> list[str]          # $$...$$ (display)
  - extract_inline_latex(text) -> list[str]         # $...$ (без $$)
  - extract_all_latex(text) -> list[tuple[str,str]] # (kind, code) kind="inline"|"display"
  - sanitize_latex_code(code) -> str
  - html_escape_latex(code) -> str
  - build_latex_fragment(code, display=False) -> str
  - build_latex_html(code, display=False, engine="katex") -> str  — полный HTML документ
  - build_latex_document(codes, display=True) -> str — документ с несколькими формулами
  - markdown_to_html_with_latex(text) -> str
  - latex_cli_available() -> bool
  - export_latex_to_png(code, output, display=False) -> bool
  - export_latex_to_png_cached(code, display=False) -> Path | None
  - has_webkit() -> bool
  - create_latex_webview(code, display=False) -> Gtk.Widget | None
  - create_latex_widget(code, display=True) -> Gtk.Widget
  - LatexWidget — Box с автопереключением WebView / PNG / Label
  - get_latex_css() -> str
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

# ── Константы ────────────────────────────────────────────────────────
KATEX_CSS = "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css"
KATEX_JS = "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"
KATEX_AUTO = "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"
KATEX_CDN = KATEX_JS

MATHJAX_CDN = "https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"
MATHJAX_FALLBACK = "https://cdnjs.cloudflare.com/ajax/libs/mathjax/3.2.2/es5/tex-mml-chtml.min.js"

LATEX_ENGINE_KATEX = "katex"
LATEX_ENGINE_MATHJAX = "mathjax"

# Кэш PNG — XDG_CACHE_HOME или /tmp
def _cache_dir() -> Path:
    import os

    base = os.environ.get("XDG_CACHE_HOME", "")
    if base:
        p = Path(base) / "fragilenotes" / "latex"
    else:
        p = Path.home() / ".cache" / "fragilenotes" / "latex"
        if not p.parent.exists():
            p = Path(tempfile.gettempdir()) / "fragilenotes-latex"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        p = Path(tempfile.gettempdir()) / "fragilenotes-latex"
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    return p


# ── Регулярки ────────────────────────────────────────────────────────
# Display: $$ ... $$  (DOTALL, нежадный, минимум 1 символ)
_LATEX_DISPLAY_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
# Inline: $ ... $  — не $$, не экранированный \$, без переноса строки, содержимое без $ и \n
_LATEX_INLINE_RE = re.compile(r"(?<!\$)(?<!\\)\$(?!\$)([^\$\n]+?)(?<!\\)\$(?!\$)(?!\$)")
# Комбинированная для split: $$...$$ приоритет, иначе $...$
_LATEX_SPLIT_RE = re.compile(r"(\$\$.+?\$\$|(?<!\\)\$(?!\$)[^\$\n]+?(?<!\\)\$(?!\$))", re.DOTALL)
# Для блочного парсера: строковые детекторы
_LATEX_BLOCK_FENCE = re.compile(r"^\s*\$\$\s*$")
_LATEX_SINGLE_LINE_DISPLAY = re.compile(r"^\s*\$\$(.+)\$\$\s*$")
# Экранированная защита: \$  — не считать началом/концом формулы
_LATEX_ESCAPED_DOLLAR = re.compile(r"\\\$")


# ── Санитайзеры ──────────────────────────────────────────────────────
def sanitize_latex_code(code: str) -> str:
    """Нормализует код: trim, fallback на пустую формулу."""
    c = (code or "").strip()
    if not c:
        return r"\text{— пусто —}"
    # Удаляем нулевые байты, нормализуем пробелы
    c = c.replace("\x00", "")
    return c


def html_escape_latex(code: str) -> str:
    """Экранирование для вставки в HTML."""
    return html.escape(code, quote=False)


def is_latex_display_delim(text: str) -> bool:
    """True если строка — display-обёртка $$...$$."""
    if not text:
        return False
    t = text.strip()
    return t.startswith("$$") and t.endswith("$$") and len(t) >= 4


def is_latex_inline_delim(text: str) -> bool:
    """True если строка — inline-обёртка $...$ (но не $$)."""
    if not text:
        return False
    t = text.strip()
    return t.startswith("$") and t.endswith("$") and not t.startswith("$$") and len(t) >= 2


# ── Проверки наличия ─────────────────────────────────────────────────
def has_latex(text: str) -> bool:
    """Есть ли в тексте хотя бы одна LaTeX формула ($...$ или $$...$$)."""
    if not text or "$" not in text:
        return False
    if _LATEX_DISPLAY_RE.search(text):
        return True
    if _LATEX_INLINE_RE.search(text):
        return True
    return False


def has_display_latex(text: str) -> bool:
    if not text or "$" not in text:
        return False
    return bool(_LATEX_DISPLAY_RE.search(text))


def has_inline_latex(text: str) -> bool:
    if not text or "$" not in text:
        return False
    # убрать display чтобы не дать ложное срабатывание на $$...$$ как два inline
    stripped = _LATEX_DISPLAY_RE.sub("", text)
    return bool(_LATEX_INLINE_RE.search(stripped))


def extract_latex_blocks(text: str) -> list[str]:
    """Вытащить display-коды $$...$$ (без обёртки)."""
    if not text:
        return []
    return [m.group(1).strip() for m in _LATEX_DISPLAY_RE.finditer(text)]


def extract_inline_latex(text: str) -> list[str]:
    """Вытащить inline-коды $...$ (без $$, без обёртки)."""
    if not text:
        return []
    # Сначала вырезать display блоки, чтобы не парсить их как два inline
    cleaned = _LATEX_DISPLAY_RE.sub(lambda _m: " " * len(_m.group(0)), text)
    return [m.group(1).strip() for m in _LATEX_INLINE_RE.finditer(cleaned)]


def extract_all_latex(text: str) -> list[tuple[str, str]]:
    """Все формулы: list[(kind, code)] kind = 'display'|'inline'."""
    if not text:
        return []
    out: list[tuple[str, str]] = []
    # display first, помечаем позиции
    for m in _LATEX_DISPLAY_RE.finditer(text):
        out.append(("display", m.group(1).strip()))
    # inline без display
    cleaned = _LATEX_DISPLAY_RE.sub(lambda _m: " " * len(_m.group(0)), text)
    for m in _LATEX_INLINE_RE.finditer(cleaned):
        out.append(("inline", m.group(1).strip()))
    return out


# ── HTML генерация (KaTeX / MathJax) ─────────────────────────────────
_KATEX_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<link rel="stylesheet" href="{katex_css}"/>
<style>
  html,body {{ margin:0; padding:8px; background:{bg}; color:#d2dae8; }}
  body {{ display:flex; align-items:center; justify-content:center; min-height:100vh; font-family: "JetBrains Mono", monospace; }}
  .math {{ max-width:100%; overflow-x:auto; }}
  .math.display {{ font-size:1.2em; }}
  .math.inline {{ font-size:1.0em; }}
</style>
<script src="{katex_js}"></script>
<script src="{katex_auto}"></script>
</head>
<body>
<div class="math {cls}">{content}</div>
<script>
  document.addEventListener("DOMContentLoaded", function() {{
    if (window.renderMathInElement) {{
      renderMathInElement(document.body, {{
        delimiters: [
          {{left: "$$", right: "$$", display: true}},
          {{left: "$", right: "$", display: false}},
          {{left: "\\[", right: "\\]", display: true}},
          {{left: "\\(", right: "\\)", display: false}}
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

_MATHJAX_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>html,body{{margin:0;padding:8px;background:{bg};color:#d2dae8}} body{{display:flex;align-items:center;justify-content:center;min-height:100vh}}</style>
<script>
window.MathJax = {{ tex: {{ inlineMath: [['$', '$'], ['\\\\(', '\\\\)']], displayMath: [['$$','$$'], ['\\\\[','\\\\]']], processEscapes:true }}, chtml: {{ scale:1.0 }}, svg: {{ scale:1.0 }} }};
</script>
<script id="MathJax-script" src="{mathjax_cdn}"></script>
</head>
<body>
<div class="math {cls}">{content}</div>
</body>
</html>
"""

_KATEX_FRAGMENT_DISPLAY = '<div class="math display">$$\n{code}\n$$</div>'
_KATEX_FRAGMENT_INLINE = '<span class="math inline">${code}$</span>'


def build_latex_fragment(code: str, display: bool = False) -> str:
    """Фрагмент HTML для вставки в существующий документ (KaTeX/MathJax auto-render).

    display=True  -> $$...$$  (блок)
    display=False -> $...$   (inline)
    """
    c = sanitize_latex_code(code)
    esc = html_escape_latex(c)
    if display:
        return _KATEX_FRAGMENT_DISPLAY.format(code=esc)
    return _KATEX_FRAGMENT_INLINE.format(code=esc)


def build_latex_html(
    code: str,
    display: bool = False,
    engine: str = LATEX_ENGINE_KATEX,
    background: str = "transparent",
    katex_css: str = KATEX_CSS,
    katex_js: str = KATEX_JS,
    katex_auto: str = KATEX_AUTO,
    mathjax_cdn: str = MATHJAX_CDN,
) -> str:
    """Полный HTML-документ с одной формулой — для WebKit WebView.load_html().

    engine: 'katex' (по умолчанию, быстрее) или 'mathjax'.
    """
    c = sanitize_latex_code(code)
    esc = html_escape_latex(c)
    if display:
        content = f"$${esc}$$"
        cls = "display"
    else:
        content = f"${esc}$"
        cls = "inline"
    if engine == LATEX_ENGINE_MATHJAX:
        return _MATHJAX_HTML_TEMPLATE.format(bg=background, mathjax_cdn=mathjax_cdn, content=content, cls=cls)
    return _KATEX_HTML_TEMPLATE.format(
        bg=background,
        katex_css=katex_css,
        katex_js=katex_js,
        katex_auto=katex_auto,
        content=content,
        cls=cls,
    )


def build_latex_document(
    codes: list[str] | str,
    display: bool = True,
    engine: str = LATEX_ENGINE_KATEX,
    background: str = "transparent",
    title: str = "LaTeX",
) -> str:
    """HTML-документ с несколькими формулами."""
    if isinstance(codes, str):
        codes = [codes]
    frags = "\n".join(build_latex_fragment(sanitize_latex_code(c), display=display) for c in codes)
    # Шапка с KaTeX + auto-render
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="{KATEX_CSS}"/>
<style>html,body{{margin:0;padding:16px;background:{background};color:#d2dae8}} .math{{margin:12px 0}}</style>
<script src="{KATEX_JS}"></script>
<script src="{KATEX_AUTO}"></script>
</head>
<body>
{frags}
<script>
document.addEventListener("DOMContentLoaded", function(){{
  if(window.renderMathInElement) renderMathInElement(document.body, {{
    delimiters:[{{left:"$$",right:"$$",display:true}},{{left:"$",right:"$",display:false}}],
    throwOnError:false
  }});
}});
</script>
</body>
</html>
"""


def markdown_to_html_with_latex(text: str, engine: str = LATEX_ENGINE_KATEX) -> str:
    """Заменить ``$...$`` и ``$$...$$`` в markdown на фрагменты для HTML-экспорта.

    Сначала display, затем inline. Код внутри ``` fences игнорируется.
    """
    if not text or "$" not in text:
        return text or ""
    # Чтобы не трогать ```code``` блоки — временно маскируем
    fence_re = re.compile(r"```.*?```", re.DOTALL)
    placeholders: dict[str, str] = {}

    def _mask_fence(m: re.Match) -> str:
        key = f"__LATEX_FENCE_{len(placeholders)}__"
        placeholders[key] = m.group(0)
        return key

    masked = fence_re.sub(_mask_fence, text)
    # display
    masked = _LATEX_DISPLAY_RE.sub(lambda m: build_latex_fragment(m.group(1), display=True), masked)
    # inline — но уже без display
    masked = _LATEX_INLINE_RE.sub(lambda m: build_latex_fragment(m.group(1), display=False), masked)
    # вернуть fences
    for k, v in placeholders.items():
        masked = masked.replace(k, v)
    return masked


# ── Проверка CLI / PNG экспорт ───────────────────────────────────────
def latex_cli_available() -> bool:
    """Есть ли CLI для LaTeX -> PNG (pdflatex/dvipng или matplotlib)."""
    # matplotlib доступен всегда как fallback если установлен
    try:
        import matplotlib  # noqa: F401

        return True
    except Exception:
        pass
    if shutil.which("pdflatex") or shutil.which("latex"):
        return True
    if shutil.which("dvipng"):
        return True
    return False


def _export_via_matplotlib(code: str, output: Path, display: bool = False, dpi: int = 180) -> bool:
    """Рендер через matplotlib mathtext (без внешнего LaTeX)."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        c = sanitize_latex_code(code)
        # matplotlib ожидает $...$ обёртку
        tex = f"${c}$" if not display else f"${c}$"
        # Для display делаем крупнее и центрируем
        fontsize = 16 if display else 12
        # Создаём фигуру с прозрачным фоном (AO Glass #06080d)
        fig = plt.figure(figsize=(6, 1.2 if not display else 2), dpi=dpi)
        fig.patch.set_alpha(0.0)
        # Убираем оси
        ax = fig.add_axes([0, 0, 1, 1])
        ax.axis("off")
        ax.set_facecolor("none")
        # Текст по центру
        ax.text(
            0.5,
            0.5,
            tex,
            ha="center",
            va="center",
            fontsize=fontsize,
            color="#d2dae8",
            wrap=True,
        )
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        fig.savefig(str(output), dpi=dpi, transparent=True, bbox_inches="tight", pad_inches=0.2)
        plt.close(fig)
        return output.exists() and output.stat().st_size > 0
    except Exception as e:
        log.debug("matplotlib latex export failed: %s", e)
        return False


def _export_via_pdflatex(code: str, output: Path, display: bool = False, dpi: int = 180, timeout: int = 20) -> bool:
    """Рендер через pdflatex + dvipng (требует texlive)."""
    pdflatex = shutil.which("pdflatex") or shutil.which("latex")
    dvipng = shutil.which("dvipng")
    if not pdflatex or not dvipng:
        return False
    c = sanitize_latex_code(code)
    # Минимальный .tex
    tex_doc = r"""\documentclass[preview]{standalone}
\usepackage{amsmath,amssymb}
\usepackage{xcolor}
\definecolor{fg}{HTML}{d2dae8}
\begin{document}
\color{fg}
""" + (f"$${c}$$" if display else f"${c}$") + r"""
\end{document}
"""
    tmp_tex: Path | None = None
    tmp_dvi: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tex", delete=False, encoding="utf-8") as f:
            f.write(tex_doc)
            tmp_tex = Path(f.name)
        workdir = tmp_tex.parent
        tmp_dvi = workdir / (tmp_tex.stem + ".dvi")
        # pdflatex
        res = subprocess.run(
            [pdflatex, "-interaction=nonstopmode", "-halt-on-error", str(tmp_tex)],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if res.returncode != 0 or not tmp_dvi.exists():
            log.debug("pdflatex failed: %s", (res.stdout or res.stderr)[:500])
            return False
        # dvipng
        # bg Transparent, fg #d2dae8 ~ rgb 0.827 0.855 0.91
        res2 = subprocess.run(
            [dvipng, "-T", "tight", "-D", str(dpi), "-bg", "Transparent", "-fg", "rgb 0.827 0.855 0.91", str(tmp_dvi), "-o", str(output)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if res2.returncode == 0 and output.exists() and output.stat().st_size > 0:
            return True
        log.debug("dvipng failed: %s", (res2.stdout or res2.stderr)[:500])
        return False
    except Exception as e:
        log.debug("pdflatex export failed: %s", e)
        return False
    finally:
        for p in (tmp_tex, tmp_dvi):
            if p is not None:
                try:
                    # также удалить .aux .log
                    for ext in (".aux", ".log", ".tex"):
                        cand = p.with_suffix(ext) if p.suffix != ext else p
                        cand.unlink(missing_ok=True)
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
        # удалить dvi если остался
        if tmp_dvi is not None:
            try:
                tmp_dvi.unlink(missing_ok=True)
            except Exception:
                pass


def export_latex_to_png(
    code: str,
    output: Path | str,
    display: bool = False,
    dpi: int = 180,
    timeout: int = 20,
) -> bool:
    """Экспорт LaTeX-кода в PNG.

    Пробует: matplotlib (mathtext) -> pdflatex+dvipng. Возвращает True при успехе.
    """
    c = sanitize_latex_code(code)
    out = Path(output)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    # 1) matplotlib — быстрый, без внешних зависимостей
    if _export_via_matplotlib(c, out, display=display, dpi=dpi):
        return True
    # 2) pdflatex + dvipng
    if _export_via_pdflatex(c, out, display=display, dpi=dpi, timeout=timeout):
        return True
    return False


def export_latex_to_png_cached(
    code: str,
    cache_dir: Path | None = None,
    display: bool = False,
    dpi: int = 180,
    timeout: int = 20,
) -> Path | None:
    """Кэшируемый экспорт: хэш кода -> PNG в кэше, возвращает путь или None.

    Если файл уже есть — не запускает рендер.
    """
    c = sanitize_latex_code(code)
    h = hashlib.sha256(f"{c}|{display}|{dpi}".encode()).hexdigest()[:16]
    cdir = cache_dir or _cache_dir()
    try:
        cdir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    out = cdir / f"latex-{'display' if display else 'inline'}-{h}.png"
    if out.exists() and out.stat().st_size > 0:
        return out
    if not latex_cli_available():
        # пробуем matplotlib всё равно (latex_cli_available уже проверил matplotlib)
        pass
    ok = export_latex_to_png(c, out, display=display, dpi=dpi, timeout=timeout)
    if ok and out.exists():
        return out
    try:
        if out.exists() and out.stat().st_size == 0:
            out.unlink(missing_ok=True)
    except Exception:
        pass
    return None


# ── WebKit WebView ───────────────────────────────────────────────────
_webkit_available: bool | None = None
_webkit_version: str | None = None


def has_webkit() -> bool:
    """Есть ли WebKitGTK (WebKit 6.0 / 4.1) для WebView."""
    global _webkit_available, _webkit_version
    if _webkit_available is not None:
        return _webkit_available
    try:
        import gi

        for ver in ("6.0", "4.1", "4.0"):
            try:
                gi.require_version("WebKit", ver)
                from gi.repository import WebKit  # noqa: F401

                _webkit_available = True
                _webkit_version = ver
                log.debug("WebKit %s available (latex)", ver)
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


def create_latex_webview(
    code: str,
    display: bool = False,
    engine: str = LATEX_ENGINE_KATEX,
    height: int = 80,
) -> object | None:
    """Создать WebKit.WebView с отрендеренной формулой, или None если WebKit недоступен."""
    if not has_webkit():
        return None
    try:

        from gi.repository import WebKit

        html_doc = build_latex_html(code, display=display, engine=engine, background="transparent")
        view = WebKit.WebView()
        # Для inline — компактный, для display — выше
        h = height if not display else max(height, 120)
        view.set_size_request(-1, h)
        view.set_hexpand(True)
        view.set_vexpand(False)
        try:
            view.set_background_color(None)  # type: ignore
        except Exception:
            pass
        view.load_html(html_doc, "file:///")
        return view
    except Exception as e:
        log.debug("create_latex_webview failed: %s", e)
        return None


def create_latex_widget(code: str, display: bool = True, height: int = 80, prefer_webkit: bool = True) -> object:
    """Создать Gtk.Widget для предпросмотра LaTeX.

    Приоритет: WebView (KaTeX/MathJax) -> PNG Picture (кэш) -> Label fallback.
    Возвращает Gtk.Widget (Box / WebView / Image / Label).
    """
    try:
        import gi

        gi.require_version("Gtk", "4.0")
    except Exception:
        return None  # type: ignore[return-value]

    from gi.repository import Gtk as _Gtk  # noqa: F811

    code = sanitize_latex_code(code)

    # 1) WebView
    if prefer_webkit and has_webkit():
        wv = create_latex_webview(code, display=display, height=height)
        if wv is not None:
            box = _Gtk.Box(orientation=_Gtk.Orientation.VERTICAL, spacing=4)
            box.set_hexpand(True)
            hdr_text = "∑ LaTeX (WebView + KaTeX)" if not display else "∑ LaTeX display (WebView + KaTeX)"
            hdr = _Gtk.Label(label=hdr_text, halign=_Gtk.Align.START, css_classes=["dim-hint", "latex-header"])
            box.append(hdr)
            sw = _Gtk.ScrolledWindow(hexpand=True, vexpand=False, css_classes=["latex-frame"])
            sw.set_policy(_Gtk.PolicyType.NEVER, _Gtk.PolicyType.NEVER)
            sw.set_min_content_height(height if not display else max(height, 80))
            sw.set_child(wv)
            box.append(sw)
            return box

    # 2) PNG через кэш (matplotlib / pdflatex)
    png_path: Path | None = None
    try:
        h = hashlib.sha256(f"{code}|{display}|180".encode()).hexdigest()[:16]
        cand = _cache_dir() / f"latex-{'display' if display else 'inline'}-{h}.png"
        if cand.exists() and cand.stat().st_size > 0:
            png_path = cand
        else:
            if latex_cli_available():
                png_path = export_latex_to_png_cached(code, display=display)
    except Exception:
        png_path = None

    if png_path is not None and png_path.exists():
        try:
            from gi.repository import Gtk as _Gtk2

            box = _Gtk2.Box(orientation=_Gtk2.Orientation.VERTICAL, spacing=4)
            box.set_hexpand(True)
            hdr = _Gtk2.Label(
                label="∑ LaTeX (PNG via matplotlib/pdflatex)",
                halign=_Gtk2.Align.START,
                css_classes=["dim-hint", "latex-header"],
            )
            box.append(hdr)
            try:
                pic = _Gtk2.Picture.new()
                from gi.repository import Gio

                gfile = Gio.File.new_for_path(str(png_path))
                pic.set_file(gfile)
                pic.set_can_shrink(True)
                pic.set_content_fit(_Gtk2.ContentFit.CONTAIN)
                pic.set_size_request(-1, height if not display else 120)
                sw = _Gtk2.ScrolledWindow(hexpand=True, vexpand=False, css_classes=["latex-frame"])
                sw.set_min_content_height(height if not display else 80)
                sw.set_child(pic)
                box.append(sw)
                return box
            except Exception:
                img = _Gtk2.Image.new_from_file(str(png_path))
                img.set_hexpand(True)
                box.append(img)
                return box
        except Exception as e:
            log.debug("PNG widget failed: %s", e)

    # 3) Fallback — текстовый блок с кодом
    try:
        from gi.repository import Gtk as _Gtk3

        box = _Gtk3.Box(orientation=_Gtk3.Orientation.VERTICAL, spacing=4, css_classes=["latex-fallback"])
        hdr = _Gtk3.Label(
            label="∑ LaTeX — предпросмотр недоступен (нужен WebKit или texlive/matplotlib)",
            halign=_Gtk3.Align.START,
            css_classes=["dim-hint", "latex-header"],
        )
        hdr.set_wrap(True)
        box.append(hdr)
        hint = _Gtk3.Label(
            label="Установи WebKitGTK 6.0 или python3-matplotlib / texlive-latex-base dvipng",
            halign=_Gtk3.Align.START,
            css_classes=["dim-hint"],
        )
        hint.set_wrap(True)
        box.append(hint)
        tv = _Gtk3.TextView(editable=False, cursor_visible=False, wrap_mode=_Gtk3.WrapMode.WORD, css_classes=["latex-code"])
        tv.set_size_request(-1, 60)
        buf = tv.get_buffer()
        prefix = "$$" if display else "$"
        suffix = "$$" if display else "$"
        buf.set_text(f"{prefix}{code}{suffix}")
        sw = _Gtk3.ScrolledWindow(hexpand=True, vexpand=False, css_classes=["editor-frame"])
        sw.set_policy(_Gtk3.PolicyType.AUTOMATIC, _Gtk3.PolicyType.NEVER)
        sw.set_child(tv)
        box.append(sw)
        return box
    except Exception:
        from gi.repository import Gtk as _Gtk4

        return _Gtk4.Label(label=code[:500], wrap=True, css_classes=["latex-code"])


# ── Gtk Widget класс ─────────────────────────────────────────────────
try:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk  # type: ignore

    class LatexWidget(Gtk.Box):
        """Box с LaTeX-формулой: WebView -> PNG -> Fallback, с кнопкой экспорта."""

        def __init__(self, code: str, display: bool = True, height: int = 80, engine: str = LATEX_ENGINE_KATEX) -> None:
            super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["latex-widget"], hexpand=True)
            self.code = sanitize_latex_code(code)
            self.display = display
            self.height = height
            self.engine = engine
            self._build()

        def _build(self) -> None:
            inner = create_latex_widget(self.code, display=self.display, height=self.height)
            if inner is not None:
                self.append(inner)
            bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, halign=Gtk.Align.END)
            export_btn = Gtk.Button(
                label="Экспорт PNG",
                icon_name="document-save-symbolic",
                css_classes=["flat", "btn-sm"],
                tooltip_text="Экспортировать формулу в PNG",
            )
            export_btn.connect("clicked", self._on_export)
            bar.append(export_btn)
            copy_btn = Gtk.Button(
                label="Копировать",
                icon_name="edit-copy-symbolic",
                css_classes=["flat", "btn-sm"],
                tooltip_text="Копировать LaTeX-код в буфер",
            )
            copy_btn.connect("clicked", self._on_copy)
            bar.append(copy_btn)
            self.append(bar)

        def _on_export(self, _btn) -> None:
            try:
                from gi.repository import Gtk as _Gtk

                dlg = _Gtk.FileDialog()
                dlg.set_title("Сохранить LaTeX PNG")
                dlg.set_initial_name("formula.png")

                def _on_save(_dlg, res) -> None:
                    try:
                        f = _dlg.save_finish(res)
                        if f is None:
                            return
                        path = Path(f.get_path())
                        ok = export_latex_to_png(self.code, path, display=self.display)
                        if not ok:
                            alt = path.with_suffix(".tex")
                            alt.write_text(self.code, encoding="utf-8")
                    except Exception as e:
                        log.debug("export save failed: %s", e)

                try:
                    dlg.save(self.get_root(), None, _on_save)  # type: ignore[arg-type]
                except Exception:
                    pass
            except Exception as e:
                log.debug("LatexWidget export failed: %s", e)

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

        def update_code(self, code: str, display: bool | None = None) -> None:
            """Обновить формулу (пересоздать внутренний виджет)."""
            while (child := self.get_first_child()) is not None:
                self.remove(child)
            self.code = sanitize_latex_code(code)
            if display is not None:
                self.display = display
            self._build()

except Exception:
    # без GTK — заглушка для импорта без GUI (py_compile, тесты)
    class LatexWidget:  # type: ignore[no-redef]
        def __init__(self, *a, **kw) -> None:
            self.code = kw.get("code", a[0] if a else "")
            self.display = kw.get("display", True)

        def update_code(self, code: str, display: bool | None = None) -> None:
            self.code = code
            if display is not None:
                self.display = display


# ── Утилиты для markdown.py ──────────────────────────────────────────
def get_latex_css() -> str:
    """CSS для LaTeX-блоков в GTK (если рендер как TextView placeholder)."""
    return """
.latex-widget { border:1px solid rgba(255,255,255,0.07); border-radius:12px; padding:8px; background:rgba(255,255,255,0.03); }
.latex-header { font-size:0.85em; letter-spacing:0.04em; }
.latex-frame { background: transparent; border-radius:8px; }
.latex-code { font-family: "JetBrains Mono", monospace; font-size:0.9em; }
.latex-fallback { opacity:0.95; }
"""


__all__ = [
    "KATEX_CSS",
    "KATEX_JS",
    "KATEX_AUTO",
    "KATEX_CDN",
    "MATHJAX_CDN",
    "MATHJAX_FALLBACK",
    "LATEX_ENGINE_KATEX",
    "LATEX_ENGINE_MATHJAX",
    "has_latex",
    "has_display_latex",
    "has_inline_latex",
    "extract_latex_blocks",
    "extract_inline_latex",
    "extract_all_latex",
    "sanitize_latex_code",
    "html_escape_latex",
    "is_latex_display_delim",
    "is_latex_inline_delim",
    "build_latex_fragment",
    "build_latex_html",
    "build_latex_document",
    "markdown_to_html_with_latex",
    "latex_cli_available",
    "export_latex_to_png",
    "export_latex_to_png_cached",
    "has_webkit",
    "get_webkit_version",
    "create_latex_webview",
    "create_latex_widget",
    "LatexWidget",
    "get_latex_css",
]
