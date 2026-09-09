"""База данных (Notion-like table view) для FragileNotes.

Таблица с колонками (Name, Status, Tags, Date) из frontmatter, сортировка по колонке,
фильтр, inline редактирование. Данные из vault/*.md frontmatter. Использует Gtk.ColumnView.
Интегрируется как вкладка (см. app.py VIEWS, workspace.py, sidebar.py).

Модель:
  DatabaseRow — GObject с properties name/status/tags/date/relation/lookup/rollup/path_str
  для сортировки через Gtk.StringSorter + Gtk.PropertyExpression.

Сканирование:
  Обход vault через os.scandir с пропуском HEAVY_DIRS / скрытых папок (как в services.vault),
  чтение frontmatter через fragilenotes.vault.parse_frontmatter.

Фильтр:
  Gtk.CustomFilter по всем полям (lower), поисковая строка + dropdown статуса.

Сортировка:
  Gtk.ColumnView + Gtk.SortListModel + sorter = column_view.get_sorter(),
  каждая колонка имеет свой Gtk.StringSorter.

Inline редактирование:
  SignalListItemFactory с Gtk.Entry (flat, no frame) — activate / focus-out сохраняет
  frontmatter (update_frontmatter) и инвалидирует vault кэш. Tags как comma-список.

Relations:
  Связи между записями хранятся как [[wikilink]] в frontmatter (поле relation).
  - Relation колонка — список связанных записей (отображаем как "A, B").
  - Lookup — подтягивает поле (по умолчанию status) из связанных записей.
  - Rollup — агрегация по связанным записям: count | sum | avg | min | max.
  Конфиг из settings: database_relation_field, database_lookup_field,
  database_rollup_field, database_rollup_agg.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from ..vault import HEAVY_DIRS, WIKILINK_RE, parse_frontmatter, serialize_frontmatter  # noqa: E402
from .widgets import empty_state, view_header  # noqa: E402

# ── relations: константы и регулярки ────────────────────────────────────────

# кандидаты ключей frontmatter для relation (первый найденный используется)
RELATION_FIELD_CANDIDATES: tuple[str, ...] = ("relation", "relations", "related", "links", "linked")
DEFAULT_RELATION_FIELD = "relation"
DEFAULT_LOOKUP_FIELD = "status"
DEFAULT_ROLLUP_FIELD = "status"
DEFAULT_ROLLUP_AGG = "count"  # count | sum | avg | min | max

# wikilink уже есть в vault.WIKILINK_RE, но дублируем для автономности
_RELATION_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
# alias -> WIKILINK_RE из vault для единообразия
WIKILINK_PATTERN = WIKILINK_RE

ROLLUP_AGGS = ("count", "sum", "avg", "min", "max", "count_not_empty", "count_empty")


# ── GObject row ──────────────────────────────────────────────────────────


class DatabaseRow(GObject.Object):
    """Строка таблицы базы данных. Properties для Gtk.StringSorter."""

    __gtype_name__ = "DatabaseRow"

    name = GObject.Property(type=str, default="")
    status = GObject.Property(type=str, default="")
    tags = GObject.Property(type=str, default="")
    date = GObject.Property(type=str, default="")
    relation = GObject.Property(type=str, default="")
    lookup = GObject.Property(type=str, default="")
    rollup = GObject.Property(type=str, default="")
    path_str = GObject.Property(type=str, default="")
    # lower для быстрой фильтрации (не видны в таблице)
    lower_name = GObject.Property(type=str, default="")
    lower_status = GObject.Property(type=str, default="")
    lower_tags = GObject.Property(type=str, default="")
    lower_date = GObject.Property(type=str, default="")
    lower_relation = GObject.Property(type=str, default="")
    lower_lookup = GObject.Property(type=str, default="")
    lower_rollup = GObject.Property(type=str, default="")

    def __init__(
        self,
        name: str,
        status: str,
        tags: str,
        date: str,
        path_str: str,
        relation: str = "",
        lookup: str = "",
        rollup: str = "",
    ) -> None:
        super().__init__()
        self.name = name
        self.status = status
        self.tags = tags
        self.date = date
        self.path_str = path_str
        self.relation = relation
        self.lookup = lookup
        self.rollup = rollup
        self.lower_name = name.lower()
        self.lower_status = status.lower()
        self.lower_tags = tags.lower()
        self.lower_date = date.lower()
        self.lower_relation = relation.lower()
        self.lower_lookup = lookup.lower()
        self.lower_rollup = rollup.lower()
        # absolute path cache for editing
        self._path = Path(path_str)
        # internal: targets list для lookup/rollup пересчёта
        self._relation_targets: list[str] = parse_relation_value(relation) if relation else []


# ── helpers: фронтматтер ───────────────────────────────────────────────


def _vault_root(settings: dict) -> Path:
    return Path(str(settings.get("vault_root") or Path.home() / "desktop"))


def _normalize_tags(raw) -> str:
    """Frontmatter tags -> строка для колонки (comma joined)."""
    if raw is None:
        return ""
    if isinstance(raw, list):
        parts = [str(x).strip() for x in raw if str(x).strip()]
        # убрать ведущий #
        parts = [p.lstrip("#").strip() for p in parts]
        return ", ".join(parts)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return ""
        # comma or space separated, also support "#a #b"
        if "," in s:
            parts = [p.strip().lstrip("#") for p in s.split(",")]
        else:
            # split by whitespace, keep hashtags
            parts = [p.strip().lstrip("#") for p in s.split()]
        parts = [p for p in parts if p]
        return ", ".join(parts)
    return str(raw).strip()


def _normalize_date(raw) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    # keep as-is, but normalize leading/trailing quotes
    return s.strip("\"' ")


def _extract_fields(path: Path, fm: dict) -> tuple[str, str, str, str]:
    """(name, status, tags, date) из fm + fallback."""
    # Name: title > name > stem
    name = ""
    for key in ("title", "name", "Title", "Name"):
        if fm.get(key):
            name = str(fm[key]).strip().strip("\"'")
            if name:
                break
    if not name:
        name = path.stem.replace("-", " ").replace("_", " ").strip() or path.stem

    status = str(fm.get("status") or fm.get("Status") or "").strip()
    tags = _normalize_tags(fm.get("tags") if "tags" in fm else fm.get("tag"))
    # if tags still empty, try to extract from body later? task says only frontmatter
    date = ""
    for key in ("date", "created", "Created", "due", "due_date", "updated"):
        if fm.get(key):
            date = _normalize_date(fm[key])
            if date:
                break
    return name, status, tags, date


# ── relation helpers ────────────────────────────────────────────────────


def parse_relation_value(raw) -> list[str]:
    """Парсит frontmatter relation значение -> список имён целей (без [[]]).

    Поддерживает:
      - list: ["[[Note]]", "[[Note|alias]]", "Note", "[[Note#head]]"] -> ["Note", ...]
      - str:  "[[A]], [[B]]" или "[[A]] [[B]]" или "A, B" -> ["A","B"]
      - None -> []
    Дубликаты удаляются (case-insensitive) с сохранением порядка.
    """
    if raw is None:
        return []
    targets: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if item is None:
                continue
            s = str(item).strip()
            if not s:
                continue
            # извлечь wikilink если есть
            m = _RELATION_WIKILINK_RE.search(s)
            if m:
                # может быть несколько wikilink в одном элементе списка? редкий случай
                found = _RELATION_WIKILINK_RE.findall(s)
                if found:
                    for f in found:
                        t = f.strip()
                        if t:
                            targets.append(t)
                    continue
                targets.append(m.group(1).strip())
            else:
                # fallback: считаем что это уже имя, возможно с запятыми
                if "," in s:
                    for p in s.split(","):
                        pp = p.strip().strip("[]#| ")
                        if pp:
                            # убрать alias/heading
                            pp = pp.split("|")[0].split("#")[0].strip()
                            if pp:
                                targets.append(pp)
                else:
                    # убрать alias/heading если случайно передали
                    cleaned = s.strip().strip("[]")
                    cleaned = cleaned.split("|")[0].split("#")[0].strip()
                    if cleaned:
                        targets.append(cleaned)
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        # найти все wikilink в строке
        found = _RELATION_WIKILINK_RE.findall(s)
        if found:
            targets = [t.strip() for t in found if t.strip()]
        else:
            # нет скобок — comma separated имена
            if "," in s:
                parts = [p.strip().strip("[]").split("|")[0].split("#")[0].strip() for p in s.split(",")]
                targets = [p for p in parts if p]
            else:
                # одиночное имя, может быть со скобками без регекса
                cleaned = s.strip("[] ")
                cleaned = cleaned.split("|")[0].split("#")[0].strip()
                if cleaned:
                    targets = [cleaned]
    else:
        # неизвестный тип — приводим к строке
        s = str(raw).strip()
        if not s:
            return []
        m = _RELATION_WIKILINK_RE.search(s)
        if m:
            targets = [m.group(1).strip()]
        else:
            targets = [s.strip("[] ")]

    # deduplicate case-insensitive, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for t in targets:
        # финальная чистка: убрать | alias и # heading если остались
        clean = t.split("|")[0].split("#")[0].strip()
        if not clean:
            continue
        low = clean.lower()
        if low not in seen:
            seen.add(low)
            out.append(clean)
    return out


def serialize_relation_value(targets: list[str]) -> list[str]:
    """Список имён -> список wikilink строк для хранения в frontmatter."""
    out: list[str] = []
    seen: set[str] = set()
    for t in targets:
        name = str(t).strip().split("|")[0].split("#")[0].strip().strip("[] ")
        if not name:
            continue
        low = name.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(f"[[{name}]]")
    return out


def _extract_relation_targets(fm: dict, relation_field: str | None = None) -> tuple[str, list[str]]:
    """Найти relation поле в fm и вернуть (key, targets).

    Если relation_field указан — ищет его. Иначе перебирает RELATION_FIELD_CANDIDATES.
    Если ни один не найден — ищет любое поле значение которого содержит [[wikilink]].
    """
    if not isinstance(fm, dict):
        return (relation_field or DEFAULT_RELATION_FIELD, [])
    # явное поле из settings
    if relation_field:
        raw = fm.get(relation_field)
        if raw is None:
            # case-insensitive поиск
            for k, v in fm.items():
                if str(k).lower() == relation_field.lower():
                    raw = v
                    break
        if raw is not None:
            return relation_field, parse_relation_value(raw)
    # кандидаты
    for cand in RELATION_FIELD_CANDIDATES:
        if cand in fm and fm[cand] is not None:
            return cand, parse_relation_value(fm[cand])
        # case-insensitive
        for k, v in fm.items():
            if str(k).lower() == cand.lower() and v is not None:
                return str(k), parse_relation_value(v)
    # fallback: любое поле с wikilink
    for k, v in fm.items():
        try:
            s = str(v) if not isinstance(v, list) else " ".join(str(x) for x in v)
        except Exception:
            continue
        if "[[" in s and "]]" in s and _RELATION_WIKILINK_RE.search(s):
            return str(k), parse_relation_value(v)
    return (relation_field or DEFAULT_RELATION_FIELD, [])


def _get_relation_key(settings: dict) -> str:
    raw = str(settings.get("database_relation_field") or settings.get("relation_field") or DEFAULT_RELATION_FIELD).strip()
    return raw or DEFAULT_RELATION_FIELD


def _get_lookup_field(settings: dict) -> str:
    raw = str(settings.get("database_lookup_field") or settings.get("lookup_field") or DEFAULT_LOOKUP_FIELD).strip()
    return raw or DEFAULT_LOOKUP_FIELD


def _get_rollup_field(settings: dict) -> str:
    raw = str(settings.get("database_rollup_field") or settings.get("rollup_field") or DEFAULT_ROLLUP_FIELD).strip()
    return raw or DEFAULT_ROLLUP_FIELD


def _get_rollup_agg(settings: dict) -> str:
    raw = str(settings.get("database_rollup_agg") or settings.get("rollup_agg") or DEFAULT_ROLLUP_AGG).strip().lower()
    if raw not in ROLLUP_AGGS:
        # алиасы
        alias = {"cnt": "count", "sum": "sum", "average": "avg", "mean": "avg", "minimum": "min", "maximum": "max"}
        raw = alias.get(raw, DEFAULT_ROLLUP_AGG)
        if raw not in ROLLUP_AGGS:
            raw = DEFAULT_ROLLUP_AGG
    return raw


def _is_numeric(value) -> bool:
    if value is None:
        return False
    s = str(value).strip()
    if not s:
        return False
    try:
        float(s)
        return True
    except Exception:
        return False


def _to_number(value) -> float | None:
    try:
        return float(str(value).strip())
    except Exception:
        return None


# совместимость: публичные алиасы для тестов
parse_relation = parse_relation_value
serialize_relation = serialize_relation_value


def resolve_relation_targets(
    targets: list[str],
    index: dict[str, DatabaseRow],
) -> list[DatabaseRow]:
    """Targets (имена) -> список DatabaseRow по индексу (case-insensitive)."""
    out: list[DatabaseRow] = []
    seen: set[str] = set()
    for t in targets:
        low = str(t).strip().lower()
        if not low or low in seen:
            continue
        row = index.get(low)
        if row is None:
            # пробуем найти по stem без учёта расширения: уже lower
            continue
        # dedup по path
        key = row.path_str.lower()
        if key in seen:
            continue
        # отмечаем оба low для избежания дублей по имени
        seen.add(low)
        seen.add(key)
        out.append(row)
    return out


def lookup_for_row(
    row: DatabaseRow,
    index: dict[str, DatabaseRow],
    lookup_field: str = DEFAULT_LOOKUP_FIELD,
    separator: str = ", ",
) -> str:
    """Lookup: собрать значения lookup_field из связанных записей."""
    targets = getattr(row, "_relation_targets", None)
    if targets is None:
        targets = parse_relation_value(row.relation)
    related = resolve_relation_targets(targets, index)
    values: list[str] = []
    fld = lookup_field.strip()
    for rel in related:
        # пробуем свойство DatabaseRow
        val = getattr(rel, fld, None)
        if val is None:
            # case-insensitive поиск среди свойств
            low = fld.lower()
            for prop in ("name", "status", "tags", "date", "relation", "lookup", "rollup"):
                if prop.lower() == low:
                    val = getattr(rel, prop, "")
                    break
            else:
                # fallback: пробуем frontmatter напрямую (читаем файл)
                try:
                    text = Path(rel.path_str).read_text(encoding="utf-8", errors="replace")
                    fm, _ = parse_frontmatter(text)
                    # case-insensitive fm lookup
                    found = None
                    for k, v in fm.items():
                        if str(k).lower() == low:
                            found = v
                            break
                    if found is not None:
                        if isinstance(found, list):
                            val = ", ".join(str(x) for x in found)
                        else:
                            val = str(found)
                    else:
                        val = ""
                except Exception:
                    val = ""
        # normalize list -> string
        if isinstance(val, list):
            s = ", ".join(str(x) for x in val if str(x).strip())
        else:
            s = str(val).strip() if val is not None else ""
        if s:
            values.append(s)
    return separator.join(values)


def rollup_for_row(
    row: DatabaseRow,
    index: dict[str, DatabaseRow],
    rollup_field: str = DEFAULT_ROLLUP_FIELD,
    agg: str = DEFAULT_ROLLUP_AGG,
) -> str:
    """Rollup агрегация по связанным записям.

    agg: count -> количество связей
         sum   -> сумма числовых значений rollup_field
         avg   -> среднее
         min/max -> минимум/максимум (числовой если возможно, иначе лексикографически)
         count_not_empty -> количество непустых значений
         count_empty -> количество пустых
    """
    targets = getattr(row, "_relation_targets", None)
    if targets is None:
        targets = parse_relation_value(row.relation)
    agg_low = agg.strip().lower()
    if agg_low == "count":
        return str(len(targets))
    # для остальных нужен resolved список
    related = resolve_relation_targets(targets, index)
    if agg_low in ("count_not_empty", "count_empty"):
        # считать по полю
        vals: list[str] = []
        for rel in related:
            v = getattr(rel, rollup_field, None)
            if v is None:
                # case-insensitive fallback
                low = rollup_field.lower()
                for prop in ("name", "status", "tags", "date"):
                    if prop.lower() == low:
                        v = getattr(rel, prop, "")
                        break
                else:
                    v = ""
            vals.append(str(v).strip() if v is not None else "")
        if agg_low == "count_not_empty":
            return str(sum(1 for x in vals if x))
        return str(sum(1 for x in vals if not x))

    # собрать значения поля для sum/avg/min/max
    raw_values: list[str] = []
    for rel in related:
        v = getattr(rel, rollup_field, None)
        if v is None:
            low = rollup_field.lower()
            # пробуем свойства
            found_prop = False
            for prop in ("name", "status", "tags", "date", "relation", "lookup", "rollup"):
                if prop.lower() == low:
                    v = getattr(rel, prop, "")
                    found_prop = True
                    break
            if not found_prop:
                # читаем frontmatter
                try:
                    text = Path(rel.path_str).read_text(encoding="utf-8", errors="replace")
                    fm, _ = parse_frontmatter(text)
                    found = None
                    for k, vv in fm.items():
                        if str(k).lower() == low:
                            found = vv
                            break
                    if found is not None:
                        v = found
                    else:
                        v = ""
                except Exception:
                    v = ""
        if isinstance(v, list):
            # для rollup берём каждый элемент? упростим: join -> не число, но для min/max ок
            # для sum/avg — пробуем каждый элемент как число
            for elem in v:
                raw_values.append(str(elem).strip())
        else:
            raw_values.append(str(v).strip() if v is not None else "")

    # filter empty for numeric
    if agg_low in ("sum", "avg"):
        nums: list[float] = []
        for s in raw_values:
            if not s:
                continue
            # tags могут быть "a, b" — не число, пропускаем
            if _is_numeric(s):
                n = _to_number(s)
                if n is not None:
                    nums.append(n)
            else:
                # пробуем split by comma для list-like strings
                if "," in s:
                    for part in s.split(","):
                        part = part.strip()
                        if _is_numeric(part):
                            n = _to_number(part)
                            if n is not None:
                                nums.append(n)
        if not nums:
            return "0" if agg_low == "sum" else ""
        if agg_low == "sum":
            total = sum(nums)
            # убрать .0 если целое
            if total == int(total):
                return str(int(total))
            return str(total)
        # avg
        avg = sum(nums) / len(nums)
        if avg == int(avg):
            return str(int(avg))
        # округляем до 2 знаков
        return f"{avg:.2f}".rstrip("0").rstrip(".")

    if agg_low in ("min", "max"):
        # фильтруем пустые
        vals = [v for v in raw_values if v]
        if not vals:
            return ""
        # пробуем числовое сравнение если все numeric
        all_num = all(_is_numeric(v) for v in vals)
        if all_num:
            nums = [float(v) for v in vals]  # type: ignore[arg-type]
            m = min(nums) if agg_low == "min" else max(nums)
            if m == int(m):
                return str(int(m))
            return str(m)
        # лексикографически case-insensitive
        vals_sorted = sorted(vals, key=lambda s: s.lower())
        return vals_sorted[0] if agg_low == "min" else vals_sorted[-1]

    # fallback count
    return str(len(targets))


# также экспортируем alias для интеграции
get_lookup = lookup_for_row
get_rollup = rollup_for_row
compute_lookup = lookup_for_row
compute_rollup = rollup_for_row


def _iter_md_files(root: Path):
    """Итератор md файлов vault с пропуском HEAVY_DIRS и скрытых."""
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    name = e.name
                    if name.startswith(".") or name in HEAVY_DIRS:
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False) and name.lower().endswith(".md"):
                            yield Path(e.path)
                    except OSError:
                        continue
        except OSError:
            continue


def _build_index(rows: list[DatabaseRow]) -> dict[str, DatabaseRow]:
    """Индекс для резолва wikilink: lower(stem) и lower(name) -> row."""
    idx: dict[str, DatabaseRow] = {}
    for r in rows:
        try:
            stem = Path(r.path_str).stem.lower()
            # не перезаписываем если уже есть (первый выигрывает)
            idx.setdefault(stem, r)
            idx.setdefault(r.name.lower(), r)
            # также без расширения? уже stem
            # дополнительно: path lower
            idx.setdefault(r.path_str.lower(), r)
        except Exception:
            continue
    return idx


def scan_database(settings: dict) -> list[DatabaseRow]:
    """Все md из vault -> DatabaseRow. Вызывается вне GUI потока допустимо."""
    root = _vault_root(settings)
    if not root.is_dir():
        return []
    relation_field = _get_relation_key(settings)
    lookup_field = _get_lookup_field(settings)
    rollup_field = _get_rollup_field(settings)
    rollup_agg = _get_rollup_agg(settings)

    rows: list[DatabaseRow] = []
    for p in _iter_md_files(root):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm, _body = parse_frontmatter(text)
        if not isinstance(fm, dict):
            fm = {}
        name, status, tags, date = _extract_fields(p, fm)
        # relation: берём указанное поле из settings или авто-детект
        _, targets = _extract_relation_targets(fm, relation_field)
        relation_display = ", ".join(targets)
        # lookup/rollup пока пустые — посчитаем вторым проходом
        row = DatabaseRow(
            name=name,
            status=status,
            tags=tags,
            date=date,
            path_str=str(p),
            relation=relation_display,
            lookup="",
            rollup="",
        )
        # сохраняем targets для второго прохода
        row._relation_targets = targets  # type: ignore[attr-defined]
        # также сохраним исходный ключ relation для корректной записи
        row._relation_key = _extract_relation_targets(fm, relation_field)[0]  # type: ignore[attr-defined]
        rows.append(row)

    # сортировка по имени по умолчанию до второго прохода (чтобы индекс стабилен)
    rows.sort(key=lambda r: r.name.lower())

    # второй проход: lookup / rollup через индекс
    idx = _build_index(rows)
    for r in rows:
        try:
            r.lookup = lookup_for_row(r, idx, lookup_field)
            r.lower_lookup = r.lookup.lower()
        except Exception:
            r.lookup = ""
            r.lower_lookup = ""
        try:
            r.rollup = rollup_for_row(r, idx, rollup_field, rollup_agg)
            r.lower_rollup = r.rollup.lower()
        except Exception:
            r.rollup = ""
            r.lower_rollup = ""
        # обновим lower_relation уже есть, но синхронизируем
        try:
            r.lower_relation = r.relation.lower()
        except Exception:
            pass
    return rows


def _update_frontmatter_field(path_str: str, field: str, value: str) -> bool:
    """Обновить одно поле frontmatter. Tags хранятся как list, relation как [[wikilink]] list."""
    path = Path(path_str)
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    fm, body = parse_frontmatter(text)
    if not isinstance(fm, dict):
        fm = {}
    # mapping колонки -> ключ frontmatter
    # relation ключ берём из текущего fm или дефолт
    # для lookup/rollup — readonly, не пишем
    if field in ("lookup", "rollup", "lower_lookup", "lower_rollup", "lower_relation"):
        return False
    key_map = {"name": "title", "status": "status", "tags": "tags", "date": "date", "relation": None}
    # relation: определить существующий ключ
    if field == "relation":
        # найдём ключ существующий или возьмём дефолт
        existing_key, _ = _extract_relation_targets(fm, None)
        # если нашли fallback поле с wikilinks — используем его,
        # иначе дефолтный relation
        fm_key = existing_key if existing_key in fm or existing_key == DEFAULT_RELATION_FIELD else DEFAULT_RELATION_FIELD
        # также уважать явное из settings? но у нас нет settings здесь — используем то что в файле
        # если field == relation и value пустая -> удаляем поле
        if not value.strip():
            fm.pop(fm_key, None)
            # также удалить алиасы если они пустые? не трогаем другие кандидаты
            for cand in RELATION_FIELD_CANDIDATES:
                if cand != fm_key and cand in fm:
                    # если там wikilink список — оставим? чистим только fm_key
                    pass
        else:
            # парсим ввод пользователя: "A, B" или "[[A]], [[B]]" -> targets
            targets = parse_relation_value(value)
            serialized = serialize_relation_value(targets)
            if not serialized:
                fm.pop(fm_key, None)
            else:
                fm[fm_key] = serialized
    elif field == "tags":
        fm_key = key_map.get(field, field)  # type: ignore[arg-type]
        # comma-separated -> list
        if not value.strip():
            fm.pop(fm_key, None)
        else:
            parts = [p.strip().lstrip("#") for p in value.split(",")]
            parts = [p for p in parts if p]
            fm[fm_key] = parts
    elif field == "name":
        fm_key = key_map.get(field, field)  # type: ignore[arg-type]
        if not value.strip():
            fm.pop(fm_key, None)
        else:
            fm[fm_key] = value.strip()
    elif field in ("status", "date"):
        fm_key = key_map.get(field, field)  # type: ignore[arg-type]
        if not value.strip():
            fm.pop(fm_key, None)
        else:
            fm[fm_key] = value.strip()
    else:
        fm_key = key_map.get(field, field)  # type: ignore[arg-type]
        if fm_key is None:
            fm_key = field
        # generic: пустая строка -> удалить
        if not value.strip():
            fm.pop(fm_key, None)
        else:
            fm[fm_key] = value.strip()
    content = serialize_frontmatter(fm) + body.lstrip("\n")
    try:
        path.write_text(content, encoding="utf-8")
    except OSError:
        return False
    # инвалидируем vault кэш
    try:
        from ..services import vault as svc

        svc.invalidate_vault_cache()
    except Exception:
        pass
    return True


# ── ColumnView factory helpers ─────────────────────────────────────────


def _make_entry_factory(prop: str, placeholder: str = ""):
    """Factory с Gtk.Entry для inline редактирования."""
    factory = Gtk.SignalListItemFactory()

    def on_setup(_fac, list_item: Gtk.ListItem) -> None:
        entry = Gtk.Entry(placeholder_text=placeholder, css_classes=["flat", "database-cell"])
        try:
            entry.set_has_frame(False)
        except Exception:
            pass
        entry.set_hexpand(True)
        # tooltip для длинных значений
        list_item.set_child(entry)

    def on_bind(_fac, list_item: Gtk.ListItem) -> None:
        row: DatabaseRow = list_item.get_item()  # type: ignore[assignment]
        entry: Gtk.Entry = list_item.get_child()  # type: ignore[assignment]
        # снять старые хендлеры
        for attr in ("_db_handler_activate", "_db_handler_focus"):
            hid = getattr(entry, attr, None)
            if hid is not None:
                try:
                    entry.disconnect(hid)
                except Exception:
                    pass
                setattr(entry, attr, None)
        # снять focus controller если был
        if hasattr(entry, "_db_focus_ctrl"):
            try:
                entry.remove_controller(entry._db_focus_ctrl)  # type: ignore[attr-defined]
            except Exception:
                pass

        val = getattr(row, prop, "") or ""
        # блокируем notify чтобы не триггерить changed
        entry.set_text(val)
        entry.set_tooltip_text(val)

        # notify row -> entry (если извне поменяли)
        def on_row_notify(obj, pspec):
            if pspec.name == prop:
                new_val = getattr(obj, prop, "") or ""
                if entry.get_text() != new_val:
                    entry.set_text(new_val)
                    entry.set_tooltip_text(new_val)

        # храним id чтобы отписать при unbind? упростим — не отписываем, так как row живет долго
        try:
            row.connect(f"notify::{prop}", on_row_notify)
        except Exception:
            pass

        def commit(*_a):
            new_text = entry.get_text()
            cur = getattr(row, prop, "") or ""
            if new_text == cur:
                return
            # обновить GObject (для сортировки/фильтра)
            setattr(row, prop, new_text)
            # lower helper
            low_prop = f"lower_{prop}"
            if hasattr(row, low_prop):
                try:
                    setattr(row, low_prop, new_text.lower())
                except Exception:
                    pass
            # для relation также обновить внутренний _relation_targets
            if prop == "relation":
                try:
                    row._relation_targets = parse_relation_value(new_text)  # type: ignore[attr-defined]
                except Exception:
                    pass
            # запись в файл
            ok = _update_frontmatter_field(row.path_str, prop, new_text)
            if not ok:
                # revert? показать toast через root
                try:
                    win = entry.get_root()
                    if win is not None and hasattr(win, "toast_overlay"):
                        toast = Adw.Toast.new(f"не удалось сохранить: {Path(row.path_str).name}")
                        toast.set_timeout(3)
                        win.toast_overlay.add_toast(toast)  # type: ignore[attr-defined]
                except Exception:
                    pass

        hid1 = entry.connect("activate", commit)
        entry._db_handler_activate = hid1  # type: ignore[attr-defined]

        focus = Gtk.EventControllerFocus.new()

        def on_leave(_c):
            # commit on focus out if changed
            commit()

        focus.connect("leave", on_leave)
        entry.add_controller(focus)
        entry._db_focus_ctrl = focus  # type: ignore[attr-defined]

    factory.connect("setup", on_setup)
    factory.connect("bind", on_bind)
    return factory


def _make_label_factory(prop: str):
    """Readonly factory (fallback) — Label."""
    factory = Gtk.SignalListItemFactory()

    def on_setup(_fac, list_item: Gtk.ListItem) -> None:
        lbl = Gtk.Label(halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["database-cell"])
        lbl.set_hexpand(True)
        list_item.set_child(lbl)

    def on_bind(_fac, list_item: Gtk.ListItem) -> None:
        row: DatabaseRow = list_item.get_item()  # type: ignore[assignment]
        lbl: Gtk.Label = list_item.get_child()  # type: ignore[assignment]
        val = getattr(row, prop, "") or ""
        lbl.set_text(val)
        lbl.set_tooltip_text(val)
        # notify update
        def on_row_notify(obj, pspec):
            if pspec.name == prop:
                new_val = getattr(obj, prop, "") or ""
                lbl.set_text(new_val)
                lbl.set_tooltip_text(new_val)
        try:
            row.connect(f"notify::{prop}", on_row_notify)
        except Exception:
            pass

    factory.connect("setup", on_setup)
    factory.connect("bind", on_bind)
    return factory


# ── Основная вьюха ─────────────────────────────────────────────────────


class DatabaseView(Gtk.Box):
    """Вкладка База данных: ColumnView + фильтр + сортировка + inline редактирование + relations."""

    def __init__(self, settings: dict, on_open=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self.on_open = on_open
        self._filter_text: str = ""
        self._status_filter: str = ""  # пусто = все
        self._alive = True
        self.connect("destroy", self._on_destroy)
        self._build_ui()
        self.reload()

    def _on_destroy(self, _w) -> None:
        self._alive = False

    # ── UI ──────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.append(view_header("🗄️", "База данных", "Notion-like таблица из vault frontmatter — сортировка по колонке, фильтр, inline редактирование + relation/lookup/rollup"))

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["toolbar", "database-toolbar"])
        toolbar.set_margin_start(14)
        toolbar.set_margin_end(14)

        self.filter_entry = Gtk.SearchEntry(placeholder_text="Фильтр по Name / Status / Tags / Date / Relation…", hexpand=True)
        self.filter_entry.connect("search-changed", self._on_filter_changed)
        toolbar.append(self.filter_entry)

        # dropdown статуса
        self.status_drop = Gtk.DropDown.new_from_strings(["Все статусы", "todo", "doing", "done", "— пусто —"])
        self.status_drop.set_selected(0)
        self.status_drop.set_tooltip_text("Фильтр по статусу")
        self.status_drop.connect("notify::selected", self._on_status_filter_changed)
        toolbar.append(self.status_drop)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Обновить таблицу")
        refresh.connect("clicked", lambda *_: self.reload(force=True))
        toolbar.append(refresh)

        self.count_label = Gtk.Label(label="", css_classes=["dim-hint", "database-count"])
        toolbar.append(self.count_label)

        self.append(toolbar)

        # ── ColumnView ──────────────────────────────────────────────
        self._store = Gio.ListStore(item_type=DatabaseRow)
        self._filter = Gtk.CustomFilter.new(self._filter_func)
        self._filter_model = Gtk.FilterListModel(model=self._store, filter=self._filter)

        # ColumnView создается до SortListModel чтобы взять его sorter
        self.column_view = Gtk.ColumnView(css_classes=["database-view"])
        self.column_view.set_show_column_separators(True)
        self.column_view.set_show_row_separators(True)
        try:
            self.column_view.set_reorderable(False)
        except Exception:
            pass

        # SortListModel с sorter от ColumnView
        try:
            sorter = self.column_view.get_sorter()
        except Exception:
            sorter = None
        if sorter is not None:
            self._sort_model = Gtk.SortListModel(model=self._filter_model, sorter=sorter)
        else:
            # fallback: без сортировки ColumnView
            self._sort_model = Gtk.SortListModel(model=self._filter_model, sorter=None)  # type: ignore[arg-type]

        self._selection = Gtk.SingleSelection(model=self._sort_model)
        self._selection.set_can_unselect(True)
        self._selection.set_autoselect(False)
        self.column_view.set_model(self._selection)

        # обработка открытия по двойному клику / activate
        try:
            self.column_view.connect("activate", self._on_activate)
        except Exception:
            pass

        # Колонки: Name (expand), Status, Tags (expand), Date, Relation (expand, editable), Lookup (readonly), Rollup (readonly)
        lookup_field = _get_lookup_field(self.settings)
        rollup_field = _get_rollup_field(self.settings)
        rollup_agg = _get_rollup_agg(self.settings)
        cols: list[tuple[str, str, str, bool, int, bool]] = [
            ("Name", "name", "Название из frontmatter title", True, 220, True),
            ("Status", "status", "Статус", False, 120, True),
            ("Tags", "tags", "Теги (comma)", True, 170, True),
            ("Date", "date", "Дата", False, 110, True),
            ("Relation", "relation", "Связи — [[wikilink]] в frontmatter relation (comma для нескольких)", True, 200, True),
            (f"Lookup:{lookup_field}", "lookup", f"Lookup — поле '{lookup_field}' из связанных записей", True, 180, False),
            (f"Rollup:{rollup_agg}({rollup_field})", "rollup", f"Rollup {rollup_agg} по полю '{rollup_field}' связанных записей", False, 130, False),
        ]
        for title, prop, tip, expand, w, editable in cols:
            if editable:
                factory = _make_entry_factory(prop, placeholder=title)
            else:
                factory = _make_label_factory(prop)
            col = Gtk.ColumnViewColumn(title=title, factory=factory)
            col.set_resizable(True)
            col.set_expand(expand)
            if w:
                try:
                    col.set_fixed_width(w)
                except Exception:
                    pass
            col.set_tooltip_text(tip) if hasattr(col, "set_tooltip_text") else None
            # сортировка через StringSorter + PropertyExpression
            try:
                expr = Gtk.PropertyExpression.new(DatabaseRow, None, prop)
                sorter_col = Gtk.StringSorter.new(expr)
                col.set_sorter(sorter_col)
            except Exception:
                # fallback: CustomSorter
                try:
                    def _make_sorter(p=prop):
                        def _sort(a, b, _data):
                            av = getattr(a, p, "") or ""
                            bv = getattr(b, p, "") or ""
                            avl, bvl = av.lower(), bv.lower()
                            if avl < bvl:
                                return Gtk.Ordering.SMALLER
                            if avl > bvl:
                                return Gtk.Ordering.LARGER
                            return Gtk.Ordering.EQUAL

                        return Gtk.CustomSorter.new(_sort)

                    col.set_sorter(_make_sorter())
                except Exception:
                    pass
            self.column_view.append_column(col)

        # фиксируем колонку пути (read-only, узкая, для отладки/копирования)
        try:
            path_factory = _make_label_factory("path_str")
            path_col = Gtk.ColumnViewColumn(title="Файл", factory=path_factory)
            path_col.set_resizable(True)
            path_col.set_expand(False)
            try:
                path_col.set_fixed_width(200)
            except Exception:
                pass
            expr_p = Gtk.PropertyExpression.new(DatabaseRow, None, "path_str")
            path_col.set_sorter(Gtk.StringSorter.new(expr_p))
            self.column_view.append_column(path_col)
        except Exception:
            pass

        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True, css_classes=["database-scroller"])
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.column_view)
        try:
            scroller.set_propagate_natural_width(False)
        except AttributeError:
            pass
        self.append(scroller)

        # empty state
        self._empty = empty_state(
            "🗄️",
            "База пуста — нет md файлов с frontmatter",
            hint="Создайте заметку с frontmatter: title, status, tags, date, relation: [[...]]",
            action_label="Обновить",
            on_action=lambda: self.reload(force=True),
        )
        self._empty.set_visible(False)
        self.append(self._empty)

    # ── фильтр ──────────────────────────────────────────────────────
    def _filter_func(self, item: GObject.Object) -> bool:
        row: DatabaseRow = item  # type: ignore[assignment]
        # статус фильтр
        sf = self._status_filter
        if sf:
            if sf == "__empty__":
                if (row.status or "").strip():
                    return False
            elif row.lower_status != sf.lower():
                # также поддержим contains для частичных статусов
                if sf.lower() not in row.lower_status:
                    return False
        q = self._filter_text.strip().lower()
        if not q:
            return True
        # поиск по всем полям + пути + relation/lookup/rollup
        hay = f"{row.lower_name} {row.lower_status} {row.lower_tags} {row.lower_date} {row.lower_relation} {row.lower_lookup} {row.lower_rollup} {row.path_str.lower()}"
        # поддержка #тег префикса
        if q.startswith("#"):
            q = q[1:].strip()
        return q in hay

    def _on_filter_changed(self, *_a) -> None:
        self._filter_text = self.filter_entry.get_text()
        # debounce 120ms через idle
        if hasattr(self, "_filter_timer") and self._filter_timer is not None:
            try:
                GLib.source_remove(self._filter_timer)
            except Exception:
                pass
        self._filter_timer = GLib.timeout_add(120, self._do_filter)

    def _do_filter(self) -> bool:
        self._filter_timer = None
        try:
            self._filter.changed(Gtk.FilterChange.DIFFERENT)
        except Exception:
            pass
        self._update_count()
        return False

    def _on_status_filter_changed(self, drop, _pspec) -> None:
        sel = drop.get_selected()
        mapping = {0: "", 1: "todo", 2: "doing", 3: "done", 4: "__empty__"}
        self._status_filter = mapping.get(int(sel), "")
        try:
            self._filter.changed(Gtk.FilterChange.DIFFERENT)
        except Exception:
            pass
        self._update_count()

    def _update_count(self) -> None:
        try:
            total = self._store.get_n_items()
            shown = self._filter_model.get_n_items()
            # SortListModel может давать столько же как filter, но считаем filter
            self.count_label.set_text(f"{shown}/{total}")
            has = total > 0
            self._empty.set_visible(not has)
            # ColumnView виден всегда если есть данные, иначе можно скрыть
            # оставляем видимым даже при 0 чтобы показать header
        except Exception:
            pass

    # ── activate (открыть файл) ───────────────────────────────────
    def _on_activate(self, _view, pos: int) -> None:
        try:
            # pos — индекс в SortListModel через selection
            item = self._selection.get_item(pos)
            if item is None:
                # пробуем selected item
                item = self._selection.get_selected_item()
            if item is None:
                return
            row: DatabaseRow = item  # type: ignore[assignment]
            if self.on_open is not None:
                self.on_open(row.path_str)
            else:
                # fallback xdg-open
                try:
                    Gio.AppInfo.launch_default_for_uri(Path(row.path_str).as_uri(), None)
                except Exception:
                    pass
        except Exception:
            pass

    def focus_filter(self) -> bool:
        """Для глобального поиска: фокус на фильтр."""
        try:
            self.filter_entry.grab_focus()
            self.filter_entry.select_region(0, -1)
            return True
        except Exception:
            return False

    # ── reload ──────────────────────────────────────────────────────
    def reload(self, force: bool = False) -> None:
        if force:
            try:
                from ..services import vault as svc

                svc.invalidate_vault_cache()
            except Exception:
                pass
        settings = dict(self.settings)

        def work() -> None:
            rows = scan_database(settings)
            GLib.idle_add(self._apply_rows, rows)

        threading.Thread(target=work, daemon=True).start()

    def _apply_rows(self, rows: list[DatabaseRow]) -> bool:
        if not self._alive:
            return False
        try:
            self._store.remove_all()
            for r in rows:
                self._store.append(r)
            self._update_count()
            # сброс сортировки к имени по умолчанию?
            # оставляем текущий ColumnView sorter
        except Exception:
            pass
        return False

    # для app.py refresh_all — alias
    def refresh(self) -> None:
        self.reload()
