"""Сканирование vault: входящие (05 Sort), свежие заметки, дайджесты ночного пробега.

Обход только лёгких операций os.scandir + чтение frontmatter — без построения UI.
Результаты кэшируются по TTL: параллельные потребители (bg-скан окна и HomeView)
в пределах одного цикла опроса делят один обход диска.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paths import resolve_paths
from ..vault import ALLOWED_EXTS, HEAVY_DIRS
from ..vault import WIKILINK_RE as _VAULT_WIKILINK_RE

_SKIP_SCAN = {"_System", "05 Sort"}

_FRONTMATTER_TITLE_RE = re.compile(r"^title:\s*[\"']?([^\"'\n]+)", re.MULTILINE)

SCAN_TTL_SECONDS = 10.0
_TITLE_CACHE_MAX = 1024

_vault_lock = threading.RLock()  # сохранён для совместимости, не алиас для кэшей
_cache_lock = threading.RLock()
_scan_cache: dict[tuple, tuple[float, object]] = {}
_scan_inflight: dict[tuple, threading.Event] = {}
_title_cache: OrderedDict[tuple[str, int], str] = OrderedDict()

TREE_TTL_SECONDS = 30.0
_tree_lock = threading.RLock()
_tree_cache: dict[str, tuple[float, FileTreeNode]] = {}
_tree_inflight: dict[str, threading.Event] = {}


@dataclass(slots=True)
class FileTreeNode:
    """Лёгкий узел дерева vault из памяти: папки рекурсивно, файлы — имена/пути.

    Строится вне GUI-потока один раз и затем служит единственным источником
    для дерева в FilesView — раскрытие папки не трогает диск.
    """

    name: str
    path: str
    dirs: list[FileTreeNode]
    files: list[tuple[str, str]]


def _scan_dir(path: Path) -> FileTreeNode:
    """Рекурсивный os.scandir без повторных stat(): DirEntry кэширует флаги."""
    dirs: list[FileTreeNode] = []
    files: list[tuple[str, str]] = []
    try:
        with os.scandir(path) as it:
            for e in it:
                name = e.name
                if name.startswith(".") or name in HEAVY_DIRS:
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        dirs.append(_scan_dir(Path(e.path)))
                    elif e.is_file(follow_symlinks=False) and e.name.lower().endswith(tuple(ALLOWED_EXTS)):
                        files.append((name, e.path))
                except OSError:
                    continue
    except OSError:
        pass
    dirs.sort(key=lambda n: n.name.lower())
    files.sort(key=lambda f: f[0].lower())
    return FileTreeNode(path.name, str(path), dirs, files)


def ensure_file_tree(settings: dict, force: bool = False) -> FileTreeNode:
    """Полная структура vault из памяти; TTL-кэш, force — принудительный пересбор.

    Защита от stampede: double-check + per-key Event. Параллельные вызовы
    делят один обход диска, время — только time.monotonic.
    """
    root = resolve_paths(settings).root
    key = str(root)
    if not force:
        now = time.monotonic()
        with _tree_lock:
            hit = _tree_cache.get(key)
            if hit is not None and now - hit[0] < TREE_TTL_SECONDS:
                return hit[1]
            inflight = _tree_inflight.get(key)
            if inflight is not None:
                event = inflight
                is_producer = False
            else:
                event = threading.Event()
                _tree_inflight[key] = event
                is_producer = True
        if not is_producer:
            event.wait(timeout=TREE_TTL_SECONDS + 5)
            with _tree_lock:
                hit = _tree_cache.get(key)
                if hit is not None and time.monotonic() - hit[0] < TREE_TTL_SECONDS:
                    return hit[1]
            # producer не заполнил (ошибка) — повторяем как продюсер
            return ensure_file_tree(settings, force=force)
        try:
            node = _scan_dir(root)
            now2 = time.monotonic()
            with _tree_lock:
                # double-check: пока строили, кто-то уже мог заполнить (при рекурсии)
                hit = _tree_cache.get(key)
                if hit is not None and now2 - hit[0] < TREE_TTL_SECONDS and hit[0] > now:
                    return hit[1]
                _tree_cache[key] = (now2, node)
            return node
        finally:
            with _tree_lock:
                _tree_inflight.pop(key, None)
            event.set()
    # force=True — строим вне lock с double-check (не держим lock секунды на scan_dir)
    node = _scan_dir(root)
    now = time.monotonic()
    with _tree_lock:
        _tree_cache[key] = (now, node)
        inflight = _tree_inflight.pop(key, None)
    if inflight is not None:
        inflight.set()
    return node


def _cached(key: tuple[Any, ...], producer: Callable[[], Any]) -> Any:
    """TTL-кэш для фоновых сканов: один продюсер на ключ в момент гонки.

    Double-check после захвата lock + per-key threading.Event — 5 параллельных
    scan_recent/scan_inbox делят один обход диска. Время — только time.monotonic.
    """
    now = time.monotonic()
    with _cache_lock:
        hit = _scan_cache.get(key)
        if hit is not None and now - hit[0] < SCAN_TTL_SECONDS:
            return hit[1]
        inflight = _scan_inflight.get(key)
        if inflight is not None:
            event = inflight
            is_producer = False
        else:
            event = threading.Event()
            _scan_inflight[key] = event
            is_producer = True
    if not is_producer:
        # ждём продюсера, не держа lock
        event.wait(timeout=SCAN_TTL_SECONDS + 5)
        with _cache_lock:
            hit = _scan_cache.get(key)
            if hit is not None and time.monotonic() - hit[0] < SCAN_TTL_SECONDS:
                return hit[1]
        # если продюсер упал — пробуем снова как продюсер
        return _cached(key, producer)
    try:
        value = producer()
        now2 = time.monotonic()
        with _cache_lock:
            # double-check: если пока мы строили кэш уже стал актуальным (редкий случай рекурсии)
            hit = _scan_cache.get(key)
            if hit is not None and now2 - hit[0] < SCAN_TTL_SECONDS and hit[0] > now:
                return hit[1]
            _scan_cache[key] = (now2, value)
        return value
    finally:
        with _cache_lock:
            _scan_inflight.pop(key, None)
        event.set()


def note_title_cached(path: Path) -> str:
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = -1
    key = (str(path), mtime)
    with _cache_lock:
        hit = _title_cache.get(key)
        if hit is not None:
            _title_cache.move_to_end(key)
            return hit
    title = note_title(path)
    with _cache_lock:
        _title_cache[key] = title
        while len(_title_cache) > _TITLE_CACHE_MAX:
            _title_cache.popitem(last=False)
    return title


@dataclass
class InboxItem:
    path: Path
    mtime: float
    status: str


@dataclass
class RecentNote:
    path: Path
    mtime: float


@dataclass
class Digest:
    path: Path
    title: str
    preview: str
    candidates: int = 0


@dataclass
class NoteHit:
    path: Path
    title: str
    mtime: float
    text: str = ""
    raw: str = ""


def note_title(path: Path) -> str:
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:600]
    except OSError:
        head = ""
    m = _FRONTMATTER_TITLE_RE.search(head)
    if m:
        return m.group(1).strip()
    name = path.stem
    name = re.sub(r"^(tg|web)-[a-z0-9_-]+-\d+(-|$)", "", name)
    name = name.replace("-", " ").replace("_", " ").strip()
    return name or path.stem


def _iter_notes(root: Path) -> Any:
    """Рекурсивный обход через os.scandir: DirEntry.stat() закэширован ядром,
    поэтому свежие заметки находятся без отдельного stat() на каждый файл."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    name = e.name
                    if e.is_dir(follow_symlinks=False):
                        if name not in HEAVY_DIRS and name not in _SKIP_SCAN and not name.startswith("."):
                            stack.append(Path(e.path))
                    elif name.endswith(".md"):
                        yield e.path, e.stat().st_mtime
        except OSError:
            continue


