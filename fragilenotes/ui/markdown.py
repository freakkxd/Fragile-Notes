"""Markdown-рендер на токенах AO Glass — режим чтения заметки, как в Obsidian.

Рендер идёт в read-only Gtk.TextView тегами (Pango). Никаких HTML-движков.
"""

from __future__ import annotations

import re

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango  # noqa: E402

from fragilenotes.ui.syntax import highlight as _highlight  # noqa: E402

# ── Mermaid поддержка (WebView + mermaid.js / SVG via mermaid-cli) ──
try:
    from fragilenotes.ui.mermaid import is_mermaid_lang as _is_mermaid_lang  # noqa: E402
except Exception:  # pragma: no cover — fallback если модуль не найден в тестах

    def _is_mermaid_lang(lang: str | None) -> bool:  # type: ignore[no-redef]
        return bool(lang and lang.strip().lower() == "mermaid")


# ── LaTeX поддержка (WebView + KaTeX/MathJax / PNG via matplotlib/pdflatex) ──
try:
    from fragilenotes.ui.latex import has_latex as _has_latex  # noqa: E402
    from fragilenotes.ui.latex import sanitize_latex_code as _sanitize_latex  # noqa: E402
except Exception:  # pragma: no cover — fallback

    def _sanitize_latex(code: str) -> str:  # type: ignore[no-redef]
        return (code or "").strip() or r"\text{— пусто —}"

    def _has_latex(text: str) -> bool:  # type: ignore[no-redef]
        return bool(text and "$" in text and ("$$" in text or "$" in text))


# ── Палитра (AO Glass, канон ao-glass-tokens / ao-glass-note) ──
H1 = "#f0f4fc"  # h1: rgba(240,244,252,0.98) near-white
H2 = "#afbacc"  # h2: text-muted (ПРОПИСНЫЕ)
H3 = "#e4eaf6"  # h3: var(--ao-text) яркий
TEXT = "#d2dae8"  # тело: text-secondary
MUTED = "#afbacc"  # em, thead: text-muted
FAINT = "#8c98ac"  # hr, слабое
ACCENT = "#8ab4ff"  # ссылки: var(--ao-link)
SUCCESS = "#78d296"  # ✓ выполнено
CODE_BG = "#171a20"  # инлайн-код: rgba(255,255,255,0.06) над #06080d
CODE_FG = "#d2dae8"
CODEBLOCK_BG = "#04050a"  # код-блок: rgba(0,0,0,0.35) над #06080d
QUOTE = "#c9d1de"  # текст цитаты (text-normal)
TAG_BG = "#1c1b2a"  # тег-пилюля: rgba(190,165,255,0.12) над фоном
TAG_FG = "#bea5ff"  # violet
MARK_BG = "#1c2538"  # ==выделение==: rgba(130,168,255,0.18) над фоном
SEARCH_BG = "#203055"  # подсветка результата поиска: rgba(130,168,255,0.26) над фоном
TH_BG = "#101520"  # thead: rgba(130,168,255,0.08) над фоном
ZEBRA_BG = "#0b0d12"  # зебра: rgba(255,255,255,0.02) над фоном

_MONO = "JetBrains Mono, Source Code Pro, Fira Code, monospace"

# ── Подсветка синтаксиса (Obsidian one-dark спектр, ao-glass-palette) ──
SYN_KW = "#bea5ff"  # ключевые слова: purple-violet
SYN_STR = "#78d296"  # строки: green
SYN_COM = "#8c98ac"  # комментарии: muted, курсив
SYN_NUM = "#e6af6e"  # числа: amber
SYN_FUNC = "#8ab4ff"  # функции: blue
SYN_KEY = "#78c8dc"  # ключи JSON: cyan
SYN_VAR = "#ffd28c"  # переменные bash: warm

_QUOTE_BG = "#151a26"  # стеклянная подложка цитаты (rgba(focus,0.06) над фоном панели)
_CALLOUT_BG = "#12161f"  # callout-стекло (glass поверх канваса)

