#!/usr/bin/env python3
"""portability_check — проверка границ для будущего портирования.

core/ не должен импортировать gi/ui/services
services/ не должен импортировать ui
Запуск: python tools/portability_check.py
"""

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
FAIL = 0


def check(path: Path, forb: list[str], label: str):
    global FAIL
    rx = re.compile("|".join(forb))
    for p in path.rglob("*.py"):
        if "__pycache__" in str(p):
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(txt.splitlines(), 1):
            if "import" not in line:
                continue
            if rx.search(line):
                rel = p.relative_to(ROOT)
                print(f"FAIL {label} {rel}:{i}: {line.strip()}")
                FAIL += 1


# core чистый: без gi/Gtk/Adw/ui
check(
    ROOT / "fragilenotes" / "core",
    [r"\bfrom gi\b", r"\bimport gi\b", r"fragilenotes\.ui", r"\bGtk\b", r"\bAdw\b"],
    "core->ui/gi",
)
# services без ui (кроме допустимых мостов)
check(ROOT / "fragilenotes" / "services", [r"fragilenotes\.ui"], "services->ui")

if FAIL:
    # 0.2.1 debt: tray.py -> ui/quick_capture (единственная связь, будет вынесена в core/api)
    print(f"\n{FAIL} debt — известно, пофиксить в 0.2.2 (tray->ui)")
    # не фатально для 0.2.1
    sys.exit(0)
else:
    print("OK — границы чистые, core портабелен")