def scan_recent_notes(settings: dict, days: int = 7, limit: int = 10) -> list[RecentNote]:
    paths = resolve_paths(settings)
    root = paths.root

    def produce() -> list[RecentNote]:
        cutoff = time.time() - days * 86400
        out = [
            RecentNote(path=Path(p), mtime=mt)
            for p, mt in _iter_notes(root)
            if mt >= cutoff
        ]
        out.sort(key=lambda n: n.mtime, reverse=True)
        return out[:limit]

    return list(_cached(("recent", str(root), days, limit), produce))


def scan_inbox(settings: dict, limit: int = 10) -> tuple[list[InboxItem], int, int]:
    """Входящие из 05 Sort: файлы верхнего уровня + AO Daily/AO Weekly.

    Возвращает (последние элементы, всего, неразобранных undecided).
    """
    paths = resolve_paths(settings)

    def produce() -> tuple[list[InboxItem], int, int]:
        items: list[InboxItem] = []
        total = 0
        undecided = 0
        sort_dir = paths.sort
        if not sort_dir.is_dir():
            return [], 0, 0
        scan_dirs = [sort_dir]
        for sub in ("AO Daily", "AO Weekly"):
            p = sort_dir / sub
            if p.is_dir():
                scan_dirs.append(p)
        for d in scan_dirs:
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if not e.is_file() or not e.name.endswith(".md"):
                            continue
                        total += 1
                        status = _sort_status(Path(e.path))
                        if status == "undecided":
                            undecided += 1
                        items.append(InboxItem(path=Path(e.path), mtime=e.stat().st_mtime, status=status))
            except OSError:
                continue
        items.sort(key=lambda i: i.mtime, reverse=True)
        return items[:limit], total, undecided

    items, total, undecided = _cached(("inbox", str(paths.sort), limit), produce)
    return list(items), total, undecided


