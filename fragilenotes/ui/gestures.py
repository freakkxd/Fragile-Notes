"""FragileNotes — 3-пальцевые жесты (swipe).

Модуль обработки 3-пальцевых свайпов:

- свайп влево  (3 пальца) → следующая вкладка (навигация вперёд)
- свайп вправо (3 пальца) → предыдущая вкладка (навигация назад)
- свайп вверх  (3 пальца) → палитра команд (command palette)

Использует :class:`Gtk.GestureSwipe` с ``n_points=3`` для строгого
отслеживания трёх точек касания. Fallback — :class:`Gtk.GestureDrag`
с ``n_points=3`` для устройств, где ``GestureSwipe`` не срабатывает.

Интеграция в :mod:`fragilenotes.ui.app`::

    from .gestures import setup_three_finger_gestures

    class FragileWindow(Adw.ApplicationWindow):
        def _setup_swipe_gestures(self):
            setup_three_finger_gestures(self)
"""

from __future__ import annotations

import gi  # type: ignore

try:
    gi.require_version("Gtk", "4.0")
except Exception:
    pass

try:
    gi.require_version("Gdk", "4.0")
except Exception:
    pass

from gi.repository import Gtk  # type: ignore  # noqa: E402

# ── Константы ────────────────────────────────────────────────────────────────
#: Кол-во пальцев для жеста (требование ТЗ — 3).
THREE_FINGERS: int = 3

#: Порог скорости свайпа (пикс/с) — ниже игнорируем случайные касания.
SWIPE_VELOCITY_THRESHOLD: float = 400.0

#: Порог дистанции для drag-fallback (пикс).
DRAG_DISTANCE_THRESHOLD: float = 80.0


# ── Утилиты направления ────────────────────────────────────────────────────
def get_swipe_direction(
    vx: float, vy: float, threshold: float = SWIPE_VELOCITY_THRESHOLD
) -> str | None:
    """Определить направление свайпа по вектору скорости.

    Args:
        vx: скорость по X (пикс/с). Положительная — вправо, отрицательная — влево.
        vy: скорость по Y (пикс/с). Отрицательная — вверх (координаты GTK: Y вниз).
        threshold: минимальный модуль скорости.

    Returns:
        Одно из ``"left"``, ``"right"``, ``"up"``, ``"down"`` или ``None``
        если скорость ниже порога.
    """
    if abs(vx) < threshold and abs(vy) < threshold:
        return None
    # горизонтальный свайп доминирует
    if abs(vx) > abs(vy):
        if abs(vx) < threshold:
            return None
        return "right" if vx > 0 else "left"
    # вертикальный
    if abs(vy) < threshold:
        return None
    return "up" if vy < 0 else "down"


def _open_palette(window: Gtk.Widget) -> bool:
    """Открыть палитру команд (command palette) на окне.

    Пробует несколько стратегий:
    1. ``window.quick.open_commands()`` — палитра команд QuickSwitcher.
    2. ``window.quick.open()`` — обычный quick switcher.
    3. ``window._open_snippets_palette()``.
    4. ``window._run_command("snippets")``.

    Returns:
        True если удалось вызвать палитру.
    """
    # 1. QuickSwitcher командная палитра (Ctrl+Shift+P)
    try:
        quick = getattr(window, "quick", None)
        if quick is not None and hasattr(quick, "open_commands"):
            quick.open_commands()  # type: ignore[attr-defined]
            return True
    except Exception:
        pass
    # 2. fallback — quick switcher
    try:
        quick = getattr(window, "quick", None)
        if quick is not None and hasattr(quick, "open"):
            quick.open()  # type: ignore[attr-defined]
            return True
    except Exception:
        pass
    # 3. snippets palette
    try:
        if hasattr(window, "_open_snippets_palette"):
            window._open_snippets_palette()  # type: ignore[attr-defined]
            return True
    except Exception:
        pass
    try:
        if hasattr(window, "_run_command"):
            window._run_command("snippets")  # type: ignore[attr-defined]
            return True
    except Exception:
        pass
    return False


