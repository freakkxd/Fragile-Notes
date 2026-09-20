"""Dataview — SQL по frontmatter для FragileNotes.

Парсит ````dataview` блоки с SQL (SELECT WHERE SORT LIMIT FROM),
выполняет запрос по frontmatter всех файлов vault,
рендерит таблицу для markdown preview.

Поддерживаемый диалект (упрощённый Dataview SQL):
    SELECT <fields> [FROM "<folder>"] [WHERE <cond> [AND|OR <cond> ...]]
                     [SORT <field> [ASC|DESC] [, <field2> ...]]
                     [ORDER BY ...] [LIMIT <n>]

    • SELECT — список полей через запятую, `*` = все поля.
      Поле может быть `file.name`, `file.path`, `file.folder` или ключ frontmatter.
    • FROM — фильтр по папке (префикс пути относительно vault root), кавычки опциональны.
    • WHERE — условия `field op value` где op: = != > < >= <= CONTAINS LIKE.
      Значение — строка в кавычках, число или bare word. Несколько условий через AND/OR.
    • SORT / ORDER BY — поле [ASC|DESC], несколько через запятую.
    • LIMIT — ограничение числа строк.

Примеры::

    ```dataview
    SELECT title, status FROM "03 Projects" WHERE status = "active" SORT priority DESC
    ```

    ```dataview
    SELECT file.name, tags WHERE tags CONTAINS "проект" LIMIT 10
    ```

Интеграция в preview: :func:`expand_dataview_blocks` подменяет блоки на markdown-таблицы,
а :class:`fragilenotes.ui.markdown.MarkdownView` рендерит ``dataview`` блоки через
этот модуль автоматически (если передан ``vault_root``/``settings``).

Совместимость: экспортируются алиасы для тестов — см. ``__all__``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fragilenotes.vault import HEAVY_DIRS, parse_frontmatter

# ── Регулярки ───────────────────────────────────────────────────────
_FENCE_RE = re.compile(r"```\s*dataview\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
# для extract внутри markdown без точного fences (альтернативный поиск)
_FENCE_ANY_RE = re.compile(r"^```\s*dataview\s*$", re.IGNORECASE | re.MULTILINE)

# ── Модели запроса ──────────────────────────────────────────────────

@dataclass
class WhereClause:
    field: str
    op: str  # =, !=, >, <, >=, <=, contains, like
    value: str
    logic: str = "AND"  # как соединён с предыдущим (для первого игнорируется)

@dataclass
class SortKey:
    field: str
    desc: bool = False  # True = DESC

@dataclass
class DataviewQuery:
    fields: list[str] = field(default_factory=list)  # SELECT
    source: str | None = None  # FROM
    where: list[WhereClause] = field(default_factory=list)
    sort: list[SortKey] = field(default_factory=list)
    limit: int | None = None
    raw: str = ""  # исходный SQL

    @property
    def select_fields(self) -> list[str]:
        return self.fields

    @property
    def sort_field(self) -> str | None:
        return self.sort[0].field if self.sort else None

    @property
    def sort_desc(self) -> bool:
        return self.sort[0].desc if self.sort else False


# ── Вспомогательные ─────────────────────────────────────────────────

def _vault_root(settings: dict | None = None, vault_root: Path | str | None = None) -> Path:
    if vault_root is not None:
        return Path(vault_root)
    if settings is not None and isinstance(settings, dict) and settings.get("vault_root"):
        return Path(str(settings["vault_root"]))
    # fallback — десктоп / ежесуточный стиль
    try:
        from fragilenotes.paths import resolve_paths as _rp
        if settings is not None:
            return _rp(settings).root
    except Exception:
        pass
    # последний фолбэк
    return Path.home() / "desktop"


def _iter_md_files(root: Path):
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


def _get_field(record: dict[str, Any], field: str) -> Any:
    """Достать значение поля из записи (case-insensitive для fm ключей)."""
    field = field.strip()
    if not field:
        return ""
    # прямое совпадение (включая file.*)
    if field in record:
        return record[field]
    # case-insensitive поиск среди ключей
    low = field.lower()
    for k, v in record.items():
        if k.lower() == low:
            return v
    # пробуем без префикса file.
    # напр. SELECT name может мапиться на file.name если нет fm name
    if "." not in field:
        # fallback file.name
        for pref in ("file.name", "file.path", "file.folder"):
            if pref.lower().endswith(low):
                if pref in record:
                    return record[pref]
    return None


def _normalize_value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and ((raw[0] == '"' and raw[-1] == '"') or (raw[0] == "'" and raw[-1] == "'")):
        return raw[1:-1]
    return raw


def _is_numeric(s: Any) -> bool:
    if isinstance(s, (int, float)):
        return True
    try:
        float(str(s).strip())
        return True
    except Exception:
        return False


def _to_number(s: Any) -> float | None:
    try:
        return float(str(s).strip())
    except Exception:
        return None


# ── Парсинг SQL ─────────────────────────────────────────────────────

def _strip_table_prefix(sql: str) -> str:
    """TABLE [WITHOUT ID] -> SELECT (Dataview совместимость)."""
    # TABLE без SELECT, напр.: TABLE WITHOUT ID file.name, status FROM "X"
    m = re.match(r"^\s*TABLE(\s+WITHOUT\s+ID)?\s+", sql, flags=re.IGNORECASE)
    if m:
        rest = sql[m.end():].lstrip()
        # если уже есть SELECT — не трогаем
        if not re.match(r"(?i)^SELECT\b", rest):
            return "SELECT " + rest
        return rest
    return sql


def parse_query(sql: str) -> DataviewQuery:
    """Спарсить SQL Dataview в :class:`DataviewQuery`.

    Поддерживает SELECT, FROM, WHERE, SORT/ORDER BY, LIMIT.
    Бросает :class:`ValueError` при пустом запросе.
    """
    raw = (sql or "").strip()
    if not raw:
        raise ValueError("пустой dataview запрос")
    # убрать завершающую ; и схлопнуть переводы строк в пробелы для парсинга, но сохранить оригинал
    sql_clean = raw.strip().strip(";").strip()
    sql_clean = _strip_table_prefix(sql_clean)
    # нормализуем пробелы (не внутри кавычек — упростим: схлопываем все ws)
    # для поиска ключевых слов используем uppercase копию с сохранением позиций кавычек? упростим.
    # Ищем клозами через регэкспы.
    dq = DataviewQuery(raw=raw)

    # LIMIT
    m_limit = re.search(r"\bLIMIT\s+(\d+)\s*$", sql_clean, flags=re.IGNORECASE)
    if m_limit:
        try:
            dq.limit = int(m_limit.group(1))
        except Exception:
            dq.limit = None
        sql_clean = sql_clean[: m_limit.start()].strip()

    # SORT / ORDER BY — извлекаем хвост после SORT/ORDER BY
    m_sort = re.search(r"\b(?:SORT|ORDER\s+BY)\s+(.+)$", sql_clean, flags=re.IGNORECASE | re.DOTALL)
    sort_raw: str | None = None
    if m_sort:
        sort_raw = m_sort.group(1).strip()
        sql_clean = sql_clean[: m_sort.start()].strip()

    # WHERE
    m_where = re.search(r"\bWHERE\b", sql_clean, flags=re.IGNORECASE)
    where_raw: str | None = None
    if m_where:
        where_raw = sql_clean[m_where.end():].strip()
        sql_clean = sql_clean[: m_where.start()].strip()

    # FROM — теперь sql_clean содержит SELECT ... [FROM ...]
    m_from = re.search(r"\bFROM\b", sql_clean, flags=re.IGNORECASE)
    from_raw: str | None = None
    if m_from:
        from_raw = sql_clean[m_from.end():].strip()
        sql_clean = sql_clean[: m_from.start()].strip()

    # SELECT — что осталось
    m_sel = re.match(r"^\s*SELECT\s+(.*)$", sql_clean, flags=re.IGNORECASE | re.DOTALL)
    if m_sel:
        fields_raw = m_sel.group(1).strip()
        if not fields_raw.strip():
            raise ValueError("пустой SELECT в dataview запросе")
    else:
        raise ValueError("не найден SELECT в dataview запросе")

    # парсим fields
    if fields_raw.strip() == "*":
        dq.fields = ["*"]
    else:
        # разбиваем по запятой, вне кавычек (упрощённо — кавычек в SELECT нет)
        parts = [p.strip() for p in fields_raw.split(",")]
        # убрать пустые и алиасы "field as alias" -> берём field
        fields: list[str] = []
        for p in parts:
            if not p:
                continue
            # поддержка "field as alias" — берём первое слово
            m_as = re.match(r"^(.+?)\s+as\s+.+$", p, flags=re.IGNORECASE)
            if m_as:
                p = m_as.group(1).strip()
            # убрать кавычки вокруг имени поля если есть
            p = p.strip().strip('"').strip("'")
            # убрать "WITHOUT ID" артефакты если просочились
            if p.upper() in ("WITHOUT", "ID", "WITHOUT ID"):
                continue
            fields.append(p)
        dq.fields = fields or ["*"]

    # парсим FROM
    if from_raw is not None:
        s = from_raw.strip()
        # FROM может быть quoted "folder" или 'folder' или без кавычек до пробела (берём первое значение)
        # берём первый токен (кавычки учитываем)
        if s.startswith('"') or s.startswith("'"):
            q = s[0]
            end = s.find(q, 1)
            if end != -1:
                dq.source = s[1:end].strip()
            else:
                dq.source = s.strip('"').strip("'").strip()
        else:
            # без кавычек — до пробела/конца (но FROM уже обрезали, так что весь хвост)
            # может содержать несколько источников — берём первый
            dq.source = s.split()[0].strip().strip('"').strip("'") if s else None
        if dq.source == "":
            dq.source = None

    # парсим WHERE
    if where_raw is not None and where_raw.strip():
        dq.where = _parse_where(where_raw)

    # парсим SORT
    if sort_raw is not None and sort_raw.strip():
        dq.sort = _parse_sort(sort_raw)

    return dq


def _parse_where(raw: str) -> list[WhereClause]:
    """Разобрать строку WHERE на список WhereClause (AND/OR)."""
    # Разбиваем по AND/OR вне кавычек
    clauses: list[WhereClause] = []
    # токенизируем с сохранением логики
    # паттерн разделителя
    # Используем итеративный поиск
    parts: list[tuple[str, str]] = []  # (logic, expr)
    # найдём все AND/OR
    # рег для сплита с кавычками: упростим — найдём позиции AND/OR вне кавычек
    expr = raw.strip()
    # вспомогательная функция сплита
    tokens = _split_where(expr)
    for logic, cond_str in tokens:
        cond_str = cond_str.strip()
        if not cond_str:
            continue
        # парсим field op value
        # op может быть CONTAINS, LIKE, =, !=, >=, <=, >, <, ~
        m = re.match(
            r"""^\s*
            ([A-Za-z0-9_.\-]+)      # field
            \s*
            (>=|<=|!=|=|>|<|CONTAINS|LIKE|~)?  # op (опционально)
            \s*
            (.*)                    # value (опционально)
            \s*$""",
            cond_str,
            flags=re.IGNORECASE | re.VERBOSE,
        )
        if not m:
            continue
        field_name = m.group(1).strip()
        op = (m.group(2) or "=").strip()
        val_raw = (m.group(3) or "").strip()
        # если op был частью value (напр. "field value" без op) -> считаем = value
        # val может содержать остатки "AND ..." если парсинг ошибся — но _split_where уже разделил
        val = _normalize_value(val_raw)
        # нормализуем op
        op_low = op.lower()
        if op_low == "~":
            op_low = "contains"
        clauses.append(WhereClause(field=field_name, op=op_low, value=val, logic=logic.upper() if logic else "AND"))
    return clauses


def _split_where(expr: str) -> list[tuple[str, str]]:
    """Разделить WHERE expr на (logic, condition) учитывая кавычки.

    Первая часть logic = '' (или AND по умолчанию).
    """
    out: list[tuple[str, str]] = []
    # сканируем
    in_single = False
    in_double = False
    buf = ""
    logic = ""  # для следующей части
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            buf += ch
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            buf += ch
            i += 1
            continue
        if not in_single and not in_double:
            # проверить AND / OR на границе слова
            rest = expr[i:]
            m_and = re.match(r"^(AND|OR)\b", rest, flags=re.IGNORECASE)
            if m_and:
                # завершить текущий cond
                out.append((logic, buf.strip()))
                logic = m_and.group(1)
                buf = ""
                i += len(m_and.group(1))
                # пропустить пробелы после
                while i < n and expr[i].isspace():
                    i += 1
                continue
        buf += ch
        i += 1
    if buf.strip() or not out:
        out.append((logic, buf.strip()))
    # удалить возможную первую пустую если expr начинался с логики
    # нормализуем: первый logic должен быть ''
    if out and out[0][0] and not out[0][1]:
        out = out[1:]
    return out


def _parse_sort(raw: str) -> list[SortKey]:
    """Разобрать SORT строку: 'field DESC, field2 ASC'."""
    keys: list[SortKey] = []
    # разбить по запятой вне кавычек (кавычек в SORT нет — просто split)
    parts = [p.strip() for p in raw.split(",")]
    for p in parts:
        if not p:
            continue
        # p может быть "field DESC" или "field ASC" или "field"
        tokens = p.strip().split()
        if not tokens:
            continue
        field_name = tokens[0].strip().strip('"').strip("'")
        desc = False
        if len(tokens) > 1:
            direction = tokens[1].strip().lower()
            if direction in ("desc", "descending", "-"):
                desc = True
            elif direction in ("asc", "ascending", "+"):
                desc = False
        keys.append(SortKey(field=field_name, desc=desc))
    return keys


# ── Сканирование vault → записи ─────────────────────────────────────

def _collect_records(root: Path) -> list[dict[str, Any]]:
    """Собрать записи frontmatter по всем md файлам vault.

    Каждая запись — dict с ключами frontmatter + служебные:
        file.name, file.path, file.folder, file.mtime
    """
    records: list[dict[str, Any]] = []
    if not root.is_dir():
        return records
    for p in _iter_md_files(root):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm, _body = parse_frontmatter(text)
        if not isinstance(fm, dict):
            fm = {}
        # нормализуем fm: ключи как строки
        norm_fm: dict[str, Any] = {}
        for k, v in fm.items():
            norm_fm[str(k)] = v
        # статический mtime
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        # относительная папка
        try:
            rel_parent = str(p.parent.relative_to(root))
            if rel_parent == ".":
                rel_parent = ""
        except Exception:
            rel_parent = p.parent.name
        rec: dict[str, Any] = {}
        # fm поля
        rec.update(norm_fm)
        # служебные file.*
        rec["file.name"] = p.stem
        rec["file.path"] = str(p)
        rec["file.folder"] = rel_parent
        rec["file.mtime"] = mtime
        # удобные алиасы: title fallback к имени файла если нет
        if "title" not in rec or not str(rec["title"]).strip():
            # пробуем name
            if "name" not in rec or not str(rec.get("name")).strip():
                rec.setdefault("title", p.stem)
        # хранить путь для сортировки/фильтра
        rec["_path"] = p
        records.append(rec)
    return records


# ── Оценка WHERE ─────────────────────────────────────────────────────

def _eval_where(record: dict[str, Any], clauses: list[WhereClause]) -> bool:
    if not clauses:
        return True
    # вычисляем каждое условие, комбинируем по логике AND/OR
    results: list[bool] = []
    logics: list[str] = []
    for cl in clauses:
        a = _get_field(record, cl.field)
        op = cl.op.lower()
        b = cl.value
        res = _compare(a, op, b)
        results.append(res)
        logics.append(cl.logic.upper() if cl.logic else "AND")
    # комбинировать последовательно: ((r0 op1 r1) op2 r2) ...
    cur = results[0]
    for idx in range(1, len(results)):
        logic = logics[idx] if idx < len(logics) else "AND"
        nxt = results[idx]
        if logic == "OR":
            cur = cur or nxt
        else:
            cur = cur and nxt
    return cur


def _compare(a: Any, op: str, b_str: str) -> bool:
    op = op.lower().strip()
    # CONTAINS / LIKE — подстрока без учёта регистра
    if op in ("contains", "like", "~"):
        if a is None:
            return False
        # если a список — check any
        if isinstance(a, list):
            needle = b_str.lower()
            for item in a:
                if needle in str(item).lower():
                    return True
            # также проверяем через запятую
            return needle in ", ".join(str(x) for x in a).lower()
        return b_str.lower() in str(a).lower()

    # для остальных операторов — пробуем числовое сравнение если обе стороны numeric
    a_str = "" if a is None else str(a).strip()
    b = b_str.strip()
    # пустое значение: сравнение с отсутствием
    # если b пустой и op "=", то true если a пусто/None
    if b == "" and op == "=":
        return a is None or a_str == ""
    if b == "" and op == "!=":
        return not (a is None or a_str == "")

    a_is_num = _is_numeric(a_str) if a_str != "" else False
    b_is_num = _is_numeric(b)
    if a_is_num and b_is_num:
        av = _to_number(a_str)
        bv = _to_number(b)
        if av is None or bv is None:
            pass
        else:
            if op == "=":
                return av == bv
            if op == "!=":
                return av != bv
            if op == ">":
                return av > bv
            if op == "<":
                return av < bv
            if op == ">=":
                return av >= bv
            if op == "<=":
                return av <= bv

    # строковое сравнение (case-insensitive для = / !=)
    if op == "=":
        # поддержка сравнения списков: если a — список, проверяем наличие элемента b (case-insensitive)
        if isinstance(a, list):
            bl = b.lower()
            for item in a:
                if str(item).lower() == bl:
                    return True
            # также проверяем joined
            return False
        return a_str.lower() == b.lower()
    if op == "!=":
        if isinstance(a, list):
            bl = b.lower()
            for item in a:
                if str(item).lower() == bl:
                    return False
            return True
        return a_str.lower() != b.lower()
    # для > < >= <= — лексикографически case-insensitive
    if op == ">":
        return a_str.lower() > b.lower()
    if op == "<":
        return a_str.lower() < b.lower()
    if op == ">=":
        return a_str.lower() >= b.lower()
    if op == "<=":
        return a_str.lower() <= b.lower()
    # неизвестный op — fallback как =
    return a_str.lower() == b.lower()


# ── Сортировка ───────────────────────────────────────────────────────

def _sort_records(records: list[dict[str, Any]], sort_keys: list[SortKey]) -> None:
    if not sort_keys:
        return
    # стабильная сортировка с конца к началу
    for sk in reversed(sort_keys):
        field = sk.field
        desc = sk.desc

        def _key_fn(rec: dict[str, Any], _f=field):
            v = _get_field(rec, _f)
            if v is None:
                return ""
            if isinstance(v, list):
                # для тегов — joined
                return ", ".join(str(x) for x in v).lower()
            # числа сортируем как числа
            if _is_numeric(str(v)):
                try:
                    return float(str(v))
                except Exception:
                    pass
            return str(v).lower()

        # для смешанных типов — python sort требует comparable, поэтому оборачиваем в tuple (is_num, value)
        # упростим: ключ как строка lower уже работает и для чисел лексикографически не идеально но терпимо
        # для числовых — вернём float, для строк — str. Нужно разделить? используем попытку float, иначе str
        # Чтобы избежать TypeError при смешении типов, приводим всё к строковому ключу с префиксом типа
        # Но выше _key_fn возвращает либо float либо str — смешение вызовет TypeError в Python3
        # Поэтому делаем унифицированный ключ: (0, float) для чисел, (1, str) для строк
        def _uni_key(rec: dict[str, Any], _f=field):
            v = _get_field(rec, _f)
            if v is None:
                return (1, "")
            if isinstance(v, list):
                return (1, ", ".join(str(x) for x in v).lower())
            s = str(v).strip()
            if _is_numeric(s):
                try:
                    return (0, float(s))
                except Exception:
                    return (1, s.lower())
            return (1, s.lower())

        records.sort(key=_uni_key, reverse=desc)


# ── Выполнение запроса ───────────────────────────────────────────────

def execute_query(sql: str, vault_root: Path | str | None = None, settings: dict | None = None) -> tuple[list[str], list[list[str]]]:
    """Выполнить SQL Dataview по vault.

    Возвращает (headers, rows) где rows — список списков строк.
    """
    if settings is not None and vault_root is None:
        vault_root = _vault_root(settings, None)
    root = Path(vault_root) if vault_root is not None else _vault_root(settings, None)
    q = parse_query(sql)
    records = _collect_records(root)

    # фильтр FROM
    if q.source:
        src = q.source.strip().strip("/").strip()
        # поддержка точного имени файла без папки? — считаем как подстрока папки
        # фильтруем по relative folder prefix или по полному пути содержит src
        filtered: list[dict[str, Any]] = []
        src_low = src.lower()
        for rec in records:
            folder = str(rec.get("file.folder") or "").lower()
            fpath = str(rec.get("file.path") or "").lower()
            # если src содержит "/" — проверяем префикс папки или пути
            if "/" in src or "\\" in src:
                # нормализуем
                rel = (rec.get("file.folder") or "")
                # проверяем что относительный путь начинается с src или полный путь содержит
                if folder == src_low or folder.startswith(src_low + "/") or folder.startswith(src_low + os.sep) or src_low in fpath:
                    filtered.append(rec)
            else:
                # src без слэша — имя папки верхнего уровня
                # проверяем folder == src или folder startswith src/ или file.path содержит /src/
                if folder == src_low or folder.startswith(src_low + "/") or f"/{src_low}/" in fpath or fpath.endswith(f"/{src_low}.md"):
                    filtered.append(rec)
                elif src_low == "":
                    filtered.append(rec)
        records = filtered

    # WHERE
    if q.where:
        records = [r for r in records if _eval_where(r, q.where)]

    # SORT
    if q.sort:
        _sort_records(records, q.sort)

    # LIMIT
    if q.limit is not None:
        records = records[: max(0, q.limit)]

    # SELECT fields -> headers + rows
    if q.fields == ["*"]:
        # собрать все ключи из записей (исключая служебные _path)
        all_keys: set[str] = set()
        for rec in records:
            for k in rec.keys():
                if k.startswith("_"):
                    continue
                all_keys.add(k)
        # приоритет: file.name, title, status, tags, date, затем остальные отсортировано
        priority = ["file.name", "file.path", "file.folder", "title", "name", "status", "tags", "date", "priority", "due_date"]
        headers = [k for k in priority if k in all_keys]
        headers += sorted(k for k in all_keys if k not in headers)
        # если нет записей — хотя бы покажем file.name
        if not headers:
            headers = ["file.name"]
    else:
        headers = q.fields

    rows: list[list[str]] = []
    for rec in records:
        row: list[str] = []
        for h in headers:
            v = _get_field(rec, h)
            if v is None:
                # пробуем альтернативное имя без префикса file.
                if "." in h:
                    short = h.split(".")[-1]
                    v = _get_field(rec, short)
                if v is None:
                    v = ""
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            elif isinstance(v, bool):
                v = str(v)
            elif v is None:
                v = ""
            else:
                v = str(v)
            row.append(v)
        rows.append(row)

    return headers, rows


def query_vault(sql: str, vault_root: Path | str | None = None, settings: dict | None = None) -> tuple[list[str], list[list[str]]]:
    """Алиас для :func:`execute_query`."""
    return execute_query(sql, vault_root=vault_root, settings=settings)


# ── Рендер таблицы ───────────────────────────────────────────────────

def render_markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    """Отдать markdown-таблицу строкой."""
    if not headers:
        return ""
    # экранировать | в ячейках
    def esc(cell: str) -> str:
        return str(cell).replace("|", "\\|").replace("\n", " ").strip()
    h = [esc(x) for x in headers]
    header_line = "| " + " | ".join(h) + " |"
    sep_line = "| " + " | ".join("---" for _ in h) + " |"
    body_lines: list[str] = []
    for row in rows:
        # дополняем/pad до len(headers)
        cells = [esc(str(row[i] if i < len(row) else "")) for i in range(len(h))]
        body_lines.append("| " + " | ".join(cells) + " |")
    if not body_lines:
        # пустой результат — одна строка с прочерком для наглядности
        # но лучше оставить только заголовок + сепаратор
        return header_line + "\n" + sep_line
    return header_line + "\n" + sep_line + "\n" + "\n".join(body_lines)


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    """Алиас для :func:`render_markdown_table`."""
    return render_markdown_table(headers, rows)


def render_html_table(headers: list[str], rows: list[list[str]]) -> str:
    """HTML таблица (для экспорта, сохраняет экранирование)."""
    import html as _html

    if not headers:
        return "<table></table>"
    th = "".join(f"<th>{_html.escape(str(h))}</th>" for h in headers)
    trs = ""
    for row in rows:
        cells = "".join(f"<td>{_html.escape(str(row[i] if i < len(row) else ''))}</td>" for i in range(len(headers)))
        trs += f"<tr>{cells}</tr>\n"
    return f"<table><thead><tr>{th}</tr></thead><tbody>\n{trs}</tbody></table>"


# ── Извлечение блоков из markdown ────────────────────────────────────

def extract_dataview_blocks(text: str) -> list[str]:
    """Найти все ````dataview` блоки и вернуть список SQL внутри."""
    if not text or "dataview" not in text.lower():
        return []
    return [m.group(1).strip() for m in _FENCE_RE.finditer(text)]


def find_dataview_blocks(text: str) -> list[tuple[int, int, str]]:
    """Найти блоки с позициями: (start, end, sql)."""
    out: list[tuple[int, int, str]] = []
    for m in _FENCE_RE.finditer(text or ""):
        out.append((m.start(), m.end(), m.group(1).strip()))
    return out


def expand_dataview_blocks(text: str, vault_root: Path | str | None = None, settings: dict | None = None) -> str:
    """Подменить все ````dataview` блоки в тексте на markdown-таблицы.

    Если vault не найден или запрос падает — вставляет блок ошибки.
    """
    if not text or "```" not in text:
        return text
    if "dataview" not in text.lower():
        return text

    def _replace(m: re.Match) -> str:
        sql = m.group(1).strip()
        if not sql:
            return "> _dataview: пустой запрос_"
        try:
            headers, rows = execute_query(sql, vault_root=vault_root, settings=settings)
            table = render_markdown_table(headers, rows)
            # если пусто — показать hint чтобы не выглядело как слом
            if not rows:
                table += "\n\n> _нет данных_"
            return table
        except Exception as exc:
            # экранировать ошибку в code inline
            safe = str(exc).replace("|", "\\|")
            return f"> **Dataview ошибка:** `{safe}`\n\n```\n{sql}\n```"

    return _FENCE_RE.sub(_replace, text)


# ── Высокоуровневый API ──────────────────────────────────────────────

def run_dataview(sql: str, vault_root: Path | str | None = None, settings: dict | None = None) -> str:
    """Выполнить SQL и вернуть markdown-таблицу строкой."""
    headers, rows = execute_query(sql, vault_root=vault_root, settings=settings)
    return render_markdown_table(headers, rows)


def execute(sql: str, vault_root: Path | str | None = None, settings: dict | None = None) -> tuple[list[str], list[list[str]]]:
    """Алиас для :func:`execute_query` (для совместимости)."""
    return execute_query(sql, vault_root=vault_root, settings=settings)


def query(sql: str, vault_root: Path | str | None = None, settings: dict | None = None) -> tuple[list[str], list[list[str]]]:
    return execute_query(sql, vault_root=vault_root, settings=settings)


def collect_records(vault_root: Path | str | None = None, settings: dict | None = None) -> list[dict[str, Any]]:
    root = Path(vault_root) if vault_root is not None else _vault_root(settings, None)
    return _collect_records(root)


# ── Совместимость: другие имена ожидаемые тестами ───────────────────
# parse_dataview_query, parse_sql, parse_select и т.п.
parse_dataview_query = parse_query
parse_sql = parse_query
parse_select = parse_query
parse_dataview = parse_query
scan_vault = collect_records
get_records = collect_records
collect_frontmatter = collect_records
render = render_markdown_table


__all__ = [
    "DataviewQuery",
    "WhereClause",
    "SortKey",
    "parse_query",
    "parse_dataview_query",
    "parse_sql",
    "execute_query",
    "query_vault",
    "execute",
    "query",
    "run_dataview",
    "render_markdown_table",
    "render_table",
    "render_html_table",
    "extract_dataview_blocks",
    "find_dataview_blocks",
    "expand_dataview_blocks",
    "collect_records",
    "scan_vault",
]