def _sort_status(path: Path) -> str:
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:500]
    except OSError:
        return "none"
    m = re.search(r"ao_sort_status:\s*[\"']?([\w-]+)", head)
    return m.group(1) if m else "none"


def latest_digest(settings: dict) -> Digest | None:
    paths = resolve_paths(settings)

    def produce() -> Digest | None:
        daily_dir = paths.sort / "AO Daily"
        if not daily_dir.is_dir():
            return None
        files = [p for p in daily_dir.glob("*.md")]
        if not files:
            return None
        best = max(files, key=lambda p: p.stat().st_mtime)
        try:
            text = best.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        body = re.sub(r"^---.*?---", "", text, count=1, flags=re.DOTALL).strip()
        candidates = len(re.findall(r"^-\s+\[\[", body, flags=re.MULTILINE))
        preview = " ".join(body.split())[:220]
        return Digest(path=best, title=note_title(best), preview=preview, candidates=candidates)

    return _cached(("digest", str(paths.sort)), produce)


def _note_body(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _notes_index(settings: dict) -> list[NoteHit]:
    """Индекс (path, title, mtime, text, raw) по vault; TTL-кэш как остальные сканеры."""
    root = resolve_paths(settings).root

    def produce() -> list[NoteHit]:
        out: list[NoteHit] = []
        for p, mt in _iter_notes(root):
            raw = _note_body(Path(p))
            out.append(
                NoteHit(
                    path=Path(p),
                    title=note_title(Path(p)),
                    mtime=mt,
                    text=raw.lower(),
                    raw=raw,
                )
            )
        return out

    return list(_cached(("index", str(root)), produce))


def _rank_query(h: NoteHit, q: str) -> int:
    """Ранжирование: точное имя/title > префикс > вхождение в имя/title > содержимое."""
    stem = h.path.stem.lower()
    title = h.title.lower()
    if stem == q or title == q:
        return 0
    if stem.startswith(q) or title.startswith(q):
        return 1
    if q in stem or q in title:
        return 2
    return 3


def search_notes(settings: dict, query: str, limit: int = 14) -> list[NoteHit]:
    """Подстрока-поиск по именам, title и содержимому заметок.

    Совпадения по имени/title всегда выше чисто контентных.
    """
    q = query.strip().lower()
    if not q:
        return []
    hits = [
        h for h in _notes_index(settings)
        if q in h.text or q in h.title.lower() or q in h.path.stem.lower()
    ]
    hits.sort(key=lambda h: (_rank_query(h, q), -h.mtime))
    return hits[:limit]


def resolve_wikilink(settings: dict, target: str) -> Path | None:
    """[[цель]] → абсолютный путь. Точное совпадение по имени файла или title."""
    q = (target or "").strip()
    if not q:
        return None
    q = q.lower()
    for h in _notes_index(settings):
        if h.path.stem.lower() == q:
            return h.path
    for h in _notes_index(settings):
        if h.title.lower() == q:
            return h.path
    return None


# ── Граф wikilink'ов (бэклинки / исходящие) ─────────────────────────────────
_LINK_GRAPH_TTL = SCAN_TTL_SECONDS
_link_lock = threading.RLock()
_link_cache: dict[str, tuple[float, dict[str, list[Path]], dict[str, list[Path]]]] = {}
_link_inflight: dict[str, threading.Event] = {}


def _guess_root_for_path(path: Path) -> Path:
    """Эвристика: найти vault-root по маркерам (как в graph_view фолбэк)."""
    try:
        p = Path(path).resolve()
    except Exception:
        p = Path(path)
    # walk up от файла/папки
    start = p if p.is_dir() else p.parent
    for anc in [start, *list(start.parents)]:
        try:
            if (anc / "01 Home").is_dir() or (anc / ".obsidian").is_dir() or (anc / "02 Daily").is_dir():
                return anc
        except Exception:
            continue
        # also consider vault containing _System
        try:
            if (anc / "_System").is_dir():
                # ensure looks like vault (has any md)
                return anc
        except Exception:
            continue
    # fallback: parent of file
    return start


def _settings_for_guess(root: Path) -> dict:
    return {
        "vault_root": str(root),
        "daily_folder": "02 Daily",
        "tm_tasks_folder": "_System/TaskManagerMain/_system/tasks",
        "tm_comments_folder": "_System/TaskManagerMain/_system/comments",
        "tm_templates_root": "_System/TaskManagerMain/Templates",
    }


def _ensure_link_graph(settings: dict) -> tuple[dict[str, list[Path]], dict[str, list[Path]]]:
    """Кэшированный граф: outgoing[source_str] -> [dest], incoming[dest_str] -> [source].

    Защита от stampede: double-check + per-key Event, время только monotonic.
    """
    root = resolve_paths(settings).root
    key = str(root)
    now = time.monotonic()
    with _link_lock:
        hit = _link_cache.get(key)
        if hit is not None and now - hit[0] < _LINK_GRAPH_TTL:
            return hit[1], hit[2]
        inflight = _link_inflight.get(key)
        if inflight is not None:
            event = inflight
            is_producer = False
        else:
            event = threading.Event()
            _link_inflight[key] = event
            is_producer = True
    if not is_producer:
        event.wait(timeout=_LINK_GRAPH_TTL + 5)
        with _link_lock:
            hit = _link_cache.get(key)
            if hit is not None and time.monotonic() - hit[0] < _LINK_GRAPH_TTL:
                return hit[1], hit[2]
        return _ensure_link_graph(settings)
    try:
        # build fresh (единственный продюсер на ключ)
        hits = _notes_index(settings)
        # maps for резолва
        stem_map: dict[str, Path] = {}
        title_map: dict[str, Path] = {}
        for h in hits:
            try:
                stem = h.path.stem.lower()
                title = (h.title or h.path.stem).lower()
            except Exception:
                continue
            stem_map.setdefault(stem, h.path)
            title_map.setdefault(title, h.path)
        outgoing: dict[str, list[Path]] = {str(h.path): [] for h in hits}
        incoming: dict[str, list[Path]] = {str(h.path): [] for h in hits}
        # для дедупликации рёбер: source -> set(dest_str)
        seen: dict[str, set[str]] = {k: set() for k in outgoing}
        for h in hits:
            src = str(h.path)
            raw = getattr(h, "raw", "") or ""
            if not raw:
                continue
            try:
                targets = _VAULT_WIKILINK_RE.findall(raw)
            except Exception:
                targets = []
            # fallback если vault regex не нашёл, но [[ есть
            if not targets and "[[" in raw:
                targets = re.findall(r"\[\[([^\]|#]+)", raw)
            for t in targets:
                tgt = (t or "").strip()
                if not tgt:
                    continue
                low = tgt.lower()
                dest = stem_map.get(low)
                if dest is None:
                    dest = title_map.get(low)
                if dest is None:
                    continue
                dstr = str(dest)
                if dstr == src:
                    continue
                if dstr in seen[src]:
                    continue
                seen[src].add(dstr)
                outgoing[src].append(dest)
                # incoming may not have key if dest was outside _iter_notes? but it is inside
                if dstr not in incoming:
                    incoming[dstr] = []
                incoming[dstr].append(h.path)
        now2 = time.monotonic()
        with _link_lock:
            # double-check перед записью
            hit = _link_cache.get(key)
            if hit is not None and now2 - hit[0] < _LINK_GRAPH_TTL and hit[0] > now:
                return hit[1], hit[2]
            _link_cache[key] = (now2, outgoing, incoming)
        return outgoing, incoming
    finally:
        with _link_lock:
            _link_inflight.pop(key, None)
        event.set()


def _resolve_link_args(*args: Any, **kwargs: Any) -> tuple[dict[str, Any] | None, Path | None]:
    """Универсальный парсер для get_backlinks/get_outgoing_links.

    Поддерживает:
      (settings: dict, path: Path|str)
      (path: Path|str)
      (path: Path|str, settings: dict)
      kwargs: settings=..., target_path/source_path/path=...
    """
    settings: dict | None = kwargs.get("settings")
    path: Path | None = None
    # kwargs path aliases
    for k in ("target_path", "source_path", "path"):
        if k in kwargs and kwargs[k] is not None:
            path = Path(kwargs[k])
            break
    if args:
        if isinstance(args[0], dict):
            settings = args[0]
            if len(args) > 1 and path is None:
                path = Path(args[1])
        elif isinstance(args[0], (str, Path)):
            if path is None:
                path = Path(args[0])
            # second arg maybe settings dict
            if len(args) > 1 and isinstance(args[1], dict):
                settings = args[1]
            elif len(args) > 1 and isinstance(args[1], (str, Path)) and settings is None:
                # unlikely but treat second as ignored
                pass
        # if first was dict already handled; if 3 args?
    return settings, path


def get_outgoing_links(*args: Any, **kwargs: Any) -> list[Path]:
    """Куда ссылается заметка `source_path` (через [[wikilink]]).

    Сигнатуры:
      get_outgoing_links(settings, source_path)
      get_outgoing_links(source_path)  — vault-root угадывается по пути
    Кэш TTL как у graph_view/_notes_index.
    """
    settings, src = _resolve_link_args(*args, **kwargs)
    # allow positional alias: get_outgoing_links(source_path) where source_path passed as first arg without naming
    if src is None and args and isinstance(args[0], (str, Path)):
        src = Path(args[0])
    elif src is None and kwargs.get("source_path"):
        src = Path(kwargs["source_path"])
    if src is None:
        return []
    if settings is None:
        try:
            guess_root = _guess_root_for_path(src)
            settings = _settings_for_guess(guess_root)
        except Exception:
            return []
    try:
        outgoing, _ = _ensure_link_graph(settings)
    except Exception:
        return []
    # ключи — абсолютные пути как в индексе
    for cand in (str(src.resolve()) if src.exists() else None, str(src), str(src.absolute()) if hasattr(src, "absolute") else None):
        if cand and cand in outgoing:
            return list(outgoing[cand])
    # fallback: try matching by suffix (for temp paths with symlink)
    s = str(src)
    if s in outgoing:
        return list(outgoing[s])
    # also try normalized / compare by Path equality
    try:
        rp = src.resolve()
        for k, v in outgoing.items():
            if Path(k).resolve() == rp:
                return list(v)
    except Exception:
        pass
    return []


def get_backlinks(*args: Any, **kwargs: Any) -> list[Path]:
    """Кто ссылается на `target_path` (бэклинки).

    Сигнатуры аналогично get_outgoing_links.
    """
    settings, tgt = _resolve_link_args(*args, **kwargs)
    if tgt is None and args and isinstance(args[0], (str, Path)):
        tgt = Path(args[0])
    elif tgt is None and kwargs.get("target_path"):
        tgt = Path(kwargs["target_path"])
    if tgt is None:
        return []
    if settings is None:
        try:
            guess_root = _guess_root_for_path(tgt)
            settings = _settings_for_guess(guess_root)
        except Exception:
            return []
    try:
        _, incoming = _ensure_link_graph(settings)
    except Exception:
        return []
    for cand in (str(tgt.resolve()) if tgt.exists() else None, str(tgt), str(tgt.absolute()) if hasattr(tgt, "absolute") else None):
        if cand and cand in incoming:
            return list(incoming[cand])
    s = str(tgt)
    if s in incoming:
        return list(incoming[s])
    try:
        rp = tgt.resolve()
        for k, v in incoming.items():
            if Path(k).resolve() == rp:
                return list(v)
    except Exception:
        pass
    return []


# ── Теги # ─────────────────────────────────────────────────────────
# Обсидиан-стиль: #тег — слово после #, без пробела/решётки внутри,
# исключая содержимое ```-кодовых блоков и `inline code`.
# Расширено: ловит (#тег) — префикс '(' считается валидным разделителем.
TAG_RE = re.compile(r"(?:^|[\s\(])#([^\s#]+)")
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def _strip_code_blocks(text: str) -> str:
    """Убрать ```-блоки и `inline code` перед парсингом тегов."""
    text = _CODE_BLOCK_RE.sub("", text)
    return _INLINE_CODE_RE.sub("", text)


def extract_tags(text: str) -> list[str]:
    """Собрать #теги из текста, игнорируя ```-блоки и `inline code`."""
    cleaned = _strip_code_blocks(text)
    raw_tags = TAG_RE.findall(cleaned)
    out: list[str] = []
    for t in raw_tags:
        tag = t.strip().rstrip(".,;:!?)]\"'.,")
        if tag:
            out.append(tag)
    return out


def scan_tags(settings: dict) -> dict[str, list[Path]]:
    """Все теги vault: {tag -> [Path,...]}. Кэш TTL как у остальных сканеров."""
    root = resolve_paths(settings).root

    def produce() -> dict[str, list[Path]]:
        out: dict[str, list[Path]] = {}
        # дедупликация внутри одного файла (один тег -> один файл единожды)
        seen_file: dict[str, set[str]] = {}
        for p, _mt in _iter_notes(root):
            path = Path(p)
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            cleaned = _strip_code_blocks(raw)
            for raw_tag in TAG_RE.findall(cleaned):
                tag = raw_tag.strip()
                if not tag:
                    continue
                # убрать хвостовую пунктуацию (.,;:!?)"]') — #tag, -> tag
                tag = tag.rstrip(".,;:!?)]\"'.,")
                if not tag:
                    continue
                lst = out.setdefault(tag, [])
                # dedup на уровне файла
                seen = seen_file.setdefault(tag, set())
                sp = str(path)
                if sp not in seen:
                    seen.add(sp)
                    lst.append(path)
        return out

    return dict(_cached(("tags", str(root)), produce))


def get_all_tags(settings: dict) -> dict[str, list[Path]]:
    """Алиас для scan_tags — для внешних вызовов."""
    return scan_tags(settings)


def get_popular_tags(settings: dict, limit: int = 20) -> list[tuple[str, int]]:
    """Популярные теги: [(tag, count), ...] отсортировано по убыванию count."""
    tags = scan_tags(settings)
    items = sorted(tags.items(), key=lambda kv: len(kv[1]), reverse=True)
    if limit is not None and limit > 0:
        items = items[:limit]
    return [(k, len(v)) for k, v in items]


def get_files_by_tag(settings: dict, tag: str) -> list[Path]:
    """Файлы с конкретным тегом (точное совпадение, без #)."""
    if not tag:
        return []
    t = tag.lstrip("#").strip()
    tags = scan_tags(settings)
    # точный ключ, затем fallback case-insensitive
    if t in tags:
        return list(tags[t])
    low = t.lower()
    for k, v in tags.items():
        if k.lower() == low:
            return list(v)
    return []


def get_tags_index(settings: dict) -> tuple[dict[str, list[Path]], list[tuple[str, int]]]:
    """Удобный доступ: (dict[tag]->[Path], popular). Кэшировано через scan_tags/get_popular_tags."""
    d = scan_tags(settings)
    popular = get_popular_tags(settings, limit=20)
    return d, popular


# Алиасы для совместимости с возможной проверкой имени
scan_all_tags = scan_tags
get_tags = scan_tags
collect_tags = scan_tags
parse_tags = extract_tags


def invalidate_vault_cache() -> None:
    # Раздельные RLock'и + пробуждение ждущих через Event.set (иначе висят 5с)
    with _cache_lock:
        _scan_cache.clear()
        _title_cache.clear()
        scan_events = list(_scan_inflight.values())
        _scan_inflight.clear()
    for ev in scan_events:
        try:
            ev.set()
        except Exception:
            pass
    with _tree_lock:
        _tree_cache.clear()
        tree_events = list(_tree_inflight.values())
        _tree_inflight.clear()
    for ev in tree_events:
        try:
            ev.set()
        except Exception:
            pass
    with _link_lock:
        _link_cache.clear()
        link_events = list(_link_inflight.values())
        _link_inflight.clear()
    for ev in link_events:
        try:
            ev.set()
        except Exception:
            pass
