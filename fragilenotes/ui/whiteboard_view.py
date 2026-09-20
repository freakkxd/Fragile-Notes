"""Whiteboard — бесконечный холст с фигурами и коннекторами (как Obsidian Canvas).

Концепт как в Obsidian Canvas: узлы — фигуры (rect/ellipse/diamond/text/note/group),
рёбра — коннекторы между узлами с привязкой к сторонам (top/bottom/left/right).
Бесконечный холст: pan (СКМ/Shift+drag/hand), zoom (колесо/кнопки), сетка.

Формат JSON (совместим с Obsidian Canvas, расширен)::

    {
      "version": 1,
      "nodes": [
        {"id":"...","type":"rect","x":0,"y":0,"width":220,"height":120,"text":"","color":"#8ab4ff","fill":false},
        {"id":"...","type":"ellipse","x":100,"y":100,"width":160,"height":100,"color":"#bea5ff"},
        {"id":"...","type":"diamond","x":50,"y":50,"width":180,"height":120,"color":"#78d296"},
        {"id":"...","type":"text","x":0,"y":0,"width":200,"height":40,"text":"hello","color":"#e4eaf6"},
        {"id":"...","type":"note","x":0,"y":0,"width":260,"height":160,"file":"Заметки/demo.md","color":"#ffd28c"},
        {"id":"...","type":"group","x":0,"y":0,"width":400,"height":300,"label":"Group","color":"#2b303b"}
      ],
      "edges": [
        {"id":"...","fromNode":"<id>","fromSide":"right","toNode":"<id>","toSide":"left","label":"","color":"#8ab4ff","width":2,"arrow":true}
      ]
    }

Поля ``fromSide``/``toSide``: top|bottom|left|right|center (center — авто-центр).
``arrow``: true — стрелка на конце, false — без.

Путь сохранения: для ``vault/notes/foo.md`` → ``vault/notes/foo.whiteboard.json`` или
``vault/Whiteboards/<name>.whiteboard.json``. Для произвольного пути — ``<name>.whiteboard.json``
рядом или в корне vault.

Публичный API (чистые функции):
- whiteboard_path_for_md(path) -> Path
- whiteboard_file_for_path(path) -> Path  (алиас)
- load_whiteboard_json(path) -> dict
- save_whiteboard_json(path, data)  — атомарно
- create_node(type, x, y, w, h, ...) -> dict
- create_edge(fromNode, toNode, ...) -> dict
- node_center(node) -> (cx,cy)
- node_port_pos(node, side) -> (x,y)
- whiteboard_from_obsidian(data) / whiteboard_to_obsidian(data) — конвертация

Виджет:
- WhiteboardView(settings, on_open=None)  — вкладка с toolbar + DrawingArea
- open_whiteboard(path), save_whiteboard(path?), new_whiteboard(), clear_whiteboard()
- save_whiteboard_for_md(md_path)
"""

from __future__ import annotations

import copy
import json
import math
import time
import uuid
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .widgets import view_header  # noqa: E402

# ── константы ─────────────────────────────────────────────────────────

WHITEBOARD_VERSION = 1

DEFAULT_COLOR = "#8ab4ff"
DEFAULT_TEXT_COLOR = "#e4eaf6"
PALETTE = ["#8ab4ff", "#bea5ff", "#78d296", "#ffd28c", "#e6af6e", "#ff8a8a", "#e4eaf6", "#2b303b"]

NODE_TYPES = ("rect", "ellipse", "diamond", "text", "note", "group")
SIDES = ("top", "bottom", "left", "right", "center")
DEFAULT_W = 220
DEFAULT_H = 120

# ── utils: цвет / id / файлы ─────────────────────────────────────────

def _new_id() -> str:
    return uuid.uuid4().hex[:10]


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


def whiteboard_path_for_md(md_path: Path) -> Path:
    """Путь whiteboard рядом с md: ``foo.md`` → ``foo.whiteboard.json``."""
    p = Path(md_path)
    if p.name.endswith(".whiteboard.json"):
        return p
    if p.suffix == ".md":
        return p.with_name(p.stem + ".whiteboard.json")
    if p.suffix == ".json" and ".whiteboard" in p.name:
        return p
    if p.suffix:
        return p.with_name(p.stem + ".whiteboard.json")
    return Path(str(p) + ".whiteboard.json")


# алиасы для совместимости
whiteboard_file_for_path = whiteboard_path_for_md
whiteboard_json_for_md = whiteboard_path_for_md
canvas_json_for_md = whiteboard_path_for_md  # type: ignore[assignment]


def load_whiteboard_json(path: Path) -> dict:
    """Загрузить whiteboard JSON. Возвращает dict с keys ``nodes``/``edges``."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return {"version": WHITEBOARD_VERSION, "nodes": [], "edges": []}
    if isinstance(data, dict):
        nodes = data.get("nodes") if isinstance(data.get("nodes"), list) else data.get("shapes") if isinstance(data.get("shapes"), list) else []
        edges = data.get("edges") if isinstance(data.get("edges"), list) else []
        # shapes из старого canvas_view → конвертим в nodes
        if nodes and isinstance(nodes[0], dict) and "type" in nodes[0] and "x" in nodes[0] and "width" not in nodes[0] and "w" in nodes[0]:
            # old canvas shapes format
            conv = []
            for s in nodes:  # type: ignore
                if not isinstance(s, dict):
                    continue
                t = s.get("type")
                if t in ("rect", "ellipse"):
                    conv.append({
                        "id": s.get("id") or _new_id(),
                        "type": t,
                        "x": float(s.get("x", 0)),
                        "y": float(s.get("y", 0)),
                        "width": float(s.get("w", DEFAULT_W)),
                        "height": float(s.get("h", DEFAULT_H)),
                        "color": s.get("color") or DEFAULT_COLOR,
                        "fill": bool(s.get("fill", False)),
                    })
                elif t == "text":
                    conv.append({
                        "id": s.get("id") or _new_id(),
                        "type": "text",
                        "x": float(s.get("x", 0)),
                        "y": float(s.get("y", 0)),
                        "width": max(120, len(str(s.get("text","")))*8 + 24),
                        "height": int(s.get("size", 14))*1.6 + 12,
                        "text": str(s.get("text","")),
                        "color": s.get("color") or DEFAULT_TEXT_COLOR,
                    })
            nodes = conv
        # фильтрация
        nodes = [n for n in nodes if isinstance(n, dict) and n.get("id")]
        edges = [e for e in edges if isinstance(e, dict) and e.get("id") and e.get("fromNode") and e.get("toNode")]
        return {"version": int(data.get("version") or WHITEBOARD_VERSION), "nodes": nodes, "edges": edges}
    return {"version": WHITEBOARD_VERSION, "nodes": [], "edges": []}


def save_whiteboard_json(path: Path, data: dict) -> None:
    """Атомарно сохранить whiteboard JSON."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": int(data.get("version") or WHITEBOARD_VERSION),
        "nodes": data.get("nodes") or [],
        "edges": data.get("edges") or [],
    }
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _load_whiteboard(path: Path) -> tuple[list[dict], list[dict]]:
    d = load_whiteboard_json(path)
    return list(d.get("nodes") or []), list(d.get("edges") or [])


def _save_whiteboard(path: Path, nodes: list[dict], edges: list[dict]) -> None:
    save_whiteboard_json(path, {"version": WHITEBOARD_VERSION, "nodes": nodes, "edges": edges})


# ── фабрики узлов/рёбер ───────────────────────────────────────────────

def create_node(
    node_type: str = "rect",
    x: float = 0,
    y: float = 0,
    width: float = DEFAULT_W,
    height: float = DEFAULT_H,
    text: str = "",
    color: str = DEFAULT_COLOR,
    **kw,
) -> dict:
    t = node_type if node_type in NODE_TYPES else "rect"
    nid = kw.get("id") or _new_id()
    node: dict = {
        "id": nid,
        "type": t,
        "x": float(x),
        "y": float(y),
        "width": float(width),
        "height": float(height),
        "color": color or DEFAULT_COLOR,
    }
    if text:
        node["text"] = str(text)
    if t == "text" and "text" not in node:
        node["text"] = kw.get("text") or "Текст"
    if t == "note":
        node["file"] = str(kw.get("file") or kw.get("path") or "")
        if not node.get("text") and node.get("file"):
            node["text"] = Path(node["file"]).stem
    if t == "group":
        node["label"] = str(kw.get("label") or kw.get("text") or "Group")
    if "fill" in kw:
        node["fill"] = bool(kw["fill"])
    if "label" in kw and t != "group":
        node["label"] = str(kw["label"])
    # preserve extra keys
    for k in ("file", "label", "fill", "strokeWidth", "fontSize"):
        if k in kw and k not in node:
            node[k] = kw[k]
    return node


def create_edge(
    from_node: str,
    to_node: str,
    from_side: str = "right",
    to_side: str = "left",
    label: str = "",
    color: str = DEFAULT_COLOR,
    width: float = 2,
    arrow: bool = True,
    **kw,
) -> dict:
    fs = from_side if from_side in SIDES else "right"
    ts = to_side if to_side in SIDES else "left"
    eid = kw.get("id") or _new_id()
    edge: dict = {
        "id": eid,
        "fromNode": str(from_node),
        "fromSide": fs,
        "toNode": str(to_node),
        "toSide": ts,
        "label": str(label or kw.get("text") or ""),
        "color": color or DEFAULT_COLOR,
        "width": float(width),
        "arrow": bool(arrow),
    }
    # optional: fromEnd/toEnd like Obsidian (arrow/none/dot)
    if "fromEnd" in kw:
        edge["fromEnd"] = kw["fromEnd"]
    if "toEnd" in kw:
        edge["toEnd"] = kw["toEnd"]
    return edge


# алиасы
create_whiteboard_node = create_node
create_whiteboard_edge = create_edge


def create_whiteboard_data(nodes: list[dict] | None = None, edges: list[dict] | None = None) -> dict:
    return {"version": WHITEBOARD_VERSION, "nodes": list(nodes or []), "edges": list(edges or [])}


# ── геометрия ─────────────────────────────────────────────────────────

def node_center(node: dict) -> tuple[float, float]:
    return (float(node.get("x", 0)) + float(node.get("width", DEFAULT_W)) / 2,
            float(node.get("y", 0)) + float(node.get("height", DEFAULT_H)) / 2)


