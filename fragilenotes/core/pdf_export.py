"""PDF экспорт для FragileNotes — markdown → PDF с темой.

Поддерживает два движка (приоритет):
1) WeasyPrint — HTML+CSS → PDF (предпочтительно, чистый Python, без внешних бинарей)
2) pdfkit (wkhtmltopdf) — fallback если WeasyPrint недоступен
3) Минимальный встроенный PDF writer — fallback без зависимостей (гарантирует создание файла)

Функция ``export_to_pdf(md_path, pdf_path, theme)`` — публичный API.
``theme`` — ``"dark"`` | ``"light"`` | ``"glass"`` (``"glass"`` == dark AO Glass).

Дизайн тем — AO Glass токены (см. ``fragilenotes/ui/style.py``).
"""

from __future__ import annotations

import html
import re
from pathlib import Path

# ── Темы ──────────────────────────────────────────────────────────

_BASE_CSS = """
@page {
    size: A4;
    margin: 20mm 16mm 20mm 16mm;
    @bottom-center {
        content: counter(page) " / " counter(pages);
        font-family: Inter, sans-serif;
        font-size: 8px;
        color: #8c98ac;
    }
}
* { box-sizing: border-box; }
body {
    font-family: Inter, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.55;
    word-wrap: break-word;
}
h1, h2, h3, h4, h5, h6 { margin: 1em 0 0.4em 0; font-weight: 650; line-height: 1.2; page-break-after: avoid; }
h1 { font-size: 22pt; border-bottom: 1px solid; padding-bottom: 6px; }
h2 { font-size: 16pt; text-transform: uppercase; letter-spacing: 0.04em; }
h3 { font-size: 13pt; }
h4 { font-size: 11pt; }
p { margin: 0.6em 0; orphans: 2; widows: 2; }
a { text-decoration: none; }
code, pre { font-family: 'JetBrains Mono', 'Source Code Pro', 'Fira Code', monospace; font-size: 9pt; }
code { padding: 1px 4px; border-radius: 4px; }
pre { padding: 10px 12px; border-radius: 8px; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; border: 1px solid; page-break-inside: avoid; }
pre code { padding: 0; border: none; background: transparent; }
blockquote { margin: 0.8em 0; padding: 8px 12px 8px 14px; border-left: 3px solid; border-radius: 6px; page-break-inside: avoid; }
blockquote p { margin: 0.3em 0; }
table { border-collapse: collapse; width: 100%; margin: 0.8em 0; font-size: 9.5pt; page-break-inside: auto; }
th, td { border: 1px solid; padding: 6px 8px; text-align: left; }
th { font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; font-size: 8.5pt; }
tr { page-break-inside: avoid; }
hr { border: none; border-top: 1px solid; margin: 1.2em 0; }
ul, ol { margin: 0.6em 0; padding-left: 1.6em; }
li { margin: 0.2em 0; }
img { max-width: 100%; height: auto; }
.cover { text-align: center; margin-bottom: 18px; padding-bottom: 12px; border-bottom: 2px solid; }
.cover h1 { border: none; margin-bottom: 4px; }
.cover .meta { font-size: 8.5pt; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 8pt; font-weight: 600; border: 1px solid; }
"""

