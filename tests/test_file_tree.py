"""Тесты предзагруженной структуры дерева vault (чистая логика, без GTK)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fragilenotes.services.vault import FileTreeNode, ensure_file_tree

_KEYS = {
    "vault_root": "",
    "daily_folder": "02 Daily",
    "tm_tasks_folder": "_System/TaskManagerMain/_system/tasks",
    "tm_comments_folder": "_System/TaskManagerMain/_system/comments",
    "tm_templates_root": "_System/TaskManagerMain/Templates",
}


def _mk() -> tuple[Path, dict]:
    tmp = Path(tempfile.mkdtemp(prefix="fn-tree-"))
    (tmp / "Альфа").mkdir()
    (tmp / "Альфа" / "подпапка").mkdir()
    (tmp / "node_modules").mkdir()
    (tmp / ".obsidian").mkdir()
    (tmp / "Альфа" / "б.md").write_text("б", encoding="utf-8")
    (tmp / "Альфа" / "а.md").write_text("а", encoding="utf-8")
    (tmp / "Альфа" / "подпапка" / "в.txt").write_text("в", encoding="utf-8")
    (tmp / "Альфа" / "картинка.png").write_text("png", encoding="utf-8")
    (tmp / "node_modules" / "скрыто.md").write_text("н", encoding="utf-8")
    (tmp / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")
    (tmp / "корень.md").write_text("к", encoding="utf-8")
    (tmp / "корень.json").write_text("[]", encoding="utf-8")
    return tmp, {**_KEYS, "vault_root": str(tmp)}


def test_tree_structure_sorted() -> None:
    root, settings = _mk()
    node = ensure_file_tree(settings, force=True)
    assert isinstance(node, FileTreeNode)
    assert [d.name for d in node.dirs] == ["Альфа"]
    assert [f[0] for f in node.files] == ["корень.json", "корень.md"]
    alfa = node.dirs[0]
    assert [d.name for d in alfa.dirs] == ["подпапка"]
    assert [f[0] for f in alfa.files] == ["а.md", "б.md"]
    assert [f[0] for f in alfa.dirs[0].files] == ["в.txt"]
    shutil.rmtree(root, ignore_errors=True)


def test_tree_skips_hidden_and_heavy() -> None:
    root, settings = _mk()
    node = ensure_file_tree(settings, force=True)
    names = [d.name for d in node.dirs] + [f[0] for f in node.files]
    assert "node_modules" not in names
    assert ".obsidian" not in names
    assert not any("скрыто.md" in f[0] or "workspace.json" in f[0] for f in node.files)
    shutil.rmtree(root, ignore_errors=True)


def test_tree_filters_non_allowed_exts() -> None:
    root, settings = _mk()
    node = ensure_file_tree(settings, force=True)
    flat = [f[0] for f in node.files] + [f[0] for f in node.dirs[0].files]
    assert "картинка.png" not in flat
    shutil.rmtree(root, ignore_errors=True)


def test_tree_nodes_are_immutable_snapshot() -> None:
    root, settings = _mk()
    first = ensure_file_tree(settings, force=False)
    second = ensure_file_tree(settings, force=False)
    assert first is second
    (root / "новый.md").write_text("н", encoding="utf-8")
    forced = ensure_file_tree(settings, force=True)
    assert forced is not first
    assert "новый.md" in [f[0] for f in forced.files]
    shutil.rmtree(root, ignore_errors=True)
