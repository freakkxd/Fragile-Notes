"""Тесты FilesView: ленивое Obsidian-дерево папок + плоский поиск."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from fragilenotes.services.vault import FileTreeNode  # noqa: E402
from fragilenotes.ui.files_view import collect_notes, count_nodes, index_nodes  # noqa: E402


def test_collect_notes_flat_and_recursive() -> None:
    """Плоский поиск рекурсивно собирает все заметки без уровней/заглушек."""
    node = FileTreeNode(
        "root", "/vault",
        dirs=[
            FileTreeNode("проекты", "/vault/проекты", dirs=[
                FileTreeNode("спорт", "/vault/проекты/спорт", [],
                             [("бег.md", "/vault/проекты/спорт/бег.md")]),
            ], files=[("план.md", "/vault/проекты/план.md")]),
            FileTreeNode("пустая", "/vault/пустая", [], []),
        ],
        files=[("индекс.md", "/vault/индекс.md"), ("заметка.txt", "/vault/заметка.txt")],
    )
    got = collect_notes(node)
    assert got == [
        ("индекс.md", "/vault/индекс.md"),
        ("заметка.txt", "/vault/заметка.txt"),
        ("план.md", "/vault/проекты/план.md"),
        ("бег.md", "/vault/проекты/спорт/бег.md"),
    ]
    assert len(got) == 4


def test_collect_notes_respects_allowed_exts() -> None:
    node = FileTreeNode("r", "/vault", [],
                        [("a.md", "/vault/a.md"), ("photo.png", "/vault/photo.png")])
    got = [n for n, _ in collect_notes(node)]
    assert "a.md" in got
    assert "photo.png" not in got


def test_index_nodes_maps_every_dir_by_abs_path() -> None:
    node = FileTreeNode(
        "root", "/vault",
        dirs=[
            FileTreeNode("проекты", "/vault/проекты", dirs=[
                FileTreeNode("спорт", "/vault/проекты/спорт", [], []),
            ], files=[]),
        ],
        files=[("индекс.md", "/vault/индекс.md")],
    )
    idx = index_nodes(node)
    assert set(idx) == {"/vault", "/vault/проекты", "/vault/проекты/спорт"}
    assert idx["/vault/проекты"].name == "проекты"


def test_count_nodes_reports_dirs_and_files() -> None:
    node = FileTreeNode(
        "root", "/vault",
        dirs=[FileTreeNode("a", "/vault/a", [], [("x.md", "/vault/a/x.md")])],
        files=[("индекс.md", "/vault/индекс.md"), ("два.txt", "/vault/два.txt")],
    )
    assert count_nodes(node) == (2, 3)


def test_files_view_has_no_placeholder_rows() -> None:
    import inspect

    src = inspect.getsource(__import__("fragilenotes.ui.files_view", fromlist=["x"]))
    assert "Placeholder" not in src and "…loading" not in src
    assert "row-expanded" in src and "лениво" in src
