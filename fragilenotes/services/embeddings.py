"""Локальные embeddings для семантического поиска (sentence-transformers или TF-IDF fallback).

Индекс хранится в ``~/.cache/fragilenotes/embeddings.db`` (WAL), обновление
инкрементально по ``st_mtime_ns``. При наличии ``sentence-transformers``
используется локальная модель (``all-MiniLM-L6-v2`` или multilingual), иначе —
чистый Python TF-IDF fallback без внешних зависимостей.

API:
  get_db_path() -> Path
  get_model_name() -> str
  is_transformer_available() -> bool
  embed_text(text: str) -> list[float]
  ensure_index(settings, force=False) -> int
  rebuild_index(settings) -> int
  upsert_file(path: Path) -> bool
  remove_file(path: Path) -> bool
  index_file(path: Path) -> bool          # alias
  search(settings, query, limit=50) -> list[dict]  # {path, title, snippet, score}
  search_notes(settings, query, limit=14) -> list[NoteHit]
  semantic_search(settings, query, limit=50) -> list[dict]  # alias
  invalidate_index() -> None
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import struct
import threading
from pathlib import Path
from typing import Any

# ── константы ────────────────────────────────────────────────────────
DB_PATH = Path.home() / ".cache" / "fragilenotes" / "embeddings.db"
_DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
# TF-IDF параметры
_TFIDF_MAX_VOCAB = 5000
_TFIDF_MAX_CHARS = 8000
_EMB_MAX_CHARS = 8000
_HASH_DIM = 384  # hashed TF-IDF dim для больших vault (>2000 доков) — 39k масштабируется
_STOP = {
    "это", "что", "как", "для", "или", "при", "про", "ещё", "еще", "был", "была",
    "было", "быть", "есть", "так", "вот", "уже", "тоже", "чтобы", "который",
    "которые", "надо", "можно", "нужно", "очень", "самый", "эта",
    "этот", "того", "такой", "тогда", "когда", "где", "если", "чем", "тем",
    "the", "and", "for", "with", "from", "that", "this", "have", "are", "was",
    "were", "will", "would", "can", "not", "but", "you", "your",
}

_lock = threading.RLock()
_model: Any | None = None
_model_name: str | None = None
_model_load_attempted = False

# ── helpers ─────────────────────────────────────────────────────────

def get_db_path() -> Path:
    """Путь к embeddings БД — ``~/.cache/fragilenotes/embeddings.db``."""
    return DB_PATH


def get_model_name() -> str:
    """Имя используемой модели (transformer или 'tfidf')."""
    if _model_name is not None:
        return _model_name
    if is_transformer_available():
        # если модель ещё не загружена — вернуть дефолт
        return _DEFAULT_MODEL
    return "tfidf"


def is_transformer_available() -> bool:
    """Доступен ли sentence-transformers (importable)."""
    if _model is not None:
        return True
    try:
        import sentence_transformers  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10.0)
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embeddings (
            path TEXT PRIMARY KEY,
            mtime_ns INTEGER NOT NULL,
            embedding BLOB NOT NULL,
            dim INTEGER NOT NULL,
            model TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embeddings_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
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
    import os

    try:
        from ..vault import ALLOWED_EXTS, HEAVY_DIRS
    except Exception:
        ALLOWED_EXTS = {".md"}
        HEAVY_DIRS = {".git", "__pycache__", ".obsidian", "node_modules"}
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


def _prepare_text(path: Path) -> str:
    """Заголовок + контент, ограничено 8k символов."""
    title = _note_title(path)
    raw = _read_content(path)
    # убрать frontmatter для чистого смысла
    try:
        from ..vault import parse_frontmatter

        _fm, body = parse_frontmatter(raw)
        if body is not None:
            raw = body
    except Exception:
        pass
    combined = f"{title}\n\n{raw}" if title else raw
    if len(combined) > _EMB_MAX_CHARS:
        combined = combined[:_EMB_MAX_CHARS]
    return combined.strip()


def _prepare_query(query: str) -> str:
    return (query or "").strip()[:2000]


# ── sentence-transformers ───────────────────────────────────────────

def _get_model() -> Any | None:
    global _model, _model_name, _model_load_attempted
    if _model is not None:
        return _model
    if _model_load_attempted:
        return None
    _model_load_attempted = True
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore

        # пробуем дефолтную лёгкую модель; если нет сети/кэша — fallback
        for cand in (_DEFAULT_MODEL, "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"):
            try:
                m = SentenceTransformer(cand)
                _model = m
                _model_name = cand
                return _model
            except Exception:
                continue
        # пробуем без имени — если кэш уже есть локально
        return None
    except Exception:
        return None


def _embed_transformer(texts: list[str]) -> list[list[float]] | None:
    model = _get_model()
    if model is None:
        return None
    try:
        # normalize_embeddings=True -> косинус = dot
        vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        # vecs может быть numpy array или list
        try:
            import numpy as np  # type: ignore

            if isinstance(vecs, np.ndarray):
                return [row.astype("float32").tolist() for row in vecs]
        except Exception:
            pass
        # fallback list
        if hasattr(vecs, "tolist"):
            try:
                vecs = vecs.tolist()
            except Exception:
                pass
        out: list[list[float]] = []
        for v in vecs:  # type: ignore
            out.append([float(x) for x in v])
        return out
    except Exception:
        return None


# ── TF-IDF fallback ─────────────────────────────────────────────────

def _tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[\w\u0400-\u04FF]+", text.lower(), flags=re.UNICODE)
    out: list[str] = []
    for t in tokens:
        if len(t) < 2:
            continue
        if t.isdigit():
            continue
        if t in _STOP:
            continue
        out.append(t)
    return out


def _vector_to_blob(vec: list[float]) -> bytes:
    """float32 LE blob."""
    try:
        import numpy as np  # type: ignore

        arr = np.array(vec, dtype="float32")
        return arr.tobytes()
    except Exception:
        return struct.pack(f"<{len(vec)}f", *vec)


def _blob_to_vector(blob: bytes, dim: int | None = None) -> list[float]:
    if not blob:
        return []
    try:
        import numpy as np  # type: ignore

        arr = np.frombuffer(blob, dtype="float32")
        return arr.astype("float64").tolist()
    except Exception:
        pass
    # pure struct
    if dim is None:
        dim = len(blob) // 4
    try:
        return list(struct.unpack(f"<{dim}f", blob[: dim * 4]))
    except Exception:
        return []


def _cosine_sim(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    try:
        import numpy as np  # type: ignore

        av = np.array(a, dtype="float64")
        bv = np.array(b, dtype="float64")
        denom = float(np.linalg.norm(av) * np.linalg.norm(bv))
        if denom == 0:
            return 0.0
        return float(np.dot(av, bv) / denom)
    except Exception:
        dot = 0.0
        na = 0.0
        nb = 0.0
        for x, y in zip(a, b):
            dot += x * y
            na += x * x
            nb += y * y
        if na == 0 or nb == 0:
            return 0.0
        return dot / (math.sqrt(na) * math.sqrt(nb))


def _l2_normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    if n == 0:
        return vec
    return [x / n for x in vec]


def _build_tfidf_vocab(docs_tokens: list[list[str]]) -> tuple[list[str], list[float]]:
    """Построить vocab (отсортирован) + idf по корпусу. Ограничить топ-5000 по df."""
    n = len(docs_tokens)
    if n == 0:
        return [], []
    df: dict[str, int] = {}
    for toks in docs_tokens:
        seen = set(toks)
        for t in seen:
            df[t] = df.get(t, 0) + 1
    # сортировка по убыванию df, затем алфавит — детерминированно, берём топ
    items = sorted(df.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(items) > _TFIDF_MAX_VOCAB:
        items = items[:_TFIDF_MAX_VOCAB]
    vocab = [k for k, _ in items]
    # idf: log((N+1)/(df+1))+1  (сглаженный)
    idf: list[float] = []
    for term in vocab:
        d = df.get(term, 0)
        v = math.log((n + 1) / (d + 1)) + 1.0
        idf.append(v)
    return vocab, idf


def _hash_token(token: str, dim: int = _HASH_DIM) -> int:
    # детерминированный хэш (md5) -> 0..dim-1
    h = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)
    return h % dim


def _build_hashed_idf(docs_tokens: list[list[str]], dim: int = _HASH_DIM) -> list[float]:
    n = len(docs_tokens)
    df = [0] * dim
    for toks in docs_tokens:
        buckets = set(_hash_token(t, dim) for t in toks)
        for b in buckets:
            df[b] += 1
    idf = [math.log((n + 1) / (d + 1)) + 1.0 for d in df]
    return idf


def _hashed_tfidf_vector(tokens: list[str], idf: list[float], dim: int = _HASH_DIM) -> list[float]:
    if not tokens or not idf:
        return [0.0] * dim
    tf = [0] * dim
    for t in tokens:
        b = _hash_token(t, dim)
        tf[b] += 1
    total = len(tokens)
    vec = [0.0] * dim
    for i in range(dim):
        if tf[i]:
            vec[i] = (tf[i] / total) * idf[i]
    return _l2_normalize(vec)


def _tfidf_vector(tokens: list[str], vocab: list[str], idf: list[float]) -> list[float]:
    if not vocab:
        return []
    idx = {t: i for i, t in enumerate(vocab)}
    tf: dict[int, int] = {}
    total = len(tokens) if tokens else 1
    for t in tokens:
        i = idx.get(t)
        if i is not None:
            tf[i] = tf.get(i, 0) + 1
    vec = [0.0] * len(vocab)
    for i, cnt in tf.items():
        # tf нормализованный + логарифм? просто cnt/total
        tf_norm = cnt / total
        vec[i] = tf_norm * idf[i]
    return _l2_normalize(vec)


def _load_tfidf_meta(conn: sqlite3.Connection) -> tuple[list[str], list[float]] | None:
    try:
        cur = conn.execute("SELECT value FROM embeddings_meta WHERE key='tfidf_vocab'")
        row = cur.fetchone()
        if not row:
            return None
        vocab = json.loads(row[0])
        cur = conn.execute("SELECT value FROM embeddings_meta WHERE key='tfidf_idf'")
        row2 = cur.fetchone()
        if not row2:
            return None
        idf = json.loads(row2[0])
        if isinstance(vocab, list) and isinstance(idf, list) and len(vocab) == len(idf):
            return vocab, [float(x) for x in idf]
    except Exception:
        pass
    return None


def _save_tfidf_meta(conn: sqlite3.Connection, vocab: list[str], idf: list[float]) -> None:
    try:
        conn.execute(
            "INSERT OR REPLACE INTO embeddings_meta (key, value) VALUES (?, ?)",
            ("tfidf_vocab", json.dumps(vocab, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO embeddings_meta (key, value) VALUES (?, ?)",
            ("tfidf_idf", json.dumps(idf, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO embeddings_meta (key, value) VALUES (?, ?)",
            ("tfidf_model", "tfidf"),
        )
        conn.commit()
    except Exception:
        pass


def _load_hashed_meta(conn: sqlite3.Connection) -> tuple[int, list[float]] | None:
    try:
        cur = conn.execute("SELECT value FROM embeddings_meta WHERE key='tfidf_hashed_idf'")
        row = cur.fetchone()
        if not row:
            return None
        idf = json.loads(row[0])
        cur = conn.execute("SELECT value FROM embeddings_meta WHERE key='tfidf_hashed_dim'")
        row2 = cur.fetchone()
        dim = int(json.loads(row2[0])) if row2 else _HASH_DIM
        if isinstance(idf, list) and len(idf) == dim:
            return dim, [float(x) for x in idf]
    except Exception:
        pass
    return None


def _save_hashed_meta(conn: sqlite3.Connection, dim: int, idf: list[float]) -> None:
    try:
        conn.execute(
            "INSERT OR REPLACE INTO embeddings_meta (key, value) VALUES (?, ?)",
            ("tfidf_hashed_idf", json.dumps(idf, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO embeddings_meta (key, value) VALUES (?, ?)",
            ("tfidf_hashed_dim", json.dumps(dim)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO embeddings_meta (key, value) VALUES (?, ?)",
            ("tfidf_model", "tfidf_hashed"),
        )
        conn.commit()
    except Exception:
        pass


def _highlight_fallback(content: str, query: str, snippet_len: int = 160) -> str:
    if not query or not content:
        return " ".join(content.split())[:snippet_len]
    q = query.strip()
    low = content.lower()
    qlow = q.lower()
    idx = low.find(qlow)
    if idx == -1:
        for tok in re.findall(r"[\w\u0400-\u04FF]+", q):
            idx = low.find(tok.lower())
            if idx != -1:
                q = tok
                break
    if idx == -1:
        return " ".join(content.split())[:snippet_len]
    start = max(0, idx - 60)
    end = min(len(content), idx + len(q) + 60)
    piece = content[start:end]
    try:
        pattern = re.compile(re.escape(q), re.IGNORECASE)
        piece = pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", piece, count=1)
    except Exception:
        pass
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    return prefix + " ".join(piece.split()) + suffix


def embed_text(text: str) -> list[float]:
    """Эмбеддинг одного текста (нормализованный)."""
    t = _prepare_query(text)
    if not t:
        return []
    # пробуем transformer
    vecs = _embed_transformer([t])
    if vecs is not None and vecs:
        return vecs[0]
    # fallback — нужен vocab; если нет, вернём пусто (вызовется через индекс)
    # для одиночного embed без корпуса — простая хэш-эмбеддинг fallback (детерм.)
    # чтобы embed_text был юзабелен без индекса
    toks = _tokenize(t)
    if not toks:
        return []
    # хэш-вектор 128-мерный
    dim = 128
    vec = [0.0] * dim
    for tok in toks:
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        idx = h % dim
        vec[idx] += 1.0
    return _l2_normalize(vec)


# ── индексация ──────────────────────────────────────────────────────

def ensure_index(settings: dict[str, Any], force: bool = False) -> int:
    """Инкрементально обновить индекс embeddings.

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
                    conn.execute("DELETE FROM embeddings")
                    conn.execute("DELETE FROM embeddings_meta WHERE key LIKE 'tfidf_%'")
                    conn.commit()
                except Exception:
                    pass
            # текущее состояние диска
            disk: dict[str, int] = {}
            disk_paths: dict[str, Path] = {}
            for p, mt in _iter_vault_files(root):
                s = str(p)
                disk[s] = mt
                disk_paths[s] = p
            # состояние в БД
            try:
                cur = conn.execute("SELECT path, mtime_ns FROM embeddings")
                meta = {row[0]: row[1] for row in cur.fetchall()}
            except Exception:
                meta = {}
            to_delete = [p for p in meta if p not in disk]
            to_upsert: list[str] = []
            for p, mt in disk.items():
                if p not in meta or meta[p] != mt:
                    to_upsert.append(p)
            # удаления
            updated = 0
            for p in to_delete:
                try:
                    conn.execute("DELETE FROM embeddings WHERE path = ?", (p,))
                    updated += 1
                except Exception:
                    continue
            if to_delete:
                conn.commit()

            # если нет изменений и не force и уже есть данные — быстро выходим
            if not to_upsert and not to_delete and not force:
                return updated

            # решаем режим: transformer vs tfidf
            use_transformer = is_transformer_available() and _get_model() is not None
            # но _get_model лениво может вернуть None, тогда fallback

            if use_transformer:
                # инкрементально считаем только изменённые
                for p in to_upsert:
                    path = disk_paths[p]
                    mt = disk[p]
                    text = _prepare_text(path)
                    if not text:
                        # пустой — удалить если есть, не вставлять
                        try:
                            conn.execute("DELETE FROM embeddings WHERE path = ?", (p,))
                            conn.execute(
                                "INSERT OR REPLACE INTO embeddings_meta (key, value) VALUES (?, ?)",
                                ("transformer_model", _model_name or _DEFAULT_MODEL),
                            )
                        except Exception:
                            pass
                        updated += 1
                        continue
                    vecs = _embed_transformer([text])
                    if vecs is None or not vecs:
                        # fallback на хэш-вектор
                        vec = embed_text(text)
                        model = "tfidf_hash"
                    else:
                        vec = vecs[0]
                        model = _model_name or _DEFAULT_MODEL
                    blob = _vector_to_blob(vec)
                    dim = len(vec)
                    try:
                        conn.execute(
                            "INSERT OR REPLACE INTO embeddings (path, mtime_ns, embedding, dim, model) VALUES (?, ?, ?, ?, ?)",
                            (p, mt, blob, dim, model),
                        )
                        updated += 1
                    except Exception:
                        continue
                conn.commit()
                # сохранить мета модели
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO embeddings_meta (key, value) VALUES (?, ?)",
                        ("model", _model_name or _DEFAULT_MODEL),
                    )
                    conn.commit()
                except Exception:
                    pass
                return updated

            # ── TF-IDF fallback: при любом изменении — пересбор vocab/idf и всех векторов
            # (idf зависит от всего корпуса). Для больших vault (>2000) — hashed 384-dim.
            need_rebuild = bool(to_upsert or to_delete or force)
            if not need_rebuild:
                return updated
            # собрать все тексты корпуса для vocab
            all_paths = list(disk.keys())
            docs_tokens: list[list[str]] = []
            texts_for_hash: list[str] = []
            for sp in all_paths:
                pp = disk_paths[sp]
                txt = _prepare_text(pp)
                toks = _tokenize(txt)
                docs_tokens.append(toks)
                texts_for_hash.append(txt)
            use_hashed = len(all_paths) > 2000
            if use_hashed:
                dim = _HASH_DIM
                idf_h = _build_hashed_idf(docs_tokens, dim=dim)
                _save_hashed_meta(conn, dim, idf_h)
                try:
                    conn.execute("DELETE FROM embeddings")
                except Exception:
                    pass
                for sp, toks in zip(all_paths, docs_tokens):
                    mt = disk[sp]
                    vec = _hashed_tfidf_vector(toks, idf_h, dim=dim)
                    if not vec:
                        vec = [0.0] * dim
                    blob = _vector_to_blob(vec)
                    try:
                        conn.execute(
                            "INSERT OR REPLACE INTO embeddings (path, mtime_ns, embedding, dim, model) VALUES (?, ?, ?, ?, ?)",
                            (sp, mt, blob, dim, "tfidf_hashed"),
                        )
                    except Exception:
                        continue
                conn.commit()
                return len(all_paths) if need_rebuild else updated
            # обычный vocab TF-IDF для малых vault
            vocab, idf = _build_tfidf_vocab(docs_tokens)
            # сохранить мету
            _save_tfidf_meta(conn, vocab, idf)
            # также очистить hashed мету чтобы search не путался
            try:
                conn.execute("DELETE FROM embeddings_meta WHERE key='tfidf_hashed_idf'")
                conn.execute("DELETE FROM embeddings_meta WHERE key='tfidf_hashed_dim'")
            except Exception:
                pass
            # пересчитать все вектора
            try:
                conn.execute("DELETE FROM embeddings")
            except Exception:
                pass
            for sp, toks in zip(all_paths, docs_tokens):
                mt = disk[sp]
                vec = _tfidf_vector(toks, vocab, idf)
                if not vec:
                    # пустой документ — нулевой вектор размерности vocab или 0
                    dim = len(vocab) if vocab else 0
                    if dim == 0:
                        continue
                    vec = [0.0] * dim
                blob = _vector_to_blob(vec)
                dim = len(vec)
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO embeddings (path, mtime_ns, embedding, dim, model) VALUES (?, ?, ?, ?, ?)",
                        (sp, mt, blob, dim, "tfidf"),
                    )
                except Exception:
                    continue
            conn.commit()
            # updated = количество затронутых документов (для TF-IDF — весь корпус при пересборе)
            return len(all_paths) if need_rebuild else updated
        finally:
            try:
                conn.close()
            except Exception:
                pass


