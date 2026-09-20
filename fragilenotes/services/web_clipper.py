"""Web-clipper: сохранение веб-страниц как markdown в vault/Clippings/.

Без внешних зависимостей (stdlib): ``urllib`` + ``html.parser``.
Если в окружении есть ``readability`` / ``html2text`` — используются
как предпочтительный путь (best-effort, try/import).
"""

from __future__ import annotations

import datetime
import html
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore  # уже в зависимостях
except Exception:  # pragma: no cover
    yaml = None  # type: ignore

from ..config import load_settings

DEFAULT_CLIPPINGS_FOLDER = "Clippings"
DEFAULT_TIMEOUT = 15.0
USER_AGENT = "FragileNotes/1.0 (+web-clipper)"

# ── utils ──────────────────────────────────────────────────────────


def _sanitize_filename(name: str, max_len: int = 120) -> str:
    s = (name or "").strip()
    if not s:
        s = "untitled"
    s = s.replace("/", "_").replace("\\", "_")
    for ch in ('\0', ':', '*', '?', '"', '<', '>', '|', '\n', '\r'):
        s = s.replace(ch, "_")
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    if not s:
        s = "untitled"
    if s.startswith("."):
        s = "_" + s
    return s


def _unique_path(base: Path) -> Path:
    if not base.exists():
        return base
    stem = base.stem
    suffix = base.suffix or ".md"
    parent = base.parent
    for i in range(1, 1000):
        cand = parent / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
    return parent / f"{stem} {int(time.time())}{suffix}"


def _clippings_dir(settings: dict[str, Any] | None) -> Path:
    s = settings or {}
    raw = str(
        s.get("web_clipper_folder")
        or s.get("clipper_folder")
        or s.get("clippings_folder")
        or DEFAULT_CLIPPINGS_FOLDER
    ).strip() or DEFAULT_CLIPPINGS_FOLDER
    vr = Path(str(s.get("vault_root") or Path.home() / "desktop")).expanduser()
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = vr / p
    return p


def _title_from_html(html_text: str) -> str:
    # <title>…</title>
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    if m:
        raw = m.group(1).strip()
        # убрать теги внутри title (редко)
        raw = re.sub(r"<[^>]+>", "", raw)
        raw = html.unescape(raw).strip()
        raw = re.sub(r"\s+", " ", raw)
        if raw:
            return raw
    # fallback <h1>
    m2 = re.search(r"<h1[^>]*>(.*?)</h1>", html_text, re.IGNORECASE | re.DOTALL)
    if m2:
        raw = re.sub(r"<[^>]+>", "", m2.group(1))
        raw = html.unescape(raw).strip()
        raw = re.sub(r"\s+", " ", raw)
        if raw:
            return raw
    return ""


def _detect_charset(headers, html_text: str) -> str:
    # 1. http header
    ctype = ""
    try:
        ctype = headers.get("Content-Type", "") if hasattr(headers, "get") else ""
    except Exception:
        ctype = ""
    m = re.search(r"charset=([^\s;]+)", ctype, re.IGNORECASE)
    if m:
        return m.group(1).strip("\"'").strip()
    # 2. meta charset
    m2 = re.search(r'<meta[^>]+charset=["\']?([^"\';\s>]+)', html_text, re.IGNORECASE)
    if m2:
        return m2.group(1).strip()
    # 3. meta http-equiv
    m3 = re.search(
        r'<meta[^>]+http-equiv=["\']?content-type["\']?[^>]*content=["\'][^"\']*charset=([^"\';\s]+)',
        html_text,
        re.IGNORECASE,
    )
    if m3:
        return m3.group(1).strip()
    return "utf-8"


# ── html -> markdown fallback (stdlib) ─────────────────────────────

_BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "main", "aside"}
_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_SKIP_TAGS = {"script", "style", "noscript", "template"}


