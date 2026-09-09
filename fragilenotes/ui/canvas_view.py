"""Canvas/Excalidraw-подобный холст для FragileNotes.

Простой canvas на Gtk.DrawingArea + cairo: фигуры — прямоугольник, эллипс,
линия, текст + свободное перо. Выбор цвета, толщины, заливки. Сохранение
в JSON рядом с md (``*.canvas.json``). Интеграция — вкладка "Canvas".

Формат JSON::

    {
      "version": 1,
      "shapes": [
        {"type":"rect","x":10,"y":10,"w":100,"h":60,"color":"#8ab4ff","width":2,"fill":false},
        {"type":"ellipse","x":10,"y":10,"w":100,"h":60,"color":"#8ab4ff","width":2,"fill":false},
        {"type":"line","x1":10,"y1":10,"x2":100,"y2":80,"color":"#8ab4ff","width":2},
        {"type":"pen","points":[[10,10],[12,12]],"color":"#8ab4ff","width":2},
        {"type":"text","x":10,"y":10,"text":"hello","color":"#e4eaf6","size":14}
      ]
    }

Путь сохранения: для ``vault/notes/foo.md`` → ``vault/notes/foo.canvas.json``.
Для произвольного имени без ``.md`` сохраняется ``<name>.canvas.json`` в
том же каталоге или в корне vault.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .widgets import view_header  # noqa: E402

# ── коллаборация (WebSocket sync + CRDT + shared cursors) ────────────────
try:
    from ..services.canvas_collab import (  # noqa: E402
        CanvasCollabService,
        CanvasCRDT,
        CursorManager,
        CursorState,
        get_canvas_replica_id,
    )

    _HAS_CANVAS_COLLAB = True
except Exception:  # pragma: no cover - без сервиса коллаб отключается
    CanvasCollabService = None  # type: ignore[assignment,misc]
    CanvasCRDT = None  # type: ignore[assignment,misc]
    CursorManager = None  # type: ignore[assignment,misc]
    CursorState = None  # type: ignore[assignment,misc]

    def get_canvas_replica_id(settings=None):  # type: ignore[no-redef]
        import uuid

        return uuid.uuid4().hex[:8]

    _HAS_CANVAS_COLLAB = False

# ── утилы цвета / путей ──────────────────────────────────────────

DEFAULT_COLOR = "#8ab4ff"
DEFAULT_TEXT_COLOR = "#e4eaf6"
PALETTE = ["#8ab4ff", "#bea5ff", "#78d296", "#ffd28c", "#e6af6e", "#ff8a8a", "#e4eaf6", "#2b303b"]

VERSION = 1


def _rgba_to_hex(rgba: Gdk.RGBA) -> str:
    r = int(round(rgba.red * 255))
    g = int(round(rgba.green * 255))
    b = int(round(rgba.blue * 255))
    return f"#{r:02x}{g:02x}{b:02x}"


def _hex_to_rgba(hex_str: str) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    try:
        ok = rgba.parse(hex_str or DEFAULT_COLOR)
        if not ok:
            rgba.parse(DEFAULT_COLOR)
    except Exception:
        rgba.parse(DEFAULT_COLOR)
    return rgba


def _hex_to_rgb_float(hex_str: str) -> tuple[float, float, float]:
    h = (hex_str or DEFAULT_COLOR).lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) < 6:
        h = h.ljust(6, "0")
    try:
        return tuple(int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
    except Exception:
        return (0.54, 0.71, 1.0)


def canvas_json_for_md(md_path: Path) -> Path:
    """Путь JSON-канваса рядом с md: ``foo.md`` → ``foo.canvas.json``."""
    p = Path(md_path)
    if p.name.endswith(".canvas.json"):
        return p
    if p.suffix == ".md":
        return p.with_name(p.stem + ".canvas.json")
    if p.suffix == ".json" and ".canvas" in p.name:
        return p
    # без md: добавляем .canvas.json
    if p.suffix:
        return p.with_name(p.stem + ".canvas.json")
    return Path(str(p) + ".canvas.json")


def _load_shapes(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, list):
        return [s for s in data if isinstance(s, dict) and "type" in s]
    if isinstance(data, dict):
        shapes = data.get("shapes")
        if isinstance(shapes, list):
            return [s for s in shapes if isinstance(s, dict) and "type" in s]
    return []


def _save_shapes(path: Path, shapes: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": VERSION, "shapes": shapes}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── геометрия / hit-test ─────────────────────────────────────────

_HIT_PAD = 6.0


def _hit_rect(s: dict, px: float, py: float) -> bool:
    x = float(s.get("x", 0))
    y = float(s.get("y", 0))
    w = float(s.get("w", 0))
    h = float(s.get("h", 0))
    x0, x1 = (x, x + w) if w >= 0 else (x + w, x)
    y0, y1 = (y, y + h) if h >= 0 else (y + h, y)
    return (x0 - _HIT_PAD <= px <= x1 + _HIT_PAD) and (y0 - _HIT_PAD <= py <= y1 + _HIT_PAD)


def _hit_ellipse(s: dict, px: float, py: float) -> bool:
    x = float(s.get("x", 0))
    y = float(s.get("y", 0))
    w = float(s.get("w", 0))
    h = float(s.get("h", 0))
    cx = x + w / 2
    cy = y + h / 2
    rx = abs(w) / 2
    ry = abs(h) / 2
    if rx < 1 or ry < 1:
        return _hit_rect(s, px, py)
    # расширим эллипс на pad
    rx += _HIT_PAD
    ry += _HIT_PAD
    dx = (px - cx) / rx
    dy = (py - cy) / ry
    return dx * dx + dy * dy <= 1.0


def _hit_line(s: dict, px: float, py: float) -> bool:
    x1 = float(s.get("x1", s.get("x", 0)))
    y1 = float(s.get("y1", s.get("y", 0)))
    x2 = float(s.get("x2", s.get("x", 0)))
    y2 = float(s.get("y2", s.get("y", 0)))
    # расстояние до отрезка
    vx = x2 - x1
    vy = y2 - y1
    wx = px - x1
    wy = py - y1
    denom = vx * vx + vy * vy
    if denom < 0.5:
        return math.hypot(px - x1, py - y1) <= _HIT_PAD + 2
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
    qx = x1 + t * vx
    qy = y1 + t * vy
    return math.hypot(px - qx, py - qy) <= _HIT_PAD + 2


def _hit_text(s: dict, px: float, py: float) -> bool:
    x = float(s.get("x", 0))
    y = float(s.get("y", 0))
    text = str(s.get("text", ""))
    size = int(s.get("size", 14))
    # грубая оценка bbox: ширина ~ 0.58*size per char, высота ~ size*1.3
    w = max(30.0, len(text) * size * 0.58)
    h = size * 1.45
    # y — baseline, bbox выше
    return (x - 2 <= px <= x + w + 4) and (y - h + 2 <= py <= y + 4)


def _hit_pen(s: dict, px: float, py: float) -> bool:
    pts = s.get("points") or []
    if len(pts) < 2:
        return False
    for i in range(len(pts) - 1):
        seg = {"x1": pts[i][0], "y1": pts[i][1], "x2": pts[i + 1][0], "y2": pts[i + 1][1]}
        if _hit_line(seg, px, py):
            return True
    return False


def shape_hit(shape: dict, px: float, py: float) -> bool:
    t = shape.get("type")
    if t == "rect":
        return _hit_rect(shape, px, py)
    if t == "ellipse":
        return _hit_ellipse(shape, px, py)
    if t == "line":
        return _hit_line(shape, px, py)
    if t == "text":
        return _hit_text(shape, px, py)
    if t == "pen":
        return _hit_pen(shape, px, py)
    return False


def shape_bbox(shape: dict) -> tuple[float, float, float, float]:
    t = shape.get("type")
    if t in ("rect", "ellipse"):
        x = float(shape.get("x", 0)); y = float(shape.get("y", 0))
        w = float(shape.get("w", 0)); h = float(shape.get("h", 0))
        x0, x1 = (x, x + w) if w >= 0 else (x + w, x)
        y0, y1 = (y, y + h) if h >= 0 else (y + h, y)
        return (x0, y0, x1 - x0, y1 - y0)
    if t == "line":
        x1 = float(shape.get("x1", 0)); y1 = float(shape.get("y1", 0))
        x2 = float(shape.get("x2", 0)); y2 = float(shape.get("y2", 0))
        return (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
    if t == "text":
        x = float(shape.get("x", 0)); y = float(shape.get("y", 0))
        txt = str(shape.get("text", "")); size = int(shape.get("size", 14))
        w = max(30.0, len(txt) * size * 0.58); h = size * 1.45
        return (x, y - h, w, h)
    if t == "pen":
        pts = shape.get("points") or []
        if not pts:
            return (0, 0, 0, 0)
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
    return (0, 0, 0, 0)


# ── отрисовка ────────────────────────────────────────────────────

def _set_source_hex(cr, hex_str: str, alpha: float = 1.0) -> None:
    r, g, b = _hex_to_rgb_float(hex_str)
    cr.set_source_rgba(r, g, b, alpha)


def _draw_shape(cr, shape: dict, selected: bool = False) -> None:
    t = shape.get("type")
    color = str(shape.get("color") or DEFAULT_COLOR)
    width = float(shape.get("width", 2))
    fill = bool(shape.get("fill", False))

    if t == "rect":
        x = float(shape.get("x", 0)); y = float(shape.get("y", 0))
        w = float(shape.get("w", 0)); h = float(shape.get("h", 0))
        if fill:
            _set_source_hex(cr, color, 0.18)
            cr.rectangle(x, y, w, h)
            cr.fill_preserve()
        _set_source_hex(cr, color, 1.0)
        cr.set_line_width(width)
        cr.rectangle(x, y, w, h)
        cr.stroke()
        if selected:
            cr.set_dash([4, 4], 0)
            cr.set_source_rgba(0.51, 0.66, 1.0, 0.9)
            cr.set_line_width(1.0)
            cr.rectangle(x - 2, y - 2, w + 4, h + 4)
            cr.stroke()
            cr.set_dash([], 0)

    elif t == "ellipse":
        x = float(shape.get("x", 0)); y = float(shape.get("y", 0))
        w = float(shape.get("w", 0)); h = float(shape.get("h", 0))
        cx = x + w / 2; cy = y + h / 2
        rx = abs(w) / 2; ry = abs(h) / 2
        if rx < 0.5 or ry < 0.5:
            return
        cr.save()
        cr.translate(cx, cy)
        cr.scale(rx, ry)
        cr.arc(0, 0, 1, 0, 2 * math.pi)
        cr.restore()
        if fill:
            _set_source_hex(cr, color, 0.18)
            cr.save()
            cr.translate(cx, cy)
            cr.scale(rx, ry)
            cr.arc(0, 0, 1, 0, 2 * math.pi)
            cr.restore()
            cr.fill_preserve()
        _set_source_hex(cr, color, 1.0)
        cr.set_line_width(width)
        # need to stroke with scaled context — recreate path
        cr.save()
        cr.translate(cx, cy)
        cr.scale(rx, ry)
        cr.arc(0, 0, 1, 0, 2 * math.pi)
        cr.restore()
        cr.stroke()
        if selected:
            cr.set_dash([4, 4], 0)
            cr.set_source_rgba(0.51, 0.66, 1.0, 0.9)
            cr.set_line_width(1.0)
            cr.rectangle(x - 2, y - 2, w + 4, h + 4)
            cr.stroke()
            cr.set_dash([], 0)

    elif t == "line":
        x1 = float(shape.get("x1", shape.get("x", 0)))
        y1 = float(shape.get("y1", shape.get("y", 0)))
        x2 = float(shape.get("x2", shape.get("x", 0)))
        y2 = float(shape.get("y2", shape.get("y", 0)))
        _set_source_hex(cr, color, 1.0)
        cr.set_line_width(width)
        cr.set_line_cap(1)  # round
        cr.move_to(x1, y1)
        cr.line_to(x2, y2)
        cr.stroke()
        if selected:
            cr.set_source_rgba(0.51, 0.66, 1.0, 0.9)
            cr.set_line_width(1.0)
            for (cx, cy) in ((x1, y1), (x2, y2)):
                cr.arc(cx, cy, 4, 0, 2 * math.pi)
                cr.fill()
            cr.set_source_rgba(0.51, 0.66, 1.0, 0.9)
            cr.set_dash([4, 4], 0)
            cr.move_to(x1, y1)
            cr.line_to(x2, y2)
            cr.stroke()
            cr.set_dash([], 0)

    elif t == "pen":
        pts = shape.get("points") or []
        if len(pts) < 2:
            return
        _set_source_hex(cr, color, 1.0)
        cr.set_line_width(width)
        cr.set_line_cap(1)
        cr.set_line_join(1)
        cr.move_to(float(pts[0][0]), float(pts[0][1]))
        for p in pts[1:]:
            cr.line_to(float(p[0]), float(p[1]))
        cr.stroke()
        if selected:
            cr.set_dash([4, 4], 0)
            cr.set_source_rgba(0.51, 0.66, 1.0, 0.6)
            cr.set_line_width(1.0)
            x0, y0, w0, h0 = shape_bbox(shape)
            cr.rectangle(x0 - 2, y0 - 2, w0 + 4, h0 + 4)
            cr.stroke()
            cr.set_dash([], 0)

    elif t == "text":
        x = float(shape.get("x", 0)); y = float(shape.get("y", 0))
        txt = str(shape.get("text", ""))
        size = int(shape.get("size", 14))
        color = str(shape.get("color") or DEFAULT_TEXT_COLOR)
        _set_source_hex(cr, color, 1.0)
        cr.select_font_face("Inter", 0, 0)
        cr.set_font_size(float(size))
        cr.move_to(x, y)
        cr.show_text(txt)
        if selected:
            ext = cr.text_extents(txt)
            cr.set_source_rgba(0.51, 0.66, 1.0, 0.9)
            cr.set_line_width(1.0)
            cr.set_dash([4, 4], 0)
            cr.rectangle(x - 2, y - ext.height - 2, ext.width + 4, ext.height + 4)
            cr.stroke()
            cr.set_dash([], 0)


# ── виджет вкладки ───────────────────────────────────────────────

class CanvasView(Gtk.Box):
    """Вкладка Canvas: toolbar + DrawingArea + JSON рядом с md."""

    def __init__(self, settings: dict) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self._vault_root = Path(str(settings.get("vault_root") or Path.home() / "desktop"))

        # модель
        self._shapes: list[dict] = []
        self._selected: int | None = None
        self._history: list[list[dict]] = []
        self._canvas_path: Path | None = None
        self._current_md: Path | None = None

        # инструмент / стиль
        self._tool: str = "select"  # select|rect|ellipse|line|pen|text
        self._color: str = DEFAULT_COLOR
        self._line_width: int = 2
        self._fill: bool = False
        self._font_size: int = 14

        # drag / preview
        self._dragging = False
        self._drag_start: tuple[float, float] | None = None
        self._drag_current: tuple[float, float] | None = None
        self._move_offset: tuple[float, float] | None = None
        self._move_start_shape: dict | None = None
        self._pen_points: list[list[float]] | None = None
        self._preview: dict | None = None

        # view transform (pan/zoom)
        self._scale: float = 1.0
        self._off_x: float = 0.0
        self._off_y: float = 0.0
        self._panning = False
        self._pan_start: tuple[float, float] | None = None
        self._pan_off: tuple[float, float] | None = None

        # коллаборация: CRDT + WebSocket stub + shared cursors
        self._collab = None  # type: ignore[assignment]
        self._collab_cursors = None  # type: ignore[assignment]
        self._collab_enabled = bool(settings.get("canvas_collab_enabled", False))
        self._collab_endpoint = str(settings.get("canvas_collab_endpoint") or "ws://localhost:8765/canvas")
        if _HAS_CANVAS_COLLAB:
            try:
                self._init_collab()
            except Exception:
                self._collab = None

        self._build_ui()
        self._update_status()
        # подключить коллаб-хуки после UI (кнопки уже созданы)
        if self._collab is not None:
            try:
                self._update_collab_ui()
            except Exception:
                pass

    # ── UI ───────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.append(view_header("🎨", "Canvas", "Excalidraw-подобный холст · прямоугольник, эллипс, линия, текст"))

        # toolbar 1: файл + действия
        file_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["toolbar", "canvas-toolbar"])
        file_bar.set_margin_start(14); file_bar.set_margin_end(14)

        open_md_btn = Gtk.Button(label="Открыть .md", tooltip_text="Выбрать md и открыть его canvas (рядом .canvas.json)")
        open_md_btn.connect("clicked", self._on_open_md)
        file_bar.append(open_md_btn)

        open_btn = Gtk.Button(label="Открыть JSON", tooltip_text="Открыть существующий .canvas.json")
        open_btn.connect("clicked", self._on_open_json)
        file_bar.append(open_btn)

        save_btn = Gtk.Button(label="Сохранить", css_classes=["suggested-action"], tooltip_text="Сохранить рядом с md (Ctrl+S)")
        save_btn.connect("clicked", lambda *_: self.save_canvas())
        file_bar.append(save_btn)

        save_as_btn = Gtk.Button(label="Сохранить как…")
        save_as_btn.connect("clicked", self._on_save_as)
        file_bar.append(save_as_btn)

        new_btn = Gtk.Button(label="Новый", tooltip_text="Очистить холст")
        new_btn.connect("clicked", lambda *_: self.new_canvas())
        file_bar.append(new_btn)

        file_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        undo_btn = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text="Отменить (Ctrl+Z)")
        undo_btn.connect("clicked", lambda *_: self.undo())
        file_bar.append(undo_btn)

        clear_btn = Gtk.Button(label="Очистить", css_classes=["destructive-action"], tooltip_text="Удалить все фигуры")
        clear_btn.connect("clicked", lambda *_: self.clear_canvas())
        file_bar.append(clear_btn)

        del_btn = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Удалить выбранное (Delete)")
        del_btn.connect("clicked", lambda *_: self.delete_selected())
        file_bar.append(del_btn)

        file_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # коллаборация: статус + вкл/выкл + курсоры
        self._collab_dot = Gtk.Label(label="◯", css_classes=["dim-hint"])
        self._collab_dot.set_tooltip_text("Коллаборация: отключена")
        file_bar.append(self._collab_dot)
        self._collab_btn = Gtk.ToggleButton(label="⛓ Коллаб", tooltip_text="Вкл/выкл WebSocket sync (CRDT) — заглушка")
        self._collab_btn.set_active(self._collab_enabled)
        self._collab_btn.connect("toggled", self._on_collab_toggled)
        file_bar.append(self._collab_btn)
        self._cursor_btn = Gtk.ToggleButton(label="👁 Курсоры", tooltip_text="Показать shared cursors участников")
        self._cursor_btn.set_active(True)
        self._cursor_btn.connect("toggled", lambda b: self._area.queue_draw())
        file_bar.append(self._cursor_btn)

        self._file_label = Gtk.Label(label="· нет файла", css_classes=["dim-hint", "canvas-file"], hexpand=True, halign=Gtk.Align.END, xalign=1, ellipsize=Pango.EllipsizeMode.MIDDLE)
        file_bar.append(self._file_label)
        self.append(file_bar)

        # toolbar 2: инструменты + стиль
        tool_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["toolbar", "canvas-tools"])
        tool_bar.set_margin_start(14); tool_bar.set_margin_end(14)

        self._tool_btns: dict[str, Gtk.ToggleButton] = {}
        tools = [
            ("select", "↖ Выбор", "Выбор/перемещение"),
            ("rect", "▭ Прямоуг.", "Прямоугольник"),
            ("ellipse", "⬭ Эллипс", "Эллипс"),
            ("line", "╱ Линия", "Линия"),
            ("pen", "✏ Перо", "Свободное перо"),
            ("text", "T Текст", "Текст (клик → ввод)"),
        ]
        for tid, label, tip in tools:
            b = Gtk.ToggleButton(label=label, tooltip_text=tip, css_classes=["media-filter"])
            b.set_active(tid == "select")
            b.connect("toggled", self._on_tool_toggled, tid)
            tool_bar.append(b)
            self._tool_btns[tid] = b

        tool_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # палитра
        pal = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._palette_btns: list[Gtk.Button] = []
        for col in PALETTE:
            btn = Gtk.Button(css_classes=["canvas-color"])
            # цвет через inline? делаем через css provider per button будет тяжело — красим via content
            # проще: label с ● окрашенным через markup? используем override color via child label
            lbl = Gtk.Label(label="●")
            # применим цвет через Pango markup не подходит для кнопки; используем css через add provider
            # fallback: установим tooltip и при клике выберем цвет
            btn.set_child(lbl)
            btn.set_tooltip_text(col)
            # покрасим label через css inline style: override color
            try:
                lbl.set_markup(f'<span foreground="{col}">●</span>')
            except Exception:
                pass
            btn.connect("clicked", self._on_palette, col)
            pal.append(btn)
            self._palette_btns.append(btn)
        tool_bar.append(pal)

        # color button custom
        try:
            self._color_btn = Gtk.ColorButton()
            try:
                rgba = _hex_to_rgba(self._color)
                self._color_btn.set_rgba(rgba)
            except Exception:
                pass
            self._color_btn.set_tooltip_text("Цвет фигуры/текста")
            self._color_btn.connect("color-set", self._on_color_set)
            tool_bar.append(self._color_btn)
        except Exception:
            self._color_btn = None  # type: ignore[assignment]

        tool_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        tool_bar.append(Gtk.Label(label="Толщ:", css_classes=["dim-hint"]))
        self._width_spin = Gtk.SpinButton.new_with_range(1, 12, 1)
        self._width_spin.set_value(float(self._line_width))
        self._width_spin.set_tooltip_text("Толщина линии")
        self._width_spin.connect("value-changed", self._on_width_changed)
        tool_bar.append(self._width_spin)

        tool_bar.append(Gtk.Label(label="Шрифт:", css_classes=["dim-hint"]))
        self._font_spin = Gtk.SpinButton.new_with_range(8, 48, 1)
        self._font_spin.set_value(float(self._font_size))
        self._font_spin.set_tooltip_text("Размер текста")
        self._font_spin.connect("value-changed", self._on_font_changed)
        tool_bar.append(self._font_spin)

        self._fill_check = Gtk.CheckButton(label="Заливка")
        self._fill_check.set_active(self._fill)
        self._fill_check.connect("toggled", self._on_fill_toggled)
        tool_bar.append(self._fill_check)

        # масштаб
        tool_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        zoom_out = Gtk.Button(label="−", tooltip_text="Уменьшить (колесо)")
        zoom_out.connect("clicked", lambda *_: self._zoom(0.88))
        tool_bar.append(zoom_out)
        self._zoom_label = Gtk.Label(label="100%", css_classes=["dim-hint"])
        tool_bar.append(self._zoom_label)
        zoom_in = Gtk.Button(label="+", tooltip_text="Увеличить (колесо)")
        zoom_in.connect("clicked", lambda *_: self._zoom(1.12))
        tool_bar.append(zoom_in)
        reset_view = Gtk.Button(label="Сброс вида")
        reset_view.connect("clicked", lambda *_: self._reset_view())
        tool_bar.append(reset_view)

        self.append(tool_bar)

        # подсказка по горячим клавишам
        hint = Gtk.Label(
            label="ЛКМ — рисовать / выбрать · перетаскивание выбранного — переместить · Delete — удалить · колесо — зум · СКМ/Shift+drag — панорама",
            css_classes=["dim-hint", "canvas-hint"], halign=Gtk.Align.START, xalign=0, wrap=True,
        )
        hint.set_margin_start(14); hint.set_margin_end(14)
        self.append(hint)

        # canvas frame
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        frame.set_margin_start(14); frame.set_margin_end(14); frame.set_margin_bottom(14)

        self._area = Gtk.DrawingArea(hexpand=True, vexpand=True, css_classes=["canvas-area", "graph-area"])
        self._area.set_draw_func(self._on_draw, None)
        self._area.set_content_width(900)
        self._area.set_content_height(560)
        self._area.set_can_focus(True)
        self._area.set_focusable(True)
        # минималки чтобы область была видна
        self._area.set_size_request(600, 360)

        # controllers: drag create/move
        drag = Gtk.GestureDrag.new()
        drag.set_button(1)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self._area.add_controller(drag)

        # pan with middle button / shift+left
        pan = Gtk.GestureDrag.new()
        pan.set_button(2)
        pan.connect("drag-begin", self._on_pan_begin)
        pan.connect("drag-update", self._on_pan_update)
        pan.connect("drag-end", self._on_pan_end)
        self._area.add_controller(pan)

        click = Gtk.GestureClick.new()
        click.set_button(1)
        click.connect("released", self._on_click)
        self._area.add_controller(click)

        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self._area.add_controller(scroll)

        motion = Gtk.EventControllerMotion.new()
        motion.connect("motion", self._on_motion)
        self._area.add_controller(motion)

        key = Gtk.EventControllerKey.new()
        key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key.connect("key-pressed", self._on_key)
        self._area.add_controller(key)
        # также глобальный key на сам view для Delete/Undo когда фокус в toolbar
        gkey = Gtk.EventControllerKey.new()
        gkey.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gkey.connect("key-pressed", self._on_key)
        self.add_controller(gkey)

        frame_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        frame_box.append(self._area)
        frame.append(frame_box)
        self.append(frame)

        # статус
        self._status = Gtk.Label(label="Готов · выберите инструмент", css_classes=["dim-hint", "canvas-status"], halign=Gtk.Align.START, xalign=0)
        self._status.set_margin_start(14)
        self.append(self._status)

    # ── состояние файла ──────────────────────────────────────────
    def _push_history(self) -> None:
        import copy
        self._history.append(copy.deepcopy(self._shapes))
        if len(self._history) > 64:
            self._history.pop(0)

    def _update_status(self) -> None:
        if self._canvas_path is not None:
            try:
                rel = self._canvas_path.relative_to(self._vault_root)
                txt = str(rel)
            except Exception:
                txt = self._canvas_path.name
            self._file_label.set_text(txt)
            self._file_label.set_tooltip_text(str(self._canvas_path))
        elif self._current_md is not None:
            try:
                rel = self._current_md.relative_to(self._vault_root)
                txt = f"{rel} → {canvas_json_for_md(self._current_md).name}"
            except Exception:
                txt = self._current_md.name
            self._file_label.set_text(txt)
            self._file_label.set_tooltip_text(str(canvas_json_for_md(self._current_md)))
        else:
            self._file_label.set_text("· нет файла (сохранится как canvas.json в vault)")
            self._file_label.set_tooltip_text("Выберите md или сохраните как…")

        n = len(self._shapes)
        sel = f" · выбрано #{self._selected}" if self._selected is not None else ""
        tool = {"select": "Выбор", "rect": "Прямоугольник", "ellipse": "Эллипс", "line": "Линия", "pen": "Перо", "text": "Текст"}.get(self._tool, self._tool)
        # коллаб: добавить инфо о подключении/курсорах если активно
        collab_info = ""
        try:
            if _HAS_CANVAS_COLLAB and self._collab is not None:
                if getattr(self, "_collab_enabled", False) and self._collab.is_connected:
                    curs = len(self._collab.get_remote_cursors()) if hasattr(self._collab, "get_remote_cursors") else 0
                    collab_info = f" · ⛓ {curs} курс."
                elif getattr(self, "_collab_enabled", False):
                    collab_info = " · ⛓ ожидание"
        except Exception:
            pass
        self._status.set_text(f"{n} фигур · {tool}{sel} · цвет {self._color} · {self._line_width}px{collab_info}")
        self._zoom_label.set_text(f"{int(self._scale*100)}%")
        # обновить коллаб-индикатор
        try:
            self._update_collab_ui()
        except Exception:
            pass

    # ── коллаборация ───────────────────────────────────────────────────
    def _init_collab(self) -> None:
        """Инициализировать CRDT + WebSocket stub + shared cursors."""
        if not _HAS_CANVAS_COLLAB or CanvasCollabService is None:
            return
        doc_id = "canvas"
        try:
            if self._canvas_path is not None:
                doc_id = str(self._canvas_path.stem.replace(".canvas", "")) or "canvas"
            elif self._current_md is not None:
                doc_id = str(self._current_md.stem) or "canvas"
        except Exception:
            pass
        replica = get_canvas_replica_id(self.settings)
        # смешаем цвет пользователя с палитрой для курсора
        try:
            self._collab = CanvasCollabService(  # type: ignore[operator]
                doc=doc_id, settings=self.settings, endpoint=self._collab_endpoint, replica_id=replica
            )
            # хуки на удалённые изменения
            self._collab.set_on_remote_shapes(self._on_collab_shapes)  # type: ignore[attr-defined]
            self._collab.set_on_remote_cursor(self._on_collab_cursor)  # type: ignore[attr-defined]
            if self._collab_enabled:
                self._collab.connect()
            # синхронизировать текущие фигуры в CRDT
            if self._shapes:
                self._collab.set_shapes(list(self._shapes))
        except Exception:
            self._collab = None

    def _update_collab_ui(self) -> None:
        if not hasattr(self, "_collab_dot") or not hasattr(self, "_collab_btn"):
            return
        enabled = bool(getattr(self, "_collab_enabled", False))
        connected = bool(self._collab and getattr(self._collab, "is_connected", False))
        try:
            if not enabled:
                self._collab_dot.set_text("◯")
                self._collab_dot.set_tooltip_text("Коллаборация отключена")
                self._collab_dot.remove_css_class("collab-on")
            elif connected:
                self._collab_dot.set_text("⬤")
                self._collab_dot.set_tooltip_text(f"Подключено: {self._collab_endpoint}")
                self._collab_dot.add_css_class("collab-on")
            else:
                self._collab_dot.set_text("◐")
                self._collab_dot.set_tooltip_text(f"Ожидание: {self._collab_endpoint}")
                self._collab_dot.remove_css_class("collab-on")
            self._collab_btn.set_active(enabled)
        except Exception:
            pass

    def _on_collab_toggled(self, btn: Gtk.ToggleButton) -> None:  # type: ignore[override]
        active = bool(btn.get_active())
        self._collab_enabled = active
        try:
            self.settings["canvas_collab_enabled"] = active
        except Exception:
            pass
        if _HAS_CANVAS_COLLAB:
            if self._collab is None:
                try:
                    self._init_collab()
                except Exception:
                    pass
            if self._collab is not None:
                if active:
                    self._collab.enable()  # type: ignore[attr-defined]
                    # отправить текущий снепшот
                    try:
                        self._collab.broadcast_shapes(list(self._shapes))
                    except Exception:
                        pass
                    self._notify("Коллаборация включена")
                else:
                    self._collab.disable()  # type: ignore[attr-defined]
                    self._notify("Коллаборация выключена")
        self._update_status()

    def _on_collab_shapes(self, shapes: list[dict]) -> None:
        """Колбэк: удалённые фигуры пришли — применить и перерисовать."""
        try:
            # GLib idle чтобы не из потока
            def _apply():
                # мердж без истории (удалённое)
                self._shapes = [dict(s) for s in shapes if isinstance(s, dict) and "type" in s]
                self._selected = None if self._selected is not None and self._selected >= len(self._shapes) else self._selected
                self._preview = None
                self._area.queue_draw()
                self._update_status()
                return False

            try:
                GLib.idle_add(_apply)
            except Exception:
                _apply()
        except Exception:
            pass

    def _on_collab_cursor(self, state) -> None:  # type: ignore[no-untyped-def]
        try:
            GLib.idle_add(lambda: (self._area.queue_draw(), False)[1])
        except Exception:
            try:
                self._area.queue_draw()
            except Exception:
                pass

    def _collab_broadcast_shapes(self) -> None:
        if not _HAS_CANVAS_COLLAB or self._collab is None or not self._collab_enabled:
            return
        try:
            self._collab.broadcast_shapes(list(self._shapes))  # type: ignore[attr-defined]
        except Exception:
            pass

    def _collab_broadcast_cursor(self, x: float, y: float) -> None:
        if not _HAS_CANVAS_COLLAB or self._collab is None or not self._collab_enabled:
            return
        try:
            # троттлинг внутри CursorManager, здесь просто прокидываем
            sel_ids: list[str] = []
            try:
                if self._selected is not None and 0 <= self._selected < len(self._shapes):
                    sid = str(self._shapes[self._selected].get("id") or "")
                    if sid:
                        sel_ids = [sid]
            except Exception:
                pass
            self._collab.update_cursor(x=float(x), y=float(y), color=self._color, label=get_canvas_replica_id(self.settings), tool=self._tool, selected=sel_ids)  # type: ignore[attr-defined]
        except Exception:
            pass

    def new_canvas(self) -> None:
        if self._shapes:
            self._push_history()
        self._shapes = []
        self._selected = None
        self._preview = None
        self._pen_points = None
        self._canvas_path = None
        self._current_md = None
        self._area.queue_draw()
        self._update_status()
        try:
            self._collab_broadcast_shapes()
        except Exception:
            pass

    def open_canvas(self, path: Path) -> None:
        """Открыть существующий .canvas.json."""
        p = Path(path)
        if p.suffix == ".md":
            # md → рядом canvas
            cand = canvas_json_for_md(p)
            if cand.is_file():
                p = cand
            else:
                # запомним md для сохранения рядом, но холст пустой
                self._current_md = p
                self._canvas_path = cand
                self._shapes = []
                self._selected = None
                self._preview = None
                self._area.queue_draw()
                self._update_status()
                try:
                    if self._collab is not None:
                        self._collab.set_shapes(list(self._shapes))
                        # doc_id по новому пути
                        try:
                            self._collab.doc.doc_id = str(cand.stem.replace(".canvas", "") or "canvas")
                        except Exception:
                            pass
                except Exception:
                    pass
                return
        if p.is_file():
            shapes = _load_shapes(p)
            self._shapes = shapes
            self._selected = None
            self._preview = None
            self._canvas_path = p
            # если это canvas.json рядом с md — запомним md
            if p.name.endswith(".canvas.json"):
                md_cand = p.with_name(p.name[: -len(".canvas.json")] + ".md")
                if md_cand.is_file():
                    self._current_md = md_cand
            self._area.queue_draw()
            self._update_status()
            try:
                if self._collab is not None:
                    self._collab.set_shapes(list(self._shapes))
                    try:
                        self._collab.doc.doc_id = str(p.stem.replace(".canvas", "") or "canvas")
                    except Exception:
                        pass
            except Exception:
                pass
        else:
            # путь не существует — считаем что это новый файл для сохранения
            self._canvas_path = p
            self._shapes = []
            self._selected = None
            self._area.queue_draw()
            self._update_status()
            try:
                if self._collab is not None:
                    self._collab.set_shapes([])
                    try:
                        self._collab.doc.doc_id = str(p.stem.replace(".canvas", "") or "canvas")
                    except Exception:
                        pass
            except Exception:
                pass

    def save_canvas(self, path: Path | None = None) -> Path | None:
        """Сохранить в JSON рядом с md. Возвращает путь или None."""
        target: Path | None = None
        if path is not None:
            target = Path(path)
        elif self._canvas_path is not None:
            target = self._canvas_path
        elif self._current_md is not None:
            target = canvas_json_for_md(self._current_md)
        else:
            # нет привязки — сохранить как vault/canvas-<ts>.canvas.json
            import time
            ts = time.strftime("%Y%m%d-%H%M%S")
            target = self._vault_root / f"canvas-{ts}.canvas.json"
        try:
            _save_shapes(target, self._shapes)
            self._canvas_path = target
            self._update_status()
            # toast via root overlay if available
            self._notify(f"Сохранено: {target.name}")
            return target
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Ошибка сохранения: {exc}")
            return None

    def save_canvas_for_md(self, md_path: Path) -> Path | None:
        """Явное сохранение рядом с указанным md (удобно для интеграций)."""
        self._current_md = Path(md_path)
        return self.save_canvas(canvas_json_for_md(self._current_md))

    def clear_canvas(self) -> None:
        if not self._shapes:
            return
        self._push_history()
        self._shapes.clear()
        self._selected = None
        self._preview = None
        self._area.queue_draw()
        self._update_status()
        try:
            self._collab_broadcast_shapes()
        except Exception:
            pass

    def delete_selected(self) -> None:
        if self._selected is None:
            return
        if 0 <= self._selected < len(self._shapes):
            self._push_history()
            del self._shapes[self._selected]
            self._selected = None
            self._preview = None
            self._area.queue_draw()
            self._update_status()
            try:
                self._collab_broadcast_shapes()
            except Exception:
                pass

    def undo(self) -> None:
        if not self._history:
            return
        self._shapes = self._history.pop()
        self._selected = None if self._selected is not None and self._selected >= len(self._shapes) else self._selected
        self._preview = None
        self._area.queue_draw()
        self._update_status()
        try:
            self._collab_broadcast_shapes()
        except Exception:
            pass

    def _notify(self, msg: str) -> None:
        # пробуем найти ToastOverlay у окна
        try:
            win = self.get_root()
            if win is not None and hasattr(win, "toast_overlay"):
                toast = Adw.Toast.new(msg)
                toast.set_timeout(3)
                win.toast_overlay.add_toast(toast)  # type: ignore[attr-defined]
                return
        except Exception:
            pass
        # fallback — статус
        self._status.set_text(msg)
        GLib.timeout_add(2500, lambda: (self._update_status(), False)[1])

    # ── файловые диалоги ─────────────────────────────────────────
    def _on_open_md(self, _btn) -> None:
        self._pick_file("md")

    def _on_open_json(self, _btn) -> None:
        self._pick_file("json")

    def _on_save_as(self, _btn) -> None:
        self._pick_save()

    def _pick_file(self, kind: str) -> None:
        # Gtk.FileDialog (4.10+) иначе fallback
        try:
            dlg = Gtk.FileDialog()
            dlg.set_title("Открыть .md" if kind == "md" else "Открыть canvas JSON")
            filt = Gio.ListStore.new(Gtk.FileFilter)
            if kind == "md":
                f = Gtk.FileFilter()
                f.set_name("Markdown (*.md)")
                f.add_pattern("*.md")
                filt.append(f)
            else:
                f = Gtk.FileFilter()
                f.set_name("Canvas (*.canvas.json)")
                f.add_pattern("*.canvas.json")
                filt.append(f)
                fj = Gtk.FileFilter()
                fj.set_name("JSON (*.json)")
                fj.add_pattern("*.json")
                filt.append(fj)
            dlg.set_filters(filt)
            # initial folder vault_root
            try:
                dlg.set_initial_folder(Gio.File.new_for_path(str(self._vault_root)))
            except Exception:
                pass
            dlg.open(self.get_root(), None, self._on_pick_file_done, kind)
            return
        except Exception:
            pass
        # fallback: simple popover with entry
        self._fallback_open_dialog(kind)

    def _on_pick_file_done(self, dlg: Gtk.FileDialog, res, kind: str) -> None:
        try:
            f = dlg.open_finish(res)
            if f is None:
                return
            path = Path(f.get_path() or "")
            if path:
                self.open_canvas(path)
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Ошибка открытия: {exc}")

    def _fallback_open_dialog(self, kind: str) -> None:
        dialog = Adw.Dialog(title="Открыть файл")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
        box.set_size_request(420, -1)
        lbl = Gtk.Label(label="Путь к файлу (от vault или абсолютный):", halign=Gtk.Align.START)
        entry = Gtk.Entry(placeholder_text="например: Заметки/foo.md  или  canvas-20240101.canvas.json")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Отмена")
        ok = Gtk.Button(label="Открыть", css_classes=["suggested-action"])
        row.append(cancel); row.append(ok)
        box.append(lbl); box.append(entry); box.append(row)
        dialog.set_child(box)
        cancel.connect("clicked", lambda *_: dialog.close())
        def _do(*_):
            raw = entry.get_text().strip()
            if not raw:
                return
            p = Path(raw)
            if not p.is_absolute():
                p = self._vault_root / p
            dialog.close()
            self.open_canvas(p)
        ok.connect("clicked", _do)
        entry.connect("activate", _do)
        dialog.present(self.get_root())

    def _pick_save(self) -> None:
        try:
            dlg = Gtk.FileDialog()
            dlg.set_title("Сохранить canvas как…")
            filt = Gio.ListStore.new(Gtk.FileFilter)
            f = Gtk.FileFilter()
            f.set_name("Canvas (*.canvas.json)")
            f.add_pattern("*.canvas.json")
            filt.append(f)
            dlg.set_filters(filt)
            try:
                dlg.set_initial_folder(Gio.File.new_for_path(str(self._vault_root)))
                if self._canvas_path is not None:
                    dlg.set_initial_name(self._canvas_path.name)
                elif self._current_md is not None:
                    dlg.set_initial_name(canvas_json_for_md(self._current_md).name)
                else:
                    dlg.set_initial_name("canvas.canvas.json")
            except Exception:
                pass
            dlg.save(self.get_root(), None, self._on_pick_save_done)
            return
        except Exception:
            pass
        self._fallback_save_dialog()

    def _on_pick_save_done(self, dlg: Gtk.FileDialog, res) -> None:
        try:
            f = dlg.save_finish(res)
            if f is None:
                return
            path = Path(f.get_path() or "")
            if path:
                if not path.name.endswith(".canvas.json") and not path.name.endswith(".json"):
                    path = path.with_name(path.name + ".canvas.json")
                self.save_canvas(path)
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Ошибка сохранения: {exc}")

    def _fallback_save_dialog(self) -> None:
        dialog = Adw.Dialog(title="Сохранить как…")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
        box.set_size_request(420, -1)
        lbl = Gtk.Label(label="Имя файла (сохранится в vault):", halign=Gtk.Align.START)
        entry = Gtk.Entry(placeholder_text="например: my-canvas.canvas.json  или  Заметки/foo.canvas.json")
        # предзаполним
        try:
            if self._canvas_path is not None:
                entry.set_text(str(self._canvas_path.relative_to(self._vault_root)))
            elif self._current_md is not None:
                entry.set_text(str(canvas_json_for_md(self._current_md).relative_to(self._vault_root)))
        except Exception:
            pass
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Отмена")
        ok = Gtk.Button(label="Сохранить", css_classes=["suggested-action"])
        row.append(cancel); row.append(ok)
        box.append(lbl); box.append(entry); box.append(row)
        dialog.set_child(box)
        cancel.connect("clicked", lambda *_: dialog.close())
        def _do(*_):
            raw = entry.get_text().strip()
            if not raw:
                return
            p = Path(raw)
            if not p.is_absolute():
                p = self._vault_root / p
            if not p.name.endswith(".canvas.json") and not p.name.endswith(".json"):
                p = p.with_name(p.name + ".canvas.json")
            dialog.close()
            self.save_canvas(p)
        ok.connect("clicked", _do)
        entry.connect("activate", _do)
        dialog.present(self.get_root())

    # ── тулбар колбэки ───────────────────────────────────────────
    def _on_tool_toggled(self, btn: Gtk.ToggleButton, tid: str) -> None:
        if not btn.get_active():
            # не даём снять все — хотя бы один активен
            if not any(b.get_active() for b in self._tool_btns.values()):
                btn.set_active(True)
            return
        for k, b in self._tool_btns.items():
            if k != tid and b.get_active():
                b.set_active(False)
        self._tool = tid
        self._update_status()
        # курсор
        try:
            if tid == "text":
                self._area.set_cursor(Gdk.Cursor.new_from_name("text"))
            elif tid == "pen":
                self._area.set_cursor(Gdk.Cursor.new_from_name("crosshair"))
            elif tid == "select":
                self._area.set_cursor(Gdk.Cursor.new_from_name("default"))
            else:
                self._area.set_cursor(Gdk.Cursor.new_from_name("crosshair"))
        except Exception:
            pass

    def _on_palette(self, _btn, col: str) -> None:
        self._color = col
        try:
            if self._color_btn is not None:
                self._color_btn.set_rgba(_hex_to_rgba(col))
        except Exception:
            pass
        # применить к выбранной фигуре
        if self._selected is not None and 0 <= self._selected < len(self._shapes):
            self._push_history()
            self._shapes[self._selected]["color"] = col
            self._area.queue_draw()
            try:
                self._collab_broadcast_shapes()
            except Exception:
                pass
        self._update_status()

    def _on_color_set(self, btn) -> None:
        try:
            rgba = btn.get_rgba()
            hexc = _rgba_to_hex(rgba)
        except Exception:
            return
        self._color = hexc
        if self._selected is not None and 0 <= self._selected < len(self._shapes):
            self._push_history()
            self._shapes[self._selected]["color"] = hexc
            self._area.queue_draw()
            try:
                self._collab_broadcast_shapes()
            except Exception:
                pass
        self._update_status()

    def _on_width_changed(self, spin) -> None:
        try:
            v = int(spin.get_value())
        except Exception:
            return
        self._line_width = max(1, min(12, v))
        if self._selected is not None and 0 <= self._selected < len(self._shapes):
            t = self._shapes[self._selected].get("type")
            if t in ("rect", "ellipse", "line", "pen"):
                self._push_history()
                self._shapes[self._selected]["width"] = self._line_width
                self._area.queue_draw()
                try:
                    self._collab_broadcast_shapes()
                except Exception:
                    pass

    def _on_font_changed(self, spin) -> None:
        try:
            v = int(spin.get_value())
        except Exception:
            return
        self._font_size = max(8, min(48, v))
        if self._selected is not None and 0 <= self._selected < len(self._shapes):
            if self._shapes[self._selected].get("type") == "text":
                self._push_history()
                self._shapes[self._selected]["size"] = self._font_size
                self._area.queue_draw()
                try:
                    self._collab_broadcast_shapes()
                except Exception:
                    pass

    def _on_fill_toggled(self, btn) -> None:
        self._fill = bool(btn.get_active())
        if self._selected is not None and 0 <= self._selected < len(self._shapes):
            t = self._shapes[self._selected].get("type")
            if t in ("rect", "ellipse"):
                self._push_history()
                self._shapes[self._selected]["fill"] = self._fill
                self._area.queue_draw()
                try:
                    self._collab_broadcast_shapes()
                except Exception:
                    pass

    # ── zoom / pan ───────────────────────────────────────────────
    def _zoom(self, factor: float) -> None:
        ns = max(0.25, min(3.0, self._scale * factor))
        if abs(ns - self._scale) < 0.01:
            return
        # zoom around center
        w = self._area.get_width() or 900
        h = self._area.get_height() or 560
        cx = w / 2; cy = h / 2
        wx = (cx - self._off_x) / self._scale
        wy = (cy - self._off_y) / self._scale
        self._scale = ns
        self._off_x = cx - wx * ns
        self._off_y = cy - wy * ns
        self._area.queue_draw()
        self._update_status()

    def _reset_view(self) -> None:
        self._scale = 1.0
        self._off_x = 0.0
        self._off_y = 0.0
        self._area.queue_draw()
        self._update_status()

    def _world(self, sx: float, sy: float) -> tuple[float, float]:
        return ((sx - self._off_x) / self._scale, (sy - self._off_y) / self._scale)

    # ── draw ─────────────────────────────────────────────────────
    def _on_draw(self, area: Gtk.DrawingArea, cr, width: int, height: int, _data) -> None:
        # фон через CSS уже есть, дорисовываем сетку
        cr.save()
        # clip
        # сетка точками
        cr.set_source_rgba(0.58, 0.63, 0.72, 0.08)
        step = 24 * self._scale
        if step >= 8:
            # grid в мировых координатах с учётом пана
            start_x = -self._off_x % step if step != 0 else 0
            start_y = -self._off_y % step if step != 0 else 0
            cr.set_line_width(1)
            # точки
            for gx in range(int(start_x), width, int(step) if step else 24):
                for gy in range(int(start_y), height, int(step) if step else 24):
                    cr.arc(gx, gy, 1.0, 0, 2 * math.pi)
                    cr.fill()

        # фигуры: применяем трансформ пана/зума через ручной пересчёт координат,
        # проще — временно сдвинуть cr
        cr.save()
        cr.translate(self._off_x, self._off_y)
        cr.scale(self._scale, self._scale)

        for idx, sh in enumerate(self._shapes):
            _draw_shape(cr, sh, selected=(idx == self._selected))

        # preview
        if self._preview is not None:
            cr.set_dash([6, 4], 0)
            _draw_shape(cr, self._preview, selected=False)
            cr.set_dash([], 0)
        if self._pen_points is not None and len(self._pen_points) >= 2:
            prev = {"type": "pen", "points": self._pen_points, "color": self._color, "width": self._line_width}
            cr.set_dash([6, 4], 0)
            _draw_shape(cr, prev, selected=False)
            cr.set_dash([], 0)

        # shared cursors (world coords, внутри scale/translate)
        try:
            if _HAS_CANVAS_COLLAB and self._collab is not None and getattr(self, "_collab_enabled", False) and getattr(self, "_cursor_btn", None) is not None and self._cursor_btn.get_active():
                cursors = self._collab.get_remote_cursors() if hasattr(self._collab, "get_remote_cursors") else []
                for c in cursors:
                    cx = float(getattr(c, "x", 0)); cy = float(getattr(c, "y", 0))
                    col = str(getattr(c, "color", "#ff8a8a") or "#ff8a8a")
                    label = str(getattr(c, "label", getattr(c, "replica_id", "")) or "")
                    # курсор — треугольник + лейбл
                    _set_source_hex(cr, col, 1.0)
                    cr.move_to(cx, cy)
                    cr.line_to(cx + 12, cy + 4)
                    cr.line_to(cx + 4, cy + 12)
                    cr.close_path()
                    cr.fill_preserve()
                    _set_source_hex(cr, "#0a0e1a", 0.85)
                    cr.set_line_width(1.0)
                    cr.stroke()
                    # лейбл
                    if label:
                        _set_source_hex(cr, "#0a0e1a", 0.78)
                        cr.select_font_face("Inter", 0, 0)
                        cr.set_font_size(9)
                        ext = cr.text_extents(label[:16])
                        pad = 3
                        lx = cx + 14; ly = cy - 8
                        cr.rectangle(lx - pad, ly - 9, ext.width + pad * 2, 12)
                        cr.fill()
                        _set_source_hex(cr, "#ffffff", 1.0)
                        cr.move_to(lx, ly)
                        cr.show_text(label[:16])
        except Exception:
            pass

        cr.restore()
        cr.restore()

    # ── hit helpers (world coords) ───────────────────────────────
    def _hit_at(self, sx: float, sy: float) -> int | None:
        wx, wy = self._world(sx, sy)
        # сверху-вниз: верхние фигуры приоритетнее (обратный порядок)
        for idx in range(len(self._shapes) - 1, -1, -1):
            if shape_hit(self._shapes[idx], wx, wy):
                return idx
        return None

    # ── controllers ──────────────────────────────────────────────
    def _on_drag_begin(self, gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        self._area.grab_focus()
        # Shift+drag = панорама
        try:
            state = gesture.get_current_event_state()  # type: ignore[attr-defined]
            if state is not None and bool(state & Gdk.ModifierType.SHIFT_MASK):  # type: ignore[attr-defined]
                self._panning = True
                self._pan_start = (x, y)
                self._pan_off = (self._off_x, self._off_y)
                return
        except Exception:
            pass
        wx, wy = self._world(x, y)
        if self._tool == "select":
            hit = self._hit_at(x, y)
            if hit is not None:
                self._selected = hit
                self._dragging = True
                self._drag_start = (wx, wy)
                # запомним исходную фигуру для дельты
                import copy
                self._move_start_shape = copy.deepcopy(self._shapes[hit])
                # вычислить оффсет для move
                sh = self._shapes[hit]
                t = sh.get("type")
                if t in ("rect", "ellipse", "text"):
                    self._move_offset = (wx - float(sh.get("x", 0)), wy - float(sh.get("y", 0)))
                elif t == "line":
                    self._move_offset = (0, 0)  # дельта считается от drag_start
                elif t == "pen":
                    self._move_offset = (wx, wy)
                else:
                    self._move_offset = (0, 0)
                # синхронизируем тулбар с выбранной фигурой
                try:
                    col = str(sh.get("color") or self._color)
                    self._color = col
                    if self._color_btn is not None:
                        self._color_btn.set_rgba(_hex_to_rgba(col))
                    if "width" in sh:
                        self._line_width = int(sh.get("width", self._line_width))
                        self._width_spin.set_value(float(self._line_width))
                    if "size" in sh:
                        self._font_size = int(sh.get("size", self._font_size))
                        self._font_spin.set_value(float(self._font_size))
                    if "fill" in sh:
                        self._fill = bool(sh.get("fill"))
                        self._fill_check.set_active(self._fill)
                except Exception:
                    pass
                self._update_status()
                self._area.queue_draw()
            else:
                # клик в пустоту — снять выделение
                if self._selected is not None:
                    self._selected = None
                    self._area.queue_draw()
                    self._update_status()
                self._dragging = False
                self._drag_start = None
            return

        if self._tool in ("rect", "ellipse", "line"):
            self._dragging = True
            self._drag_start = (wx, wy)
            self._drag_current = (wx, wy)
            self._preview = None
        elif self._tool == "pen":
            self._dragging = True
            self._drag_start = (wx, wy)
            self._pen_points = [[wx, wy]]

    def _on_drag_update(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self._panning:
            if self._pan_start is not None and self._pan_off is not None:
                self._off_x = self._pan_off[0] + dx
                self._off_y = self._pan_off[1] + dy
                self._area.queue_draw()
            return
        if not self._dragging or self._drag_start is None:
            return
        start = gesture.get_start_point()
        if not start[0]:
            return
        sx, sy = start[1], start[2]
        cx = sx + dx
        cy = sy + dy
        wx, wy = self._world(cx, cy)

        if self._tool == "select" and self._selected is not None and self._move_start_shape is not None:
            orig = self._move_start_shape
            t = orig.get("type")
            dxw = wx - self._drag_start[0]
            dyw = wy - self._drag_start[1]
            sh = self._shapes[self._selected]
            if t in ("rect", "ellipse", "text"):
                sh["x"] = float(orig.get("x", 0)) + dxw
                sh["y"] = float(orig.get("y", 0)) + dyw
            elif t == "line":
                sh["x1"] = float(orig.get("x1", 0)) + dxw
                sh["y1"] = float(orig.get("y1", 0)) + dyw
                sh["x2"] = float(orig.get("x2", 0)) + dxw
                sh["y2"] = float(orig.get("y2", 0)) + dyw
            elif t == "pen":
                pts = orig.get("points") or []
                sh["points"] = [[float(p[0]) + dxw, float(p[1]) + dyw] for p in pts]
            self._area.queue_draw()
            return

        if self._tool == "rect":
            x0, y0 = self._drag_start
            self._preview = {"type": "rect", "x": x0, "y": y0, "w": wx - x0, "h": wy - y0, "color": self._color, "width": self._line_width, "fill": self._fill}
            self._area.queue_draw()
        elif self._tool == "ellipse":
            x0, y0 = self._drag_start
            self._preview = {"type": "ellipse", "x": x0, "y": y0, "w": wx - x0, "h": wy - y0, "color": self._color, "width": self._line_width, "fill": self._fill}
            self._area.queue_draw()
        elif self._tool == "line":
            x0, y0 = self._drag_start
            self._preview = {"type": "line", "x1": x0, "y1": y0, "x2": wx, "y2": wy, "color": self._color, "width": self._line_width}
            self._area.queue_draw()
        elif self._tool == "pen" and self._pen_points is not None:
            self._pen_points.append([wx, wy])
            self._area.queue_draw()

    def _on_drag_end(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self._panning:
            self._panning = False
            self._pan_start = None
            self._pan_off = None
            return
        if not self._dragging:
            return
        self._dragging = False
        # завершить move select
        if self._tool == "select" and self._selected is not None and self._move_start_shape is not None:
            # проверить изменилось ли
            changed = self._shapes[self._selected] != self._move_start_shape
            if changed:
                self._push_history()
            self._move_start_shape = None
            self._move_offset = None
            self._drag_start = None
            if changed:
                try:
                    self._collab_broadcast_shapes()
                except Exception:
                    pass
            return

        # создать фигуру из preview
        if self._tool in ("rect", "ellipse"):
            if self._preview is not None:
                # игнор слишком маленьких
                if abs(float(self._preview.get("w", 0))) < 4 or abs(float(self._preview.get("h", 0))) < 4:
                    self._preview = None
                else:
                    self._push_history()
                    self._shapes.append(self._preview)
                    self._selected = len(self._shapes) - 1
                self._preview = None
                self._area.queue_draw()
                self._update_status()
                try:
                    self._collab_broadcast_shapes()
                except Exception:
                    pass
        elif self._tool == "line":
            if self._preview is not None:
                x1 = float(self._preview.get("x1", 0)); y1 = float(self._preview.get("y1", 0))
                x2 = float(self._preview.get("x2", 0)); y2 = float(self._preview.get("y2", 0))
                if math.hypot(x2 - x1, y2 - y1) >= 4:
                    self._push_history()
                    self._shapes.append(self._preview)
                    self._selected = len(self._shapes) - 1
                self._preview = None
                self._area.queue_draw()
                self._update_status()
                try:
                    self._collab_broadcast_shapes()
                except Exception:
                    pass
        elif self._tool == "pen":
            if self._pen_points is not None and len(self._pen_points) >= 2:
                # упростим? оставляем как есть
                # игнор слишком коротких
                length = 0.0
                for i in range(len(self._pen_points) - 1):
                    length += math.hypot(self._pen_points[i + 1][0] - self._pen_points[i][0], self._pen_points[i + 1][1] - self._pen_points[i][1])
                if length >= 6:
                    self._push_history()
                    self._shapes.append({"type": "pen", "points": list(self._pen_points), "color": self._color, "width": self._line_width})
                    self._selected = len(self._shapes) - 1
            self._pen_points = None
            self._area.queue_draw()
            self._update_status()
            try:
                self._collab_broadcast_shapes()
            except Exception:
                pass

        self._drag_start = None
        self._drag_current = None

    def _on_click(self, gesture: Gtk.GestureClick, n: int, x: float, y: float) -> None:
        # текст по клику
        if self._tool != "text":
            return
        # игнор если был drag
        wx, wy = self._world(x, y)
        self._show_text_dialog(wx, wy)

    def _show_text_dialog(self, x: float, y: float) -> None:
        dialog = Adw.Dialog(title="Текст")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
        box.set_size_request(360, -1)
        entry = Gtk.Entry(placeholder_text="Введите текст…")
        entry.set_hexpand(True)
        # предзаполнить если выбран текст
        if self._selected is not None and 0 <= self._selected < len(self._shapes):
            sh = self._shapes[self._selected]
            if sh.get("type") == "text":
                entry.set_text(str(sh.get("text", "")))
                x = float(sh.get("x", x)); y = float(sh.get("y", y))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Отмена")
        ok = Gtk.Button(label="OK", css_classes=["suggested-action"])
        row.append(cancel); row.append(ok)
        box.append(entry); box.append(row)
        dialog.set_child(box)
        cancel.connect("clicked", lambda *_: dialog.close())
        def _do(*_):
            txt = entry.get_text().strip()
            dialog.close()
            if not txt:
                return
            # если редактируем выбранный текст — обновляем
            if self._selected is not None and 0 <= self._selected < len(self._shapes) and self._shapes[self._selected].get("type") == "text":
                # проверим близость клика к выбранному — если да, редактируем, иначе создаём новый
                sh = self._shapes[self._selected]
                if abs(float(sh.get("x", 0)) - x) < 60 and abs(float(sh.get("y", 0)) - y) < 30:
                    self._push_history()
                    sh["text"] = txt
                    sh["color"] = self._color
                    sh["size"] = self._font_size
                    self._area.queue_draw()
                    self._update_status()
                    try:
                        self._collab_broadcast_shapes()
                    except Exception:
                        pass
                    return
            self._push_history()
            self._shapes.append({"type": "text", "x": x, "y": y, "text": txt, "color": self._color, "size": self._font_size})
            self._selected = len(self._shapes) - 1
            self._area.queue_draw()
            self._update_status()
            try:
                self._collab_broadcast_shapes()
            except Exception:
                pass
        ok.connect("clicked", _do)
        entry.connect("activate", _do)
        dialog.present(self.get_root())
        entry.grab_focus()

    # ── pan controllers ──────────────────────────────────────────
    def _on_pan_begin(self, gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        self._panning = True
        self._pan_start = (x, y)
        self._pan_off = (self._off_x, self._off_y)

    def _on_pan_update(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self._panning and self._pan_start is not None and self._pan_off is not None:
            self._off_x = self._pan_off[0] + dx
            self._off_y = self._pan_off[1] + dy
            self._area.queue_draw()

    def _on_pan_end(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        self._panning = False
        self._pan_start = None
        self._pan_off = None

    def _on_scroll(self, ctrl: Gtk.EventControllerScroll, dx: float, dy: float) -> bool:
        # dy <0 zoom in, >0 zoom out
        factor = 1.12 if dy < 0 else 0.88 if dy > 0 else 1.0
        if factor == 1.0:
            return False
        # zoom around cursor
        pos = getattr(self, "_last_motion", None)
        if pos is None:
            w = self._area.get_width() or 900; h = self._area.get_height() or 560
            mx, my = w / 2, h / 2
        else:
            mx, my = pos
        ns = max(0.25, min(3.0, self._scale * factor))
        if abs(ns - self._scale) < 0.01:
            return True
        wx = (mx - self._off_x) / self._scale
        wy = (my - self._off_y) / self._scale
        self._scale = ns
        self._off_x = mx - wx * ns
        self._off_y = my - wy * ns
        self._area.queue_draw()
        self._update_status()
        return True

    _last_motion: tuple[float, float] | None = None

    def _on_motion(self, ctrl: Gtk.EventControllerMotion, x: float, y: float) -> None:
        self._last_motion = (x, y)
        # shared cursor broadcast (world coords)
        try:
            wx, wy = self._world(x, y)
            self._collab_broadcast_cursor(wx, wy)
        except Exception:
            pass
        # курсор уже управляется _on_tool_toggled, но для select подсветим pointer над фигурой
        if self._tool == "select":
            hit = self._hit_at(x, y)
            try:
                if hit is not None:
                    self._area.set_cursor(Gdk.Cursor.new_from_name("pointer"))
                else:
                    self._area.set_cursor(None)
            except Exception:
                pass

    def _on_key(self, _ctrl, keyval, _keycode, state) -> bool:
        # Delete / Backspace
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace, Gdk.KEY_KP_Delete):
            if self._selected is not None:
                self.delete_selected()
                return True
        # Ctrl+S save
        if bool(state & Gdk.ModifierType.CONTROL_MASK) and keyval in (Gdk.KEY_s, Gdk.KEY_S):
            self.save_canvas()
            return True
        # Ctrl+Z undo
        if bool(state & Gdk.ModifierType.CONTROL_MASK) and keyval in (Gdk.KEY_z, Gdk.KEY_Z):
            # Shift+Z redo? не реализуем
            if not bool(state & Gdk.ModifierType.SHIFT_MASK):
                self.undo()
                return True
        # Esc снять выделение
        if keyval == Gdk.KEY_Escape:
            if self._selected is not None:
                self._selected = None
                self._area.queue_draw()
                self._update_status()
                return True
            if self._preview is not None or self._pen_points is not None:
                self._preview = None
                self._pen_points = None
                self._area.queue_draw()
                return True
        return False