_THEMES: dict[str, str] = {
    "dark": """
body { background: #06080d; color: #d2dae8; }
h1 { color: #f0f4fc; border-color: rgba(255,255,255,0.12); }
h2 { color: #afbacc; border-color: rgba(255,255,255,0.07); }
h3, h4 { color: #e4eaf6; }
a { color: #8ab4ff; }
code { background: #171a20; color: #d2dae8; border: 1px solid rgba(255,255,255,0.06); }
pre { background: #04050a; color: #d2dae8; border-color: rgba(255,255,255,0.07); }
blockquote { background: #151a26; color: #c9d1de; border-left-color: #8ab4ff; border: 1px solid rgba(255,255,255,0.06); border-left: 3px solid #8ab4ff; }
th { background: #101520; color: #afbacc; border-color: rgba(255,255,255,0.08); }
td { border-color: rgba(255,255,255,0.08); }
tr:nth-child(even) td { background: #0b0d12; }
hr { border-color: rgba(255,255,255,0.08); }
.cover { border-color: rgba(130,168,255,0.22); }
.cover .meta { color: #8c98ac; }
.tag { background: rgba(190,165,255,0.12); color: #bea5ff; border-color: rgba(190,165,255,0.22); }
""",
    "light": """
body { background: #ffffff; color: #1e232e; }
h1 { color: #0a0e16; border-color: #e2e6ef; }
h2 { color: #4a5568; border-color: #edf0f5; }
h3, h4 { color: #1e232e; }
a { color: #2b5ea3; }
code { background: #f2f4f8; color: #1e232e; border: 1px solid #e2e6ef; }
pre { background: #f7f8fb; color: #1e232e; border-color: #e2e6ef; }
blockquote { background: #f7f8fb; color: #3a4455; border-left-color: #2b5ea3; border: 1px solid #e2e6ef; border-left: 3px solid #2b5ea3; }
th { background: #eef1f7; color: #4a5568; border-color: #dde2ec; }
td { border-color: #dde2ec; }
tr:nth-child(even) td { background: #f7f8fb; }
hr { border-color: #e2e6ef; }
.cover { border-color: #2b5ea3; }
.cover .meta { color: #6b7a90; }
.tag { background: #eef1ff; color: #4a3fbf; border-color: #c9c6ff; }
""",
}

# glass == dark (AO Glass)
_THEMES["glass"] = _THEMES["dark"]

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
_TAG_RE = re.compile(r"(?<![\w/])#([a-zA-Z\u0400-\u04FF][\w\u0400-\u04FF-]*)")


def _normalize_theme(theme: str | None) -> str:
    if not theme:
        return "dark"
    t = str(theme).strip().lower()
    if t in _THEMES:
        return t
    # aliases
    if t in ("dark-glass", "ao-glass", "obsidian"):
        return "dark"
    if t in ("white", "day"):
        return "light"
    return "dark"


def _markdown_to_html(md_text: str) -> str:
    """Конвертирует markdown в HTML.

    Приоритет: ``markdown`` пакет → ``markdown_it`` → наивный fallback.
    Wikilinks ``[[...]]`` и ``#теги`` нормализуются до читаемого текста.
    """
    # Препроцесс: wikilinks → alias или target
    def _wikilink_sub(m: re.Match[str]) -> str:
        target = (m.group(1) or "").strip()
        alias = (m.group(2) or "").strip()
        text = alias or target
        # экранируем для html
        return text

    # Заменяем wikilinks на plain текст до markdown-парсинга, чтобы не ломать разметку
    # Но сохраняем как ссылку-визуально: просто текст
    cleaned = _WIKILINK_RE.sub(lambda m: _wikilink_sub(m), md_text)

    # Попытка 1: python-markdown
    try:
        import markdown as _md  # type: ignore[import-not-found]

        return _md.markdown(
            cleaned,
            extensions=["extra", "codehilite", "tables", "fenced_code", "sane_lists"],
        )
    except Exception:
        pass

    # Попытка 2: markdown-it-py
    try:
        from markdown_it import MarkdownIt as _MI  # type: ignore[import-not-found]

        mi = _MI("commonmark", {"html": False, "linkify": True, "typographer": False})
        # Включаем таблицы если доступно
        try:
            mi.enable("table")
        except Exception:
            pass
        return mi.render(cleaned)
    except Exception:
        pass

    # Fallback: наивный рендер (заголовки, списки, код, параграфы)
    return _naive_markdown_to_html(cleaned)


