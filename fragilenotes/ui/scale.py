"""Хаб масштабирования: UI-масштаб, зум редактора и системный text-scaling.

GTK4 сам НЕ применяет org.gnome.desktop.interface text-scaling-factor
(в CSS-движке поддержки нет), поэтому читаем его из gsettings и домножаем
на пользовательский масштаб интерфейса. Зум редактора (Ctrl+=/Ctrl+-)
меняет только шрифты редактора/просмотра и персистится в настройки.
"""

from __future__ import annotations

import shutil
import subprocess

ZOOM_MIN = 0.5
ZOOM_MAX = 2.0
UI_MIN = 0.75
UI_MAX = 2.0
ZOOM_STEP = 0.1

_state = {"ui": 1.0, "zoom": 1.0, "follow": True}
_system = 1.0
_listeners: list[object] = []


def init(ui_scale: float, editor_zoom: float, follow_system: bool) -> None:
    global _system
    _state["ui"] = float(ui_scale)
    _state["zoom"] = float(editor_zoom)
    _state["follow"] = bool(follow_system)
    _system = system_text_scale()


def system_text_scale() -> float:
    """Читает GNOME text-scaling-factor (1.0, 1.25, …). Нет gsettings/сбоя → 1.0."""
    exe = shutil.which("gsettings")
    if not exe:
        return 1.0
    try:
        out = subprocess.run(
            [exe, "get", "org.gnome.desktop.interface", "text-scaling-factor"],
            capture_output=True, text=True, timeout=4,
        ).stdout.strip()
        return float(out)
    except (ValueError, subprocess.SubprocessError, OSError):
        return 1.0


def monitor_scale() -> int:
    """Scale factor текущего монитора (правая информация для HiDPI)."""
    try:
        from gi.repository import Gdk
        disp = Gdk.Display.get_default()
        if disp is None:
            return 1
        mon = disp.get_primary_monitor()
        if mon is None and disp.get_n_monitors():
            mon = disp.get_monitor(0)
        return mon.get_scale_factor() if mon is not None else 1
    except Exception:  # noqa: BLE001
        return 1


def system() -> float:
    return _system


def ui_scale() -> float:
    return _state["ui"]


def editor_zoom() -> float:
    return _state["zoom"]


def follow_system() -> bool:
    return _state["follow"]


def effective_ui() -> float:
    """Итоговый множитель интерфейса: user × (system, если следим за ним)."""
    factor = _state["ui"] * (_system if _state["follow"] else 1.0)
    return factor


def set_ui(value: float) -> None:
    _state["ui"] = _clamp(float(value), UI_MIN, UI_MAX)
    _notify()


def set_follow(value: bool) -> None:
    _state["follow"] = bool(value)
    _notify()


def zoom_by(delta: float) -> float:
    _state["zoom"] = _clamp(_state["zoom"] + delta, ZOOM_MIN, ZOOM_MAX)
    _notify()
    return _state["zoom"]


def set_zoom(value: float) -> float:
    _state["zoom"] = _clamp(float(value), ZOOM_MIN, ZOOM_MAX)
    _notify()
    return _state["zoom"]


def subscribe(fn):
    """Подписка на смену масштаба; возвращает функцию отписки."""
    if fn not in _listeners:
        _listeners.append(fn)

    def unsubscribe() -> None:
        try:
            _listeners.remove(fn)
        except ValueError:
            pass

    return unsubscribe


def attach_zoom_keys(widget) -> object:
    """Ctrl+= / Ctrl+- / Ctrl+0 зум редактора, Ctrl+0 сбрасывает. Хоткеи на виджете."""
    from gi.repository import Gdk, Gtk

    ctrl = Gtk.EventControllerKey.new()

    def _on_key(_c, keyval, _keycode, state) -> bool:
        if state & Gdk.ModifierType.CONTROL_MASK:
            if keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
                zoom_by(ZOOM_STEP)
                return True
            if keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
                zoom_by(-ZOOM_STEP)
                return True
            if keyval in (Gdk.KEY_0, Gdk.KEY_KP_0):
                set_zoom(1.0)
                return True
        return False

    ctrl.connect("key-pressed", _on_key)
    widget.add_controller(ctrl)
    return ctrl


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _notify() -> None:
    for fn in list(_listeners):
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass
