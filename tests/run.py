#!/usr/bin/env python3
"""Тонкий враппер для совместимости: делегирует pytest.

Запуск:
  python tests/run.py [pytest args]
  python -m pytest tests/
Старое поведение (хардкод TESTS) удалено — теперь единый вход через pytest.
Если pytest не установлен, выполняется fallback-раннер без зависимостей.
"""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path


def _fallback() -> int:
    """Старый раннер без внешних зависимостей (используется только если pytest отсутствует)."""
    TESTS = [
        "test_runner",
        "test_tasks",
        "test_vault_media",
        "test_markdown",
        "test_scale",
        "test_search",
        "test_file_tree",
        "test_css",
        "test_files_view",
        "test_syntax",
        "test_workspace",
    ]
    root = Path(__file__).resolve().parent
    project_root = root.parent
    sys.path.insert(0, str(project_root))
    sys.path.insert(0, str(root))
    failures = 0
    passed = 0
    for name in TESTS:
        try:
            mod = importlib.import_module(name)
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL import {name}")
            traceback.print_exc()
            continue
        for attr in sorted(dir(mod)):
            if not attr.startswith("test_"):
                continue
            fn = getattr(mod, attr)
            if not callable(fn):
                continue
            try:
                fn()
                passed += 1
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}.{attr}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failures} failed (fallback)")
    return 1 if failures else 0


def main() -> int:
    # Обеспечиваем импорт fragilenotes при запуске как `python tests/run.py`
    root = Path(__file__).resolve().parent
    project_root = root.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    try:
        import pytest  # type: ignore[import-not-found]
    except ImportError:
        print("pytest не установлен — используется fallback-раннер", file=sys.stderr)
        return _fallback()
    # Делегируем pytest; по умолчанию гоняем tests/
    args = sys.argv[1:] if len(sys.argv) > 1 else ["tests"]
    # pytest.main возвращает код возврата
    return int(pytest.main(args))


if __name__ == "__main__":
    raise SystemExit(main())