def _naive_markdown_to_html(text: str) -> str:
    """Минимальный markdown → HTML без зависимостей (для fallback)."""
    lines = text.split("\n")
    out: list[str] = []
    in_code = False
    code_buf: list[str] = []
    in_list = False
    list_tag = "ul"

    def _flush_list() -> None:
        nonlocal in_list
        if in_list:
            out.append(f"</{list_tag}>")
            in_list = False

    def _escape(s: str) -> str:
        return html.escape(s)

    def _inline(s: str) -> str:
        # экранируем, затем применяем inline
        s = html.escape(s)
        # жирный ** ** и __ __
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"__(.+?)__", r"<strong>\1</strong>", s)
        # курсив * *
        s = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", s)
        # зачёркнутый ~~ ~~
        s = re.sub(r"~~(.+?)~~", r"<del>\1</del>", s)
        # выделение == ==
        s = re.sub(r"==(.+?)==", r"<mark>\1</mark>", s)
        # инлайн код ` `
        s = re.sub(r"`([^`]+?)`", r"<code>\1</code>", s)
        # ссылки [text](url)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
        # теги #tag
        s = _TAG_RE.sub(r'<span class="tag">#\1</span>', s)
        return s

    for line in lines:
        stripped = line.strip()
        if line.strip().startswith("```"):
            if not in_code:
                _flush_list()
                in_code = True
                code_buf = []
            else:
                in_code = False
                code_text = "\n".join(code_buf)
                out.append(f"<pre><code>{_escape(code_text)}</code></pre>")
                code_buf = []
            continue
        if in_code:
            code_buf.append(line)
            continue
        if not stripped:
            _flush_list()
            continue
        # заголовки
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            _flush_list()
            level = len(m.group(1))
            title = _inline(m.group(2).strip())
            out.append(f"<h{level}>{title}</h{level}>")
            continue
        # hr
        if re.match(r"^\s*([-*_])\1{2,}\s*$", line):
            _flush_list()
            out.append("<hr/>")
            continue
        # задачи - [x] / - [ ]
        m = re.match(r"^\s*[-*+]\s+\[([ xX])\]\s+(.*)$", line)
        if m:
            _flush_list()
            checked = m.group(1).lower() == "x"
            txt = _inline(m.group(2))
            box = "☑" if checked else "☐"
            out.append(f"<p>{box} {txt}</p>")
            continue
        # списки
        m = re.match(r"^\s*([-*+]|\d+[.)])\s+(.*)$", line)
        if m:
            bullet = m.group(1)
            txt = _inline(m.group(2))
            cur_tag = "ol" if re.match(r"\d+", bullet) else "ul"
            if not in_list or list_tag != cur_tag:
                _flush_list()
                out.append(f"<{cur_tag}>")
                in_list = True
                list_tag = cur_tag
            out.append(f"<li>{txt}</li>")
            continue
        # цитаты >
        if stripped.startswith(">"):
            _flush_list()
            # собираем последовательные строки цитаты (упрощённо — по одной)
            q = stripped[1:].strip()
            out.append(f"<blockquote><p>{_inline(q)}</p></blockquote>")
            continue
        # таблица (упрощённо — без детального парсинга)
        if stripped.startswith("|") and stripped.endswith("|"):
            _flush_list()
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(set(c) <= set("-:") and c for c in cells):
                continue
            is_header = not out or not out[-1].startswith("<table")
            # naive: каждая строка — отдельный table (для простоты)
            # Лучше собрать rows, но для fallback достаточно строки
            row_html = "".join(f"<td>{_inline(c)}</td>" for c in cells)
            out.append(f"<table><tr>{row_html}</tr></table>")
            continue
        # параграф
        _flush_list()
        out.append(f"<p>{_inline(stripped)}</p>")

    _flush_list()
    if in_code and code_buf:
        code_text = "\n".join(code_buf)
        out.append(f"<pre><code>{_escape(code_text)}</code></pre>")

    return "\n".join(out)