class _MarkdownConverter(HTMLParser):
    """Простой конвертер HTML → Markdown (без зависимостей)."""

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(convert_charrefs=False)
        self.base_url = base_url
        self.out: list[str] = []
        self._skip_depth = 0
        self._skip_tag: str | None = None
        self._in_pre = False
        self._in_code = False
        self._list_stack: list[str] = []  # "ul" / "ol"
        self._ol_counters: list[int] = []
        self._href: str | None = None
        self._href_text: list[str] = []
        self._in_a = False
        self._in_title = False
        self._in_head = False

    # helpers
    def _append(self, s: str) -> None:
        self.out.append(s)

    def _is_skipping(self) -> bool:
        return self._skip_depth > 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t == "head":
            self._in_head = True
        if self._in_head and t not in ("title",):
            # head content ignored except title (already extracted separately)
            if t in _SKIP_TAGS:
                self._skip_depth += 1
                if self._skip_tag is None:
                    self._skip_tag = t
            return
        if t in _SKIP_TAGS:
            self._skip_depth += 1
            if self._skip_tag is None:
                self._skip_tag = t
            return
        if self._is_skipping():
            return
        attrd = {k.lower(): (v or "") for k, v in attrs}
        if t == "br":
            self._append("  \n")
        elif t == "hr":
            self._append("\n\n---\n\n")
        elif t in _HEADING_TAGS:
            level = _HEADING_TAGS[t]
            self._append("\n\n" + "#" * level + " ")
        elif t == "p":
            self._append("\n\n")
        elif t == "blockquote":
            self._append("\n\n> ")
        elif t in ("strong", "b"):
            self._append("**")
        elif t in ("em", "i"):
            self._append("*")
        elif t == "code":
            if not self._in_pre:
                self._append("`")
            self._in_code = True
        elif t == "pre":
            self._in_pre = True
            self._append("\n\n```\n")
        elif t == "ul":
            self._list_stack.append("ul")
            self._append("\n")
        elif t == "ol":
            self._list_stack.append("ol")
            try:
                start = int(attrd.get("start", "1"))
            except Exception:
                start = 1
            self._ol_counters.append(start - 1)
            self._append("\n")
        elif t == "li":
            self._append("\n")
            depth = max(0, len(self._list_stack) - 1)
            indent = "  " * depth
            if self._list_stack and self._list_stack[-1] == "ol":
                if self._ol_counters:
                    self._ol_counters[-1] += 1
                    n = self._ol_counters[-1]
                else:
                    n = 1
                self._append(f"{indent}{n}. ")
            else:
                self._append(f"{indent}- ")
        elif t == "a":
            href = attrd.get("href", "").strip()
            if href and self.base_url:
                try:
                    href = urllib.parse.urljoin(self.base_url, href)
                except Exception:
                    pass
            self._href = href or None
            self._href_text = []
            self._in_a = True
        elif t == "img":
            src = attrd.get("src", "").strip()
            alt = attrd.get("alt", "").strip()
            if src and self.base_url:
                try:
                    src = urllib.parse.urljoin(self.base_url, src)
                except Exception:
                    pass
            if src:
                alt = alt or "image"
                # экранируем скобки в alt
                alt = alt.replace("[", "\\[").replace("]", "\\]")
                self._append(f"\n\n![{alt}]({src})\n\n")
        elif t in _BLOCK_TAGS:
            self._append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "head":
            self._in_head = False
            return
        if self._skip_tag is not None:
            if t == self._skip_tag:
                self._skip_depth = max(0, self._skip_depth - 1)
                if self._skip_depth == 0:
                    self._skip_tag = None
            elif t in _SKIP_TAGS and self._skip_depth > 0:
                # nested skip? просто декремент если совпал уровень
                pass
            return
        if self._is_skipping():
            return
        if t in _HEADING_TAGS:
            self._append("\n\n")
        elif t == "p":
            self._append("\n\n")
        elif t == "blockquote":
            self._append("\n\n")
        elif t in ("strong", "b"):
            self._append("**")
        elif t in ("em", "i"):
            self._append("*")
        elif t == "code":
            if not self._in_pre:
                self._append("`")
            self._in_code = False
        elif t == "pre":
            self._in_pre = False
            self._append("\n```\n\n")
        elif t == "ul":
            if self._list_stack and self._list_stack[-1] == "ul":
                self._list_stack.pop()
            self._append("\n")
        elif t == "ol":
            if self._list_stack and self._list_stack[-1] == "ol":
                self._list_stack.pop()
            if self._ol_counters:
                self._ol_counters.pop()
            self._append("\n")
        elif t == "a":
            text = "".join(self._href_text).strip()
            text = re.sub(r"\s+", " ", text)
            href = (self._href or "").strip()
            if text and href:
                # если текст уже равен href — не дублировать
                if text == href:
                    self._append(text)
                else:
                    # экранируем [] в тексте
                    text = text.replace("[", "\\[").replace("]", "\\]")
                    self._append(f"[{text}]({href})")
            elif text:
                self._append(text)
            elif href:
                self._append(href)
            self._in_a = False
            self._href = None
            self._href_text = []
        elif t in _BLOCK_TAGS:
            self._append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._is_skipping() or self._in_head:
            return
        if not data:
            return
        # decode entities
        data = html.unescape(data)
        if self._in_pre:
            # в pre сохраняем как есть
            if self._in_a:
                self._href_text.append(data)
            else:
                self._append(data)
            return
        # collapse whitespace для обычного текста
        # но сохраняем один пробел между словами
        if self._in_a:
            self._href_text.append(data)
            return
        # обычное текстовое содержимое
        # нормализуем пробелы, но не трогаем переносы которые мы сами вставили
        # data может быть "\n   "
        if data.strip() == "":
            # если уже есть перенос в конце — не добавлять пробел
            if self.out and self.out[-1].endswith("\n"):
                return
            # иначе один пробел
            if self.out and not self.out[-1].endswith(" "):
                self._append(" ")
            return
        # есть видимый текст
        # нормализуем внутренние пробелы/переносы
        norm = re.sub(r"\s+", " ", data)
        self._append(norm)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.handle_data(f"&#{name};")

    def get_markdown(self) -> str:
        raw = "".join(self.out)
        # нормализация пустых строк: не более 2 переносов подряд
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        # убрать пробелы перед переносами
        raw = re.sub(r" +\n", "\n", raw)
        # схлопнуть `**` / `*` артефакты если пустые
        raw = raw.strip()
        # финальный cleanup: множественные пробелы (не в pre)
        # внутри строк уже схлопнуты
        return raw + ("\n" if raw and not raw.endswith("\n") else "")