# ── Callout'ы (Obsidian, канон tm-daily / ao-glass-note) ──
# тип: (иконка, (r,g,b) акцент-цвета). tm-* — каждодневные секции дневника,
# стандартные — базовые callout'ы Obsidian.
_CALLOUT_FACE = {
    "tm-today": ("✦", (100, 180, 220)),
    "tm-context": ("⚡", (130, 168, 255)),
    "tm-sleep": ("😴", (150, 140, 220)),
    "tm-mood": ("🧠", (190, 165, 255)),
    "tm-body": ("🫀", (220, 145, 165)),
    "tm-day": ("📦", (120, 200, 220)),
    "tm-substances": ("💊", (210, 170, 120)),
    "tm-summary": ("✅", (120, 210, 150)),
    "note": ("📝", (130, 170, 255)),
    "info": ("ℹ️", (130, 168, 255)),
    "tip": ("🔥", (120, 200, 220)),
    "hint": ("💡", (120, 200, 220)),
    "important": ("❗", (230, 175, 110)),
    "success": ("✅", (120, 210, 150)),
    "done": ("✓", (120, 210, 150)),
    "question": ("❓", (130, 170, 255)),
    "help": ("🆘", (130, 170, 255)),
    "warning": ("⚠️", (230, 175, 110)),
    "caution": ("⚠️", (230, 175, 110)),
    "attention": ("⚠️", (230, 175, 110)),
    "danger": ("⚡", (220, 130, 145)),
    "error": ("✖️", (220, 130, 145)),
    "abstract": ("📋", (130, 170, 210)),
    "summary": ("📋", (120, 200, 220)),
    "tldr": ("📋", (120, 200, 220)),
    "example": ("⭐", (190, 165, 255)),
    "quote": ("🕮", (175, 186, 204)),
    "cite": ("🕮", (175, 186, 204)),
    "todo": ("☑", (120, 210, 150)),
    "fail": ("✖️", (220, 130, 145)),
    "bug": ("🐞", (220, 130, 145)),
    "wip": ("🚧", (230, 175, 110)),
    "pinned": ("📌", (130, 168, 255)),
}
_CALLOUT_DEFAULT = ("▸", (155, 165, 190))
_CALLOUT_TYPE = re.compile(r"^\[!([A-Za-z0-9_-]+)(\|[-+])?\]\s*(.*)$")