def rebuild_index(settings: dict[str, Any]) -> int:
    """Полная пересборка индекса."""
    return ensure_index(settings, force=True)


def upsert_file(path: Path) -> bool:
    """Обновить один файл в индексе (по mtime). Для TF-IDF — триггерит полный ребилд vault."""
    with _lock:
        p = Path(path)
        s = str(p)
        try:
            mt = p.stat().st_mtime_ns
        except OSError:
            return remove_file(p)
        # определить vault root по файлу
        try:
            guess_root = p.parent
            # walk up до маркера
            cur = p if p.is_dir() else p.parent
            for anc in [cur, *list(cur.parents)]:
                try:
                    if (anc / "01 Home").is_dir() or (anc / "_System").is_dir() or (anc / ".obsidian").is_dir():
                        guess_root = anc
                        break
                except Exception:
                    continue
            settings = {"vault_root": str(guess_root)}
        except Exception:
            settings = {"vault_root": str(p.parent)}
        # Для TF-IDF режима нужен полный ребилд, чтобы idf обновился
        if not is_transformer_available() or _get_model() is None:
            try:
                ensure_index(settings, force=False)
                return True
            except Exception:
                return False
        # transformer — точечный апсерт
        text = _prepare_text(p)
        vecs = _embed_transformer([text]) if text else None
        if vecs is None or not vecs:
            vec = embed_text(text) if text else []
            model = "tfidf_hash"
        else:
            vec = vecs[0]
            model = _model_name or _DEFAULT_MODEL
        if not vec:
            return remove_file(p)
        blob = _vector_to_blob(vec)
        dim = len(vec)
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO embeddings (path, mtime_ns, embedding, dim, model) VALUES (?, ?, ?, ?, ?)",
                (s, mt, blob, dim, model),
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
            conn.execute("DELETE FROM embeddings WHERE path = ?", (s,))
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