def _html_to_markdown_fallback(html_text: str, base_url: str | None = None) -> str:
    parser = _MarkdownConverter(base_url=base_url)
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        pass
    md = parser.get_markdown()
    if md.strip():
        return md
    # ultimate fallback: strip tags
    text = re.sub(r"<script[^>]*>.*?</script>", "", html_text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text + "\n" if text else ""


def _try_readability(html_text: str, url: str | None = None) -> str | None:
    """Попытаться использовать readability-lxml если установлен."""
    try:
        from readability import Document  # type: ignore

        doc = Document(html_text)
        cleaned = doc.summary(html_partial=True)
        if cleaned and "<" in cleaned:
            return cleaned
        # fallback: doc.content()
        try:
            cleaned2 = doc.content()
            if cleaned2:
                return cleaned2
        except Exception:
            pass
        title = doc.title()
        if title:
            return f"<h1>{html.escape(title)}</h1>\n" + (cleaned or "")
    except ImportError:
        return None
    except Exception:
        return None
    return None


def _try_html2text(html_text: str, base_url: str | None = None) -> str | None:
    try:
        import html2text  # type: ignore

        h = html2text.HTML2Text()
        h.body_width = 0
        h.ignore_links = False
        h.ignore_images = False
        h.protect_links = True
        h.unicode_snob = True
        if base_url:
            try:
                h.baseurl = base_url  # type: ignore[attr-defined]
            except Exception:
                pass
        md = h.handle(html_text)
        if md and md.strip():
            return md
    except ImportError:
        return None
    except Exception:
        return None
    return None


def html_to_markdown(html_text: str, base_url: str | None = None, prefer_readability: bool = True) -> str:
    """HTML → Markdown: readability + html2text если есть, иначе fallback."""
    # 1. readability очистка (если есть)
    cleaned = None
    if prefer_readability:
        cleaned = _try_readability(html_text, url=base_url)
    # 2. html2text на очищенном или исходном
    source = cleaned if cleaned is not None else html_text
    md = _try_html2text(source, base_url=base_url)
    if md is not None and md.strip():
        # если readability дал cleaned — html2text уже на нём
        # небольшая нормализация
        md = re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"
        return md
    # 3. fallback parser на очищенном если есть, иначе исходный
    return _html_to_markdown_fallback(source, base_url=base_url)


# ── fetch ───────────────────────────────────────────────────────────


@dataclass(slots=True)
class FetchResult:
    url: str
    final_url: str
    html: str
    status: int
    headers: dict[str, str]


def fetch_html(url: str, timeout: float = DEFAULT_TIMEOUT) -> FetchResult:
    """Скачать HTML по URL (urllib, без внешних зависимостей)."""
    if not url or not str(url).strip():
        raise ValueError("URL пустой")
    url = str(url).strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        # поддержку голого домена: https://
        if re.match(r"^[\w.-]+\.[a-z]{2,}", url, re.IGNORECASE):
            url = "https://" + url
        else:
            raise ValueError(f"некорректный URL: {url}")
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru,en;q=0.8",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            status = int(getattr(resp, "status", 200) or 200)
            raw = resp.read()
            headers = {k.lower(): v for k, v in (resp.headers.items() if hasattr(resp, "headers") else [])}
            final_url = resp.geturl() if hasattr(resp, "geturl") else url
            # декодируем
            charset = _detect_charset(headers, raw[:4096].decode("latin1", errors="ignore"))
            try:
                text = raw.decode(charset, errors="replace")
            except (LookupError, ValueError):
                text = raw.decode("utf-8", errors="replace")
            return FetchResult(url=url, final_url=final_url, html=text, status=status, headers=headers)
    except urllib.error.HTTPError as exc:
        # для 4xx/5xx пробуем прочитать тело для диагностики
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            body = ""
        raise OSError(f"HTTP {exc.code} {exc.reason}: {body[:200]}") from exc
    except urllib.error.URLError as exc:
        raise OSError(f"не удалось загрузить {url}: {exc.reason if hasattr(exc, 'reason') else exc}") from exc
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OSError(f"ошибка загрузки {url}: {exc}") from exc


# ── save ────────────────────────────────────────────────────────────


def _build_markdown(
    title: str,
    markdown_body: str,
    source_url: str,
    final_url: str | None = None,
    fetched_at: datetime.datetime | None = None,
) -> str:
    """Собрать markdown с YAML-frontmatter."""
    now = fetched_at or datetime.datetime.now().astimezone()
    fm: dict[str, Any] = {
        "title": title or "Clipping",
        "source": source_url,
        "clipped": now.isoformat(timespec="seconds"),
    }
    if final_url and final_url != source_url:
        fm["final_url"] = final_url
    fm["tags"] = ["clipping"]
    body = (markdown_body or "").strip()
    if not body:
        body = "_пустая страница_"
    # frontmatter YAML
    if yaml is not None:
        try:
            block = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, width=1000)
        except Exception:
            block = "\n".join(f"{k}: {v}" for k, v in fm.items()) + "\n"
    else:
        block = "\n".join(f"{k}: {v}" for k, v in fm.items()) + "\n"
    front = f"---\n{block}---\n\n"
    # заголовок уже в body? оставляем как есть, но добавляем H1 если нет
    if not body.lstrip().startswith("#"):
        front_body = f"# {title}\n\n> Source: [{source_url}]({source_url})\n> Clipped: {now.strftime('%Y-%m-%d %H:%M')}\n\n---\n\n" + body
    else:
        # вставим мета-блок после первого заголовка
        front_body = body
        # добавим источник сверху если нет source ссылки
        if source_url not in body[:800]:
            front_body = f"> Source: [{source_url}]({source_url})  ·  Clipped: {now.strftime('%Y-%m-%d %H:%M')}\n\n" + body
    return front + front_body.rstrip() + "\n"


def clip_url(
    url: str,
    settings: dict[str, Any] | None = None,
    title_override: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Path:
    """Скачать URL и сохранить как markdown в ``vault/Clippings/``.

    Возвращает путь к созданному файлу. Бросает ``OSError`` / ``ValueError`` при ошибке.
    """
    s = settings or load_settings()
    fetched = fetch_html(url, timeout=timeout)
    html_text = fetched.html
    final_url = fetched.final_url
    # title
    title = (title_override or "").strip()
    if not title:
        title = _title_from_html(html_text).strip()
    if not title:
        # fallback из URL
        try:
            parsed = urllib.parse.urlparse(final_url or url)
            title = (parsed.netloc + parsed.path).strip("/").replace("/", " — ") or "Clipping"
        except Exception:
            title = "Clipping"
    title = re.sub(r"\s+", " ", title).strip() or "Clipping"
    # html -> markdown
    cleaned_for_title = title  # keep
    md_body = html_to_markdown(html_text, base_url=final_url or url)
    # fallback если пусто
    if not md_body.strip():
        md_body = html_to_markdown(_try_readability(html_text, url) or html_text, base_url=final_url or url)
    md_full = _build_markdown(cleaned_for_title, md_body, url, final_url=final_url)
    # сохранение
    folder = _clippings_dir(s)
    folder.mkdir(parents=True, exist_ok=True)
    # имя файла: "YYYY-MM-DD — Title.md" → безопасное
    date_prefix = datetime.datetime.now().strftime("%Y-%m-%d")
    # slug из title (без даты в имени если title уже содержит спецсимволы)
    safe_title = _sanitize_filename(title, max_len=80)
    # убрать .md если случайно попал
    if safe_title.lower().endswith(".md"):
        safe_title = safe_title[:-3]
    fname = f"{date_prefix} — {safe_title}.md"
    # доп. санитизация полного имени (тире + пробелы ок)
    # но _sanitize уже сделал, а дата-префикс безопасен
    target = folder / fname
    target = _unique_path(target)
    target.write_text(md_full, encoding="utf-8")
    # инвалидация кэша vault (best-effort)
    try:
        from . import vault as _vault

        _vault.invalidate_vault_cache()
    except Exception:
        pass
    try:
        from .vault_service import VaultService as _VS  # noqa: F401

        # VaultService кэш тоже инвалидируется через vault module
        pass
    except Exception:
        pass
    return target


def clip_html(
    html_text: str,
    source_url: str,
    settings: dict[str, Any] | None = None,
    title_override: str | None = None,
) -> Path:
    """Сохранить уже загруженный HTML как markdown (без сетевого запроса). Удобно для тестов)."""
    s = settings or load_settings()
    title = (title_override or "").strip() or _title_from_html(html_text).strip() or "Clipping"
    title = re.sub(r"\s+", " ", title).strip() or "Clipping"
    md_body = html_to_markdown(html_text, base_url=source_url)
    md_full = _build_markdown(title, md_body, source_url)
    folder = _clippings_dir(s)
    folder.mkdir(parents=True, exist_ok=True)
    date_prefix = datetime.datetime.now().strftime("%Y-%m-%d")
    safe_title = _sanitize_filename(title, max_len=80)
    if safe_title.lower().endswith(".md"):
        safe_title = safe_title[:-3]
    fname = f"{date_prefix} — {safe_title}.md"
    target = folder / fname
    target = _unique_path(target)
    target.write_text(md_full, encoding="utf-8")
    try:
        from . import vault as _vault

        _vault.invalidate_vault_cache()
    except Exception:
        pass
    return target


# ── Service (SettingsObserver + DI аналог ZoteroService) ────────────


class WebClipperService:
    """Тонкий сервис над clip_url / clip_html (аналог VaultService)."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self._settings: dict[str, Any] = dict(settings) if settings is not None else {}
        if not self._settings:
            try:
                self._settings = load_settings()
            except Exception:
                self._settings = {}
        self._lock = threading.RLock()
        self._last_error: str | None = None
        self._last_path: Path | None = None

    @property
    def settings(self) -> dict[str, Any]:
        return self._settings

    @settings.setter
    def settings(self, value: dict[str, Any]) -> None:
        self._settings = dict(value) if value is not None else {}

    def update_settings(self, settings: dict[str, Any]) -> None:
        self._settings = dict(settings) if settings is not None else {}

    def _s(self, settings: dict[str, Any] | None) -> dict[str, Any]:
        return settings if settings is not None else self._settings

    def clippings_dir(self, settings: dict[str, Any] | None = None) -> Path:
        return _clippings_dir(self._s(settings))

    def ensure_clippings_dir(self, settings: dict[str, Any] | None = None) -> Path:
        p = self.clippings_dir(settings)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def clip(
        self,
        url: str,
        settings: dict[str, Any] | None = None,
        title: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Path:
        """Синхронный клип: бросает исключение при ошибке."""
        s = self._s(settings)
        try:
            path = clip_url(url, settings=s, title_override=title, timeout=timeout)
            with self._lock:
                self._last_path = path
                self._last_error = None
            return path
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._last_error = str(exc)
            raise

    def clip_html(
        self,
        html_text: str,
        source_url: str,
        settings: dict[str, Any] | None = None,
        title: str | None = None,
    ) -> Path:
        s = self._s(settings)
        try:
            path = clip_html(html_text, source_url, settings=s, title_override=title)
            with self._lock:
                self._last_path = path
                self._last_error = None
            return path
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._last_error = str(exc)
            raise

    def list_clippings(self, settings: dict[str, Any] | None = None, limit: int = 50) -> list[Path]:
        d = self.clippings_dir(settings)
        if not d.is_dir():
            return []
        try:
            files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
        return files[: max(0, int(limit))]

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    @property
    def last_path(self) -> Path | None:
        with self._lock:
            return self._last_path


__all__ = [
    "DEFAULT_CLIPPINGS_FOLDER",
    "WebClipperService",
    "FetchResult",
    "clip_url",
    "clip_html",
    "fetch_html",
    "html_to_markdown",
    "USER_AGENT",
]
