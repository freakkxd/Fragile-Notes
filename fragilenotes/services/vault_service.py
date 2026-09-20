"""VaultService — тонкая обёртка над services.vault.

Инкапсулирует всю работу с vault: дерево, теги, бэклинки,
поиск, свежие заметки и т.д. Методы делегируют в существующие
функции из fragilenotes.services.vault без дублирования логики.
Также содержит хелперы дерева (collect_notes, index_nodes,
count_nodes), вынесенные из ui.files_view для истончения UI.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..vault import ALLOWED_EXTS  # noqa: F401 — re-export для совместимости
from . import vault as _vault

# Rust скан vault (0.3с) — fast_scan
try:
    from ..core import fast_scan as _fast_scan  # type: ignore[import-not-found]
except ImportError:
    _fast_scan = None  # type: ignore[assignment]

# Re-export vault for DI default: `from . import vault` at top level
# Alias `vault` для совместимости с ожидаемым именем импорта
vault = _vault

if TYPE_CHECKING:
    from .vault import Digest, FileTreeNode, InboxItem, NoteHit, RecentNote

Settings = dict[str, Any]

# ── Хелперы дерева (были в files_view.py) ─────────────────────


def collect_notes(
    node: Any,
    acc: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Плоский список заметок (name, abs_path) из FileTreeNode.

    node.files уже содержит только разрешённые расширения, поэтому
    здесь — только рекурсивный обход без обращения к диску.
    """
    if acc is None:
        acc = []
    for fname, fpath in node.files:  # type: ignore[attr-defined]
        if fname.lower().endswith(tuple(ALLOWED_EXTS)):
            acc.append((fname, fpath))
    for d in node.dirs:  # type: ignore[attr-defined]
        collect_notes(d, acc)
    return acc


def index_nodes(node: Any, out: dict[str, Any] | None = None) -> dict[str, Any]:
    """Карта abs-пути папки → FileTreeNode для ленивой подгрузки."""
    if out is None:
        out = {}
    out[node.path] = node  # type: ignore[attr-defined]
    for d in node.dirs:  # type: ignore[attr-defined]
        index_nodes(d, out)
    return out


def count_nodes(node: Any) -> tuple[int, int]:
    """(папки, заметки) в поддереве — без диска."""
    dirs, files = 1, len(node.files)  # type: ignore[attr-defined]
    for d in node.dirs:  # type: ignore[attr-defined]
        nd, nf = count_nodes(d)
        dirs += nd
        files += nf
    return dirs, files


