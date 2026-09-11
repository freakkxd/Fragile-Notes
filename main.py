#!/usr/bin/env python3
"""Fragile Notes — точка входа."""

from __future__ import annotations

import os
import sys

# Для PyInstaller bundle — найти typelibs и DLL внутри _MEIPASS
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    meipass = sys._MEIPASS  # type: ignore[attr-defined]
    for p in [
        os.path.join(meipass, "gi", "repository"),
        os.path.join(meipass, "lib", "girepository-1.0"),
        os.path.join(meipass, "girepository-1.0"),
    ]:
        if os.path.isdir(p):
            os.environ["GI_TYPELIB_PATH"] = p + os.pathsep + os.environ.get("GI_TYPELIB_PATH", "")
    # DLLs
    try:
        for dll_dir in [meipass, os.path.join(meipass, "bin"), os.path.join(meipass, "lib")]:
            if os.path.isdir(dll_dir):
                try:
                    os.add_dll_directory(dll_dir)  # type: ignore[attr-defined]
                except Exception:
                    pass
                os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass

# Для headless сборки (PyInstaller, CI) — не требовать дисплей
os.environ.setdefault("GDK_BACKEND", "offscreen")

import gi  # noqa: E402

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gsk", "4.0")
    gi.require_version("Gdk", "4.0")
except (ValueError, ImportError) as e:
    # На CI без GTK — PyInstaller analysis не должен падать
    print(f"Gtk/Adw/Gsk not available at build time: {e}", file=sys.stderr)

from gi.repository import Adw  # noqa: E402

from fragilenotes import APP_ID  # noqa: E402
from fragilenotes.ui.app import FragileWindow  # noqa: E402
from fragilenotes.ui.style import load_css  # noqa: E402


class FragileApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=0)
        self.window: FragileWindow | None = None
        self._app_tray = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        # Фоновый сервис Gio.Application --gapplication-service: держим D-Bus имя
        try:
            from fragilenotes.services.tray import is_service_mode, setup_tray

            if is_service_mode():
                try:
                    self.hold()
                except Exception:
                    pass
                # tray без окна для чистого сервиса (до первого activate)
                try:
                    from fragilenotes.config import load_settings

                    _s = load_settings()
                except Exception:
                    _s = {}
                try:
                    self._app_tray = setup_tray(self, None, _s)
                except Exception:
                    self._app_tray = None
        except Exception:
            pass

    def do_activate(self) -> None:
        load_css()
        # Тема до создания окна (Adw.StyleManager + custom.css)
        try:
            from fragilenotes.config import load_settings
            from fragilenotes.ui import theme_manager
            from fragilenotes.ui.style import apply_custom_css

            _s = load_settings()
            theme_manager.apply_theme(theme_manager.get_theme(_s))
            try:
                apply_custom_css(_s)
            except Exception:
                theme_manager.apply_custom_css(_s)
        except Exception:
            pass
        if self.window is None:
            self.window = FragileWindow(self)
            # если сервис tray был создан без окна — перепривязать к новому окну
            try:
                if getattr(self, "_app_tray", None) is not None:
                    tray = self._app_tray
                    if hasattr(tray, "window"):
                        tray.window = self.window
                    # также делегируем в оконный tray если он уже есть
                    if getattr(self.window, "_tray", None) is None:
                        self.window._tray = tray
            except Exception:
                pass
        # вместо destroy — показать (для tray hide/show)
        try:
            self.window.set_visible(True)
        except Exception:
            pass
        self.window.present()


def main() -> None:
    # CLI: fragile publish — экспорт vault без запуска GUI
    if len(sys.argv) > 1 and sys.argv[1] in ("publish", "export", "site"):
        try:
            from fragilenotes.core.publish import main as _pub_main  # noqa: E402

            raise SystemExit(_pub_main(sys.argv[1:]))
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"publish error: {exc}", file=sys.stderr)
            raise SystemExit(1)
    app = FragileApp()
    app.run(sys.argv)


if __name__ == "__main__":
    main()