# ── поиск ───────────────────────────────────────────────────────────

def _load_all_embeddings(conn: sqlite3.Connection, vault_root: Path | None = None) -> list[tuple[str, list[float], int]]:
    try:
        cur = conn.execute("SELECT path, embedding, dim FROM embeddings")
        rows = cur.fetchall()
    except Exception:
        return []
    out: list[tuple[str, list[float], int]] = []
    for path, blob, dim in rows:
        # фильтр по vault_root если задан
        if vault_root is not None:
            try:
                if not str(path).startswith(str(vault_root)):
                    # пропускаем чужие vault'ы
                    continue
            except Exception:
                pass
        vec = _blob_to_vector(blob, dim)
        if vec:
            out.append((path, vec, dim))
    return out


def search(
    settings: dict[str, Any],
    query: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Семантический поиск (косинусная близость).

    Обеспечивает актуальность индекса (ensure_index инкрементально).
    Возвращает список ``{path, title, snippet, score}`` отсортированный по убыванию score.
    При ошибке — пустой список.
    """
    q = _prepare_query(query)
    if not q:
        return []
    try:
        ensure_index(settings, force=False)
    except Exception:
        pass
    root = _vault_root(settings)
    conn = _connect()
    try:
        # определить режим по мета
        try:
            cur = conn.execute("SELECT value FROM embeddings_meta WHERE key='model'")
            row = cur.fetchone()
            model_meta = row[0] if row else ""
        except Exception:
            model_meta = ""
        # также проверяем tfidf_model ключ (реальный)
        try:
            cur2 = conn.execute("SELECT value FROM embeddings_meta WHERE key='tfidf_model'")
            row2 = cur2.fetchone()
            tfidf_model = row2[0] if row2 else ""
        except Exception:
            tfidf_model = ""
        is_tfidf = (model_meta in ("tfidf", "tfidf_hashed") or tfidf_model in ("tfidf", "tfidf_hashed")) or (not is_transformer_available())
        # проверить наличие данных
        try:
            cur = conn.execute("SELECT count(*) FROM embeddings")
            cnt = cur.fetchone()[0]
            if cnt == 0:
                return []
        except Exception:
            return []

        if is_tfidf or _get_model() is None:
            # TF-IDF путь — пробуем vocab, затем hashed
            # сначала проверяем hashed (большие vault)
            hashed = _load_hashed_meta(conn)
            if hashed is not None:
                dim, idf_h = hashed
                q_toks = _tokenize(q)
                if not q_toks:
                    return []
                q_vec = _hashed_tfidf_vector(q_toks, idf_h, dim=dim)
                if not q_vec or all(v == 0 for v in q_vec):
                    return []
                docs = _load_all_embeddings(conn, vault_root=root)
                scored: list[tuple[float, str]] = []
                for path, vec, _dim in docs:
                    if len(vec) != dim:
                        continue
                    score = _cosine_sim(q_vec, vec)
                    if score > 0.01:
                        scored.append((score, path))
                scored.sort(key=lambda x: x[0], reverse=True)
                out: list[dict[str, Any]] = []
                for score, path in scored[: int(limit)]:
                    title = _note_title(Path(path))
                    try:
                        content = _read_content(Path(path))
                        snippet = _highlight_fallback(content, q)
                    except Exception:
                        snippet = ""
                    out.append(
                        {
                            "path": path,
                            "title": title,
                            "snippet": snippet,
                            "score": float(score),
                            "rank": float(1.0 - score),
                        }
                    )
                return out
            # обычный vocab TF-IDF
            tfidf = _load_tfidf_meta(conn)
            if tfidf is None:
                # fallback: если нет vocab — пересбор
                try:
                    conn.close()
                except Exception:
                    pass
                ensure_index(settings, force=True)
                conn = _connect()
                # повторная попытка — сначала hashed, затем vocab
                hashed = _load_hashed_meta(conn)
                if hashed is not None:
                    dim, idf_h = hashed
                    q_toks = _tokenize(q)
                    if not q_toks:
                        return []
                    q_vec = _hashed_tfidf_vector(q_toks, idf_h, dim=dim)
                    docs = _load_all_embeddings(conn, vault_root=root)
                    scored = []
                    for path, vec, _dim in docs:
                        if len(vec) != dim:
                            continue
                        score = _cosine_sim(q_vec, vec)
                        if score > 0.01:
                            scored.append((score, path))
                    scored.sort(key=lambda x: x[0], reverse=True)
                    out = []
                    for score, path in scored[: int(limit)]:
                        title = _note_title(Path(path))
                        try:
                            content = _read_content(Path(path))
                            snippet = _highlight_fallback(content, q)
                        except Exception:
                            snippet = ""
                        out.append(
                            {
                                "path": path,
                                "title": title,
                                "snippet": snippet,
                                "score": float(score),
                                "rank": float(1.0 - score),
                            }
                        )
                    return out
                tfidf = _load_tfidf_meta(conn)
                if tfidf is None:
                    return []
            vocab, idf = tfidf
            q_toks = _tokenize(q)
            if not q_toks:
                return []
            q_vec = _tfidf_vector(q_toks, vocab, idf)
            if not q_vec or all(v == 0 for v in q_vec):
                # если запрос не попал в vocab — хэш-вектор fallback
                q_vec = embed_text(q)
                # тогда dim не совпадёт — нельзя сравнить с TF-IDF векторами
                # отдаём пусто или пробуем хэш-сравнение через отдельную логику
                # для TF-IDF просто вернём 0 результатов, fallback на substring будет в files_view
                return []
            # загрузить все док-вектора
            docs = _load_all_embeddings(conn, vault_root=root)
            scored: list[tuple[float, str]] = []
            for path, vec, _dim in docs:
                if len(vec) != len(q_vec):
                    continue
                score = _cosine_sim(q_vec, vec)
                if score > 0.01:  # порог шума
                    scored.append((score, path))
            scored.sort(key=lambda x: x[0], reverse=True)
            out: list[dict[str, Any]] = []
            for score, path in scored[: int(limit)]:
                title = _note_title(Path(path))
                snippet = ""
                try:
                    content = _read_content(Path(path))
                    snippet = _highlight_fallback(content, q)
                except Exception:
                    snippet = ""
                out.append(
                    {
                        "path": path,
                        "title": title,
                        "snippet": snippet,
                        "score": float(score),
                        "rank": float(1.0 - score),
                    }
                )
            return out
        else:
            # transformer
            q_vecs = _embed_transformer([q])
            if q_vecs is None or not q_vecs:
                return []
            q_vec = q_vecs[0]
            docs = _load_all_embeddings(conn, vault_root=root)
            scored = []
            for path, vec, _dim in docs:
                if len(vec) != len(q_vec):
                    continue
                # так как нормализованы — dot = cosine
                try:
                    import numpy as np  # type: ignore

                    score = float(np.dot(np.array(q_vec), np.array(vec)))
                except Exception:
                    score = _cosine_sim(q_vec, vec)
                if score > 0.01:
                    scored.append((score, path))
            scored.sort(key=lambda x: x[0], reverse=True)
            out = []
            for score, path in scored[: int(limit)]:
                title = _note_title(Path(path))
                try:
                    content = _read_content(Path(path))
                    snippet = _highlight_fallback(content, q)
                except Exception:
                    snippet = ""
                out.append(
                    {
                        "path": path,
                        "title": title,
                        "snippet": snippet,
                        "score": float(score),
                        "rank": float(1.0 - score),
                    }
                )
            return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


def semantic_search(
    settings: dict[str, Any],
    query: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Алиас для search — семантический поиск."""
    return search(settings, query, limit=limit)


def search_notes(
    settings: dict[str, Any],
    query: str,
    limit: int = 14,
) -> list[Any]:
    """Совместимый с ``services.vault.search_notes`` семантический поиск.

    Возвращает ``list[NoteHit]`` отсортированный по косинусной близости.
    При недоступности embeddings — делегирует в ``vault.search_notes``.
    """
    q = (query or "").strip()
    if not q:
        return []
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
                snippet = h.get("snippet") or ""
                try:
                    raw = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    raw = snippet
                out.append(
                    NoteHit(
                        path=p,
                        title=h.get("title") or _note_title(p),
                        mtime=mt,
                        text=snippet.lower() if snippet else (raw.lower()[:2000]),
                        raw=raw,
                    )
                )
                try:
                    out[-1].snippet = snippet  # type: ignore[attr-defined]
                    out[-1].score = h.get("score")  # type: ignore[attr-defined]
                    out[-1].rank = h.get("rank")  # type: ignore[attr-defined]
                except Exception:
                    pass
            return out
    except Exception:
        pass
    try:
        from . import vault as _vault

        return _vault.search_notes(settings, q, limit=limit)
    except Exception:
        return []


def get_snippet(path: Path, query: str, window: int = 10) -> str:
    """Сниппет подсветки для одного файла (fallback)."""
    q = (query or "").strip()
    if not q or not Path(path).is_file():
        return ""
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
            for suf in ("-wal", "-shm"):
                p = Path(str(DB_PATH) + suf)
                try:
                    if p.exists():
                        p.unlink()
                except OSError:
                    pass
        except OSError:
            pass
        global _model, _model_name, _model_load_attempted
        # не сбрасываем модель — кэш в памяти остаётся


__all__ = [
    "DB_PATH",
    "get_db_path",
    "get_model_name",
    "is_transformer_available",
    "embed_text",
    "ensure_index",
    "rebuild_index",
    "upsert_file",
    "remove_file",
    "index_file",
    "search",
    "semantic_search",
    "search_notes",
    "get_snippet",
    "invalidate_index",
]