# ── Блочный парсер ────────────────────────────────────────────
_FENCE = re.compile(r"^```\s*([A-Za-z0-9_+-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HR = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_TABLE = re.compile(r"^\s*\|.*\|\s*$")
_LIST = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TASK = re.compile(r"^(\s*)[-*+]\s+\[([ xX])\]\s+(.*)$")
_LINK = re.compile(r"^\[([^\]]*)\]\(([^)]*)\)$")
_WIKILINK_INNER = re.compile(r"\[\[([^\]|#]+)(?:#([^\]|]*))?(?:\|([^\]]*))?\]\]")
# LaTeX: $$...$$ (display) приоритет над $...$ (inline), без экранирования \$
_LATEX_FENCE = re.compile(r"^\s*\$\$\s*$")
_LATEX_SINGLE = re.compile(r"^\s*\$\$(.+)\$\$\s*$")
_INLINE = re.compile(
    r"((?<!\\)\$\$.+?(?<!\\)\$\$|(?<!\\)\$(?!\$)[^\$\n]+?(?<!\\)\$(?!\$)"
    r"|\[\[[^\]\n]+\]\]|~~.+?~~|\*\*.+?\*\*|__.+?__|==.+?==|\*.+?\*|`[^`\n]+`|\[[^\]]+\]\([^)]*\)"
    r"|(?<![\w-])#[a-zA-Z\u0400-\u04FF][\w\u0400-\u04FF-]*)"
)

# ── Outline (оглавление) ───────────────────────────────────────
_OUTLINE_RE = re.compile(r"^#{1,6}\s+(.+)")


def parse_headings(text: str) -> list[tuple[int, str, int]]:
    """Парсит markdown-заголовки: регулярка r\"^#{1,6}\\s+(.+)\".

    Возвращает list[(level, text, line)] где level 1..6, text — без '#',
    line — номер строки (0-index).
    """
    out: list[tuple[int, str, int]] = []
    for idx, line in enumerate(text.split("\n")):
        m = _OUTLINE_RE.match(line)
        if m:
            # уровень — количество '#' в начале строки
            lvl = 0
            for ch in line:
                if ch == "#":
                    lvl += 1
                else:
                    break
            lvl = max(1, min(6, lvl))
            out.append((lvl, m.group(1).strip(), idx))
    return out


# алиасы для совместимости
parse_outline = parse_headings
get_headings = parse_headings
extract_headings = parse_headings
get_outline = parse_headings


def parse_blocks(text: str) -> list[tuple[str, object]]:
    """Разбивает markdown на блоки (kind, payload).

    kind: h1|h2|h3|h4|code|mermaid|latex|dataview|hr|quote|callout|tasks|list|table|para|meta
    mermaid — блок ```mermaid (рендерится через mermaid.js / mermaid-cli -> SVG)
    latex   — блок $$...$$ (display math, рендерится через KaTeX/MathJax -> PNG/WebView)
    dataview — блок ```dataview (SQL по frontmatter -> таблица, см. core.dataview)
    """
    blocks: list[tuple[str, object]] = []
    lines = text.split("\n")
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        if i == 0 and lines[0].strip() == "---":
            meta_lines: list[str] = []
            j = 1
            while j < n and lines[j].strip() != "---":
                meta_lines.append(lines[j])
                j += 1
            if j < n:
                meta: list[tuple[str, str]] = []
                for ml in meta_lines:
                    ml = ml.strip()
                    if not ml:
                        continue
                    if ":" in ml:
                        key, _, val = ml.partition(":")
                        meta.append((key.strip(), val.strip()))
                    else:
                        meta.append((ml, ""))
                blocks.append(("meta", meta))
                i = j + 1
                continue
        if _FENCE.match(line):
            lang = _FENCE.match(line).group(1)
            i += 1
            code: list[str] = []
            while i < n and not _FENCE.match(lines[i]):
                code.append(lines[i])
                i += 1
            i += 1
            text_code = "\n".join(code)
            if _is_mermaid_lang(lang):
                blocks.append(("mermaid", text_code))
            elif (lang or "").strip().lower() == "dataview":
                blocks.append(("dataview", text_code))
            else:
                blocks.append(("code", (lang, text_code)))
            continue
        # ── LaTeX display block $$...$$ ──
        m_single = _LATEX_SINGLE.match(line)
        if m_single:
            blocks.append(("latex", _sanitize_latex(m_single.group(1))))
            i += 1
            continue
        if _LATEX_FENCE.match(line):
            i += 1
            lat: list[str] = []
            while i < n and not _LATEX_FENCE.match(lines[i]):
                # поддержка закрытия на той же строке: "формула $$"
                if lines[i].strip().endswith("$$") and not lines[i].strip().startswith("$$"):
                    idx_d = lines[i].rfind("$$")
                    lat.append(lines[i][:idx_d])
                    i += 1
                    break
                lat.append(lines[i])
                i += 1
            if i < n and _LATEX_FENCE.match(lines[i]):
                i += 1
            code_latex = "\n".join(lat).strip()
            if not code_latex:
                code_latex = r"\text{— пусто —}"
            blocks.append(("latex", _sanitize_latex(code_latex)))
            continue
        if not line.strip():
            i += 1
            continue
        m = _HEADING.match(line)
        if m:
            level = min(len(m.group(1)), 4)
            blocks.append((f"h{level}", m.group(2)))
            i += 1
            continue
        if _HR.match(line):
            blocks.append(("hr", ""))
            i += 1
            continue
        if _TABLE.match(line):
            table: list[str] = []
            while i < n and _TABLE.match(lines[i]):
                table.append(lines[i].strip())
                i += 1
            blocks.append(("table", "\n".join(table)))
            continue
        if _QUOTE.match(line):
            quote: list[str] = []
            while i < n and _QUOTE.match(lines[i]):
                quote.append(_QUOTE.match(lines[i]).group(1))
                i += 1
            cm = _CALLOUT_TYPE.match(quote[0])
            if cm is not None:
                blocks.append(("callout", (cm.group(1).lower(), cm.group(3), "\n".join(quote[1:]))))
            else:
                blocks.append(("quote", "\n".join(quote)))
            continue
        m = _TASK.match(line)
        if m:
            tasks: list[tuple[bool, str]] = []
            while i < n and (mt := _TASK.match(lines[i])):
                tasks.append((mt.group(2).lower() == "x", mt.group(3)))
                i += 1
            blocks.append(("tasks", tasks))
            continue
        m = _LIST.match(line)
        if m:
            items: list[tuple[str, str, str]] = []
            while i < n and (mi := _LIST.match(lines[i])):
                items.append((mi.group(1), mi.group(2), mi.group(3)))
                i += 1
            blocks.append(("list", items))
            continue
        para: list[str] = []
        while i < n:
            ln = lines[i]
            if (
                not ln.strip()
                or _FENCE.match(ln)
                or _LATEX_FENCE.match(ln)
                or _LATEX_SINGLE.match(ln)
                or _HEADING.match(ln)
                or _HR.match(ln)
                or _TABLE.match(ln)
                or _QUOTE.match(ln)
                or _LIST.match(ln)
                or _TASK.match(ln)
            ):
                break
            para.append(ln)
            i += 1
        blocks.append(("para", "\n".join(para)))
    return blocks


def inline_segments(text: str) -> list[tuple[str, str | None, str | None]]:
    """Разбивает строку на сегменты (текст, тэг|None, цель).

    Третий элемент актуален для wikilink (имя заметки) и обычных ссылок (URL).
    """
    out: list[tuple[str, str | None, str | None]] = []
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("[[") and part.endswith("]]"):
            m = _WIKILINK_INNER.match(part)
            if m:
                target = (m.group(1) or "").strip()
                alias = (m.group(3) or "").strip() or target
                out.append((alias, "wikilink", target))
            else:
                out.append((part, None, None))
        elif part.startswith("~~") and part.endswith("~~") and len(part) > 4:
            out.append((part[2:-2], "strike", None))
        elif part.startswith("**") and part.endswith("**") and len(part) > 4:
            out.append((part[2:-2], "bold", None))
        elif part.startswith("__") and part.endswith("__") and len(part) > 4:
            out.append((part[2:-2], "bold", None))
        elif part.startswith("==") and part.endswith("==") and len(part) > 4:
            out.append((part[2:-2], "mark", None))
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            out.append((part[1:-1], "italic", None))
        elif part.startswith("`") and part.endswith("`") and len(part) >= 2:
            out.append((part[1:-1], "code", None))
        elif part.startswith("$$") and part.endswith("$$") and len(part) >= 5:
            # display math $$...$$ — внутри параграфа (inline-display)
            out.append((part[2:-2].strip(), "latex_display", None))
        elif (
            part.startswith("$")
            and part.endswith("$")
            and len(part) >= 3
            and not part.startswith("$$")
        ):
            # inline math $...$ — экранированный \$ не входит из-за _INLINE
            inner = part[1:-1]
            # защита от lone $ с пробелами: требуем непустой и не только пробелы
            if inner.strip():
                out.append((inner.strip(), "latex_inline", None))
            else:
                out.append((part, None, None))
        elif part.startswith("#") and len(part) > 1:
            out.append((part[1:], "tag", None))
        elif part.startswith("["):
            m = _LINK.match(part)
            if m:
                out.append((m.group(1), "link", m.group(2)))
            else:
                out.append((part, None, None))
        else:
            out.append((part, None, None))
    return out


class MarkdownView(Gtk.TextView):
    """Read-only TextView с красивым рендером markdown (режим чтения).

    `set_markdown(text, highlight="")` подсвечивает все вхождения фразы
    и скроллит к первому — так полнотекстовый поиск «показывает» совпадение.
    `on_wikilink(target)` вызывается по клику на [[заметка]] — позволяет
    вьюхам открывать целевой файл в редакторе.
    """

    def __init__(self, markdown: str = "") -> None:
        super().__init__(
            editable=False,
            cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD,
            top_margin=6,
            bottom_margin=6,
            left_margin=10,
            right_margin=10,
            hexpand=True,
            vexpand=True,
            css_classes=["markdown"],
        )
        self._buf = Gtk.TextBuffer()
        self.set_buffer(self._buf)
        self._tags: dict[str, object] = {}
        self._callout_tags: dict[str, object] = {}
        self._wikilinks: list[tuple[int, int, str]] = []
        self._mermaid_widgets: list[
            object
        ] = []  # удержание Gtk.Widget от GC (TextView child anchor)
        self._latex_widgets: list[object] = []  # WebView/PNG для LaTeX
        self.on_wikilink = None
        self._vault_root: str | None = None
        self._vault_settings: dict | None = None
        self._mk_tags()
        self._setup_click()
        if markdown:
            self.set_markdown(markdown)

    def _setup_click(self) -> None:
        gesture = Gtk.GestureClick()
        gesture.connect("released", self._on_clicked)
        self.add_controller(gesture)

    def _on_clicked(self, _gesture, _n_press, x, y) -> None:
        ok, it = self.get_iter_at_location(x, y)
        if not ok:
            return
        offset = it.get_offset()
        for start, end, target in self._wikilinks:
            if start <= offset <= end:
                if self.on_wikilink is not None:
                    self.on_wikilink(target)
                return

    def _mk_tags(self) -> None:
        mk = lambda name, **kw: self._tags.__setitem__(name, self._buf.create_tag(name, **kw))  # noqa: E731
        mk("para", foreground=TEXT, pixels_below_lines=3)
        mk(
            "h1",
            foreground=H1,
            scale=1.5,
            weight=Pango.Weight.SEMIBOLD,
            pixels_above_lines=14,
            pixels_below_lines=4,
        )
        mk(
            "h2",
            foreground=H2,
            scale=0.82,
            weight=Pango.Weight.BOLD,
            pixels_above_lines=18,
            pixels_below_lines=8,
        )
        mk(
            "h3",
            foreground=H3,
            scale=1.11,
            weight=Pango.Weight.SEMIBOLD,
            pixels_above_lines=12,
            pixels_below_lines=2,
        )
        mk("h4", foreground=TEXT, scale=1.0, weight=Pango.Weight.SEMIBOLD, pixels_above_lines=10)
        mk("bold", foreground=H3, weight=Pango.Weight.SEMIBOLD)
        mk("italic", style=Pango.Style.ITALIC, foreground=MUTED)
        mk("strike", strikethrough=True, foreground=MUTED)
        mk("code", font="JetBrains Mono", background=CODE_BG, foreground=CODE_FG, scale=0.85)
        mk(
            "codeblock",
            font="JetBrains Mono",
            background=CODEBLOCK_BG,
            foreground=CODE_FG,
            scale=0.85,
            left_margin=12,
            right_margin=12,
            pixels_above_lines=6,
            pixels_below_lines=6,
        )
        # mermaid — чуть светлее codeblock, акцент синий как у ссылок
        mk(
            "mermaid",
            font="JetBrains Mono",
            background="#0a0f1e",
            foreground="#c9d1de",
            scale=0.85,
            left_margin=12,
            right_margin=12,
            pixels_above_lines=6,
            pixels_below_lines=6,
        )
        mk(
            "mermaid_header",
            foreground=ACCENT,
            weight=Pango.Weight.SEMIBOLD,
            scale=0.85,
            pixels_above_lines=8,
            pixels_below_lines=2,
        )
        # latex — inline и display (KaTeX/MathJax)
        mk(
            "latex_inline",
            font="JetBrains Mono",
            background="#1a1f2e",
            foreground="#e4eaf6",
            scale=0.9,
        )
        mk(
            "latex_display",
            font="JetBrains Mono",
            background="#0a0f1e",
            foreground="#d2dae8",
            scale=0.95,
            left_margin=12,
            right_margin=12,
            pixels_above_lines=8,
            pixels_below_lines=8,
            justification=Gtk.Justification.CENTER,
        )
        mk(
            "latex_block",
            font="JetBrains Mono",
            background="#0a0f1e",
            foreground="#d2dae8",
            scale=0.9,
            left_margin=12,
            right_margin=12,
            pixels_above_lines=8,
            pixels_below_lines=8,
            justification=Gtk.Justification.CENTER,
        )
        mk(
            "latex_header",
            foreground=ACCENT,
            weight=Pango.Weight.SEMIBOLD,
            scale=0.85,
            pixels_above_lines=8,
            pixels_below_lines=2,
        )
        mk("link", foreground=ACCENT)
        mk("wikilink", foreground=ACCENT, underline=Pango.Underline.SINGLE)
        mk("mark", background=MARK_BG)
        mk("search", background=SEARCH_BG, foreground=ACCENT)
        mk("tag", background=TAG_BG, foreground=TAG_FG, scale=0.9)
        mk(
            "quote",
            foreground=QUOTE,
            style=Pango.Style.ITALIC,
            background=_QUOTE_BG,
            pixels_above_lines=6,
            pixels_below_lines=6,
        )
        mk("quotemark", foreground=ACCENT, weight=Pango.Weight.BOLD, background=_QUOTE_BG)
        mk("callout", background=_CALLOUT_BG, pixels_above_lines=6, pixels_below_lines=6)
        mk("task_todo", foreground=MUTED, weight=Pango.Weight.BOLD)
        mk("task_done", foreground=SUCCESS, weight=Pango.Weight.BOLD)
        mk("list", foreground=TEXT)
        mk("hr", foreground=FAINT, scale=0.8)
        mk(
            "meta_head",
            font="JetBrains Mono",
            foreground="rgb(155,165,190)",
            weight=Pango.Weight.BOLD,
            scale=0.8,
            pixels_above_lines=2,
            pixels_below_lines=2,
        )
        mk("meta", font="JetBrains Mono", foreground="rgb(130,140,162)", scale=0.85)
        mk("table", font="JetBrains Mono", foreground=TEXT, scale=0.85)
        mk(
            "th",
            font="JetBrains Mono",
            foreground=MUTED,
            weight=Pango.Weight.SEMIBOLD,
            background=TH_BG,
            scale=0.85,
        )
        mk("zebra", background=ZEBRA_BG)
        mk("syn_kw", foreground=SYN_KW)
        mk("syn_str", foreground=SYN_STR)
        mk("syn_com", foreground=SYN_COM, style=Pango.Style.ITALIC)
        mk("syn_num", foreground=SYN_NUM)
        mk("syn_func", foreground=SYN_FUNC)
        mk("syn_key", foreground=SYN_KEY)
        mk("syn_var", foreground=SYN_VAR)

    def set_markdown(
        self,
        text: str,
        highlight: str = "",
        vault_root: str | None = None,
        settings: dict | None = None,
    ) -> None:
        # очистить предыдущие mermaid/ latex-виджеты (child anchor)
        for _w in getattr(self, "_mermaid_widgets", []):
            try:
                if hasattr(_w, "unparent"):
                    _w.unparent()
            except Exception:
                pass
        self._mermaid_widgets = []
        for _w in getattr(self, "_latex_widgets", []):
            try:
                if hasattr(_w, "unparent"):
                    _w.unparent()
            except Exception:
                pass
        self._latex_widgets = []
        self._buf.set_text("")
        self._wikilinks = []
        self._highlight = (highlight or "").strip().lower()
        # сохранить vault контекст для dataview
        if vault_root is not None:
            try:
                self._vault_root = str(vault_root)
            except Exception:
                pass
        if settings is not None:
            try:
                self._vault_settings = dict(settings)
                if self._vault_root is None and settings.get("vault_root"):
                    self._vault_root = str(settings["vault_root"])
            except Exception:
                pass
        self._render(parse_blocks(text))
        self._apply_highlight()

    def _apply_highlight(self) -> None:
        """Подсвечивает найденную фразу и подводит к первому вхождению."""
        q = self._highlight
        if not q:
            return
        text = self._buf.get_text(self._buf.get_start_iter(), self._buf.get_end_iter(), True)
        first: Gtk.TextIter | None = None
        idx = text.lower().find(q)
        while idx != -1:
            a = self._buf.get_iter_at_offset(idx)
            b = self._buf.get_iter_at_offset(idx + len(q))
            self._buf.apply_tag(self._tags["search"], a, b)
            if first is None:
                first = self._buf.get_iter_at_offset(idx)
            idx = text.lower().find(q, idx + len(q))
        if first is not None:
            mark = self._buf.create_mark(None, first)
            self.scroll_mark_onscreen(mark)
            self._buf.delete_mark(mark)

    def _callout_tag(self, ctype: str) -> str:
        """Акцентный тег для типа callout (создаётся лениво по палитре)."""
        name = f"callout_{ctype}"
        if name in self._callout_tags:
            return name
        icon, (r, g, b) = _CALLOUT_FACE.get(ctype, _CALLOUT_DEFAULT)
        self._callout_tags[name] = self._buf.create_tag(
            name,
            foreground=f"#{r:02x}{g:02x}{b:02x}",
            weight=Pango.Weight.SEMIBOLD,
        )
        return name

    def _put(
        self,
        text: str,
        tag: str | None,
        segs: list[tuple[str, str | None, str | None]] | None = None,
    ) -> None:
        if not text and not segs:
            return
        # Если переданы сегменты, вставляем склейку seg_text (без маркдаун-разметки **, $, $$ и т.п.)
        # иначе — исходный text. Это нужно для корректного рендера inline LaTeX и др.
        if segs is not None:
            block = "".join(t for t, _, _ in segs)
            # если сегменты пустые (например, текст без форматирования) — используем text
            if not block:
                block = text
        else:
            block = text
        if not block:
            return
        end = self._buf.get_end_iter()
        base_off = end.get_offset()
        self._buf.insert(end, block)
        if tag:
            a = self._buf.get_iter_at_offset(base_off)
            b = self._buf.get_iter_at_offset(base_off + len(block))
            self._buf.apply_tag(self._tags[tag], a, b)
        if segs:
            pos = 0
            for seg_text, seg_tag, seg_target in segs:
                if seg_tag:
                    a = self._buf.get_iter_at_offset(base_off + pos)
                    b = self._buf.get_iter_at_offset(base_off + pos + len(seg_text))
                    self._buf.apply_tag(self._tags[seg_tag], a, b)
                if seg_tag == "wikilink" and seg_target:
                    self._wikilinks.append(
                        (base_off + pos, base_off + pos + len(seg_text), seg_target)
                    )
                pos += len(seg_text)

    def _nl(self) -> None:
        self._buf.insert(self._buf.get_end_iter(), "\n")

    def _render_callout(self, payload: object) -> None:
        """Callout: стеклянный блок с акцент-баром слева, заголовком и содержимым."""
        ctype, title, content = payload  # type: ignore[misc]
        accent = self._callout_tag(ctype)
        icon, _rgb = _CALLOUT_FACE.get(ctype, _CALLOUT_DEFAULT)
        title = (title or ctype.replace("tm-", "").capitalize()).strip()
        if not title.startswith(icon):
            title = f"{icon} " + title
        runs: list[tuple[str, str | None, str | None]] = []
        runs.append(("▍", "quotemark", None))
        runs.append((f"  {title}", accent, None))
        runs.append(("\n", None, None))
        body = str(content)
        if body.strip():
            for raw in body.split("\n"):
                if not raw.strip():
                    continue
                mt = _TASK.match(raw)
                if mt:
                    runs.append(("▍", "quotemark", None))
                    if mt.group(2).lower() == "x":
                        runs.append(("☑ ", "task_done", None))
                    else:
                        runs.append(("☐ ", "task_todo", None))
                    runs.extend(inline_segments(mt.group(3)))
                    runs.append(("\n", None, None))
                else:
                    runs.append(("▍", "quotemark", None))
                    runs.append(("  ", None, None))
                    runs.extend(inline_segments(raw.strip()))
                    runs.append(("\n", None, None))
        block = "".join(t for t, _, _ in runs)
        end = self._buf.get_end_iter()
        base_off = end.get_offset()
        self._buf.insert(end, block)
        ca = self._buf.get_iter_at_offset(base_off)
        cb = self._buf.get_iter_at_offset(base_off + len(block))
        self._buf.apply_tag(self._tags["callout"], ca, cb)
        pos = 0
        for text, seg_tag, seg_target in runs:
            if seg_tag:
                sa = self._buf.get_iter_at_offset(base_off + pos)
                sb = self._buf.get_iter_at_offset(base_off + pos + len(text))
                tag = self._callout_tags.get(seg_tag, self._tags.get(seg_tag))
                if tag is not None:
                    self._buf.apply_tag(tag, sa, sb)
                if seg_tag == "wikilink" and seg_target:
                    self._wikilinks.append((base_off + pos, base_off + pos + len(text), seg_target))
            pos += len(text)

    def _render_mermaid(self, code: str) -> None:
        """Рендер mermaid-блока: WebView (mermaid.js) -> SVG (mmdc) -> fallback текст.

        В Gtk.TextView вставляется заголовок + (опционально) Gtk.Widget через
        TextChildAnchor, плюс исходный код с тегом mermaid для копирования.
        Без WebKit/mermaid-cli — просто стилизованный текстовый placeholder.
        """
        raw = (code or "").strip("\n")
        if not raw.strip():
            raw = "graph TD\n    A[Пусто]"
        # заголовок блока
        self._put("🧜 Mermaid", "mermaid_header")
        self._nl()
        # попытка встроить интерактивный виджет (WebView / SVG)
        embedded = False
        try:
            from fragilenotes.ui.mermaid import create_mermaid_widget as _create_mw
            from fragilenotes.ui.mermaid import sanitize_mermaid_code as _san

            raw_san = _san(raw)
            widget = _create_mw(raw_san, height=280, prefer_webkit=True)
            # WebView/SVG widget — только если это Gtk.Widget и TextView поддерживает anchor
            if widget is not None:
                # TextView.add_child_at_anchor — GTK4 (депрекирован но работает)
                # Держим ссылку чтобы не собрал GC
                self._mermaid_widgets.append(widget)
                try:
                    end = self._buf.get_end_iter()
                    anchor = self._buf.create_child_anchor(end)
                    # add_child_at_anchor может отсутствовать в некоторых окружениях — try
                    if hasattr(self, "add_child_at_anchor"):
                        self.add_child_at_anchor(widget, anchor)  # type: ignore[arg-type]
                        embedded = True
                    elif hasattr(self, "add_child"):
                        # fallback — просто добавить как overlay (не идеально)
                        embedded = False
                except Exception:
                    embedded = False
                if embedded:
                    # после виджета — newline для отделения от следующего блока
                    self._nl()
        except Exception:
            embedded = False
        # всегда показываем исходник (для копирования и fallback)
        # если уже встроен WebView — показываем компактно, иначе как основной контент
        if embedded:
            self._put(raw.strip() + "\n", "mermaid")
        else:
            # fallback: текстовый placeholder + код
            self._put(raw.strip() + "\n", "codeblock")
            # подсказка как включить предпросмотр
            self._put(
                "— предпросмотр: нужен WebKitGTK 6.0/4.1 + mermaid.js или mermaid-cli (npm i -g @mermaid-js/mermaid-cli)",
                "meta",
            )
            self._nl()
        # разделитель
        self._nl()

    def _render_latex(self, code: str) -> None:
        """Рендер LaTeX display-блока: WebView (KaTeX/MathJax) -> PNG (matplotlib/pdflatex) -> fallback текст.

        В Gtk.TextView вставляется заголовок + (опционально) Gtk.Widget через
        TextChildAnchor, плюс исходный код с тегом latex для копирования.
        Без WebKit/PNG — просто стилизованный текстовый placeholder.
        """
        raw = _sanitize_latex(code or "")
        if not raw.strip():
            raw = r"\text{— пусто —}"
        self._put("∑ LaTeX", "latex_header")
        self._nl()
        embedded = False
        try:
            from fragilenotes.ui.latex import create_latex_widget as _create_lw

            widget = _create_lw(raw, display=True, height=80, prefer_webkit=True)
            if widget is not None:
                self._latex_widgets.append(widget)
                try:
                    end = self._buf.get_end_iter()
                    anchor = self._buf.create_child_anchor(end)
                    if hasattr(self, "add_child_at_anchor"):
                        self.add_child_at_anchor(widget, anchor)  # type: ignore[arg-type]
                        embedded = True
                    elif hasattr(self, "add_child"):
                        embedded = False
                except Exception:
                    embedded = False
                if embedded:
                    self._nl()
        except Exception:
            embedded = False
        if embedded:
            self._put(raw.strip() + "\n", "latex_block")
        else:
            self._put(raw.strip() + "\n", "latex_block")
            self._put(
                "— предпросмотр: нужен WebKitGTK 6.0/4.1 + KaTeX/MathJax или python3-matplotlib / texlive (pdflatex+dvipng) для PNG",
                "meta",
            )
            self._nl()
        self._nl()

    def _render_dataview(self, sql: str) -> None:
        """Рендер dataview-блока: SQL -> таблица по frontmatter vault."""
        raw = (sql or "").strip()
        if not raw:
            self._put("— dataview: пустой запрос —", "meta")
            self._nl()
            return
        self._put("▸ DATAVIEW", "meta_head")
        self._nl()
        # показать SQL для прозрачности
        self._put(raw.strip() + "\n", "codeblock")
        # выполнить запрос
        try:
            from fragilenotes.core.dataview import execute_query as _dv_exec  # noqa: E402

            vault_root = getattr(self, "_vault_root", None)
            settings = getattr(self, "_vault_settings", None)
            # fallback: попытаться взять из окружения / дефолта
            if vault_root is None and settings is not None:
                try:
                    vault_root = str(settings.get("vault_root") or "")
                except Exception:
                    vault_root = None
            if not vault_root:
                # последний фолбэк — Path.home/desktop (как в dataview)
                vault_root = None
            headers, rows = _dv_exec(raw, vault_root=vault_root, settings=settings)
            if not headers and not rows:
                self._put("— нет данных —", "meta")
                self._nl()
                return
            # рендер таблицы (header + rows) с тегами th / zebra / table
            if headers:
                self._put(" │ ".join(h.upper() for h in headers) + "\n", "th")
            if rows:
                for ri, cells in enumerate(rows):
                    row_text = " │ ".join(str(c) for c in cells)
                    self._put(row_text + "\n", "zebra" if ri % 2 == 0 else "table")
            else:
                self._put("— нет строк —", "meta")
                self._nl()
            # счётчик
            self._put(f"— {len(rows)} строк —", "meta")
            self._nl()
        except Exception as exc:
            self._put(f"Dataview ошибка: {exc}", "meta")
            self._nl()
            self._put(raw.strip() + "\n", "codeblock")
        self._nl()

    def _render(self, blocks: list[tuple[str, object]]) -> None:
        for kind, payload in blocks:
            if kind == "dataview":
                self._render_dataview(str(payload))
                continue
            if kind == "latex":
                self._render_latex(str(payload))
                continue
            if kind == "mermaid":
                self._render_mermaid(str(payload))
                continue
            if kind == "meta":
                if payload:
                    self._put("▸ PROPERTIES", "meta_head")
                    self._nl()
                    for key, val in payload:
                        self._put("  " + (f"{key}: {val}" if val else key), "meta")
                        self._nl()
                    self._nl()
            elif kind == "code":
                if isinstance(payload, tuple):
                    lang, body = payload
                else:
                    lang, body = "", str(payload)
                # legacy: код-блок с lang=mermaid/dataview (если пришёл извне как code)
                if str(lang).strip().lower() == "mermaid":
                    self._render_mermaid(str(body))
                    continue
                if str(lang).strip().lower() == "dataview":
                    self._render_dataview(str(body))
                    continue
                runs = _highlight(str(body).strip("\n"), str(lang))
                if runs and runs[-1][0] and not runs[-1][0].endswith("\n"):
                    runs.append(("\n", None))
                segs = [(t, tg, None) for t, tg in runs]
                self._put("".join(t for t, _, _ in segs), "codeblock", segs)
            elif kind == "hr":
                self._put("— ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─", "hr")
                self._nl()
            elif kind in ("h1", "h2", "h3", "h4"):
                payload = str(payload)
                if kind == "h2":
                    payload = payload.upper()
                self._put(payload, kind, inline_segments(payload))
                self._nl()
            elif kind == "quote":
                lines = str(payload).split("\n")
                runs: list[tuple[str, str | None, str | None]] = []
                for line in lines:
                    runs.append(("▍", "quotemark", None))
                    runs.append((" ", None, None))
                    runs.extend(inline_segments(line))
                    runs.append(("\n", None, None))
                block = "".join(t for t, _, _ in runs)
                end = self._buf.get_end_iter()
                base_off = end.get_offset()
                self._buf.insert(end, block)
                qa = self._buf.get_iter_at_offset(base_off)
                qb = self._buf.get_iter_at_offset(base_off + len(block))
                self._buf.apply_tag(self._tags["quote"], qa, qb)
                pos = 0
                for text, seg_tag, seg_target in runs:
                    if seg_tag:
                        sa = self._buf.get_iter_at_offset(base_off + pos)
                        sb = self._buf.get_iter_at_offset(base_off + pos + len(text))
                        self._buf.apply_tag(self._tags[seg_tag], sa, sb)
                        if seg_tag == "wikilink" and seg_target:
                            self._wikilinks.append(
                                (base_off + pos, base_off + pos + len(text), seg_target)
                            )
                    pos += len(text)
            elif kind == "callout":
                self._render_callout(payload)
            elif kind == "tasks":
                for done, text in payload:  # type: ignore[union-attr]
                    self._put("☑ " if done else "☐ ", "task_done" if done else "task_todo")
                    self._put(text, "para", inline_segments(text))
                    self._nl()
            elif kind == "list":
                for indent, bullet, text in payload:  # type: ignore[union-attr]
                    prefix = "    " * (len(indent) // 2) + "•  "
                    self._put(prefix, "list")
                    self._put(text, "para", inline_segments(text))
                    self._nl()
            elif kind == "table":
                rows: list[list[str]] = []
                for line in str(payload).split("\n"):
                    cells = [c.strip() for c in line.strip().strip("|").split("|")]
                    if all(c and set(c) <= set("-:") for c in cells):
                        continue
                    rows.append(cells)
                for ri, cells in enumerate(rows):
                    row_text = " │ ".join(cells)
                    if ri == 0:
                        self._put(row_text.upper() + "\n", "th")
                    else:
                        self._put(row_text + "\n", "zebra" if ri % 2 == 0 else "table")
            else:  # para
                self._put(str(payload), "para", inline_segments(str(payload)))
                self._nl()
