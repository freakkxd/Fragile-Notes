"""WorkspaceMixin — управление панелью, Paned и размерами окна.

Вынесено из ui.app.FragileWindow для истончения главного окна.
Миксин ожидает, что у host-класса есть атрибуты:

- self.settings: dict
- self.top: Gtk.Paned
- self.sidebar_revealer: Gtk.Revealer
- self.sidebar_toggle: Gtk.Button
- self._sidebar_width: int

Методы тонко оборачивают логику из ui.workspace (модель ribbon/panel).
"""

from __future__ import annotations

from gi.repository import GLib, Gtk

from ..config import save_settings
from . import workspace as ws

_SIDEBAR_MIN = ws.SIDEBAR_WIDTH_MIN
_SIDEBAR_MAX = ws.SIDEBAR_WIDTH_MAX
_SIDEBAR_DEFAULT = ws.SIDEBAR_WIDTH_DEFAULT
_SIDEBAR_COLLAPSED = ws.SIDEBAR_COLLAPSED_WIDTH


class WorkspaceMixin:
    """Миксин для FragileWindow: Paned, сайдбар, размеры окна."""

    # чтобы mypy/pyright не ругались — атрибуты будут у наследника
    settings: dict
    top: Gtk.Paned
    sidebar_revealer: Gtk.Revealer
    sidebar_toggle: Gtk.Button
    _sidebar_width: int

    # ── размеры сайдбара ─────────────────────────────────────
    def _get_sidebar_width(self) -> int:
        raw = (self.settings.get("workspace") or {}).get("sidebar_width")
        if raw is None:
            raw = ws.normalize(self.settings).get("sidebar_width", _SIDEBAR_DEFAULT)
        try:
            v = int(raw)
        except (TypeError, ValueError):
            v = _SIDEBAR_DEFAULT
        return max(_SIDEBAR_MIN, min(_SIDEBAR_MAX, v))

    def _save_sidebar_width(self, width: int) -> None:
        width = max(_SIDEBAR_MIN, min(_SIDEBAR_MAX, int(width)))
        self._sidebar_width = width
        wsd = dict(self.settings.get("workspace") or {})
        if wsd.get("sidebar_width") != width:
            wsd["sidebar_width"] = width
            self.settings["workspace"] = wsd
            save_settings(self.settings)

    def _on_paned_position(self, paned: Gtk.Paned, _pspec) -> None:
        if (
            not getattr(self, "sidebar_revealer", None)
            or not self.sidebar_revealer.get_reveal_child()
        ):
            return
        pos = paned.get_position()
        clamped = max(_SIDEBAR_MIN, min(_SIDEBAR_MAX, pos))
        if pos != clamped and 0 < pos < 2000:
            GLib.idle_add(
                lambda: paned.set_position(clamped) if paned.get_position() != clamped else False
            )
            pos = clamped
        if pos < _SIDEBAR_MIN or pos > _SIDEBAR_MAX:
            return
        if getattr(self, "_sidebar_width", None) != pos:
            self._save_sidebar_width(pos)

    # ── переключение сайдбара ────────────────────────────────
    def _on_toggle_sidebar(self, _btn: Gtk.Button | None = None) -> None:
        was_max = self.is_maximized()  # type: ignore[attr-defined]
        old_w = self.get_width()  # type: ignore[attr-defined]
        old_h = self.get_height()  # type: ignore[attr-defined]
        vis = not self.sidebar_revealer.get_reveal_child()
        if not vis:
            cur = self.top.get_position() if hasattr(self, "top") else self._sidebar_width
            if _SIDEBAR_MIN <= cur <= _SIDEBAR_MAX:
                self._save_sidebar_width(cur)
        self.sidebar_revealer.set_reveal_child(vis)
        self._update_sidebar_chrome()
        # Плавное схлопывание: side_column 44px ↔ 232px
        try:
            sc = getattr(self, "side_column", None)
            if sc is not None:
                if vis:
                    sc.add_css_class("expanded")
                else:
                    sc.remove_css_class("expanded")
        except Exception:
            pass
        if hasattr(self, "top"):
            if vis:
                pos = getattr(self, "_sidebar_width", _SIDEBAR_DEFAULT)
                pos = max(_SIDEBAR_MIN, min(_SIDEBAR_MAX, int(pos)))
                self.top.set_position(pos)
            else:
                self.top.set_position(_SIDEBAR_COLLAPSED)
        if not was_max and old_w > 0 and old_h > 0:
            checks = {"n": 10}

            def _keep_size():
                cur_w = self.get_width()  # type: ignore[attr-defined]
                if cur_w > old_w + 4:
                    self.set_default_size(old_w, old_h)  # type: ignore[attr-defined]
                checks["n"] -= 1
                return checks["n"] > 0

            GLib.timeout_add(30, _keep_size)

    def _update_sidebar_chrome(self) -> None:
        open_ = self.sidebar_revealer.get_reveal_child()
        tb = getattr(self, "sidebar_toggle", None)
        if tb is not None:
            tb.set_icon_name("sidebar-hide-symbolic" if open_ else "sidebar-show-symbolic")

    # ── размеры окна (helper) ─────────────────────────────────
    def _default_size_percent(self) -> None:
        disp = self.get_display()  # type: ignore[attr-defined]
        if disp is None:
            return
        mons = list(disp.get_monitors())
        mon = mons[0] if mons else None
        if mon is None:
            return
        geo = mon.get_geometry()
        if geo.width > 0 and geo.height > 0:
            self.set_default_size(int(geo.width * 0.82), int(geo.height * 0.84))  # type: ignore[attr-defined]

    def _resize_window(self, width: float, height: float) -> None:
        w, h = max(200, int(width)), max(160, int(height))
        self.settings["ui_state"] = {
            **self.settings.get("ui_state", {}),
            "maximized": False,
            "width": w,
            "height": h,
        }
        save_settings(self.settings)
        self.set_default_size(w, h)  # type: ignore[attr-defined]
        self.present()  # type: ignore[attr-defined]


__all__ = ["WorkspaceMixin"]