def node_port_pos(node: dict, side: str) -> tuple[float, float]:
    x = float(node.get("x", 0)); y = float(node.get("y", 0))
    w = float(node.get("width", DEFAULT_W)); h = float(node.get("height", DEFAULT_H))
    if side == "left":
        return (x, y + h / 2)
    if side == "right":
        return (x + w, y + h / 2)
    if side == "top":
        return (x + w / 2, y)
    if side == "bottom":
        return (x + w / 2, y + h)
    # center
    return (x + w / 2, y + h / 2)


def _closest_side(node: dict, px: float, py: float) -> str:
    """Ближайшая сторона узла к точке (для авто-привязки)."""
    x = float(node.get("x", 0)); y = float(node.get("y", 0))
    w = float(node.get("width", DEFAULT_W)); h = float(node.get("height", DEFAULT_H))
    cx, cy = x + w / 2, y + h / 2
    dx = px - cx; dy = py - cy
    # нормируем к полу-размерам
    if w < 1 or h < 1:
        return "center"
    nx = dx / (w / 2) if w else 0
    ny = dy / (h / 2) if h else 0
    if abs(nx) > abs(ny):
        return "right" if dx > 0 else "left"
    return "bottom" if dy > 0 else "top"


def node_bbox(node: dict) -> tuple[float, float, float, float]:
    return (float(node.get("x", 0)), float(node.get("y", 0)),
            float(node.get("width", DEFAULT_W)), float(node.get("height", DEFAULT_H)))


def _hit_node(node: dict, px: float, py: float, pad: float = 0) -> bool:
    x, y, w, h = node_bbox(node)
    return (x - pad <= px <= x + w + pad) and (y - pad <= py <= y + h + pad)


def _hit_port(node: dict, px: float, py: float, side: str, r: float = 10) -> bool:
    sx, sy = node_port_pos(node, side)
    return math.hypot(px - sx, py - sy) <= r


def _dist_to_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    vx = x2 - x1; vy = y2 - y1
    wx = px - x1; wy = py - y1
    denom = vx * vx + vy * vy
    if denom < 0.5:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
    qx = x1 + t * vx; qy = y1 + t * vy
    return math.hypot(px - qx, py - qy)


def _hit_edge(edge: dict, nodes_map: dict[str, dict], px: float, py: float, thresh: float = 8) -> bool:
    fn = nodes_map.get(str(edge.get("fromNode")))
    tn = nodes_map.get(str(edge.get("toNode")))
    if not fn or not tn:
        return False
    x1, y1 = node_port_pos(fn, str(edge.get("fromSide") or "right"))
    x2, y2 = node_port_pos(tn, str(edge.get("toSide") or "left"))
    # bezier approx: sample 12 points
    # cubic bezier with controls offset by 0.5*distance horizontally
    # for simplicity use midpoint curve like mindmap
    # Use 16 samples along curve
    dx = abs(x2 - x1)
    off = max(40, min(160, dx * 0.45))
    # direction based on sides
    def _off_for_side(s: str):
        if s == "left": return (-off, 0)
        if s == "right": return (off, 0)
        if s == "top": return (0, -off)
        if s == "bottom": return (0, off)
        return (off, 0)
    c1x = x1 + _off_for_side(str(edge.get("fromSide") or "right"))[0]
    c1y = y1 + _off_for_side(str(edge.get("fromSide") or "right"))[1]
    c2x = x2 + _off_for_side(str(edge.get("toSide") or "left"))[0]
    c2y = y2 + _off_for_side(str(edge.get("toSide") or "left"))[1]
    # sample
    prev = (x1, y1)
    for i in range(1, 17):
        t = i / 16
        mt = 1 - t
        # cubic bezier formula
        bx = mt*mt*mt*x1 + 3*mt*mt*t*c1x + 3*mt*t*t*c2x + t*t*t*x2
        by = mt*mt*mt*y1 + 3*mt*mt*t*c1y + 3*mt*t*t*c2y + t*t*t*y2
        if _dist_to_segment(px, py, prev[0], prev[1], bx, by) <= thresh:
            return True
        prev = (bx, by)
    return False


def whiteboard_from_obsidian(data: dict) -> dict:
    """Конвертировать Obsidian Canvas JSON в наш формат (nodes/edges совпадают)."""
    if not isinstance(data, dict):
        return {"version": WHITEBOARD_VERSION, "nodes": [], "edges": []}
    nodes = data.get("nodes") or []
    edges = data.get("edges") or []
    # Obsidian color is "1"..; map to hex if needed
    _color_map = {"1": "#fb464c", "2": "#ff8a2b", "3": "#ffbb33", "4": "#44c268", "5": "#4992ff", "6": "#a567ff"}
    out_nodes = []
    for n in nodes:
        if not isinstance(n, dict) or not n.get("id"):
            continue
        c = n.get("color")
        if isinstance(c, str) and c in _color_map:
            n = dict(n)
            n["color"] = _color_map[c]
        out_nodes.append(n)
    out_edges = []
    for e in edges:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        c = e.get("color")
        if isinstance(c, str) and c in _color_map:
            e = dict(e)
            e["color"] = _color_map[c]
        out_edges.append(e)
    return {"version": WHITEBOARD_VERSION, "nodes": out_nodes, "edges": out_edges}


def whiteboard_to_obsidian(data: dict) -> dict:
    """Экспорт в Obsidian Canvas совместимый JSON."""
    nodes = data.get("nodes") or []
    edges = data.get("edges") or []
    return {"nodes": nodes, "edges": edges}


# ── отрисовка ─────────────────────────────────────────────────────────

def _set_source_hex(cr, hex_str: str, alpha: float = 1.0) -> None:
    r, g, b = _hex_to_rgb_float(hex_str)
    cr.set_source_rgba(r, g, b, alpha)