def _build_html_document(title: str, html_body: str, theme: str) -> str:
    theme_key = _normalize_theme(theme)
    theme_css = _THEMES.get(theme_key, _THEMES["dark"])
    css = _BASE_CSS + "\n" + theme_css
    safe_title = html.escape(title)
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{safe_title}</title>
<style>
{css}
</style>
</head>
<body>
<div class="cover">
<h1>{safe_title}</h1>
<div class="meta">FragileNotes · PDF экспорт · тема: {html.escape(theme_key)}</div>
</div>
<div class="content">
{html_body}
</div>
</body>
</html>
"""


def _write_with_weasyprint(html_str: str, pdf_path: Path) -> bool:
    try:
        from weasyprint import HTML as _WPHTML  # type: ignore[import-not-found]

        _WPHTML(string=html_str).write_pdf(str(pdf_path))
        return True
    except Exception:
        return False


def _write_with_pdfkit(html_str: str, pdf_path: Path) -> bool:
    try:
        import pdfkit as _pdfkit  # type: ignore[import-not-found]

        # pdfkit требует wkhtmltopdf — пробуем вызвать
        _pdfkit.from_string(html_str, str(pdf_path), options={"encoding": "UTF-8", "enable-local-file-access": ""})
        return pdf_path.exists() and pdf_path.stat().st_size > 0
    except Exception:
        return False


def _pdf_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _write_minimal_pdf(md_text: str, pdf_path: Path, title: str) -> None:
    """Создаёт валидный PDF без внешних зависимостей (Helvetica, без встраивания шрифтов).

    Разбивает текст на страницы по ~45 строк, каждая строка ~90 символов.
    """
    # Подготовим строки: заголовок + markdown как plain text с переносом
    raw_lines: list[str] = []
    raw_lines.append(title)
    raw_lines.append("")
    # Разбиваем md_text на строки с word-wrap ~90
    for para in md_text.split("\n"):
        if not para.strip():
            raw_lines.append("")
            continue
        # word wrap
        words = para.split(" ")
        cur = ""
        for w in words:
            if len(cur) + len(w) + 1 <= 90:
                cur = (cur + " " + w).strip()
            else:
                if cur:
                    raw_lines.append(cur)
                # если слово длиннее 90 — режем
                while len(w) > 90:
                    raw_lines.append(w[:90])
                    w = w[90:]
                cur = w
        if cur:
            raw_lines.append(cur)

    # Пагинация: 45 строк на страницу (с учётом отступов)
    chunk_size = 45
    pages: list[list[str]] = []
    for i in range(0, len(raw_lines) or 1, chunk_size):
        chunk = raw_lines[i : i + chunk_size]
        if chunk:
            pages.append(chunk)
    if not pages:
        pages = [[""]]

    # Построим PDF объекты
    # Объекты: 1 Catalog, 2 Pages, 3 Font
    # Затем для каждой страницы: Page obj, Content obj
    objs: list[bytes] = []

    def _obj(n: int, body: str) -> bytes:
        return f"{n} 0 obj\n{body}\nendobj\n".encode()

    # 1 Catalog
    objs.append(_obj(1, "<< /Type /Catalog /Pages 2 0 R >>"))
    # 2 Pages — Kids заполним после подсчёта
    num_pages = len(pages)
    # номера page объектов: 4,6,8,...
    page_obj_nums = [4 + i * 2 for i in range(num_pages)]
    kids_str = " ".join(f"{n} 0 R" for n in page_obj_nums)
    objs.append(_obj(2, f"<< /Type /Pages /Kids [{kids_str}] /Count {num_pages} >>"))
    # 3 Font Helvetica
    objs.append(_obj(3, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"))

    # Страницы + контенты
    for idx, lines in enumerate(pages):
        page_num = page_obj_nums[idx]
        content_num = page_num + 1
        # content stream
        # BT /F1 10 Tf 50 800 Td ... Tj
        y_start = 800
        line_height = 14
        stream_lines: list[str] = ["BT", "/F1 10 Tf", f"50 {y_start} Td"]
        for li, line in enumerate(lines):
            # обрезаем не-ASCII? WinAnsiEncoding поддерживает latin + кириллица частично нет, но запишем как есть (pdf viewer покажет ? для кириллицы — ок для fallback)
            # Попробуем закодировать latin1 с заменой, иначе escape
            # Для кириллицы Helvetica не подходит, но fallback всё равно создаст файл
            safe = line
            # Удалим непечатаемые и ограничим длину
            safe = safe.replace("\r", "")
            # PDF string в WinAnsi — кириллица будет mojibake, но файл валиден; лучше транслитерировать? оставим как есть, escape
            esc = _pdf_escape(safe)
            # Ограничим 200 символов на строку для Tj
            if len(esc) > 200:
                esc = esc[:200]
            if li == 0:
                stream_lines.append(f"({esc}) Tj")
            else:
                stream_lines.append(f"0 -{line_height} Td ({esc}) Tj")
        # номера страниц внизу
        stream_lines.append(f"0 -{line_height*2} Td")
        stream_lines.append(f"(Page {idx+1}/{num_pages}) Tj")
        stream_lines.append("ET")
        stream_text = "\n".join(stream_lines)
        stream_bytes = stream_text.encode("utf-8", errors="replace")
        # content object
        # page object
        # A4: 595x842, margins 50
        page_obj_body = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_num} 0 R >>"
        )
        content_body = f"<< /Length {len(stream_bytes)} >>\nstream\n{stream_text}\nendstream"
        # Сохраняем в порядке: page obj, content obj (номера идут последовательно)
        # Но objs список должен соответствовать номерам: objs[0]=1, objs[1]=2, objs[2]=3, objs[3]=4, objs[4]=5, ...
        # Поэтому append в порядке возрастания номеров
        objs.append(_obj(page_num, page_obj_body))
        objs.append(_obj(content_num, content_body))

    # Сборка файла с xref
    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    # Вычислим оффсеты
    offsets: list[int] = []
    cur = len(header)
    for o in objs:
        offsets.append(cur)
        cur += len(o)
    xref_offset = cur
    # xref
    xref_parts: list[bytes] = [b"xref\n", f"0 {len(objs)+1}\n".encode("ascii"), b"0000000000 65535 f \n"]
    for off in offsets:
        xref_parts.append(f"{off:010d} 00000 n \n".encode("ascii"))
    # trailer
    trailer = f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    pdf_bytes = header + b"".join(objs) + b"".join(xref_parts) + trailer
    pdf_path.write_bytes(pdf_bytes)


def export_to_pdf(md_path: str | Path, pdf_path: str | Path, theme: str = "dark") -> Path:
    """Экспортировать markdown-файл в PDF с темой.

    Args:
        md_path: Путь к исходному ``.md`` (или ``.md.enc`` — будет ошибка чтения, экспортируй расшифрованный).
        pdf_path: Путь к выходному PDF (родители создадутся).
        theme: ``"dark"`` | ``"light"`` | ``"glass"`` — влияет на CSS.

    Returns:
        Path к созданному PDF.

    Raises:
        FileNotFoundError: если ``md_path`` не существует.
        IsADirectoryError: если ``md_path`` — директория.
        OSError: при ошибках записи PDF.
    """
    md_p = Path(md_path)
    pdf_p = Path(pdf_path)

    if not md_p.exists():
        raise FileNotFoundError(f"markdown файл не найден: {md_p}")
    if md_p.is_dir():
        raise IsADirectoryError(f"ожидался файл, получена директория: {md_p}")

    # Создать родителей для pdf
    try:
        pdf_p.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise OSError(f"не удалось создать директорию для PDF: {exc}") from exc

    theme_key = _normalize_theme(theme)

    # Чтение markdown
    try:
        md_text = md_p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # fallback latin1
        md_text = md_p.read_text(encoding="utf-8", errors="replace")

    title = md_p.stem
    html_body = _markdown_to_html(md_text)
    html_doc = _build_html_document(title, html_body, theme_key)

    # Попытка 1: WeasyPrint
    if _write_with_weasyprint(html_doc, pdf_p):
        return pdf_p

    # Попытка 2: pdfkit
    if _write_with_pdfkit(html_doc, pdf_p):
        return pdf_p

    # Fallback: минимальный PDF
    try:
        _write_minimal_pdf(md_text, pdf_p, title)
    except Exception as exc:
        raise OSError(f"не удалось создать PDF fallback: {exc}") from exc

    if not pdf_p.exists() or pdf_p.stat().st_size == 0:
        raise OSError(f"PDF не создан: {pdf_p}")

    return pdf_p


__all__ = ["export_to_pdf", "_markdown_to_html", "_build_html_document"]
