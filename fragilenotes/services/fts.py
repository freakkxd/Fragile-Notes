"""SQLite FTS5 полнотекстовый поиск для vault (title + content).

Индекс в ``~/.cache/fragilenotes/fts.db`` (WAL), обновление при изменении файлов
(сравнение ``st_mtime_ns``), ранжирование BM25, подсветка через
``highlight()`` / ``snippet()`` FTS5.

API:
  get_db_path() -> Path
  ensure_index(settings, force=False)  — инкрементальный ребилд vault
  rebuild_index(settings)               — force rebuild
  upsert_file(path: Path)              — обновить один файл
  remove_file(path: Path)              — удалить из индекса
  search(settings, query, limit=50)    -> list[dict] с BM25 + подсветка
  search_notes(settings, query, limit) -> list[NoteHit] (совместимо с vault.search_notes)
  highlight_snippet(content, query)    -> fallback подсветка без БД
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

# ── константы ───────────────────────────────────────────────────────
DB_PATH = Path.home() / ".cache" / "fragilenotes" / "fts.db"
# веса BM25: path 1, title 5, content 1  — title важнее
_BM25_WEIGHTS = (1.0, 5.0, 1.0)

_lock = threading.RLock()

# ── helpers ─────────────────────────────────────────────────────────

def get_db_path() -> Path:
    """Путь к FTS БД — ``~/.cache/fragilenotes/fts.db``."""
    return DB_PATH


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10.0)
    # WAL для конкурентного чтения/записи
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    try:
        conn.execute("PRAGMA synchronous=NORMAL;")
    except Exception:
        pass
    try:
        conn.execute("PRAGMA temp_store=MEMORY;")
    except Exception:
        pass
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    # FTS5 таблица: path, title, content
    # porter + unicode61 для латиницы + кириллицы
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                path,
                title,
                content,
                tokenize='porter unicode61'
            )
            """
        )
    except sqlite3.OperationalError:
        # fallback без porter (если сборка без porter)
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                    path,
                    title,
                    content,
                    tokenize='unicode61'
                )
                """
            )
        except sqlite3.OperationalError:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                    path,
                    title,
                    content
                )
                """
            )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fts_meta (
            path TEXT PRIMARY KEY,
            mtime_ns INTEGER NOT NULL
        )
        """
    )
    conn.commit()


def _vault_root(settings: dict[str, Any]) -> Path:
    try:
        from ..paths import resolve_paths  # type: ignore
        return resolve_paths(settings).root
    except Exception:
        return Path(settings.get("vault_root", str(Path.home() / "desktop")))


def _iter_vault_files(root: Path):
    """Генератор (Path, mtime_ns) — как в services.vault._iter_notes, но только .md."""
    import os

    try:
        from ..vault import ALLOWED_EXTS, HEAVY_DIRS
    except Exception:
        ALLOWED_EXTS = {".md"}
        HEAVY_DIRS = {".git", "__pycache__", ".obsidian", "node_modules"}

    skip = {"_System", "05 Sort"}  # не пропускаем строго? оставляем все кроме HEAVY
    # но FTS должен индексировать весь vault; _System содержит шаблоны — можно но не критично
    # индексируем все .md кроме HEAVY
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    name = e.name
                    if name.startswith("."):
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if name in HEAVY_DIRS:
                                continue
                            # не пропускаем _System/05 Sort жёстко? индексируем, но можно
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            if e.name.lower().endswith(".md"):
                                try:
                                    st = e.stat()
                                except OSError:
                                    continue
                                yield Path(e.path), st.st_mtime_ns
                    except OSError:
                        continue
        except OSError:
            continue


def _note_title(path: Path) -> str:
    try:
        from .vault import note_title as _nt
        return _nt(path)
    except Exception:
        return path.stem


def _read_content(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _escape_fts_query(query: str) -> str:
    """Построить безопасный FTS5 MATCH запрос с prefix-*.

    Каждый токен оборачивается в кавычки и добавляется * для префикс-поиска.
    Токены — последовательности букв/цифр (включая кириллицу).
    Пустой результат -> экранированная исходная строка.
    """
    q = (query or "").strip()
    if not q:
        return '""'
    # извлечь слова (юникод буквы + цифры)
    tokens = re.findall(r"[\w\u0400-\u04FF]+", q, flags=re.UNICODE)
    if not tokens:
        # fallback: экранировать кавычки
        safe = q.replace('"', '""')
        return f'"{safe}"'
    # префикс-поиск: "token"*
    parts: list[str] = []
    for t in tokens:
        # ограничить длину токена чтобы не взорвать запрос
        t = t[:64]
        if not t:
            continue
        safe = t.replace('"', '""')
        parts.append(f'"{safe}"*')
    if not parts:
        safe = q.replace('"', '""')
        return f'"{safe}"'
    # AND через пробел (FTS5: пробел = AND; OR — явный)
    return " ".join(parts)


def _highlight_fallback(content: str, query: str, snippet_len: int = 120) -> str:
    """Простая подсветка <mark> без БД — для preview."""
    if not query or not content:
        return content[:snippet_len]
    q = query.strip()
    if not q:
        return content[:snippet_len]
    # найти первое вхождение case-insensitive
    low = content.lower()
    qlow = q.lower()
    idx = low.find(qlow)
    if idx == -1:
        # токен-поиск
        for tok in re.findall(r"[\w\u0400-\u04FF]+", q):
            idx = low.find(tok.lower())
            if idx != -1:
                q = tok
                qlow = tok.lower()
                break
    if idx == -1:
        return " ".join(content.split())[:snippet_len]
    start = max(0, idx - 60)
    end = min(len(content), idx + len(q) + 60)
    piece = content[start:end]
    # подсветка
    # экранируем? просто заменяем первое вхождение
    try:
        # case-insensitive replace первого
        pattern = re.compile(re.escape(q), re.IGNORECASE)
        piece = pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", piece, count=1)
    except Exception:
        pass
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    return prefix + " ".join(piece.split()) + suffix


# ── индексация ──────────────────────────────────────────────────────

def ensure_index(settings: dict[str, Any], force: bool = False) -> int:
    """Инкрементально обновить FTS индекс vault.

    Сравнивает mtime_ns из fts_meta с диском; удалённые — удаляет,
    изменённые — re-insert, новые — insert.
    Возвращает количество обновлённых документов.
    """
    with _lock:
        root = _vault_root(settings)
        if not root.is_dir():
            return 0
        conn = _connect()
        try:
            if force:
                try:
                    conn.execute("DELETE FROM notes_fts")
                    conn.execute("DELETE FROM fts_meta")
                    conn.commit()
                except Exception:
                    pass
            # текущее состояние на диске
            disk: dict[str, int] = {}
            disk_paths: dict[str, Path] = {}
            for p, mt in _iter_vault_files(root):
                s = str(p)
                disk[s] = mt
                disk_paths[s] = p
            # состояние в БД
            try:
                cur = conn.execute("SELECT path, mtime_ns FROM fts_meta")
                meta = {row[0]: row[1] for row in cur.fetchall()}
            except Exception:
                meta = {}
            to_delete = [p for p in meta if p not in disk]
            to_upsert: list[str] = []
            for p, mt in disk.items():
                if p not in meta or meta[p] != mt:
                    to_upsert.append(p)

            updated = 0
            # удаления
            for p in to_delete:
                try:
                    conn.execute("DELETE FROM notes_fts WHERE path = ?", (p,))
                    conn.execute("DELETE FROM fts_meta WHERE path = ?", (p,))
                    updated += 1
                except Exception:
                    continue
            # вставки/обновления
            for p in to_upsert:
                path = disk_paths[p]
                mt = disk[p]
                title = _note_title(path)
                content = _read_content(path)
                # FTS5: delete old row if exists (path is not primary, but we match)
                try:
                    conn.execute("DELETE FROM notes_fts WHERE path = ?", (p,))
                except Exception:
                    pass
                try:
                    conn.execute(
                        "INSERT INTO notes_fts (path, title, content) VALUES (?, ?, ?)",
                        (p, title, content),
                    )
                except Exception:
                    # fallback без title? try again
                    try:
                        conn.execute(
                            "INSERT INTO notes_fts (path, title, content) VALUES (?, ?, ?)",
                            (p, title, content[:100000]),
                        )
                    except Exception:
                        continue
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO fts_meta (path, mtime_ns) VALUES (?, ?)",
                        (p, mt),
                    )
                except Exception:
                    pass
                updated += 1
            conn.commit()
            # optimize иногда
            try:
                if updated > 50:
                    conn.execute("INSERT INTO notes_fts(notes_fts) VALUES('optimize')")
                    conn.commit()
            except Exception:
                pass
            return updated
        finally:
            try:
                conn.close()
            except Exception:
                pass


def rebuild_index(settings: dict[str, Any]) -> int:
    """Полная пересборка индекса."""
    return ensure_index(settings, force=True)


def upsert_file(path: Path) -> bool:
    """Обновить один файл в индексе (по mtime)."""
    with _lock:
        p = Path(path)
        s = str(p)
        try:
            mt = p.stat().st_mtime_ns
        except OSError:
            return remove_file(p)
        title = _note_title(p)
        content = _read_content(p)
        conn = _connect()
        try:
            try:
                conn.execute("DELETE FROM notes_fts WHERE path = ?", (s,))
            except Exception:
                pass
            conn.execute(
                "INSERT INTO notes_fts (path, title, content) VALUES (?, ?, ?)",
                (s, title, content),
            )
            conn.execute(
                "INSERT OR REPLACE INTO fts_meta (path, mtime_ns) VALUES (?, ?)",
                (s, mt),
            )
            conn.commit()
            return True
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass


def remove_file(path: Path) -> bool:
    """Удалить файл из индекса."""
    with _lock:
        s = str(Path(path))
        conn = _connect()
        try:
            conn.execute("DELETE FROM notes_fts WHERE path = ?", (s,))
            conn.execute("DELETE FROM fts_meta WHERE path = ?", (s,))
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass


def index_file(path: Path) -> bool:
    """Алиас для upsert_file."""
    return upsert_file(path)


# ── поиск BM25 + подсветка ─────────────────────────────────────────

def search(
    settings: dict[str, Any],
    query: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Полнотекстовый поиск с ранжированием BM25 и подсветкой.

    Обеспечивает актуальность индекса (ensure_index инкрементально).
    Возвращает список ``{path, title, snippet, highlight_title, highlight_content, rank}``
    отсортированный по BM25 (меньше = релевантнее).
    Подсветка через FTS5 ``highlight()`` / ``snippet()`` — теги <mark>.
    При ошибке FTS (нет таблицы, bad query) — fallback на vault.search_notes.
    """
    q = (query or "").strip()
    if not q:
        return []
    # инкрементальное обновление (быстро, если нет изменений)
    try:
        ensure_index(settings, force=False)
    except Exception:
        pass

    fts_q = _escape_fts_query(q)
    conn = _connect()
    try:
        # проверить наличие данных
        try:
            cur = conn.execute("SELECT count(*) FROM notes_fts")
            cnt = cur.fetchone()[0]
            if cnt == 0:
                return []
        except Exception:
            return []
        # BM25 с весами: path, title, content
        w0, w1, w2 = _BM25_WEIGHTS
        sql = f"""
            SELECT
                path,
                title,
                snippet(notes_fts, 2, '<mark>', '</mark>', ' … ', 10) as snippet,
                highlight(notes_fts, 1, '<mark>', '</mark>') as hl_title,
                highlight(notes_fts, 2, '<mark>', '</mark>') as hl_content,
                bm25(notes_fts, {w0}, {w1}, {w2}) as rank
            FROM notes_fts
            WHERE notes_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        try:
            cur = conn.execute(sql, (fts_q, int(limit)))
            rows = cur.fetchall()
        except sqlite3.OperationalError as exc:
            # malformed MATCH — пробуем без prefix или как plain
            msg = str(exc).lower()
            if "no such column" in msg or "fts5" in msg:
                return []
            # fallback: экранировать как phrase
            safe = q.replace('"', '""')
            try:
                cur = conn.execute(sql, (f'"{safe}"', int(limit)))
                rows = cur.fetchall()
            except Exception:
                return []
        out: list[dict[str, Any]] = []
        for path, title, snippet, hl_title, hl_content, rank in rows:
            # snippet может быть None если совпадение только в title/path
            if snippet is None:
                snippet = ""
            if hl_title is None:
                hl_title = title or ""
            if hl_content is None:
                hl_content = ""
            out.append(
                {
                    "path": path,
                    "title": title or Path(path).stem,
                    "snippet": snippet,
                    "highlight_title": hl_title,
                    "highlight_content": hl_content,
                    "rank": float(rank) if rank is not None else 0.0,
                }
            )
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


def search_notes(
    settings: dict[str, Any],
    query: str,
    limit: int = 14,
) -> list[Any]:
    """Совместимый с ``services.vault.search_notes`` поиск через FTS5.

    Возвращает ``list[NoteHit]`` отсортированный по BM25, внутри — подсветка
    доступна через поиск ``search()`` сниппетов. При недоступности FTS —
    делегирует в ``vault.search_notes``.
    """
    q = (query or "").strip()
    if not q:
        return []
    # пробуем FTS
    try:
        hits = search(settings, q, limit=limit)
        if hits:
            from .vault import NoteHit  # type: ignore
            out: list[Any] = []
            for h in hits:
                p = Path(h["path"])
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    mt = 0.0
                snippet = h.get("snippet") or h.get("highlight_content") or ""
                # raw с маркерами для подсветки preview
                raw = ""
                try:
                    raw = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    raw = snippet
                # NoteHit(title, path, mtime, text, raw)
                out.append(
                    NoteHit(
                        path=p,
                        title=h.get("title") or _note_title(p),
                        mtime=mt,
                        text=snippet.lower() if snippet else (raw.lower()[:2000]),
                        raw=raw,
                    )
                )
                # дополнительно сохраняем подсветку как атрибуты
                try:
                    out[-1].snippet = snippet  # type: ignore[attr-defined]
                    out[-1].highlight_title = h.get("highlight_title")  # type: ignore[attr-defined]
                    out[-1].highlight_content = h.get("highlight_content")  # type: ignore[attr-defined]
                    out[-1].rank = h.get("rank")  # type: ignore[attr-defined]
                except Exception:
                    pass
            return out
    except Exception:
        pass
    # fallback — старый substring поиск
    try:
        from . import vault as _vault
        return _vault.search_notes(settings, q, limit=limit)
    except Exception:
        return []


def highlight_snippet(content: str, query: str) -> str:
    """Подсветка фрагмента <mark> без БД (fallback)."""
    return _highlight_fallback(content or "", query or "")


def get_snippet(path: Path, query: str, window: int = 10) -> str:
    """Сниппет FTS5 highlight для одного файла (или fallback)."""
    q = (query or "").strip()
    if not q or not Path(path).is_file():
        return ""
    # пробуем через FTS highlight
    try:
        conn = _connect()
        try:
            fts_q = _escape_fts_query(q)
            # snippet/ highlight требуют MATCH; используем временный запрос
            sql = """
                SELECT snippet(notes_fts, 2, '<mark>', '</mark>', ' … ', ?) as snip
                FROM notes_fts WHERE path = ? AND notes_fts MATCH ?
                LIMIT 1
            """
            cur = conn.execute(sql, (int(window), str(path), fts_q))
            row = cur.fetchone()
            if row and row[0]:
                return str(row[0])
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        pass
    # fallback
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    return _highlight_fallback(text, q)


def invalidate_index() -> None:
    """Удалить БД индекс (для тестов)."""
    with _lock:
        try:
            if DB_PATH.exists():
                DB_PATH.unlink()
            # WAL/SHM
            for suf in ("-wal", "-shm"):
                p = Path(str(DB_PATH) + suf)
                try:
                    if p.exists():
                        p.unlink()
                except OSError:
                    pass
        except OSError:
            pass


__all__ = [
    "DB_PATH",
    "get_db_path",
    "ensure_index",
    "rebuild_index",
    "upsert_file",
    "remove_file",
    "index_file",
    "search",
    "search_notes",
    "highlight_snippet",
    "get_snippet",
    "invalidate_index",
]