def _draw_node(cr, node: dict, selected: bool = False, scale: float = 1.0) -> None:
    t = node.get("type") or "rect"
    x = float(node.get("x", 0)); y = float(node.get("y", 0))
    w = float(node.get("width", DEFAULT_W)); h = float(node.get("height", DEFAULT_H))
    color = str(node.get("color") or DEFAULT_COLOR)
    fill = bool(node.get("fill", False))
    text = str(node.get("text") or node.get("label") or "")
    # group — пунктир
    if t == "group":
        _set_source_hex(cr, color, 0.06 if not fill else 0.10)
        cr.rectangle(x, y, w, h)
        cr.fill_preserve()
        _set_source_hex(cr, color, 0.38)
        cr.set_dash([8, 6], 0)
        cr.set_line_width(1.2)
        cr.rectangle(x, y, w, h)
        cr.stroke()
        cr.set_dash([], 0)
        # label at top
        if text:
            _set_source_hex(cr, color, 0.9)
            cr.select_font_face("Inter", 0, 1)
            cr.set_font_size(11)
            cr.move_to(x + 8, y + 16)
            cr.show_text(text[:48])
        if selected:
            _set_source_hex(cr, "#8ab4ff", 0.0)
            cr.set_dash([4, 4], 0)
            cr.set_source_rgba(0.51, 0.66, 1.0, 0.9)
            cr.set_line_width(1.0)
            cr.rectangle(x - 2, y - 2, w + 4, h + 4)
            cr.stroke()
            cr.set_dash([], 0)
        return
    if t == "rect":
        if fill:
            _set_source_hex(cr, color, 0.16)
            cr.rectangle(x, y, w, h)
            cr.fill_preserve()
        _set_source_hex(cr, color, 1.0)
        cr.set_line_width(float(node.get("strokeWidth") or node.get("width_line") or 2))
        # rounded rect
        r = min(10, w / 6, h / 6)
        if r > 1:
            cr.new_path()
            cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
            cr.arc(x + w - r, y + r, r, 3 * math.pi / 2, 0)
            cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
            cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
            cr.close_path()
            cr.stroke()
            if selected:
                cr.set_dash([4, 4], 0)
                cr.set_source_rgba(0.51, 0.66, 1.0, 0.9)
                cr.set_line_width(1.0)
                cr.rectangle(x - 3, y - 3, w + 6, h + 6)
                cr.stroke()
                cr.set_dash([], 0)
        else:
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
        cx = x + w / 2; cy = y + h / 2
        rx = abs(w) / 2; ry = abs(h) / 2
        if rx < 0.5 or ry < 0.5:
            return
        if fill:
            _set_source_hex(cr, color, 0.16)
            cr.save()
            cr.translate(cx, cy)
            cr.scale(rx, ry)
            cr.arc(0, 0, 1, 0, 2 * math.pi)
            cr.restore()
            cr.fill_preserve()
        _set_source_hex(cr, color, 1.0)
        cr.set_line_width(float(node.get("strokeWidth") or 2))
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
    elif t == "diamond":
        # ромб: 4 точки по центрам сторон
        cx = x + w / 2; cy = y + h / 2
        pts = [(cx, y), (x + w, cy), (cx, y + h), (x, cy)]
        if fill:
            _set_source_hex(cr, color, 0.16)
            cr.move_to(*pts[0])
            for px, py in pts[1:]:
                cr.line_to(px, py)
            cr.close_path()
            cr.fill_preserve()
        _set_source_hex(cr, color, 1.0)
        cr.set_line_width(float(node.get("strokeWidth") or 2))
        cr.move_to(*pts[0])
        for px, py in pts[1:]:
            cr.line_to(px, py)
        cr.close_path()
        cr.stroke()
        if selected:
            cr.set_dash([4, 4], 0)
            cr.set_source_rgba(0.51, 0.66, 1.0, 0.9)
            cr.set_line_width(1.0)
            cr.rectangle(x - 2, y - 2, w + 4, h + 4)
            cr.stroke()
            cr.set_dash([], 0)
    elif t == "text":
        # текст без рамки, фон слегка
        _set_source_hex(cr, color, 0.06)
        cr.rectangle(x, y, w, h)
        cr.fill()
        _set_source_hex(cr, str(node.get("color") or DEFAULT_TEXT_COLOR), 1.0)
        # font size from node
        fs = float(node.get("fontSize") or node.get("size") or 14)
        cr.select_font_face("Inter", 0, 0)
        cr.set_font_size(fs)
        # wrap: простое разбиение по ширине approx
        # для простоты — одна строка + ellipsis, либо многострочный если есть \n
        lines = (text or "Текст").split("\n")[:6]
        ty = y + fs + 6
        for line in lines:
            if not line:
                ty += fs * 1.3
                continue
            # truncate if too long
            ext = cr.text_extents(line)
            max_w = w - 12
            if ext.width > max_w:
                # binary truncate
                while len(line) > 1 and cr.text_extents(line + "…").width > max_w:
                    line = line[:-1]
                line = line + "…"
            cr.move_to(x + 6, ty)
            cr.show_text(line)
            ty += fs * 1.35
            if ty > y + h - 4:
                break
        if selected:
            cr.set_dash([4, 4], 0)
            cr.set_source_rgba(0.51, 0.66, 1.0, 0.9)
            cr.set_line_width(1.0)
            cr.rectangle(x - 2, y - 2, w + 4, h + 4)
            cr.stroke()
            cr.set_dash([], 0)
        return
    elif t == "note":
        # карточка файла: заголовок + preview
        # фон
        _set_source_hex(cr, "#1a1d24", 0.88)
        cr.rectangle(x, y, w, h)
        cr.fill_preserve()
        _set_source_hex(cr, color, 0.55)
        cr.set_line_width(1.2)
        # rounded
        r = 8
        cr.new_path()
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.arc(x + w - r, y + r, r, 3 * math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.close_path()
        cr.stroke()
        # stripe top
        _set_source_hex(cr, color, 1.0)
        cr.rectangle(x, y, w, 4)
        cr.fill()
        # file icon + title
        _set_source_hex(cr, "#e4eaf6", 0.96)
        cr.select_font_face("Inter", 0, 1)
        cr.set_font_size(11)
        title = text or str(node.get("file") or "Note")[:32]
        # иконка файла
        cr.move_to(x + 10, y + 22)
        cr.show_text("📄 " + title)
        # file path small
        fp = str(node.get("file") or "")[:42]
        if fp:
            _set_source_hex(cr, "#8a90a8", 0.85)
            cr.select_font_face("Inter", 0, 0)
            cr.set_font_size(8)
            cr.move_to(x + 10, y + 36)
            cr.show_text(fp)
        # separator
        _set_source_hex(cr, color, 0.18)
        cr.set_line_width(1)
        cr.move_to(x + 8, y + 44)
        cr.line_to(x + w - 8, y + 44)
        cr.stroke()
        # preview placeholder
        _set_source_hex(cr, "#8a90a8", 0.52)
        cr.select_font_face("Inter", 0, 0)
        cr.set_font_size(9)
        cr.move_to(x + 10, y + 62)
        cr.show_text("↗ двойной клик — открыть")
        if selected:
            cr.set_dash([4, 4], 0)
            cr.set_source_rgba(0.51, 0.66, 1.0, 0.9)
            cr.set_line_width(1.2)
            cr.rectangle(x - 2, y - 2, w + 4, h + 4)
            cr.stroke()
            cr.set_dash([], 0)
        return
    # общий текст внутри фигуры (если есть text и тип не text/note/group)
    if text and t not in ("text", "note", "group"):
        _set_source_hex(cr, "#e4eaf6", 0.92)
        cr.select_font_face("Inter", 0, 0)
        cr.set_font_size(11)
        # центрированный текст
        lines = text.split("\n")[:3]
        # measure first line for center
        for idx, line in enumerate(lines):
            ext = cr.text_extents(line[:28])
            tx = x + (w - ext.width) / 2
            ty = y + h / 2 - (len(lines)-1)*7 + idx*14
            cr.move_to(max(x+6, tx), ty)
            cr.show_text(line[:28])
    if t not in ("group", "text", "note") and selected:
        # порты для коннекторов при выделении
        for side in ("top", "bottom", "left", "right"):
            sx, sy = node_port_pos(node, side)
            _set_source_hex(cr, "#8ab4ff", 1.0)
            cr.arc(sx, sy, 5, 0, 2*math.pi)
            cr.fill_preserve()
            _set_source_hex(cr, "#0a0e1a", 1.0)
            cr.set_line_width(1.2)
            cr.stroke()
            # inner dot
            _set_source_hex(cr, "#ffffff", 0.92)
            cr.arc(sx, sy, 2, 0, 2*math.pi)
            cr.fill()


def _draw_edge(cr, edge: dict, nodes_map: dict[str, dict], selected: bool = False) -> None:
    fn = nodes_map.get(str(edge.get("fromNode")))
    tn = nodes_map.get(str(edge.get("toNode")))
    if not fn or not tn:
        return
    fs = str(edge.get("fromSide") or "right")
    ts = str(edge.get("toSide") or "left")
    x1, y1 = node_port_pos(fn, fs)
    x2, y2 = node_port_pos(tn, ts)
    color = str(edge.get("color") or DEFAULT_COLOR)
    width = float(edge.get("width") or 2)
    # bezier control offset
    dx = abs(x2 - x1)
    dy = abs(y2 - y1)
    off = max(36, min(180, max(dx, dy) * 0.42))
    def _off(side: str):
        if side == "left": return (-off, 0)
        if side == "right": return (off, 0)
        if side == "top": return (0, -off)
        if side == "bottom": return (0, off)
        return (off, 0)
    c1x = x1 + _off(fs)[0]; c1y = y1 + _off(fs)[1]
    c2x = x2 + _off(ts)[0]; c2y = y2 + _off(ts)[1]
    # glow if selected
    if selected:
        _set_source_hex(cr, color, 0.18)
        cr.set_line_width(width + 6)
        cr.set_line_cap(1)
        cr.move_to(x1, y1)
        cr.curve_to(c1x, c1y, c2x, c2y, x2, y2)
        cr.stroke()
    _set_source_hex(cr, color, 0.95 if selected else 0.88)
    cr.set_line_width(width if not selected else width + 0.8)
    cr.set_line_cap(1)
    cr.set_line_join(1)
    if edge.get("dash"):
        cr.set_dash([6, 4], 0)
    cr.move_to(x1, y1)
    cr.curve_to(c1x, c1y, c2x, c2y, x2, y2)
    cr.stroke()
    cr.set_dash([], 0)
    # arrow head at to
    if edge.get("arrow", True):
        # tangent at end: approximate direction from c2 to end
        ang = math.atan2(y2 - c2y, x2 - c2x)
        if math.hypot(x2 - c2x, y2 - c2y) < 1:
            ang = math.atan2(y2 - y1, x2 - x1)
        ah = 10  # arrow size
        aw = 6
        # arrow points
        p1x = x2 - ah * math.cos(ang) + aw * math.sin(ang)
        p1y = y2 - ah * math.sin(ang) - aw * math.cos(ang)
        p2x = x2 - ah * math.cos(ang) - aw * math.sin(ang)
        p2y = y2 - ah * math.sin(ang) + aw * math.cos(ang)
        _set_source_hex(cr, color, 1.0)
        cr.move_to(x2, y2)
        cr.line_to(p1x, p1y)
        cr.line_to(p2x, p2y)
        cr.close_path()
        cr.fill()
        # outline
        _set_source_hex(cr, "#0a0e1a", 0.42)
        cr.set_line_width(1)
        cr.move_to(x2, y2)
        cr.line_to(p1x, p1y)
        cr.line_to(p2x, p2y)
        cr.close_path()
        cr.stroke()
    # label in middle
    label = str(edge.get("label") or "").strip()
    if label:
        # midpoint of bezier at t=0.5
        t = 0.5; mt = 0.5
        mx = mt*mt*mt*x1 + 3*mt*mt*t*c1x + 3*mt*t*t*c2x + t*t*t*x2
        my = mt*mt*mt*y1 + 3*mt*mt*t*c1y + 3*mt*t*t*c2y + t*t*t*y2
        _set_source_hex(cr, "#0e1220", 0.82)
        cr.select_font_face("Inter", 0, 0)
        cr.set_font_size(9)
        ext = cr.text_extents(label[:28])
        pad = 4
        cr.rectangle(mx - ext.width/2 - pad, my - 9 - pad/2, ext.width + pad*2, 13 + pad)
        cr.fill()
        _set_source_hex(cr, "#e4eaf6", 0.96)
        cr.move_to(mx - ext.width/2, my)
        cr.show_text(label[:28])
    # handle dots for selected
    if selected:
        _set_source_hex(cr, color, 0.95)
        for (cx, cy) in ((x1, y1), (x2, y2)):
            cr.arc(cx, cy, 4, 0, 2*math.pi)
            cr.fill()
            _set_source_hex(cr, "#ffffff", 0.94)
            cr.arc(cx, cy, 1.8, 0, 2*math.pi)
            cr.fill()
            _set_source_hex(cr, color, 0.95)


# ── виджет вкладки ───────────────────────────────────────────────────

class WhiteboardView(Gtk.Box):
    """Вкладка Whiteboard: бесконечный холст с фигурами и коннекторами."""

    def __init__(self, settings: dict, on_open=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self.on_open = on_open
        self._vault_root = Path(str(settings.get("vault_root") or Path.home() / "desktop"))

        # модель
        self._nodes: list[dict] = []
        self._edges: list[dict] = []
        self._selected_node: str | None = None
        self._selected_edge: str | None = None
        self._history: list[tuple[list[dict], list[dict]]] = []
        self._whiteboard_path: Path | None = None
        self._current_md: Path | None = None

        # инструмент/стиль
        self._tool: str = "select"  # select|hand|rect|ellipse|diamond|text|note|group|connector
        self._color: str = DEFAULT_COLOR
        self._line_width: int = 2
        self._fill: bool = False
        self._font_size: int = 14
        self._arrow: bool = True

        # drag/view
        self._dragging = False
        self._drag_start: tuple[float, float] | None = None
        self._drag_current: tuple[float, float] | None = None
        self._move_start_node: dict | None = None
        self._handle: str | None = None  # resize handle: nw/ne/sw/se/n/s/e/w or port
        self._port_drag: dict | None = None  # {fromNode, fromSide}
        self._preview_node: dict | None = None
        self._preview_edge: dict | None = None

        # view transform
        self._scale: float = 1.0
        self._off_x: float = 0.0
        self._off_y: float = 0.0
        self._panning = False
        self._pan_start: tuple[float, float] | None = None
        self._pan_off: tuple[float, float] | None = None

        # hover
        self._hover_node: str | None = None
        self._hover_edge: str | None = None
        self._hover_port: tuple[str, str] | None = None

        self._build_ui()
        self._update_status()

    # ── UI ───────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.append(view_header("🧊", "Whiteboard", "Бесконечный холст · фигуры и коннекторы как в Obsidian Canvas · pan/zoom, JSON"))

        # toolbar файл
        file_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["toolbar", "whiteboard-toolbar"])
        file_bar.set_margin_start(14); file_bar.set_margin_end(14)

        open_md_btn = Gtk.Button(label="Открыть .md", tooltip_text="Выбрать md и открыть его whiteboard (рядом .whiteboard.json)")
        open_md_btn.connect("clicked", self._on_open_md)
        file_bar.append(open_md_btn)

        open_btn = Gtk.Button(label="Открыть JSON", tooltip_text="Открыть существующий .whiteboard.json / canvas.json")
        open_btn.connect("clicked", self._on_open_json)
        file_bar.append(open_btn)

        save_btn = Gtk.Button(label="Сохранить", css_classes=["suggested-action"], tooltip_text="Сохранить рядом с md (Ctrl+S)")
        save_btn.connect("clicked", lambda *_: self.save_whiteboard())
        file_bar.append(save_btn)

        save_as_btn = Gtk.Button(label="Сохранить как…")
        save_as_btn.connect("clicked", self._on_save_as)
        file_bar.append(save_as_btn)

        new_btn = Gtk.Button(label="Новый", tooltip_text="Очистить холст")
        new_btn.connect("clicked", lambda *_: self.new_whiteboard())
        file_bar.append(new_btn)

        file_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        undo_btn = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text="Отменить (Ctrl+Z)")
        undo_btn.connect("clicked", lambda *_: self.undo())
        file_bar.append(undo_btn)

        dup_btn = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Дублировать выбранное (Ctrl+D)")
        dup_btn.connect("clicked", lambda *_: self.duplicate_selected())
        file_bar.append(dup_btn)

        clear_btn = Gtk.Button(label="Очистить", css_classes=["destructive-action"], tooltip_text="Удалить все")
        clear_btn.connect("clicked", lambda *_: self.clear_whiteboard())
        file_bar.append(clear_btn)

        del_btn = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Удалить выбранное (Delete)")
        del_btn.connect("clicked", lambda *_: self.delete_selected())
        file_bar.append(del_btn)

        self._file_label = Gtk.Label(label="· нет файла", css_classes=["dim-hint", "whiteboard-file"], hexpand=True, halign=Gtk.Align.END, xalign=1, ellipsize=Pango.EllipsizeMode.MIDDLE)
        file_bar.append(self._file_label)
        self.append(file_bar)

        # toolbar инструменты
        tool_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["toolbar", "whiteboard-tools"])
        tool_bar.set_margin_start(14); tool_bar.set_margin_end(14)

        self._tool_btns: dict[str, Gtk.ToggleButton] = {}
        tools = [
            ("select", "↖ Выбор", "Выбор/перемещение · порты для коннекторов"),
            ("hand", "✋ Рука", "Панорамирование (или СКМ/Shift+drag)"),
            ("rect", "▭ Прямоуг.", "Прямоугольник"),
            ("ellipse", "⬭ Эллипс", "Эллипс"),
            ("diamond", "⬥ Ромб", "Ромб"),
            ("text", "T Текст", "Текстовый блок"),
            ("note", "📄 Заметка", "Карточка заметки (.md)"),
            ("group", "▭ Группа", "Группирующий фрейм"),
            ("connector", "⟷ Связь", "Коннектор: тяни от порта к порту"),
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
        for col in PALETTE:
            btn = Gtk.Button(css_classes=["whiteboard-color"])
            lbl = Gtk.Label(label="●")
            btn.set_child(lbl)
            btn.set_tooltip_text(col)
            try:
                lbl.set_markup(f'<span foreground="{col}">●</span>')
            except Exception:
                pass
            btn.connect("clicked", self._on_palette, col)
            pal.append(btn)
        tool_bar.append(pal)

        try:
            self._color_btn = Gtk.ColorButton()
            try:
                self._color_btn.set_rgba(_hex_to_rgba(self._color))
            except Exception:
                pass
            self._color_btn.set_tooltip_text("Цвет фигуры/коннектора")
            self._color_btn.connect("color-set", self._on_color_set)
            tool_bar.append(self._color_btn)
        except Exception:
            self._color_btn = None  # type: ignore[assignment]

        tool_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        tool_bar.append(Gtk.Label(label="Толщ:", css_classes=["dim-hint"]))
        self._width_spin = Gtk.SpinButton.new_with_range(1, 10, 1)
        self._width_spin.set_value(float(self._line_width))
        self._width_spin.connect("value-changed", self._on_width_changed)
        tool_bar.append(self._width_spin)

        tool_bar.append(Gtk.Label(label="Шрифт:", css_classes=["dim-hint"]))
        self._font_spin = Gtk.SpinButton.new_with_range(8, 36, 1)
        self._font_spin.set_value(float(self._font_size))
        self._font_spin.connect("value-changed", self._on_font_changed)
        tool_bar.append(self._font_spin)

        self._fill_check = Gtk.CheckButton(label="Заливка")
        self._fill_check.set_active(self._fill)
        self._fill_check.connect("toggled", self._on_fill_toggled)
        tool_bar.append(self._fill_check)

        self._arrow_check = Gtk.CheckButton(label="Стрелка")
        self._arrow_check.set_active(self._arrow)
        self._arrow_check.connect("toggled", self._on_arrow_toggled)
        tool_bar.append(self._arrow_check)

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
        fit_btn = Gtk.Button(label="Вписать", tooltip_text="Вписать все узлы")
        fit_btn.connect("clicked", lambda *_: self._fit_view())
        tool_bar.append(fit_btn)

        self.append(tool_bar)

        hint = Gtk.Label(
            label="ЛКМ — создать/выбрать · тяни порты ● для коннектора · перетаскивание — переместить · Delete — удалить · колесо — зум · СКМ/Shift+drag/Рука — панорама · двойной клик на заметку — открыть",
            css_classes=["dim-hint", "whiteboard-hint"], halign=Gtk.Align.START, xalign=0, wrap=True,
        )
        hint.set_margin_start(14); hint.set_margin_end(14)
        self.append(hint)

        # canvas
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        frame.set_margin_start(14); frame.set_margin_end(14); frame.set_margin_bottom(14)

        self._area = Gtk.DrawingArea(hexpand=True, vexpand=True, css_classes=["whiteboard-area", "graph-area"])
        self._area.set_draw_func(self._on_draw, None)
        self._area.set_content_width(900)
        self._area.set_content_height(560)
        self._area.set_can_focus(True)
        self._area.set_focusable(True)
        self._area.set_size_request(640, 380)

        # drag create/move
        drag = Gtk.GestureDrag.new()
        drag.set_button(1)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self._area.add_controller(drag)

        # pan with middle
        pan = Gtk.GestureDrag.new()
        pan.set_button(2)
        pan.connect("drag-begin", self._on_pan_begin)
        pan.connect("drag-update", self._on_pan_update)
        pan.connect("drag-end", self._on_pan_end)
        self._area.add_controller(pan)

        # also right-button pan
        pan_r = Gtk.GestureDrag.new()
        pan_r.set_button(3)
        pan_r.connect("drag-begin", self._on_pan_begin)
        pan_r.connect("drag-update", self._on_pan_update)
        pan_r.connect("drag-end", self._on_pan_end)
        self._area.add_controller(pan_r)

        click = Gtk.GestureClick.new()
        click.set_button(1)
        click.connect("pressed", self._on_click_pressed)
        click.connect("released", self._on_click_released)
        self._area.add_controller(click)

        dbl = Gtk.GestureClick.new()
        dbl.set_button(1)
        try:
            dbl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        except Exception:
            pass
        dbl.connect("pressed", self._on_double_click)
        self._area.add_controller(dbl)

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

        gkey = Gtk.EventControllerKey.new()
        gkey.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gkey.connect("key-pressed", self._on_key)
        self.add_controller(gkey)

        frame_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        frame_box.append(self._area)
        frame.append(frame_box)
        self.append(frame)

        self._status = Gtk.Label(label="Готов · выберите инструмент", css_classes=["dim-hint", "whiteboard-status"], halign=Gtk.Align.START, xalign=0)
        self._status.set_margin_start(14)
        self.append(self._status)

    # ── история ──────────────────────────────────────────────────────
    def _push_history(self) -> None:
        self._history.append((copy.deepcopy(self._nodes), copy.deepcopy(self._edges)))
        if len(self._history) > 64:
            self._history.pop(0)

    # ── статус/файл ──────────────────────────────────────────────────
    def _update_status(self) -> None:
        if self._whiteboard_path is not None:
            try:
                rel = self._whiteboard_path.relative_to(self._vault_root)
                txt = str(rel)
            except Exception:
                txt = self._whiteboard_path.name
            self._file_label.set_text(txt)
            self._file_label.set_tooltip_text(str(self._whiteboard_path))
        elif self._current_md is not None:
            try:
                rel = self._current_md.relative_to(self._vault_root)
                txt = f"{rel} → {whiteboard_path_for_md(self._current_md).name}"
            except Exception:
                txt = self._current_md.name
            self._file_label.set_text(txt)
            self._file_label.set_tooltip_text(str(whiteboard_path_for_md(self._current_md)))
        else:
            self._file_label.set_text("· нет файла (сохранится как .whiteboard.json в vault)")
            self._file_label.set_tooltip_text("Выберите md или сохраните как…")
        n = len(self._nodes); e = len(self._edges)
        sel = ""
        if self._selected_node is not None:
            sel = f" · выбрано узел {self._selected_node[:6]}"
        elif self._selected_edge is not None:
            sel = f" · выбрано ребро {self._selected_edge[:6]}"
        tool_names = {"select":"Выбор","hand":"Рука","rect":"Прямоугольник","ellipse":"Эллипс","diamond":"Ромб","text":"Текст","note":"Заметка","group":"Группа","connector":"Коннектор"}
        tool = tool_names.get(self._tool, self._tool)
        self._status.set_text(f"{n} узлов · {e} связей · {tool}{sel} · {int(self._scale*100)}%")
        self._zoom_label.set_text(f"{int(self._scale*100)}%")

    def _notify(self, msg: str) -> None:
        try:
            win = self.get_root()
            if win is not None and hasattr(win, "toast_overlay"):
                toast = Adw.Toast.new(msg)
                toast.set_timeout(3)
                win.toast_overlay.add_toast(toast)  # type: ignore[attr-defined]
                return
        except Exception:
            pass
        self._status.set_text(msg)
        GLib.timeout_add(2500, lambda: (self._update_status(), False)[1])

    # ── public API ───────────────────────────────────────────────────
    def new_whiteboard(self) -> None:
        if self._nodes or self._edges:
            self._push_history()
        self._nodes = []
        self._edges = []
        self._selected_node = None
        self._selected_edge = None
        self._preview_node = None
        self._preview_edge = None
        self._whiteboard_path = None
        self._current_md = None
        self._area.queue_draw()
        self._update_status()

    def open_whiteboard(self, path: Path) -> None:
        p = Path(path)
        if p.suffix == ".md":
            cand = whiteboard_path_for_md(p)
            if cand.is_file():
                p = cand
            else:
                self._current_md = p
                self._whiteboard_path = cand
                self._nodes = []
                self._edges = []
                self._selected_node = None
                self._selected_edge = None
                self._area.queue_draw()
                self._update_status()
                return
        if p.is_file():
            nodes, edges = _load_whiteboard(p)
            # also try obsidian canvas .canvas extension
            if not nodes and not edges and p.suffix == ".canvas":
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    conv = whiteboard_from_obsidian(data)
                    nodes = conv.get("nodes") or []
                    edges = conv.get("edges") or []
                except Exception:
                    pass
            self._nodes = nodes
            self._edges = edges
            self._selected_node = None
            self._selected_edge = None
            self._whiteboard_path = p
            if p.name.endswith(".whiteboard.json"):
                md_cand = p.with_name(p.name[: -len(".whiteboard.json")] + ".md")
                if md_cand.is_file():
                    self._current_md = md_cand
            elif p.name.endswith(".canvas"):
                md_cand = p.with_name(p.stem + ".md")
                if md_cand.is_file():
                    self._current_md = md_cand
            self._area.queue_draw()
            self._update_status()
            self._fit_view()
        else:
            self._whiteboard_path = p
            self._nodes = []
            self._edges = []
            self._selected_node = None
            self._selected_edge = None
            self._area.queue_draw()
            self._update_status()

    def save_whiteboard(self, path: Path | None = None) -> Path | None:
        target: Path | None = None
        if path is not None:
            target = Path(path)
        elif self._whiteboard_path is not None:
            target = self._whiteboard_path
        elif self._current_md is not None:
            target = whiteboard_path_for_md(self._current_md)
        else:
            ts = time.strftime("%Y%m%d-%H%M%S")
            target = self._vault_root / "Whiteboards" / f"whiteboard-{ts}.whiteboard.json"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _save_whiteboard(target, self._nodes, self._edges)
            self._whiteboard_path = target
            self._update_status()
            self._notify(f"Сохранено: {target.name}")
            return target
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Ошибка сохранения: {exc}")
            return None

    def save_whiteboard_for_md(self, md_path: Path) -> Path | None:
        self._current_md = Path(md_path)
        return self.save_whiteboard(whiteboard_path_for_md(self._current_md))

    def clear_whiteboard(self) -> None:
        if not self._nodes and not self._edges:
            return
        self._push_history()
        self._nodes.clear()
        self._edges.clear()
        self._selected_node = None
        self._selected_edge = None
        self._preview_node = None
        self._preview_edge = None
        self._area.queue_draw()
        self._update_status()

    def delete_selected(self) -> None:
        if self._selected_edge is not None:
            self._push_history()
            self._edges = [e for e in self._edges if e.get("id") != self._selected_edge]
            self._selected_edge = None
            self._area.queue_draw()
            self._update_status()
            return
        if self._selected_node is not None:
            self._push_history()
            nid = self._selected_node
            self._nodes = [n for n in self._nodes if n.get("id") != nid]
            # удалить связанные рёбра
            self._edges = [e for e in self._edges if e.get("fromNode") != nid and e.get("toNode") != nid]
            self._selected_node = None
            self._area.queue_draw()
            self._update_status()

    def duplicate_selected(self) -> None:
        if self._selected_node is None:
            return
        src = next((n for n in self._nodes if n.get("id") == self._selected_node), None)
        if not src:
            return
        self._push_history()
        dup = copy.deepcopy(src)
        dup["id"] = _new_id()
        dup["x"] = float(dup.get("x", 0)) + 24
        dup["y"] = float(dup.get("y", 0)) + 24
        self._nodes.append(dup)
        self._selected_node = dup["id"]
        self._area.queue_draw()
        self._update_status()

    def undo(self) -> None:
        if not self._history:
            return
        nodes, edges = self._history.pop()
        self._nodes = nodes
        self._edges = edges
        # validate selection still exists
        if self._selected_node is not None and not any(n.get("id")==self._selected_node for n in self._nodes):
            self._selected_node = None
        if self._selected_edge is not None and not any(e.get("id")==self._selected_edge for e in self._edges):
            self._selected_edge = None
        self._preview_node = None
        self._preview_edge = None
        self._area.queue_draw()
        self._update_status()

    def export_obsidian_canvas(self, path: Path) -> Path | None:
        """Экспорт в Obsidian Canvas формат (.canvas)."""
        target = Path(path)
        if not target.suffix:
            target = target.with_suffix(".canvas")
        try:
            data = whiteboard_to_obsidian({"version": WHITEBOARD_VERSION, "nodes": self._nodes, "edges": self._edges})
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(target)
            self._notify(f"Экспортировано: {target.name}")
            return target
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Ошибка экспорта: {exc}")
            return None

    # ── файловые диалоги ─────────────────────────────────────────────
    def _on_open_md(self, _btn) -> None:
        self._pick_file("md")

    def _on_open_json(self, _btn) -> None:
        self._pick_file("json")

    def _on_save_as(self, _btn) -> None:
        self._pick_save()

    def _pick_file(self, kind: str) -> None:
        try:
            dlg = Gtk.FileDialog()
            dlg.set_title("Открыть .md" if kind == "md" else "Открыть whiteboard JSON")
            filt = Gio.ListStore.new(Gtk.FileFilter)
            if kind == "md":
                f = Gtk.FileFilter()
                f.set_name("Markdown (*.md)")
                f.add_pattern("*.md")
                filt.append(f)
            else:
                f = Gtk.FileFilter()
                f.set_name("Whiteboard (*.whiteboard.json, *.canvas)")
                f.add_pattern("*.whiteboard.json")
                f.add_pattern("*.canvas")
                filt.append(f)
                fj = Gtk.FileFilter()
                fj.set_name("JSON (*.json)")
                fj.add_pattern("*.json")
                filt.append(fj)
            dlg.set_filters(filt)
            try:
                dlg.set_initial_folder(Gio.File.new_for_path(str(self._vault_root)))
            except Exception:
                pass
            dlg.open(self.get_root(), None, self._on_pick_file_done, kind)
            return
        except Exception:
            pass
        self._fallback_open_dialog(kind)

    def _on_pick_file_done(self, dlg: Gtk.FileDialog, res, kind: str) -> None:
        try:
            f = dlg.open_finish(res)
            if f is None:
                return
            path = Path(f.get_path() or "")
            if path:
                self.open_whiteboard(path)
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Ошибка открытия: {exc}")

    def _fallback_open_dialog(self, kind: str) -> None:
        dialog = Adw.Dialog(title="Открыть файл")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
        box.set_size_request(420, -1)
        lbl = Gtk.Label(label="Путь к файлу (от vault или абсолютный):", halign=Gtk.Align.START)
        entry = Gtk.Entry(placeholder_text="например: Заметки/foo.md  или  board.whiteboard.json")
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
            self.open_whiteboard(p)
        ok.connect("clicked", _do)
        entry.connect("activate", _do)
        dialog.present(self.get_root())

    def _pick_save(self) -> None:
        try:
            dlg = Gtk.FileDialog()
            dlg.set_title("Сохранить whiteboard как…")
            filt = Gio.ListStore.new(Gtk.FileFilter)
            f = Gtk.FileFilter()
            f.set_name("Whiteboard (*.whiteboard.json)")
            f.add_pattern("*.whiteboard.json")
            filt.append(f)
            dlg.set_filters(filt)
            try:
                dlg.set_initial_folder(Gio.File.new_for_path(str(self._vault_root)))
                if self._whiteboard_path is not None:
                    dlg.set_initial_name(self._whiteboard_path.name)
                elif self._current_md is not None:
                    dlg.set_initial_name(whiteboard_path_for_md(self._current_md).name)
                else:
                    dlg.set_initial_name("board.whiteboard.json")
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
                if not path.name.endswith(".whiteboard.json") and not path.name.endswith(".json") and not path.name.endswith(".canvas"):
                    path = path.with_name(path.name + ".whiteboard.json")
                self.save_whiteboard(path)
        except Exception as exc:  # noqa: BLE001
            self._notify(f"Ошибка сохранения: {exc}")

    def _fallback_save_dialog(self) -> None:
        dialog = Adw.Dialog(title="Сохранить как…")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
        box.set_size_request(420, -1)
        lbl = Gtk.Label(label="Имя файла (сохранится в vault):", halign=Gtk.Align.START)
        entry = Gtk.Entry(placeholder_text="например: board.whiteboard.json  или  Whiteboards/foo.whiteboard.json")
        try:
            if self._whiteboard_path is not None:
                entry.set_text(str(self._whiteboard_path.relative_to(self._vault_root)))
            elif self._current_md is not None:
                entry.set_text(str(whiteboard_path_for_md(self._current_md).relative_to(self._vault_root)))
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
            if not p.name.endswith(".whiteboard.json") and not p.name.endswith(".json") and not p.name.endswith(".canvas"):
                p = p.with_name(p.name + ".whiteboard.json")
            dialog.close()
            self.save_whiteboard(p)
        ok.connect("clicked", _do)
        entry.connect("activate", _do)
        dialog.present(self.get_root())

    # ── toolbar callbacks ────────────────────────────────────────────
    def _on_tool_toggled(self, btn: Gtk.ToggleButton, tid: str) -> None:
        if not btn.get_active():
            if not any(b.get_active() for b in self._tool_btns.values()):
                btn.set_active(True)
            return
        for k, b in self._tool_btns.items():
            if k != tid and b.get_active():
                b.set_active(False)
        self._tool = tid
        self._update_status()
        try:
            cursors = {"hand":"grab","connector":"crosshair","text":"text","select":"default"}
            cname = cursors.get(tid, "crosshair" if tid in ("rect","ellipse","diamond","note","group") else "default")
            self._area.set_cursor(Gdk.Cursor.new_from_name(cname))
        except Exception:
            pass

    def _on_palette(self, _btn, col: str) -> None:
        self._color = col
        try:
            if self._color_btn is not None:
                self._color_btn.set_rgba(_hex_to_rgba(col))
        except Exception:
            pass
        # применить к выбранному
        if self._selected_node is not None:
            idx = next((i for i,n in enumerate(self._nodes) if n.get("id")==self._selected_node), None)
            if idx is not None:
                self._push_history()
                self._nodes[idx]["color"] = col
                self._area.queue_draw()
        elif self._selected_edge is not None:
            idx = next((i for i,e in enumerate(self._edges) if e.get("id")==self._selected_edge), None)
            if idx is not None:
                self._push_history()
                self._edges[idx]["color"] = col
                self._area.queue_draw()
        self._update_status()

    def _on_color_set(self, btn) -> None:
        try:
            hexc = _rgba_to_hex(btn.get_rgba())
        except Exception:
            return
        self._color = hexc
        if self._selected_node is not None:
            idx = next((i for i,n in enumerate(self._nodes) if n.get("id")==self._selected_node), None)
            if idx is not None:
                self._push_history()
                self._nodes[idx]["color"] = hexc
                self._area.queue_draw()
        elif self._selected_edge is not None:
            idx = next((i for i,e in enumerate(self._edges) if e.get("id")==self._selected_edge), None)
            if idx is not None:
                self._push_history()
                self._edges[idx]["color"] = hexc
                self._area.queue_draw()
        self._update_status()

    def _on_width_changed(self, spin) -> None:
        try:
            v = int(spin.get_value())
        except Exception:
            return
        self._line_width = max(1, min(10, v))
        if self._selected_edge is not None:
            idx = next((i for i,e in enumerate(self._edges) if e.get("id")==self._selected_edge), None)
            if idx is not None:
                self._push_history()
                self._edges[idx]["width"] = self._line_width
                self._area.queue_draw()
        elif self._selected_node is not None:
            idx = next((i for i,n in enumerate(self._nodes) if n.get("id")==self._selected_node), None)
            if idx is not None and self._nodes[idx].get("type") in ("rect","ellipse","diamond","group"):
                self._push_history()
                self._nodes[idx]["strokeWidth"] = self._line_width
                self._area.queue_draw()

    def _on_font_changed(self, spin) -> None:
        try:
            v = int(spin.get_value())
        except Exception:
            return
        self._font_size = max(8, min(36, v))
        if self._selected_node is not None:
            idx = next((i for i,n in enumerate(self._nodes) if n.get("id")==self._selected_node), None)
            if idx is not None and self._nodes[idx].get("type") in ("text","rect","ellipse","diamond"):
                self._push_history()
                self._nodes[idx]["fontSize"] = self._font_size
                self._area.queue_draw()

    def _on_fill_toggled(self, btn) -> None:
        self._fill = bool(btn.get_active())
        if self._selected_node is not None:
            idx = next((i for i,n in enumerate(self._nodes) if n.get("id")==self._selected_node), None)
            if idx is not None and self._nodes[idx].get("type") in ("rect","ellipse","diamond","group"):
                self._push_history()
                self._nodes[idx]["fill"] = self._fill
                self._area.queue_draw()

    def _on_arrow_toggled(self, btn) -> None:
        self._arrow = bool(btn.get_active())
        if self._selected_edge is not None:
            idx = next((i for i,e in enumerate(self._edges) if e.get("id")==self._selected_edge), None)
            if idx is not None:
                self._push_history()
                self._edges[idx]["arrow"] = self._arrow
                self._area.queue_draw()

    # ── view transform ───────────────────────────────────────────────
    def _zoom(self, factor: float) -> None:
        ns = max(0.15, min(4.0, self._scale * factor))
        if abs(ns - self._scale) < 0.01:
            return
        w = self._area.get_width() or 900
        h = self._area.get_height() or 560
        cx = w / 2; cy = h / 2
        if getattr(self, "_last_motion", None):
            try:
                cx, cy = self._last_motion  # type: ignore
            except Exception:
                pass
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

    def _fit_view(self) -> None:
        if not self._nodes:
            self._reset_view()
            return
        xs = [float(n.get("x",0)) for n in self._nodes]
        ys = [float(n.get("y",0)) for n in self._nodes]
        ws = [float(n.get("width", DEFAULT_W)) for n in self._nodes]
        hs = [float(n.get("height", DEFAULT_H)) for n in self._nodes]
        min_x = min(x for x in xs)
        max_x = max(x+w for x,w in zip(xs, ws))
        min_y = min(y for y in ys)
        max_y = max(y+h for y,h in zip(ys, hs))
        w = max(100, max_x - min_x)
        h = max(100, max_y - min_y)
        cx = (min_x + max_x)/2
        cy = (min_y + max_y)/2
        aw = self._area.get_width() or 900
        ah = self._area.get_height() or 560
        pad = 64
        sx = (aw - pad*2)/w if w>0 else 1
        sy = (ah - pad*2)/h if h>0 else 1
        s = min(sx, sy, 1.6)
        s = max(0.15, min(3.0, s))
        self._scale = s
        self._off_x = aw/2 - cx*s
        self._off_y = ah/2 - cy*s
        self._area.queue_draw()
        self._update_status()

    def _world(self, sx: float, sy: float) -> tuple[float, float]:
        return ((sx - self._off_x)/self._scale, (sy - self._off_y)/self._scale)

    def _screen(self, wx: float, wy: float) -> tuple[float, float]:
        return (wx*self._scale + self._off_x, wy*self._scale + self._off_y)

    # ── draw ─────────────────────────────────────────────────────────
    def _on_draw(self, area: Gtk.DrawingArea, cr, width: int, height: int, _data) -> None:
        # фон через CSS, рисуем сетку
        cr.save()
        # dot grid
        step = 24 * self._scale
        if step >= 7:
            cr.set_source_rgba(0.58, 0.63, 0.72, 0.09)
            start_x = (-self._off_x) % step if step else 0
            # смещение чтобы точки двигались с панорамой
            # используем -off % step, но в экранных координатах start = (-off_x % step)
            # упростим: рисуем в экранных координатах с шагом step
            sx0 = -self._off_x % step if step else 0
            sy0 = -self._off_y % step if step else 0
            # convert to screen start
            # For infinite grid we use screen coords: dots at (sx0 + k*step)
            # But need to account off: world grid at multiples of 24
            # Compute first world grid line visible
            # world origin at 0,0 -> screen at off
            # grid world x = n*24 -> screen = n*24*scale + off
            # first n where screen >=0
            if self._scale > 0:
                n0x = math.floor((-self._off_x)/(24*self._scale))
                n1x = math.ceil((width - self._off_x)/(24*self._scale))
                n0y = math.floor((-self._off_y)/(24*self._scale))
                n1y = math.ceil((height - self._off_y)/(24*self._scale))
                cr.set_line_width(1)
                for nx in range(n0x, n1x+1):
                    for ny in range(n0y, n1y+1):
                        sx = nx*24*self._scale + self._off_x
                        sy = ny*24*self._scale + self._off_y
                        cr.arc(sx, sy, 1.1, 0, 2*math.pi)
                        cr.fill()
        cr.restore()

        # фигуры + рёбра в мировых координатах
        cr.save()
        cr.translate(self._off_x, self._off_y)
        cr.scale(self._scale, self._scale)

        nodes_map = {str(n.get("id")): n for n in self._nodes if n.get("id")}

        # сначала группы (нижний слой)
        sorted_nodes = sorted(self._nodes, key=lambda n: 0 if n.get("type")=="group" else 1)
        for n in sorted_nodes:
            _draw_node(cr, n, selected=(str(n.get("id"))==self._selected_node))

        # preview node
        if self._preview_node is not None:
            cr.set_dash([6,4],0)
            _draw_node(cr, self._preview_node, selected=False)
            cr.set_dash([],0)

        # рёбра (поверх узлов)
        for e in self._edges:
            _draw_edge(cr, e, nodes_map, selected=(str(e.get("id"))==self._selected_edge))

        # preview edge (при создании коннектора)
        if self._preview_edge is not None:
            # preview_edge хранит fromNode/fromSide и текущую мировую точку to
            fn = nodes_map.get(str(self._preview_edge.get("fromNode")))
            if fn is not None:
                x1,y1 = node_port_pos(fn, str(self._preview_edge.get("fromSide") or "right"))
                x2 = float(self._preview_edge.get("_tx", x1+80))
                y2 = float(self._preview_edge.get("_ty", y1))
                # временный edge для отрисовки
                tmp = dict(self._preview_edge)
                # создадим фейковый узел в точке курсора
                fake = {"id":"_tmp","x":x2-1,"y":y2-1,"width":2,"height":2}
                tmp_map = dict(nodes_map)
                tmp_map["_tmp"] = fake
                tmp["toNode"] = "_tmp"
                tmp["toSide"] = "center"
                cr.set_dash([6,4],0)
                _draw_edge(cr, tmp, tmp_map, selected=False)
                cr.set_dash([],0)
                # также точка курсора
                _set_source_hex(cr, self._color, 0.9)
                cr.arc(x2, y2, 6, 0, 2*math.pi)
                cr.set_line_width(1.5)
                cr.stroke()

        cr.restore()

        # overlay: координаты центра
        # (не рисуем текст чтобы не грузить)

    # ── hit helpers ──────────────────────────────────────────────────
    def _hit_node_at(self, sx: float, sy: float) -> dict | None:
        wx, wy = self._world(sx, sy)
        # topmost last in list
        for n in reversed(self._nodes):
            if _hit_node(n, wx, wy):
                return n
        return None

    def _hit_edge_at(self, sx: float, sy: float) -> dict | None:
        wx, wy = self._world(sx, sy)
        nodes_map = {str(n.get("id")): n for n in self._nodes if n.get("id")}
        for e in reversed(self._edges):
            if _hit_edge(e, nodes_map, wx, wy):
                return e
        return None

    def _hit_port_at(self, sx: float, sy: float) -> tuple[dict, str] | None:
        if self._selected_node is None:
            # проверяем любой узел под курсором
            n = self._hit_node_at(sx, sy)
            if n is None:
                return None
            wx, wy = self._world(sx, sy)
            for side in ("top","bottom","left","right"):
                if _hit_port(n, wx, wy, side, 12):
                    return (n, side)
            return None
        else:
            n = next((x for x in self._nodes if str(x.get("id"))==self._selected_node), None)
            if n is None:
                return None
            wx, wy = self._world(sx, sy)
            for side in ("top","bottom","left","right"):
                if _hit_port(n, wx, wy, side, 12):
                    return (n, side)
            return None

    def _hit_handle_at(self, sx: float, sy: float) -> str | None:
        if self._selected_node is None:
            return None
        n = next((x for x in self._nodes if str(x.get("id"))==self._selected_node), None)
        if n is None:
            return None
        wx, wy = self._world(sx, sy)
        x = float(n.get("x",0)); y=float(n.get("y",0)); w=float(n.get("width", DEFAULT_W)); h=float(n.get("height", DEFAULT_H))
        # handles: nw, ne, sw, se, n, s, e, w (размер 8 в мировых координатах с учётом scale)
        hr = 8 / self._scale
        handles = {
            "nw": (x, y),
            "ne": (x+w, y),
            "sw": (x, y+h),
            "se": (x+w, y+h),
            "n": (x+w/2, y),
            "s": (x+w/2, y+h),
            "e": (x+w, y+h/2),
            "w": (x, y+h/2),
        }
        for hid, (hx, hy) in handles.items():
            if abs(wx - hx) <= hr and abs(wy - hy) <= hr:
                return hid
        return None

    # ── controllers ──────────────────────────────────────────────────
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
        if self._tool == "hand":
            self._panning = True
            self._pan_start = (x, y)
            self._pan_off = (self._off_x, self._off_y)
            return
        wx, wy = self._world(x, y)
        # connector tool: start from port
        if self._tool == "connector":
            # find node under cursor or near port
            hit = self._hit_node_at(x, y)
            if hit is None:
                return
            side = _closest_side(hit, wx, wy)
            # if near actual port, use that side else closest
            for s in ("top","bottom","left","right"):
                if _hit_port(hit, wx, wy, s, 14):
                    side = s
                    break
            self._dragging = True
            self._drag_start = (wx, wy)
            self._port_drag = {"fromNode": str(hit.get("id")), "fromSide": side}
            self._preview_edge = {"id": "_preview", "fromNode": str(hit.get("id")), "fromSide": side, "toNode": "_tmp", "toSide": "center", "_tx": wx, "_ty": wy, "color": self._color, "width": self._line_width, "arrow": self._arrow}
            return
        if self._tool in ("rect","ellipse","diamond","text","note","group"):
            self._dragging = True
            self._drag_start = (wx, wy)
            self._drag_current = (wx, wy)
            nid = _new_id()
            w = 0; h = 0
            self._preview_node = {"id": nid, "type": self._tool, "x": wx, "y": wy, "width": w, "height": h, "color": self._color, "fill": self._fill}
            if self._tool == "text":
                self._preview_node["text"] = "Текст"
                self._preview_node["fontSize"] = self._font_size
                self._preview_node["color"] = DEFAULT_TEXT_COLOR
            if self._tool == "note":
                self._preview_node["file"] = ""
                self._preview_node["text"] = "Note"
            if self._tool == "group":
                self._preview_node["label"] = "Group"
            return
        if self._tool == "select":
            # check port first (for connector creation from selected node)
            port_hit = self._hit_port_at(x, y)
            if port_hit is not None:
                n, side = port_hit
                self._dragging = True
                self._drag_start = (wx, wy)
                self._port_drag = {"fromNode": str(n.get("id")), "fromSide": side}
                self._preview_edge = {"id": "_preview", "fromNode": str(n.get("id")), "fromSide": side, "toNode": "_tmp", "toSide": "center", "_tx": wx, "_ty": wy, "color": self._color, "width": self._line_width, "arrow": self._arrow}
                return
            # check handle for resize
            handle = self._hit_handle_at(x, y)
            if handle is not None:
                self._dragging = True
                self._drag_start = (wx, wy)
                self._handle = handle
                n = next((x for x in self._nodes if str(x.get("id"))==self._selected_node), None)
                if n is not None:
                    self._move_start_node = copy.deepcopy(n)
                return
            # hit test node/edge
            hit_node = self._hit_node_at(x, y)
            if hit_node is not None:
                nid = str(hit_node.get("id"))
                if self._selected_node != nid:
                    self._selected_node = nid
                    self._selected_edge = None
                    self._update_status()
                    self._area.queue_draw()
                self._dragging = True
                self._drag_start = (wx, wy)
                self._move_start_node = copy.deepcopy(hit_node)
                # sync toolbar with node
                try:
                    col = str(hit_node.get("color") or self._color)
                    self._color = col
                    if self._color_btn is not None:
                        self._color_btn.set_rgba(_hex_to_rgba(col))
                    if "strokeWidth" in hit_node:
                        self._line_width = int(hit_node.get("strokeWidth") or self._line_width)
                        self._width_spin.set_value(float(self._line_width))
                    if "fontSize" in hit_node:
                        self._font_size = int(hit_node.get("fontSize") or self._font_size)
                        self._font_spin.set_value(float(self._font_size))
                    if "fill" in hit_node:
                        self._fill = bool(hit_node.get("fill"))
                        self._fill_check.set_active(self._fill)
                except Exception:
                    pass
                return
            hit_edge = self._hit_edge_at(x, y)
            if hit_edge is not None:
                eid = str(hit_edge.get("id"))
                self._selected_edge = eid
                self._selected_node = None
                self._dragging = True
                self._drag_start = (wx, wy)
                self._update_status()
                self._area.queue_draw()
                return
            # click empty: deselect
            if self._selected_node is not None or self._selected_edge is not None:
                self._selected_node = None
                self._selected_edge = None
                self._area.queue_draw()
                self._update_status()

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
        cx = sx + dx; cy = sy + dy
        wx, wy = self._world(cx, cy)
        # port drag (connector)
        if self._port_drag is not None and self._preview_edge is not None:
            self._preview_edge["_tx"] = wx
            self._preview_edge["_ty"] = wy
            # highlight hover node
            hit = self._hit_node_at(cx, cy)
            if hit is not None:
                self._hover_node = str(hit.get("id"))
            else:
                self._hover_node = None
            self._area.queue_draw()
            return
        # resize handle
        if self._handle is not None and self._move_start_node is not None and self._selected_node is not None:
            orig = self._move_start_node
            idx = next((i for i,n in enumerate(self._nodes) if str(n.get("id"))==self._selected_node), None)
            if idx is None:
                return
            ox = float(orig.get("x",0)); oy=float(orig.get("y",0)); ow=float(orig.get("width", DEFAULT_W)); oh=float(orig.get("height", DEFAULT_H))
            # delta in world
            # compute new rect based on handle
            nx, ny, nw, nh = ox, oy, ow, oh
            if self._handle in ("nw","w","sw"):
                nw = ow + (ox - wx)
                nx = wx
            if self._handle in ("ne","e","se"):
                nw = wx - ox
            if self._handle in ("nw","n","ne"):
                nh = oh + (oy - wy)
                ny = wy
            if self._handle in ("sw","s","se"):
                nh = wy - oy
            # clamp
            if nw < 24: 
                if self._handle in ("nw","w","sw"):
                    nx = ox + ow - 24
                nw = 24
            if nh < 24:
                if self._handle in ("nw","n","ne"):
                    ny = oy + oh - 24
                nh = 24
            self._nodes[idx]["x"] = nx
            self._nodes[idx]["y"] = ny
            self._nodes[idx]["width"] = nw
            self._nodes[idx]["height"] = nh
            self._area.queue_draw()
            return
        # move selected node
        if self._tool == "select" and self._selected_node is not None and self._move_start_node is not None:
            idx = next((i for i,n in enumerate(self._nodes) if str(n.get("id"))==self._selected_node), None)
            if idx is None:
                return
            dxw = wx - self._drag_start[0]
            dyw = wy - self._drag_start[1]
            self._nodes[idx]["x"] = float(self._move_start_node.get("x",0)) + dxw
            self._nodes[idx]["y"] = float(self._move_start_node.get("y",0)) + dyw
            self._area.queue_draw()
            return
        # preview creation
        if self._preview_node is not None and self._drag_start is not None:
            x0, y0 = self._drag_start
            w = wx - x0; h = wy - y0
            # allow negative drag (flip)
            nx = x0 if w >= 0 else wx
            ny = y0 if h >= 0 else wy
            nw = abs(w); nh = abs(h)
            if self._tool == "text":
                nw = max(80, nw); nh = max(32, nh)
            else:
                nw = max(24, nw); nh = max(24, nh)
            self._preview_node["x"] = nx
            self._preview_node["y"] = ny
            self._preview_node["width"] = nw
            self._preview_node["height"] = nh
            self._area.queue_draw()
            return
        # select drag: ignore

    def _on_drag_end(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self._panning:
            self._panning = False
            self._pan_start = None
            self._pan_off = None
            return
        if not self._dragging:
            return
        start = gesture.get_start_point()
        sx = start[1] if start[0] else 0
        sy = start[2] if start[0] else 0
        cx = sx + dx; cy = sy + dy
        wx, wy = self._world(cx, cy)

        # port -> create edge
        if self._port_drag is not None:
            # find target node
            hit = self._hit_node_at(cx, cy)
            if hit is not None:
                from_id = str(self._port_drag.get("fromNode"))
                to_id = str(hit.get("id"))
                if from_id != to_id:
                    # find closest sides
                    from_side = str(self._port_drag.get("fromSide") or "right")
                    to_side = _closest_side(hit, wx, wy)
                    # avoid duplicate
                    exists = any(e.get("fromNode")==from_id and e.get("toNode")==to_id and e.get("fromSide")==from_side and e.get("toSide")==to_side for e in self._edges)
                    if not exists:
                        self._push_history()
                        edge = create_edge(from_id, to_id, from_side, to_side, color=self._color, width=self._line_width, arrow=self._arrow)
                        # label prompt? skip
                        self._edges.append(edge)
                        self._selected_edge = edge["id"]
                        self._selected_node = None
                        self._update_status()
                else:
                    # self-loop not allowed: ignore
                    pass
            self._port_drag = None
            self._preview_edge = None
            self._dragging = False
            self._drag_start = None
            self._hover_node = None
            self._area.queue_draw()
            return

        # handle resize end: push history once
        if self._handle is not None:
            self._push_history()
            self._handle = None
            self._move_start_node = None
            self._dragging = False
            self._drag_start = None
            self._area.queue_draw()
            return

        # move node end
        if self._tool == "select" and self._selected_node is not None and self._move_start_node is not None:
            # check if moved
            idx = next((i for i,n in enumerate(self._nodes) if str(n.get("id"))==self._selected_node), None)
            if idx is not None:
                orig = self._move_start_node
                cur = self._nodes[idx]
                if abs(float(cur.get("x",0))-float(orig.get("x",0)))>0.5 or abs(float(cur.get("y",0))-float(orig.get("y",0)))>0.5:
                    self._push_history()
            self._move_start_node = None
            self._dragging = False
            self._drag_start = None
            return

        # creation end
        if self._preview_node is not None:
            # validate size
            w = float(self._preview_node.get("width",0)); h=float(self._preview_node.get("height",0))
            if w >= 20 and h >= 20:
                self._push_history()
                node = copy.deepcopy(self._preview_node)
                # placeholder text handling
                if node.get("type") == "text" and not node.get("text"):
                    # prompt for text
                    self._prompt_text_for_node(node)
                    self._nodes.append(node)
                    self._selected_node = node["id"]
                    self._selected_edge = None
                elif node.get("type") == "note":
                    self._prompt_note_for_node(node)
                    self._nodes.append(node)
                    self._selected_node = node["id"]
                    self._selected_edge = None
                else:
                    self._nodes.append(node)
                    self._selected_node = node["id"]
                    self._selected_edge = None
                    self._update_status()
            self._preview_node = None
            self._dragging = False
            self._drag_start = None
            self._drag_current = None
            self._area.queue_draw()
            self._update_status()
            return

        self._dragging = False
        self._drag_start = None

    def _prompt_text_for_node(self, node: dict) -> None:
        dialog = Adw.Dialog(title="Текст")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
        box.set_size_request(380, -1)
        entry = Gtk.Entry(placeholder_text="Введи текст…", text=str(node.get("text") or ""))
        box.append(Gtk.Label(label="Содержимое текстового блока:", halign=Gtk.Align.START))
        box.append(entry)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Отмена")
        ok = Gtk.Button(label="ОК", css_classes=["suggested-action"])
        row.append(cancel); row.append(ok)
        box.append(row)
        dialog.set_child(box)
        cancel.connect("clicked", lambda *_: (dialog.close(), self._area.queue_draw()))
        def _do(*_):
            txt = entry.get_text().strip()
            if txt:
                node["text"] = txt
            dialog.close()
            self._area.queue_draw()
            self._update_status()
        ok.connect("clicked", _do)
        entry.connect("activate", _do)
        dialog.present(self.get_root())

    def _prompt_note_for_node(self, node: dict) -> None:
        dialog = Adw.Dialog(title="Заметка")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
        box.set_size_request(420, -1)
        box.append(Gtk.Label(label="Путь к заметке (от vault или абсолютный):", halign=Gtk.Align.START))
        entry = Gtk.Entry(placeholder_text="например: Заметки/demo.md")
        entry.set_text(str(node.get("file") or ""))
        title_entry = Gtk.Entry(placeholder_text="Заголовок карточки (опционально)")
        title_entry.set_text(str(node.get("text") or ""))
        box.append(entry)
        box.append(Gtk.Label(label="Заголовок:", halign=Gtk.Align.START, css_classes=["dim-label"]))
        box.append(title_entry)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Отмена")
        ok = Gtk.Button(label="ОК", css_classes=["suggested-action"])
        row.append(cancel); row.append(ok)
        box.append(row)
        dialog.set_child(box)
        cancel.connect("clicked", lambda *_: (dialog.close(), self._area.queue_draw()))
        def _do(*_):
            raw = entry.get_text().strip()
            ttl = title_entry.get_text().strip()
            if raw:
                # validate relative
                p = Path(raw)
                if not p.is_absolute():
                    p = self._vault_root / p
                # store relative if inside vault
                try:
                    rel = p.relative_to(self._vault_root)
                    node["file"] = str(rel)
                except Exception:
                    node["file"] = str(raw)
                if ttl:
                    node["text"] = ttl
                elif not node.get("text"):
                    node["text"] = Path(raw).stem
            elif ttl:
                node["text"] = ttl
            dialog.close()
            self._area.queue_draw()
            self._update_status()
        ok.connect("clicked", _do)
        entry.connect("activate", _do)
        title_entry.connect("activate", _do)
        dialog.present(self.get_root())

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

    def _on_click_pressed(self, gesture, n_press, x, y) -> None:
        # single press handled in drag begin
        pass

    def _on_click_released(self, gesture, n_press, x, y) -> None:
        # for text edit on double? handled separately
        if n_press == 1 and self._tool == "select" and self._selected_node is not None:
            # single click on note already handled
            pass

    def _on_double_click(self, gesture, n_press, x, y) -> None:
        if n_press != 2:
            return
        # double click on node: edit text/note or open file
        hit = self._hit_node_at(x, y)
        if hit is None:
            return
        t = hit.get("type")
        if t == "note":
            fp = str(hit.get("file") or "").strip()
            if fp:
                p = Path(fp)
                if not p.is_absolute():
                    p = self._vault_root / p
                if p.is_file():
                    if self.on_open is not None:
                        try:
                            self.on_open(str(p))
                            return
                        except Exception:
                            pass
                    try:
                        Gio.AppInfo.launch_default_for_uri(p.as_uri(), None)
                        return
                    except Exception:
                        pass
            # fallback: prompt to set file
            self._prompt_note_for_node(hit)
            self._area.queue_draw()
        elif t in ("text","rect","ellipse","diamond","group"):
            # edit text
            dialog = Adw.Dialog(title="Редактировать текст")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
            box.set_size_request(380, -1)
            entry = Gtk.Entry(text=str(hit.get("text") or hit.get("label") or ""))
            box.append(Gtk.Label(label="Текст:", halign=Gtk.Align.START))
            box.append(entry)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
            cancel = Gtk.Button(label="Отмена")
            ok = Gtk.Button(label="Сохранить", css_classes=["suggested-action"])
            row.append(cancel); row.append(ok)
            box.append(row)
            dialog.set_child(box)
            cancel.connect("clicked", lambda *_: dialog.close())
            def _do(*_):
                txt = entry.get_text().strip()
                self._push_history()
                if t == "group":
                    hit["label"] = txt
                else:
                    hit["text"] = txt
                dialog.close()
                self._area.queue_draw()
            ok.connect("clicked", _do)
            entry.connect("activate", _do)
            dialog.present(self.get_root())
        # for connector: edit label
        hit_edge = self._hit_edge_at(x, y) if hit is None else None
        if hit_edge is not None:
            dialog = Adw.Dialog(title="Метка связи")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
            box.set_size_request(360, -1)
            entry = Gtk.Entry(text=str(hit_edge.get("label") or ""))
            box.append(Gtk.Label(label="Текст на коннекторе:", halign=Gtk.Align.START))
            box.append(entry)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
            cancel = Gtk.Button(label="Отмена")
            ok = Gtk.Button(label="Сохранить", css_classes=["suggested-action"])
            row.append(cancel); row.append(ok)
            box.append(row)
            dialog.set_child(box)
            cancel.connect("clicked", lambda *_: dialog.close())
            def _do2(*_):
                self._push_history()
                hit_edge["label"] = entry.get_text().strip()
                dialog.close()
                self._area.queue_draw()
            ok.connect("clicked", _do2)
            entry.connect("activate", _do2)
            dialog.present(self.get_root())

    def _on_scroll(self, ctrl: Gtk.EventControllerScroll, dx: float, dy: float) -> bool:
        pos = getattr(self, "_last_motion", None) or (self._area.get_width()/2, self._area.get_height()/2)
        mx, my = pos
        factor = 1.12 if dy < 0 else 0.88 if dy > 0 else 1.0
        if factor == 1.0:
            return False
        ns = self._scale * factor
        ns = max(0.15, min(4.0, ns))
        if abs(ns - self._scale) < 0.001:
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
        # hover feedback cursor
        if self._tool == "hand":
            return
        if self._tool == "connector":
            hit = self._hit_node_at(x, y)
            if hit is not None:
                self._area.set_cursor(Gdk.Cursor.new_from_name("crosshair"))
            else:
                self._area.set_cursor(Gdk.Cursor.new_from_name("default"))
            return
        if self._tool == "select":
            port = self._hit_port_at(x, y)
            if port is not None:
                self._area.set_cursor(Gdk.Cursor.new_from_name("crosshair"))
                return
            handle = self._hit_handle_at(x, y)
            if handle is not None:
                cursors = {"nw":"nw-resize","ne":"ne-resize","sw":"sw-resize","se":"se-resize","n":"n-resize","s":"s-resize","e":"e-resize","w":"w-resize"}
                self._area.set_cursor(Gdk.Cursor.new_from_name(cursors.get(handle,"grab")))
                return
            hit_node = self._hit_node_at(x, y)
            if hit_node is not None:
                self._area.set_cursor(Gdk.Cursor.new_from_name("grab"))
                return
            hit_edge = self._hit_edge_at(x, y)
            if hit_edge is not None:
                self._area.set_cursor(Gdk.Cursor.new_from_name("pointer"))
                return
            self._area.set_cursor(None)

    def _on_key(self, _ctrl, keyval: int, _code: int, state: Gdk.ModifierType) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete, Gdk.KEY_BackSpace):
            self.delete_selected()
            return True
        if ctrl and keyval in (Gdk.KEY_z, Gdk.KEY_Z):
            self.undo()
            return True
        if ctrl and keyval in (Gdk.KEY_d, Gdk.KEY_D):
            self.duplicate_selected()
            return True
        if ctrl and keyval in (Gdk.KEY_s, Gdk.KEY_S):
            self.save_whiteboard()
            return True
        if keyval in (Gdk.KEY_Escape,):
            if self._selected_node is not None or self._selected_edge is not None:
                self._selected_node = None
                self._selected_edge = None
                self._area.queue_draw()
                self._update_status()
                return True
            if self._tool != "select":
                self._tool_btns["select"].set_active(True)
                return True
        if not ctrl and keyval in (Gdk.KEY_v, Gdk.KEY_V):
            self._tool_btns["select"].set_active(True)
            return True
        if not ctrl and keyval in (Gdk.KEY_h, Gdk.KEY_H):
            self._tool_btns["hand"].set_active(True)
            return True
        if keyval in (Gdk.KEY_plus, Gdk.KEY_KP_Add, Gdk.KEY_equal):
            self._zoom(1.12); return True
        if keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract, Gdk.KEY_underscore):
            self._zoom(0.88); return True
        if keyval in (Gdk.KEY_0, Gdk.KEY_KP_0):
            self._fit_view(); return True
        return False


__all__ = [
    "WhiteboardView",
    "WHITEBOARD_VERSION",
    "whiteboard_path_for_md",
    "whiteboard_file_for_path",
    "whiteboard_json_for_md",
    "load_whiteboard_json",
    "save_whiteboard_json",
    "create_node",
    "create_edge",
    "create_whiteboard_node",
    "create_whiteboard_edge",
    "create_whiteboard_data",
    "node_center",
    "node_port_pos",
    "whiteboard_from_obsidian",
    "whiteboard_to_obsidian",
]