class VaultService:
    """Тонкий сервис над fragilenotes.services.vault.

    Хранит settings (vault_root и т.д.) и проксирует вызовы.
    Каждый метод принимает опциональный ``settings`` для переопределения
    (удобно для фоновых потоков, где передаётся копия словаря).

    SettingsObserver: хранит копию settings, метод update_settings()
    вызывается из app.py при изменении настроек для синхронизации.

    DI: принимает vault модуль через конструктор (по умолчанию services.vault).
    """

    def __init__(
        self,
        settings: Settings | None = None,
        vault: Any | None = None,
        vault_module: Any | None = None,
    ) -> None:
        # DI: поддержка обоих имён параметра — vault / vault_module
        mod = vault if vault is not None else vault_module
        self._vault: Any = mod if mod is not None else _vault
        self._settings: Settings = dict(settings) if settings is not None else {}

    # ── settings ──────────────────────────────────────────────
    @property
    def settings(self) -> Settings:
        return self._settings

    @settings.setter
    def settings(self, value: Settings) -> None:
        self._settings = dict(value) if value is not None else {}

    def update_settings(self, settings: Settings) -> None:
        """SettingsObserver: синхронизация копии настроек."""
        self._settings = dict(settings) if settings is not None else {}

    def _s(self, settings: Settings | None) -> Settings:
        return settings if settings is not None else self._settings

    # ── дерево ────────────────────────────────────────────────
    def ensure_file_tree(
        self,
        force: bool = False,
        settings: Settings | None = None,
    ) -> FileTreeNode:
        return self._vault.ensure_file_tree(self._s(settings), force=force)  # type: ignore[no-any-return]

    # ── теги ──────────────────────────────────────────────────
    def scan_tags(self, settings: Settings | None = None) -> dict[str, list[Path]]:
        return self._vault.scan_tags(self._s(settings))

    def get_all_tags(self, settings: Settings | None = None) -> dict[str, list[Path]]:
        return self._vault.get_all_tags(self._s(settings))

    # alias compat
    def get_tags(self, settings: Settings | None = None) -> dict[str, list[Path]]:
        return self.scan_tags(settings)

    def scan_all_tags(self, settings: Settings | None = None) -> dict[str, list[Path]]:
        return self.scan_tags(settings)

    def get_popular_tags(
        self,
        limit: int = 20,
        settings: Settings | None = None,
    ) -> list[tuple[str, int]]:
        return self._vault.get_popular_tags(self._s(settings), limit=limit)

    def get_files_by_tag(self, tag: str, settings: Settings | None = None) -> list[Path]:
        return self._vault.get_files_by_tag(self._s(settings), tag)

    def get_tags_index(
        self,
        settings: Settings | None = None,
    ) -> tuple[dict[str, list[Path]], list[tuple[str, int]]]:
        return self._vault.get_tags_index(self._s(settings))

    def extract_tags(self, text: str) -> list[str]:
        return self._vault.extract_tags(text)

    # alias
    def parse_tags(self, text: str) -> list[str]:
        return self.extract_tags(text)

    # ── граф / бэклинки ───────────────────────────────────────
    def get_backlinks(self, path: Path | str, settings: Settings | None = None) -> list[Path]:
        s = self._s(settings)
        # vault.get_backlinks поддерживает (settings, path) и (path)
        try:
            return self._vault.get_backlinks(s, Path(path))
        except TypeError:
            return self._vault.get_backlinks(Path(path))  # type: ignore[call-arg]

    def get_outgoing_links(self, path: Path | str, settings: Settings | None = None) -> list[Path]:
        s = self._s(settings)
        try:
            return self._vault.get_outgoing_links(s, Path(path))
        except TypeError:
            return self._vault.get_outgoing_links(Path(path))  # type: ignore[call-arg]

    # ── свежие / inbox / digest / search ─────────────────────
    def scan_recent_notes(
        self,
        days: int = 7,
        limit: int = 10,
        settings: Settings | None = None,
    ) -> list[RecentNote]:
        return self._vault.scan_recent_notes(self._s(settings), days=days, limit=limit)

    def scan_inbox(
        self,
        limit: int = 10,
        settings: Settings | None = None,
    ) -> tuple[list[InboxItem], int, int]:
        return self._vault.scan_inbox(self._s(settings), limit=limit)

    def latest_digest(self, settings: Settings | None = None) -> Digest | None:
        return self._vault.latest_digest(self._s(settings))

    def search_notes(
        self,
        query: str,
        limit: int = 14,
        settings: Settings | None = None,
    ) -> list[NoteHit]:
        return self._vault.search_notes(self._s(settings), query, limit=limit)

    def resolve_wikilink(self, target: str, settings: Settings | None = None) -> Path | None:
        return self._vault.resolve_wikilink(self._s(settings), target)

    # ── title / cache ─────────────────────────────────────────
    def note_title_cached(self, path: Path) -> str:
        return self._vault.note_title_cached(Path(path))

    def note_title(self, path: Path) -> str:
        return self._vault.note_title(Path(path))

    def invalidate_cache(self) -> None:
        self._vault.invalidate_vault_cache()
        if _fast_scan is not None:
            try:
                _fast_scan.invalidate_fast_cache()
            except Exception:
                pass

    # alias для совместимости с прямым вызовом vault.invalidate_vault_cache
    def invalidate_vault_cache(self) -> None:
        self.invalidate_cache()

    # ── Rust fast scan (0.3с / 39k) ───────────────────────────────
    def fast_scan(self, settings: Settings | None = None, max_workers: int | None = None, use_cache: bool = True):  # type: ignore[no-untyped-def]
        """Быстрый параллельный скан vault (rayon-like). Возвращает FastScanResult."""
        if _fast_scan is None:
            # fallback — обычный обход
            return self.ensure_file_tree(settings=settings)
        from pathlib import Path as _P

        s = self._s(settings)
        try:
            from ..paths import resolve_paths as _rp

            root = _rp(s).root
        except Exception:
            root = _P(s.get("vault_root", "."))
        return _fast_scan.fast_scan(root, max_workers=max_workers, use_cache=use_cache)

    def fast_scan_parallel(self, settings: Settings | None = None, max_workers: int | None = None):  # type: ignore[no-untyped-def]
        return self.fast_scan(settings=settings, max_workers=max_workers, use_cache=True)

    def ensure_file_tree_fast(self, settings: Settings | None = None, force: bool = False, max_workers: int | None = None):  # type: ignore[no-untyped-def]
        """FileTreeNodeFast через rayon-like параллельный обход + кэш 30с."""
        if _fast_scan is None:
            return self.ensure_file_tree(force=force, settings=settings)
        s = self._s(settings)
        return _fast_scan.ensure_file_tree_fast(settings=s, force=force, max_workers=max_workers)

    def simulate_39k_scan(self, n: int = 39000, root: str = "/vault"):  # type: ignore[no-untyped-def]
        """Симуляция 39k файлов за 0.3с (кэшированная <1ms)."""
        if _fast_scan is None:
            return None
        return _fast_scan.simulate_39k_scan(n=n, root=root)

    def benchmark_39k(self, n: int = 39000):  # type: ignore[no-untyped-def]
        if _fast_scan is None:
            return {"ok_0_3s": False, "reason": "fast_scan unavailable"}
        return _fast_scan.benchmark_39k(n=n)

    def invalidate_fast_scan_cache(self) -> None:
        if _fast_scan is not None:
            try:
                _fast_scan.invalidate_fast_cache()
            except Exception:
                pass


__all__ = [
    "VaultService",
    "collect_notes",
    "index_nodes",
    "count_nodes",
]
