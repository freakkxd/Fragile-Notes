#!/usr/bin/env python3
"""devshot: рендерит настоящее окно Fragile Notes на :1 и снимает все вкладки.

Запуск:  python3 tools/devshot.py [outdir]
Не трогает реальный конфиг (XDG_CONFIG_HOME → временный).
"""
from __future__ import annotations

import os
import subprocess
import sys

os.environ.setdefault("GDK_BACKEND", "x11")
XDG = "/tmp/opencode/devshot/cfg"
os.environ["XDG_CONFIG_HOME"] = XDG
os.makedirs(XDG, exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib  # noqa: E402

from fragilenotes import APP_ID  # noqa: E402
from fragilenotes.ui.app import VIEWS, FragileWindow  # noqa: E402
from fragilenotes.ui.style import load_css  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/opencode/devshot/shots"
os.makedirs(OUT, exist_ok=True)

DELAY = {  # секции дают фоновым сканам время отрисовать данные
    "home": 3500,
    "runner": 1800,
    "tasks": 2500,
    "daily": 1800,
    "files": 2200,
    "media": 2500,
    "settings": 1200,
}


class Harness:
    def __init__(self) -> None:
        self.app = Adw.Application(application_id=APP_ID + ".devshot")
        load_css()
        self.win = FragileWindow(self.app)
        mon = Gdk.Display.get_default().get_monitors()[0]
        geo = mon.get_geometry()
        self.win.set_default_size(min(1360, geo.width), min(900, geo.height))
        self.win.present()
        self.i = 0
        self.loop = GLib.MainLoop()
        GLib.timeout_add(1200, self._step)

    def _step(self) -> bool:
        if self.i >= len(VIEWS):
            self.win.close()
            self.loop.quit()
            return False
        name = VIEWS[self.i]
        self.win._show_view(name)
        GLib.timeout_add(int(DELAY.get(name, 1500)), self._shot, name)
        return False

    def _shot(self, name: str) -> bool:
        for _ in range(5):
            r = subprocess.run(
                ["import", "-display", ":1", "-window", "root", f"{OUT}/{name}.png"],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                break
        print(f"shot {name}", flush=True)
        self.i += 1
        GLib.timeout_add(150, self._step)
        return False

    def run(self) -> None:
        self.loop.run()


if __name__ == "__main__":
    Harness().run()