def _navigate(window: Gtk.Widget, delta: int) -> bool:
    """Навигация между вкладками.

    Пробует ``window._navigate_swipe(delta)``, иначе ``window._on_nav`` с
    вычислением следующей вьюхи из ``VIEWS``.

    Returns:
        True если навигация выполнена.
    """
    try:
        if hasattr(window, "_navigate_swipe"):
            window._navigate_swipe(delta)  # type: ignore[attr-defined]
            return True
    except Exception:
        pass
    # fallback — ручная навигация через VIEWS / _on_nav
    try:
        views = getattr(window, "VIEWS", None)
        # если VIEWS не на окне, пробуем импортировать из workspace/app
        if views is None:
            try:
                from .app import VIEWS as _V  # type: ignore

                views = _V
            except Exception:
                try:
                    from .workspace import VIEWS as _V2  # type: ignore

                    views = _V2
                except Exception:
                    views = None
        if views is None:
            return False
        cur = ""
        if hasattr(window, "_visible_name"):
            try:
                cur = window._visible_name()  # type: ignore[attr-defined]
            except Exception:
                cur = ""
        if hasattr(window, "stack") and cur == "":
            try:
                cur = window.stack.get_visible_child_name() or ""  # type: ignore[attr-defined]
            except Exception:
                pass
        if cur not in views:
            return False
        idx = views.index(cur)
        nxt = (idx + delta) % len(views)
        if hasattr(window, "_on_nav"):
            window._on_nav(views[nxt])  # type: ignore[attr-defined]
            return True
    except Exception:
        pass
    return False


def handle_three_finger_swipe(window: Gtk.Widget, vx: float, vy: float) -> bool:
    """Центральный обработчик 3-пальцевого свайпа.

    - горизонтальный свайп (влево/вправо) → навигация
    - свайп вверх → палитра команд
    - свайп вниз — игнорируется (резерв)

    Args:
        window: окно :class:`FragileWindow` или любой виджет с методами
                ``_navigate_swipe`` / ``quick``.
        vx, vy: скорости свайпа (пикс/с) из сигнала ``Gtk.GestureSwipe::swipe``.

    Returns:
        True если жест обработан, False если проигнорирован.
    """
    direction = get_swipe_direction(vx, vy, threshold=SWIPE_VELOCITY_THRESHOLD)
    if direction is None:
        return False
    if direction == "right":
        # свайп вправо — назад по вкладкам
        return _navigate(window, -1)
    if direction == "left":
        # свайп влево — вперёд по вкладкам
        return _navigate(window, 1)
    if direction == "up":
        return _open_palette(window)
    # down — игнорируем (можно расширить)
    return False


def handle_three_finger_drag(window: Gtk.Widget, dx: float, dy: float) -> bool:
    """Fallback-обработчик для :class:`Gtk.GestureDrag` (n_points=3).

    Срабатывает когда ``GestureSwipe`` недоступен. Логика аналогична
    :func:`handle_three_finger_swipe` но на основе дистанции смещения.

    Args:
        window: окно.
        dx, dy: смещения drag (пикс) из сигнала ``drag-end``.

    Returns:
        True если обработан.
    """
    if abs(dx) < DRAG_DISTANCE_THRESHOLD and abs(dy) < DRAG_DISTANCE_THRESHOLD:
        return False
    if abs(dx) < abs(dy):
        # вертикальный drag
        if abs(dy) < DRAG_DISTANCE_THRESHOLD:
            return False
        if dy < 0:
            # drag вверх (отрицательный dy — вверх, т.к. начало ниже)
            return _open_palette(window)
        return False
    # горизонтальный
    if abs(dx) < DRAG_DISTANCE_THRESHOLD:
        return False
    # dx >0 — движение вправо, dx <0 — влево
    if dx > 0:
        return _navigate(window, -1)
    return _navigate(window, 1)


def create_three_finger_swipe_gesture(window: Gtk.Widget) -> Gtk.GestureSwipe | None:
    """Создать :class:`Gtk.GestureSwipe` для 3 пальцев.

    Args:
        window: виджет, к которому будет привязан жест (нужен только для
                лямбда-замыкания; сам жест не привязывается).

    Returns:
        Настроенный жест или ``None`` при ошибке.
    """
    try:
        # n_points=3 — строго 3 пальца
        try:
            swipe = Gtk.GestureSwipe(n_points=THREE_FINGERS)
        except TypeError:
            # старый биндинг без n_points в конструкторе
            swipe = Gtk.GestureSwipe.new()
            try:
                # попытка через GObject.new уже покрыта, fallback оставляем 1
                pass
            except Exception:
                pass
        try:
            swipe.set_touch_only(True)
        except Exception:
            pass
        try:
            swipe.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        except Exception:
            pass
        swipe.connect(
            "swipe", lambda gesture, vx, vy: handle_three_finger_swipe(window, float(vx), float(vy))
        )
        return swipe
    except Exception:
        return None


