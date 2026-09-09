"""Zotero интеграция: чтение библиотеки via API или локальной zotero.sqlite, импорт как заметок.

Поддерживает два источника:

* **Локальная БД** ``zotero.sqlite`` — читается read-only через ``sqlite3`` без
  зависимостей. Авто-поиск типичных путей (``~/Zotero/zotero.sqlite`` и
  ``settings["zotero_db_path"]`` / ``settings["vault_root"]``). Схема
  парсится best-effort: разные версии Zotero (5/6/7) имеют небольшие отличия
  в именах таблиц (``itemDataValues`` / ``fieldsCombined`` и т.д.) —
  каждый запрос обёрнут в ``try/except`` и пропускается при отсутствии таблицы.

* **Zotero Web API** — ``https://api.zotero.org`` через ``urllib.request``
  (без внешних зависимостей, как ``services.llm``/``enrich``). Требует
  ``zotero_api_key`` и ``zotero_library_id`` (``user`` или ``group``).

Импорт: каждый :class:`ZoteroItem` конвертируется в markdown-файл с
YAML-frontmatter (``title``, ``authors``, ``date``, ``doi``, ``url``,
``zotero_key`` и т.д.) и телом с библиографической карточкой. Файл
кладётся в ``settings["zotero_import_folder"]`` (по умолчанию
``"04 FreakyWiki/Zotero"``) относительно ``vault_root``.

UI: :mod:`fragilenotes.ui.zotero_view` (``ZoteroView``) использует этот
сервис — панель с поиском, списком результатов и кнопками импорта.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import load_settings
from ..vault import ALLOWED_EXTS  # noqa: F401 — re-export hint

# ── константы / дефолты ──────────────────────────────────────────────

DEFAULT_IMPORT_FOLDER = "04 FreakyWiki/Zotero"
DEFAULT_LIBRARY_TYPE = "user"
ZOTERO_API_BASE = "https://api.zotero.org"

# Типичные расположения zotero.sqlite (десктоп-клиент)
_CANDIDATE_DB_PATHS = [
    Path.home() / "Zotero" / "zotero.sqlite",
    Path.home() / ".zotero" / "zotero.sqlite",
    Path.home() / "Snap" / "zotero" / "common" / "Zotero" / "zotero.sqlite",
    Path.home() / ".mozilla" / "zotero" / "zotero.sqlite",
]

# ItemType которые НЕ являются библиографическими записями
_SKIP_ITEM_TYPES = {
    "attachment",
    "note",
    "annotation",
}

# Поле → ключ frontmatter (нормализация)
_FIELD_MAP = {
    "title": "title",
    "bookTitle": "bookTitle",
    "publicationTitle": "publicationTitle",
    "journalAbbreviation": "journalAbbreviation",
    "abstractNote": "abstract",
    "abstract": "abstract",
    "date": "date",
    "DOI": "doi",
    "doi": "doi",
    "url": "url",
    "ISBN": "isbn",
    "isbn": "isbn",
    "ISSN": "issn",
    "language": "language",
    "publisher": "publisher",
    "place": "place",
    "volume": "volume",
    "issue": "issue",
    "pages": "pages",
    "numPages": "numPages",
    "edition": "edition",
    "series": "series",
    "seriesTitle": "seriesTitle",
    "rights": "rights",
    "extra": "extra",
    "archive": "archive",
    "archiveLocation": "archiveLocation",
    "callNumber": "callNumber",
    "libraryCatalog": "libraryCatalog",
}

# ── dataclass ─────────────────────────────────────────────────────────

@dataclass(slots=True)
class ZoteroItem:
    """Нормализованная запись библиотеки Zotero."""

    key: str
    title: str
    item_type: str = "journalArticle"
    creators: list[dict[str, str]] = field(default_factory=list)
    date: str = ""
    year: str = ""
    abstract: str = ""
    doi: str = ""
    url: str = ""
    isbn: str = ""
    issn: str = ""
    publisher: str = ""
    journal: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    language: str = ""
    extra: str = ""
    tags: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    # ── helpers ───────────────────────────────────────────────────
    def creators_short(self) -> str:
        if not self.creators:
            return ""
        parts: list[str] = []
        for c in self.creators[:4]:
            last = (c.get("lastName") or c.get("name") or "").strip()
            first = (c.get("firstName") or "").strip()
            if last and first:
                parts.append(f"{last}, {first[0]}.")
            elif last:
                parts.append(last)
            elif c.get("name"):
                parts.append(c["name"])
        s = ", ".join(parts)
        if len(self.creators) > 4:
            s += " et al."
        return s

    def citation(self) -> str:
        """Короткая цитата вида ``Author (Year). Title. Journal.``."""
        auth = self.creators_short() or "—"
        yr = self.year or self.date[:4] if self.date else "n.d."
        j = self.journal or self.publisher
        parts = [f"{auth} ({yr}).", self.title + "." if self.title else ""]
        if j:
            parts.append(f"*{j}*.")
        if self.volume:
            vol = self.volume + (f"({self.issue})" if self.issue else "")
            parts.append(vol + (f", {self.pages}" if self.pages else "") + ".")
        elif self.pages:
            parts.append(f"{self.pages}.")
        if self.doi:
            parts.append(f"DOI: {self.doi}")
        elif self.url:
            parts.append(self.url)
        return " ".join(p for p in parts if p)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "itemType": self.item_type,
            "creators": list(self.creators),
            "date": self.date,
            "year": self.year,
            "abstract": self.abstract,
            "doi": self.doi,
            "url": self.url,
            "isbn": self.isbn,
            "issn": self.issn,
            "publisher": self.publisher,
            "journal": self.journal,
            "volume": self.volume,
            "issue": self.issue,
            "pages": self.pages,
            "language": self.language,
            "extra": self.extra,
            "tags": list(self.tags),
            "collections": list(self.collections),
        }

    @classmethod
    def from_api_json(cls, data: dict[str, Any]) -> ZoteroItem:
        """Построить из объекта API (``/items?format=json`` — поле ``data``)."""
        d: dict[str, Any] = data.get("data", data) if isinstance(data, dict) else {}
        if not isinstance(d, dict):
            d = {}
        key = str(d.get("key") or data.get("key") or "")
        raw_title = str(d.get("title") or d.get("shortTitle") or "").strip()
        item_type = str(d.get("itemType") or "journalArticle")
        creators = d.get("creators") if isinstance(d.get("creators"), list) else []
        norm_creators: list[dict[str, str]] = []
        for c in creators:
            if not isinstance(c, dict):
                continue
            if "name" in c and c["name"]:
                norm_creators.append({"name": str(c["name"])})
            else:
                norm_creators.append({
                    "firstName": str(c.get("firstName") or ""),
                    "lastName": str(c.get("lastName") or ""),
                    "creatorType": str(c.get("creatorType") or "author"),
                })
        date_s = str(d.get("date") or "")
        year = _extract_year(date_s)
        abstract = str(d.get("abstractNote") or d.get("abstract") or "")
        doi = str(d.get("DOI") or d.get("doi") or "")
        url = str(d.get("url") or "")
        isbn = str(d.get("ISBN") or "")
        issn = str(d.get("ISSN") or "")
        publisher = str(d.get("publisher") or "")
        journal = str(d.get("publicationTitle") or d.get("journalAbbreviation") or d.get("bookTitle") or "")
        volume = str(d.get("volume") or "")
        issue = str(d.get("issue") or "")
        pages = str(d.get("pages") or "")
        language = str(d.get("language") or "")
        extra = str(d.get("extra") or "")
        tags_raw = d.get("tags") if isinstance(d.get("tags"), list) else []
        tags: list[str] = []
        for t in tags_raw:
            if isinstance(t, dict) and t.get("tag"):
                tags.append(str(t["tag"]))
            elif isinstance(t, str) and t:
                tags.append(t)
        collections = [str(c) for c in d.get("collections", []) if isinstance(c, str)] if isinstance(d.get("collections"), list) else []
        return cls(
            key=key,
            title=raw_title or "(без названия)",
            item_type=item_type,
            creators=norm_creators,
            date=date_s,
            year=year,
            abstract=abstract,
            doi=doi,
            url=url,
            isbn=isbn,
            issn=issn,
            publisher=publisher,
            journal=journal,
            volume=volume,
            issue=issue,
            pages=pages,
            language=language,
            extra=extra,
            tags=tags,
            collections=collections,
            raw=dict(data) if isinstance(data, dict) else {},
        )


def _extract_year(date_s: str) -> str:
    if not date_s:
        return ""
    m = re.search(r"\b(1[5-9]\d{2}|20\d{2})\b", date_s)
    return m.group(1) if m else ""


def _sanitize_filename(name: str) -> str:
    """Безопасное имя файла (как в templates_view / vault)."""
    s = (name or "").strip()
    if not s:
        s = "untitled"
    s = s.replace("/", "_").replace("\\", "_")
    for ch in ('\0', ':', '*', '?', '"', '<', '>', '|', '\n', '\r'):
        s = s.replace(ch, "_")
    s = re.sub(r"\s+", " ", s).strip()
    # ограничение длины (файловая система)
    if len(s) > 120:
        s = s[:120].rstrip(" .")
    if not s.lower().endswith(".md"):
        s += ".md"
    if s.startswith("."):
        s = "_" + s
    return s


def _item_frontmatter(item: ZoteroItem) -> dict[str, Any]:
    """Frontmatter для заметки-импорта."""
    fm: dict[str, Any] = {
        "title": item.title,
        "zotero_key": item.key,
        "zotero_type": item.item_type,
    }
    if item.creators:
        fm["authors"] = [c.get("lastName") or c.get("name") or "" for c in item.creators if (c.get("lastName") or c.get("name"))]
        # полная форма для шаблонов
        fm["creators"] = item.creators
    if item.date:
        fm["date"] = item.date
    if item.year:
        fm["year"] = item.year
    if item.doi:
        fm["doi"] = item.doi
    if item.url:
        fm["url"] = item.url
    if item.journal:
        fm["journal"] = item.journal
    if item.publisher:
        fm["publisher"] = item.publisher
    if item.isbn:
        fm["isbn"] = item.isbn
    if item.issn:
        fm["issn"] = item.issn
    if item.volume:
        fm["volume"] = item.volume
    if item.issue:
        fm["issue"] = item.issue
    if item.pages:
        fm["pages"] = item.pages
    if item.language:
        fm["language"] = item.language
    if item.tags:
        fm["tags"] = list(item.tags)
    # zotero-специфичное
    fm["source"] = "zotero"
    return fm


def item_to_markdown(item: ZoteroItem) -> str:
    """Конвертировать :class:`ZoteroItem` → markdown с frontmatter."""
    import yaml  # локальный импорт — PyYAML уже в зависимостях

    fm = _item_frontmatter(item)
    # compact yaml dump (как в vault.serialize_frontmatter)
    block = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, width=1000)
    front = f"---\n{block}---\n\n"
    lines: list[str] = []
    lines.append(f"# {item.title}\n")
    # бейдж типа
    lines.append(f"> **{item.item_type}**  ·  key `{item.key}`\n")
    # авторы / дата
    if item.creators:
        authors_line = "; ".join(
            (c.get("lastName") or c.get("name") or "").strip() + (f", {c.get('firstName','').strip()}" if c.get("firstName") else "")
            for c in item.creators
        )
        lines.append(f"**Авторы:** {authors_line}\n")
    meta_parts: list[str] = []
    if item.date:
        meta_parts.append(f"Дата: {item.date}")
    if item.journal:
        meta_parts.append(f"Журнал: *{item.journal}*")
    if item.publisher:
        meta_parts.append(f"Издатель: {item.publisher}")
    if item.volume:
        meta_parts.append(f"Том: {item.volume}")
    if item.issue:
        meta_parts.append(f"Выпуск: {item.issue}")
    if item.pages:
        meta_parts.append(f"Стр.: {item.pages}")
    if meta_parts:
        lines.append(" · ".join(meta_parts) + "\n")
    links: list[str] = []
    if item.doi:
        # нормализуем doi → https://doi.org/
        doi = item.doi.strip()
        if doi.lower().startswith("http"):
            links.append(f"[DOI]({doi})")
        elif doi:
            links.append(f"[DOI](https://doi.org/{doi})")
    if item.url:
        links.append(f"[URL]({item.url})")
    if links:
        lines.append(" · ".join(links) + "\n")
    if item.tags:
        lines.append(" ".join(f"#{_tag_slug(t)}" for t in item.tags) + "\n")
    lines.append("\n---\n")
    # цитата
    lines.append(f"> {item.citation()}\n")
    lines.append("\n---\n")
    if item.abstract:
        lines.append("## Аннотация\n\n")
        lines.append(item.abstract.strip() + "\n\n")
    if item.extra:
        lines.append("## Extra\n\n")
        lines.append(item.extra.strip() + "\n\n")
    if item.raw:
        # ссылка на raw key для отладки (свёрнута)
        pass
    lines.append("\n*Импортировано из Zotero — ключ `%s`*\n" % item.key)
    body = "\n".join(lines)
    return front + body


def _tag_slug(tag: str) -> str:
    s = (tag or "").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\-/а-яА-Яёё]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "tag"


# ── API клиент ───────────────────────────────────────────────────────

@dataclass(slots=True)
class ZoteroApiConfig:
    library_id: str
    library_type: str = DEFAULT_LIBRARY_TYPE
    api_key: str = ""
    base_url: str = ZOTERO_API_BASE


def _api_headers(api_key: str) -> dict[str, str]:
    h: dict[str, str] = {"Zotero-API-Version": "3"}
    if api_key:
        h["Zotero-API-Key"] = api_key
    return h


def _api_library_prefix(cfg: ZoteroApiConfig) -> str:
    kind = "users" if cfg.library_type == "user" else "groups"
    return f"{cfg.base_url}/{kind}/{urllib.parse.quote(cfg.library_id)}"


def fetch_via_api(
    cfg: ZoteroApiConfig,
    limit: int = 25,
    start: int = 0,
    query: str | None = None,
    item_type: str | None = None,
    timeout: float = 15.0,
) -> tuple[list[ZoteroItem], int]:
    """Запросить страницу библиотеки через Zotero Web API.

    Возвращает ``(items, total)`` где ``total`` — значение заголовка
    ``Total-Results`` (или ``len(items)`` если заголовок отсутствует).
    """
    params: dict[str, str] = {
        "format": "json",
        "limit": str(max(1, min(100, int(limit)))),
        "start": str(max(0, int(start))),
        "include": "data",
    }
    if query:
        params["q"] = query
        params["qmode"] = "titleCreatorYear"
    if item_type and item_type != "all":
        params["itemType"] = item_type
    url = _api_library_prefix(cfg) + "/items?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_api_headers(cfg.api_key), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status not in (200, 201, 304):
                raise OSError(f"HTTP {resp.status}")
            raw = resp.read().decode("utf-8", errors="replace")
            total_s = resp.headers.get("Total-Results") or resp.headers.get("total-results") or ""
            try:
                total = int(total_s) if total_s else 0
            except ValueError:
                total = 0
            data = json.loads(raw) if raw else []
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise OSError(f"Zotero API HTTP {exc.code}: {body or exc.reason}") from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise OSError(f"Zotero API error: {exc}") from exc

    if not isinstance(data, list):
        data = [data] if isinstance(data, dict) else []
    items: list[ZoteroItem] = []
    for obj in data:
        if not isinstance(obj, dict):
            continue
        # API отдаёт {"key": "...", "data": {...}} — проксируем оба уровня
        try:
            it = ZoteroItem.from_api_json(obj)
            # пропускаем вложения/заметки
            if it.item_type in _SKIP_ITEM_TYPES:
                continue
            items.append(it)
        except Exception:
            continue
    if total == 0:
        total = len(items)
    return items, total


def fetch_all_via_api(
    cfg: ZoteroApiConfig,
    query: str | None = None,
    max_items: int = 200,
    timeout: float = 15.0,
) -> list[ZoteroItem]:
    """Пагинация до ``max_items`` (по 100 за запрос)."""
    out: list[ZoteroItem] = []
    start = 0
    while len(out) < max_items:
        batch, total = fetch_via_api(cfg, limit=min(100, max_items - len(out)), start=start, query=query, timeout=timeout)
        if not batch:
            break
        out.extend(batch)
        start += len(batch)
        if len(out) >= total or len(batch) < 100:
            break
        # вежливая пауза чтобы не упереться в rate-limit
        time.sleep(0.15)
    return out[:max_items]


# ── Локальная БД ─────────────────────────────────────────────────────

def _resolve_db_path(settings: dict[str, Any] | None) -> Path | None:
    """Найти ``zotero.sqlite``: ``settings["zotero_db_path"]`` → кандидаты → vault."""
    s = settings or {}
    # 1. явный путь из настроек
    for key in ("zotero_db_path", "zotero.sqlite", "zotero_db"):
        raw = s.get(key)
        if raw:
            p = Path(str(raw)).expanduser()
            if p.is_file():
                return p
            # относительный от vault_root
            try:
                vr = Path(str(s.get("vault_root") or "")).expanduser()
                cand = (vr / str(raw)).expanduser()
                if cand.is_file():
                    return cand
            except Exception:
                pass
    # 2. кандидаты по умолчанию
    for cand in _CANDIDATE_DB_PATHS:
        try:
            if cand.is_file():
                return cand
        except Exception:
            continue
    # 3. рядом с vault (если vault внутри профиля?)
    try:
        vr = Path(str(s.get("vault_root") or "")).expanduser()
        for parent in [vr, *list(vr.parents[:4])]:
            cand = parent / "zotero.sqlite"
            if cand.is_file():
                return cand
    except Exception:
        pass
    return None


def _open_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # ускорители для read-only
    try:
        conn.execute("PRAGMA query_only = ON")
    except Exception:
        pass
    return conn


def _load_maps(conn: sqlite3.Connection) -> tuple[dict[int, str], dict[int, str], dict[int, str]]:
    """``(itemTypeID→typeName, fieldID→fieldName, creatorTypeID→name)``."""
    type_map: dict[int, str] = {}
    field_map: dict[int, str] = {}
    ctype_map: dict[int, str] = {}
    try:
        for row in conn.execute("SELECT itemTypeID, typeName FROM itemTypes"):
            type_map[int(row[0])] = str(row[1])
    except Exception:
        pass
    try:
        for row in conn.execute("SELECT fieldID, fieldName FROM fields"):
            field_map[int(row[0])] = str(row[1])
    except Exception:
        pass
    # Zotero 7: fieldsCombined
    if not field_map:
        try:
            for row in conn.execute("SELECT fieldID, fieldName FROM fieldsCombined"):
                field_map[int(row[0])] = str(row[1])
        except Exception:
            pass
    try:
        for row in conn.execute("SELECT creatorTypeID, creatorType FROM creatorTypes"):
            ctype_map[int(row[0])] = str(row[1])
    except Exception:
        pass
    return type_map, field_map, ctype_map


def _fetch_local_items(
    conn: sqlite3.Connection,
    limit: int = 200,
    offset: int = 0,
    query: str | None = None,
) -> list[ZoteroItem]:
    """Best-effort чтение из ``zotero.sqlite``.

    Стратегия: собрать ``itemID→{key,type,fields,creators,tags}`` из
    нескольких таблиц, игнорируя отсутствующие таблицы/колонки.
    """
    type_map, field_map, ctype_map = _load_maps(conn)

    # ── 1. список itemID + key ──────────────────────────────────
    rows: list[sqlite3.Row] = []
    try:
        # deletedItems — исключить удалённые
        deleted_ids: set[int] = set()
        try:
            for r in conn.execute("SELECT itemID FROM deletedItems"):
                deleted_ids.add(int(r[0]))
        except Exception:
            pass
        # основной запрос — берём items
        # пробуем колонки: itemID, key, itemTypeID, libraryID
        # некоторые версии: items.key = zotero key (8 chars)
        cur = conn.execute("SELECT itemID, key, itemTypeID FROM items LIMIT ? OFFSET ?", (int(limit * 3), int(offset)))
        for r in cur:
            iid = int(r["itemID"])
            if iid in deleted_ids:
                continue
            rows.append(r)
            if len(rows) >= limit * 2:
                break
    except Exception:
        return []

    # фильтр по _SKIP_ITEM_TYPES — понадобится typeName
    filtered_rows: list[sqlite3.Row] = []
    for r in rows:
        try:
            tid = int(r["itemTypeID"]) if r["itemTypeID"] is not None else -1
        except Exception:
            tid = -1
        tname = type_map.get(tid, "")
        if tname in _SKIP_ITEM_TYPES:
            continue
        filtered_rows.append(r)
    rows = filtered_rows[:limit] if len(filtered_rows) > limit else filtered_rows
    if not rows:
        return []

    item_ids = [int(r["itemID"]) for r in rows]
    key_map = {int(r["itemID"]): str(r["key"] or "") for r in rows}
    type_map_for_items = {int(r["itemID"]): type_map.get(int(r["itemTypeID"]), "journalArticle") if r["itemTypeID"] is not None else "journalArticle" for r in rows}

    # ── 2. поля (itemData → itemDataValues) ─────────────────────
    fields_by_item: dict[int, dict[str, str]] = {iid: {} for iid in item_ids}
    try:
        placeholders = ",".join("?" for _ in item_ids)
        # схема: itemData(itemID, fieldID, valueID) → itemDataValues(valueID, value)
        # fallback: itemData напрямую хранит value (старые версии)
        try:
            q = f"SELECT itemID, fieldID, valueID FROM itemData WHERE itemID IN ({placeholders})"
            id_rows = list(conn.execute(q, item_ids))
            # собрать valueID→value
            v_ids = list({int(r["valueID"]) for r in id_rows if r["valueID"] is not None})
            val_map: dict[int, str] = {}
            if v_ids:
                # батчим по 500
                for i in range(0, len(v_ids), 500):
                    chunk = v_ids[i:i+500]
                    ph = ",".join("?" for _ in chunk)
                    for vr in conn.execute(f"SELECT valueID, value FROM itemDataValues WHERE valueID IN ({ph})", chunk):
                        val_map[int(vr["valueID"])] = str(vr["value"] or "")
            for r in id_rows:
                iid = int(r["itemID"])
                fid = int(r["fieldID"])
                fname = field_map.get(fid, f"field{fid}")
                vid = r["valueID"]
                val = val_map.get(int(vid), "") if vid is not None else ""
                if val:
                    fields_by_item[iid][fname] = val
        except Exception:
            # fallback: itemData.value напрямую?
            try:
                q = f"SELECT itemID, fieldID, value FROM itemData WHERE itemID IN ({placeholders})"
                for r in conn.execute(q, item_ids):
                    iid = int(r["itemID"])
                    fname = field_map.get(int(r["fieldID"]), str(r["fieldID"]))
                    fields_by_item[iid][fname] = str(r["value"] or "")
            except Exception:
                pass
    except Exception:
        pass

    # ── 3. создатели ─────────────────────────────────────────────
    creators_by_item: dict[int, list[dict[str, str]]] = {iid: [] for iid in item_ids}
    try:
        placeholders = ",".join("?" for _ in item_ids)
        # itemCreators(itemID, creatorID, orderIndex, creatorTypeID)
        for r in conn.execute(
            f"SELECT itemID, creatorID, creatorTypeID, orderIndex FROM itemCreators WHERE itemID IN ({placeholders}) ORDER BY orderIndex",
            item_ids,
        ):
            iid = int(r["itemID"])
            cid = int(r["creatorID"])
            ctype = ctype_map.get(int(r["creatorTypeID"]), "author") if r["creatorTypeID"] is not None else "author"
            # creators(creatorID, firstName, lastName, fieldMode)
            try:
                cr = conn.execute("SELECT firstName, lastName, fieldMode FROM creators WHERE creatorID=?", (cid,)).fetchone()
            except Exception:
                cr = None
            if cr is None:
                continue
            try:
                fm = int(cr["fieldMode"]) if cr["fieldMode"] is not None else 0
            except Exception:
                fm = 0
            first = str(cr["firstName"] or "")
            last = str(cr["lastName"] or "")
            if fm == 1:
                # institution — lastName хранит полное имя
                creators_by_item[iid].append({"name": last or first, "creatorType": ctype})
            elif last or first:
                creators_by_item[iid].append({"firstName": first, "lastName": last, "creatorType": ctype})
    except Exception:
        pass

    # ── 4. теги ──────────────────────────────────────────────────
    tags_by_item: dict[int, list[str]] = {iid: [] for iid in item_ids}
    try:
        placeholders = ",".join("?" for _ in item_ids)
        # itemTags(itemID, tagID) → tags(tagID, name)
        # альтернатива: tags.name уже в itemTags?
        tag_id_map: dict[int, str] = {}
        try:
            for r in conn.execute("SELECT tagID, name FROM tags"):
                tag_id_map[int(r["tagID"])] = str(r["name"] or "")
        except Exception:
            pass
        if tag_id_map:
            for r in conn.execute(f"SELECT itemID, tagID FROM itemTags WHERE itemID IN ({placeholders})", item_ids):
                iid = int(r["itemID"])
                tname = tag_id_map.get(int(r["tagID"]), "")
                if tname:
                    tags_by_item[iid].append(tname)
        else:
            # fallback: tags напрямую?
            try:
                for r in conn.execute(f"SELECT itemID, tag FROM itemTags WHERE itemID IN ({placeholders})", item_ids):
                    iid = int(r["itemID"])
                    t = str(r["tag"] or "")
                    if t:
                        tags_by_item[iid].append(t)
            except Exception:
                pass
    except Exception:
        pass

    # ── 5. сборка ZoteroItem ─────────────────────────────────────
    items: list[ZoteroItem] = []
    q_lower = (query or "").strip().lower()
    for iid in item_ids:
        f = fields_by_item.get(iid, {})
        tname = type_map_for_items.get(iid, "journalArticle")
        title = str(f.get("title") or f.get("shortTitle") or f.get("bookTitle") or "").strip() or "(без названия)"
        date_s = str(f.get("date") or "")
        year = _extract_year(date_s)
        abstract = str(f.get("abstractNote") or f.get("abstract") or "")
        doi = str(f.get("DOI") or f.get("doi") or "")
        url = str(f.get("url") or "")
        isbn = str(f.get("ISBN") or f.get("isbn") or "")
        issn = str(f.get("ISSN") or "")
        publisher = str(f.get("publisher") or "")
        # journal — publicationTitle / journalAbbreviation / bookTitle
        journal = str(f.get("publicationTitle") or f.get("journalAbbreviation") or f.get("bookTitle") or "")
        volume = str(f.get("volume") or "")
        issue = str(f.get("issue") or "")
        pages = str(f.get("pages") or "")
        language = str(f.get("language") or "")
        extra = str(f.get("extra") or "")
        creators = creators_by_item.get(iid, [])
        tags = tags_by_item.get(iid, [])

        # query фильтр — title / creators / abstract / tags / journal
        if q_lower:
            hay = " ".join([
                title.lower(),
                abstract.lower(),
                journal.lower(),
                doi.lower(),
                " ".join(tags).lower(),
                " ".join((c.get("lastName") or c.get("name") or "").lower() for c in creators),
            ])
            if q_lower not in hay:
                continue

        items.append(ZoteroItem(
            key=key_map.get(iid, f"local-{iid}"),
            title=title,
            item_type=tname,
            creators=creators,
            date=date_s,
            year=year,
            abstract=abstract,
            doi=doi,
            url=url,
            isbn=isbn,
            issn=issn,
            publisher=publisher,
            journal=journal,
            volume=volume,
            issue=issue,
            pages=pages,
            language=language,
            extra=extra,
            tags=tags,
            raw={"itemID": iid, **f},
        ))
        if len(items) >= limit:
            break
    return items


def list_from_db(
    db_path: Path | str | None = None,
    settings: dict[str, Any] | None = None,
    limit: int = 100,
    query: str | None = None,
) -> list[ZoteroItem]:
    """Прочитать записи из локальной ``zotero.sqlite``.

    ``db_path`` приоритетнее ``settings``. Если оба не указаны — пытается
    авто-определить путь через :func:`_resolve_db_path`.
    """
    path: Path | None = None
    if db_path is not None:
        p = Path(str(db_path)).expanduser()
        if p.is_file():
            path = p
    if path is None:
        path = _resolve_db_path(settings)
    if path is None or not path.is_file():
        return []
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_ro(path)
        return _fetch_local_items(conn, limit=int(limit), query=query)
    except (OSError, sqlite3.Error):
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def search_items(
    items: list[ZoteroItem],
    query: str,
    limit: int = 50,
) -> list[ZoteroItem]:
    """Подстрока-поиск по уже загруженному списку (ранжирование как в vault)."""
    q = (query or "").strip().lower()
    if not q:
        return items[:limit]
    scored: list[tuple[int, ZoteroItem]] = []
    for it in items:
        title_l = it.title.lower()
        hay_title = q in title_l
        hay_auth = any(q in (c.get("lastName") or c.get("name") or "").lower() for c in it.creators)
        hay_abs = q in it.abstract.lower()
        hay_tag = any(q in t.lower() for t in it.tags)
        hay_j = q in it.journal.lower()
        if not (hay_title or hay_auth or hay_abs or hay_tag or hay_j):
            continue
        # ранжирование: точное title > префикс > вхождение
        if title_l == q:
            rank = 0
        elif title_l.startswith(q):
            rank = 1
        elif hay_title:
            rank = 2
        elif hay_auth:
            rank = 3
        else:
            rank = 4
        scored.append((rank, it))
    scored.sort(key=lambda x: (x[0], x[1].title.lower()))
    return [it for _, it in scored[:limit]]


# ── Импорт как заметок ───────────────────────────────────────────────

def _import_folder(settings: dict[str, Any] | None) -> Path:
    s = settings or {}
    raw = str(s.get("zotero_import_folder") or s.get("zotero_folder") or DEFAULT_IMPORT_FOLDER).strip()
    # относительный от vault_root или абсолютный
    vr = Path(str(s.get("vault_root") or Path.home() / "desktop")).expanduser()
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = vr / p
    return p


def _unique_path(base: Path) -> Path:
    """Если файл существует — добавить `` (1)`` / `` (2)`` …"""
    if not base.exists():
        return base
    stem = base.stem
    suffix = base.suffix or ".md"
    parent = base.parent
    for i in range(1, 1000):
        cand = parent / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
    # fallback — timestamp
    return parent / f"{stem} {int(time.time())}{suffix}"


def import_item(
    item: ZoteroItem,
    settings: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> Path:
    """Импортировать один :class:`ZoteroItem` как markdown-заметку.

    Возвращает путь к созданному файлу.
    """
    folder = _import_folder(settings)
    folder.mkdir(parents=True, exist_ok=True)
    # имя файла: "Author Year - Title.md" → безопасное
    base_name = item.title.strip() or item.key
    # префикс из первого автора + года для сортировки
    prefix = ""
    if item.creators:
        first = item.creators[0]
        last = (first.get("lastName") or first.get("name") or "").strip()
        if last:
            prefix = last
    if item.year:
        prefix = f"{prefix} {item.year}" if prefix else item.year
    if prefix:
        base_name = f"{prefix} — {base_name}"
    fname = _sanitize_filename(base_name)
    target = folder / fname
    if not overwrite:
        target = _unique_path(target)
    md = item_to_markdown(item)
    target.write_text(md, encoding="utf-8")
    return target


def import_items(
    items: list[ZoteroItem],
    settings: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> list[Path]:
    """Импортировать список записей; возвращает пути созданных файлов."""
    out: list[Path] = []
    for it in items:
        try:
            out.append(import_item(it, settings=settings, overwrite=overwrite))
        except OSError:
            continue
    return out


# ── Сервис (SettingsObserver + DI) ───────────────────────────────────

class ZoteroService:
    """Тонкий сервис над API / локальной БД (аналог VaultService / LlmService).

    Хранит копию ``settings`` и проксирует вызовы. Поддерживает
    ``update_settings()`` для синхронизации из ``app.py``.
    """

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self._settings: dict[str, Any] = dict(settings) if settings is not None else {}
        if not self._settings:
            try:
                self._settings = load_settings()
            except Exception:
                self._settings = {}
        self._cache: list[ZoteroItem] = []
        self._cache_at: float = 0.0
        self._cache_lock = threading.RLock()
        self._last_error: str | None = None

    # ── settings ──────────────────────────────────────────────────
    @property
    def settings(self) -> dict[str, Any]:
        return self._settings

    @settings.setter
    def settings(self, value: dict[str, Any]) -> None:
        self._settings = dict(value) if value is not None else {}

    def update_settings(self, settings: dict[str, Any]) -> None:
        self._settings = dict(settings) if settings is not None else {}
        with self._cache_lock:
            self._cache = []
            self._cache_at = 0.0

    def _s(self, settings: dict[str, Any] | None) -> dict[str, Any]:
        return settings if settings is not None else self._settings

    # ── конфиг API ────────────────────────────────────────────────
    def _api_config(self, settings: dict[str, Any] | None = None) -> ZoteroApiConfig | None:
        s = self._s(settings)
        lib_id = str(s.get("zotero_library_id") or s.get("zotero_user_id") or s.get("zoteroLibraryID") or "").strip()
        if not lib_id:
            return None
        lib_type = str(s.get("zotero_library_type") or s.get("zoteroLibraryType") or DEFAULT_LIBRARY_TYPE).strip().lower()
        if lib_type not in ("user", "group"):
            lib_type = DEFAULT_LIBRARY_TYPE
        api_key = str(s.get("zotero_api_key") or s.get("zoteroApiKey") or s.get("ZOTERO_API_KEY") or "").strip()
        base = str(s.get("zotero_api_base") or ZOTERO_API_BASE).strip() or ZOTERO_API_BASE
        return ZoteroApiConfig(library_id=lib_id, library_type=lib_type, api_key=api_key, base_url=base)

    # ── db path ───────────────────────────────────────────────────
    def db_path(self, settings: dict[str, Any] | None = None) -> Path | None:
        return _resolve_db_path(self._s(settings))

    def is_db_available(self, settings: dict[str, Any] | None = None) -> bool:
        p = self.db_path(settings)
        return p is not None and p.is_file()

    def is_api_configured(self, settings: dict[str, Any] | None = None) -> bool:
        return self._api_config(settings) is not None

    # ── источники ─────────────────────────────────────────────────
    def list_from_db(
        self,
        limit: int = 100,
        query: str | None = None,
        settings: dict[str, Any] | None = None,
        db_path: Path | str | None = None,
    ) -> list[ZoteroItem]:
        return list_from_db(db_path=db_path, settings=self._s(settings), limit=limit, query=query)

    def list_from_api(
        self,
        limit: int = 50,
        query: str | None = None,
        settings: dict[str, Any] | None = None,
        start: int = 0,
    ) -> tuple[list[ZoteroItem], int]:
        cfg = self._api_config(settings)
        if cfg is None:
            return [], 0
        return fetch_via_api(cfg, limit=limit, start=start, query=query)

    def list_items(
        self,
        limit: int = 100,
        query: str | None = None,
        source: str = "auto",
        settings: dict[str, Any] | None = None,
    ) -> list[ZoteroItem]:
        """Единый вход: ``source`` = ``auto`` | ``db`` | ``api``.

        ``auto`` — сначала пробует локальную БД, при отсутствии/пустоте
        падает на API (если настроен).
        """
        src = (source or "auto").strip().lower()
        s = self._s(settings)
        if src in ("db", "local"):
            return self.list_from_db(limit=limit, query=query, settings=s)
        if src == "api":
            items, _ = self.list_from_api(limit=limit, query=query, settings=s)
            return items
        # auto
        if self.is_db_available(s):
            items = self.list_from_db(limit=limit, query=query, settings=s)
            if items:
                return items
        if self.is_api_configured(s):
            try:
                items, _ = self.list_from_api(limit=limit, query=query, settings=s)
                if items:
                    return items
            except OSError as exc:
                self._last_error = str(exc)
        # fallback — пустой список
        return []

    def search(
        self,
        query: str,
        limit: int = 50,
        source: str = "auto",
        settings: dict[str, Any] | None = None,
    ) -> list[ZoteroItem]:
        """Поиск (делегирует в ``list_items`` с ``query`` + локальное ранжирование)."""
        items = self.list_items(limit=max(limit * 2, 100), query=query, source=source, settings=settings)
        # если источник уже фильтровал по query — просто ранжируем
        return search_items(items, query, limit=limit) if query.strip() else items[:limit]

    # ── кэш (опционально для UI) ──────────────────────────────────
    def cached_items(self, max_age: float = 30.0) -> list[ZoteroItem] | None:
        with self._cache_lock:
            if self._cache and (time.monotonic() - self._cache_at) < max_age:
                return list(self._cache)
            return None

    def prime_cache(self, items: list[ZoteroItem]) -> None:
        with self._cache_lock:
            self._cache = list(items)
            self._cache_at = time.monotonic()

    @property
    def last_error(self) -> str | None:
        return self._last_error

    # ── импорт ────────────────────────────────────────────────────
    def import_item(
        self,
        item: ZoteroItem,
        settings: dict[str, Any] | None = None,
        overwrite: bool = False,
    ) -> Path:
        return import_item(item, settings=self._s(settings), overwrite=overwrite)

    def import_items(
        self,
        items: list[ZoteroItem],
        settings: dict[str, Any] | None = None,
        overwrite: bool = False,
    ) -> list[Path]:
        return import_items(items, settings=self._s(settings), overwrite=overwrite)

    def import_by_keys(
        self,
        keys: list[str],
        settings: dict[str, Any] | None = None,
        source: str = "auto",
    ) -> list[Path]:
        """Импорт по ключам (находит items в библиотеке, затем сохраняет)."""
        if not keys:
            return []
        wanted = {k.strip() for k in keys if k.strip()}
        all_items = self.list_items(limit=500, source=source, settings=settings)
        matched = [it for it in all_items if it.key in wanted]
        return self.import_items(matched, settings=settings)


__all__ = [
    "ZoteroItem",
    "ZoteroApiConfig",
    "ZoteroService",
    "DEFAULT_IMPORT_FOLDER",
    "ZOTERO_API_BASE",
    "fetch_via_api",
    "fetch_all_via_api",
    "list_from_db",
    "search_items",
    "item_to_markdown",
    "_sanitize_filename",
    "import_item",
    "import_items",
]