def create_three_finger_drag_gesture(window: Gtk.Widget) -> Gtk.GestureDrag | None:
    """Создать fallback :class:`Gtk.GestureDrag` для 3 пальцев."""
    try:
        try:
            drag = Gtk.GestureDrag(n_points=THREE_FINGERS)
        except TypeError:
            drag = Gtk.GestureDrag.new()
        try:
            drag.set_touch_only(True)
        except Exception:
            pass
        try:
            drag.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)
        except Exception:
            pass
        drag.connect(
            "drag-end",
            lambda gesture, dx, dy: handle_three_finger_drag(window, float(dx), float(dy)),
        )
        return drag
    except Exception:
        return None


def setup_three_finger_gestures(window: Gtk.Widget) -> list[Gtk.Gesture]:
    """Установить 3-пальцевые жесты на окно.

    Создаёт и привязывает к ``window``:

    - ``Gtk.GestureSwipe(n_points=3)`` — основной обработчик свайпов
    - ``Gtk.GestureDrag(n_points=3)`` — fallback для тачпадов/устройств
      где ``GestureSwipe`` не генерирует сигнал

    Жесты сохраняются на ``window._three_finger_gestures`` и
    ``window._swipe_gesture`` / ``window._drag_gesture`` для предотвращения
    сборки мусора и совместимости со старым кодом.

    Args:
        window: :class:`Gtk.Widget` / :class:`Adw.ApplicationWindow`.

    Returns:
        Список установленных жестов.
    """
    gestures: list[Gtk.Gesture] = []

    # ── Swipe (основной) ───────────────────────────────────────────────
    swipe = create_three_finger_swipe_gesture(window)
    if swipe is not None:
        try:
            window.add_controller(swipe)  # type: ignore[attr-defined]
            gestures.append(swipe)
            # совместимость: старый код ожидает _swipe_gesture
            try:
                window._swipe_gesture = swipe  # type: ignore[attr-defined]
            except Exception:
                pass
            # общий список
            try:
                window._three_finger_gestures = gestures  # type: ignore[attr-defined]
            except Exception:
                pass
        except Exception:
            pass

    # ── Drag fallback ───────────────────────────────────────────────────
    drag = create_three_finger_drag_gesture(window)
    if drag is not None:
        try:
            window.add_controller(drag)  # type: ignore[attr-defined]
            gestures.append(drag)
            try:
                window._drag_gesture = drag  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                window._three_finger_gestures = gestures  # type: ignore[attr-defined]
            except Exception:
                pass
        except Exception:
            pass

    # ── Резерв: если n_points=3 не поддерживается тачпадом/драйвером,
    #         добавляем однопальцевый swipe как fallback для мыши/тачпада ──
    #         (логика в handle всё равно сработает; 3-пальца — приоритет)
    if not gestures:
        try:
            fallback = Gtk.GestureSwipe.new()
            try:
                fallback.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            except Exception:
                pass
            fallback.connect(
                "swipe", lambda g, vx, vy: handle_three_finger_swipe(window, float(vx), float(vy))
            )
            window.add_controller(fallback)  # type: ignore[attr-defined]
            gestures.append(fallback)
            try:
                window._swipe_gesture = fallback  # type: ignore[attr-defined]
            except Exception:
                pass
        except Exception:
            pass

    return gestures


# Алиас для удобства импорта
attach_three_finger_gestures = setup_three_finger_gestures
install_three_finger_gestures = setup_three_finger_gestures

__all__ = [
    "THREE_FINGERS",
    "SWIPE_VELOCITY_THRESHOLD",
    "DRAG_DISTANCE_THRESHOLD",
    "get_swipe_direction",
    "handle_three_finger_swipe",
    "handle_three_finger_drag",
    "create_three_finger_swipe_gesture",
    "create_three_finger_drag_gesture",
    "setup_three_finger_gestures",
    "attach_three_finger_gestures",
    "install_three_finger_gestures",
]
