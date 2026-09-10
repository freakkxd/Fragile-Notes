"""Mind Map из заголовков — авто-граф H1→H2→H3 текущей заметки.

DrawingArea + cairo: узлы = заголовки, рёбра = иерархия, интерактив (drag, pan, zoom, клик).
Интеграция как вкладка (MindMapView).

Парсинг: fence-aware (```), frontmatter ``---`` пропускается, уровни 1..3.
Лэйаут: иерархическое дерево top-down — листья по x равномерно, родители центрированы.
Фичи: drag узлов, pan пустым местом / Shift+drag, zoom колесом/кнопками, hover, клик → on_navigate(line).

Публичный API (чистые функции):
- parse_mindmap_headings(text) -> list[tuple[int,str,int]]
- build_mindmap_tree(headings) -> tuple[list[MindNode], list[tuple[int,int]], list[int]]

Виджет:
- MindMapView(settings, on_navigate=None)  # on_navigate(line:int,title:str,path:Path|None)
- set_current_file(path: Path|None)
- load_text(text, path=None)
- reload()
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .widgets import view_header  # noqa: E402

# ── парсинг заголовков (H1..H3) ───────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*\S)\s*$")
_FENCE_RE = re.compile(r"^\s*```")
_FRONT_DELIM = re.compile(r"^---\s*$")

# цвета по уровням (AO Glass палитра)
LEVEL_COLOR = {
    1: "#8ab4ff",  # H1 blue
    2: "#bea5ff",  # H2 violet
    3: "#78d296",  # H3 green
}
LEVEL_BG_RGB = {
    1: (0.54, 0.71, 1.0),
    2: (0.74, 0.65, 1.0),
    3: (0.47, 0.82, 0.59),
}
LEVEL_R = {1: 28, 2: 22, 3: 18}
LEVEL_FONT = {1: 11.5, 2: 10.0, 3: 9.0}

NODE_PAD_X = 14
NODE_PAD_Y = 8
V_GAP = 90  # vertical between depths
H_GAP = 26  # horizontal between sibling subtrees (leaf spacing base)
LABEL_MAX = 28


def parse_mindmap_headings(text: str) -> list[tuple[int, str, int]]:
    """Вернуть список (level, title, line) для H1..H3 вне fences/frontmatter.

    - Frontmatter ``--- ... ---`` в начале файла пропускается.
    - Заголовки внутри ````` fences игнорируются.
    - Учитывает строки с ``# `` .. ``### `` (1..3).
    """
    if not text:
        return []
    lines = text.splitlines()
    n = len(lines)
    start = 0
    if n > 0 and lines[0].strip() == "---":
        for j in range(1, n):
            if lines[j].strip() == "---":
                start = j + 1
                break
    out: list[tuple[int, str, int]] = []
    in_fence = False
    for idx in range(start, n):
        line = lines[idx]
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m:
            lvl = len(m.group(1))
            title = m.group(2).strip()
            # убрать trailing #'s как в markdown? не требуется
            # обрезать длинные
            if title:
                out.append((lvl, title, idx))
    return out


# алиасы для совместимости
parse_headings = parse_mindmap_headings
extract_headings = parse_mindmap_headings
get_mindmap_headings = parse_mindmap_headings


@dataclass(slots=True)
class MindNode:
    """Узел mind map."""
    title: str
    level: int  # 1..3
    line: int
    idx: int = -1
    parent: int | None = None
    children: list[int] = field(default_factory=list)  # type: ignore
    depth: int = 0
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    fixed: bool = False


def build_mindmap_tree(headings: list[tuple[int, str, int]]) -> tuple[list[MindNode], list[tuple[int, int]], list[int]]:
    """Построить дерево из плоского списка заголовков.

    Правила иерархии:
      H1 — корень; H2 — ребёнок ближайшего предыдущего H1; H3 — ребёнок ближайшего H2
      (если H2 отсутствует — H3 становится ребёнком H1, если и H1 нет — корень).
      Глубина вычисляется по фактическому parent, а не по сырому level.

    Returns: (nodes, edges, roots)  edges: (parent, child)
    """
    nodes: list[MindNode] = []
    edges: list[tuple[int, int]] = []
    roots: list[int] = []
    # стек последних узлов по уровням для быстрого родителя
    # храним idx последнего узла каждого level в пути
    stack: list[MindNode] = []
    for lvl, title, line in headings:
        if not 1 <= lvl <= 3:
            continue
        # создаём узел
        node = MindNode(title=title, level=lvl, line=line, idx=len(nodes))
        # найти родителя: ближайший в стеке с level < lvl
        parent: MindNode | None = None
        for cand in reversed(stack):
            if cand.level < lvl:
                parent = cand
                break
        if parent is not None:
            node.parent = parent.idx
            node.depth = parent.depth + 1
            parent.children.append(node.idx)
            edges.append((parent.idx, node.idx))
        else:
            node.depth = 0
            roots.append(node.idx)
        # обновить стек: убрать узлы с level >= lvl
        while stack and stack[-1].level >= lvl:
            stack.pop()
        stack.append(node)
        nodes.append(node)
    return nodes, edges, roots


# алиас
build_tree = build_mindmap_tree


def _estimate_node_size(title: str, level: int) -> tuple[float, float]:
    """Оценка размера ноды по тексту (без Pango measure — приближённо)."""
    capped = title[:LABEL_MAX]
    # ширина символа ~0.58 * font
    fs = LEVEL_FONT.get(level, 9)
    # разный scale для H1/H2/H3
    char_w = fs * 0.58
    w = max(56, min(220, len(capped) * char_w + NODE_PAD_X * 2))
    h = max(28, fs * 1.6 + NODE_PAD_Y * 2)
    # H1 чуть шире
    if level == 1:
        w = max(w, 90)
        h = max(h, 34)
    return w, h


def layout_mindmap(nodes: list[MindNode], roots: list[int]) -> None:
    """Расставить nodes x,y иерархическим лэйаутом (top-down, parents centered).

    Мутирует nodes inplace.
    Листья распределяются равномерно по x с шагом, внутренние — среднее детей.
    """
    if not nodes:
        return
    # precalc sizes
    for n in nodes:
        w, h = _estimate_node_size(n.title, n.level)
        n.w = w
        n.h = h
        n.x = 0
        n.y = 0

    # map idx->node
    by_idx = {n.idx: n for n in nodes}

    # helper: count leaves under node
    leaf_cache: dict[int, int] = {}

    def count_leaves(idx: int) -> int:
        if idx in leaf_cache:
            return leaf_cache[idx]
        nd = by_idx[idx]
        if not nd.children:
            leaf_cache[idx] = 1
            return 1
        s = sum(count_leaves(c) for c in nd.children)
        leaf_cache[idx] = max(1, s)
        return leaf_cache[idx]

    for r in roots:
        count_leaves(r)

    # assignment pass: leaves occupy slots, parents centered
    # global leaf index across all roots
    leaf_idx = 0
    # we need to compute x via DFS left-to-right
    # To support multiple roots, spread roots horizontally: treat virtual root
    # For simplicity, lay roots sequentially as if they were siblings under virtual root.
    # Compute root x positions similarly: each root occupies its leaves width.

    # compute unit spacing: dynamic from max width? Use base spacing ~ 160..220
    # estimate spacing = max node w + H_GAP
    # We'll use adaptive spacing: 170

    LEAF_STEP = 170  # distance between leaf centers
    # Also add extra gap between root subtrees
    ROOT_GAP = 48

    def assign(idx: int) -> float:
        nonlocal leaf_idx
        nd = by_idx[idx]
        nd.y = 40 + nd.depth * V_GAP
        if not nd.children:
            x = leaf_idx * LEAF_STEP
            leaf_idx += 1
            nd.x = x
            return x
        # internal: assign children first
        child_xs = [assign(c) for c in nd.children]
        # center between extremes (or average)
        nd.x = sum(child_xs) / len(child_xs)
        return nd.x

    # assign each root sequentially; but leaf_idx runs across roots
    # Need to offset roots so they Center collectively around 0? We'll center after.
    # First assign with shared leaf_idx
    for r in roots:
        assign(r)
    # add ROOT_GAP between root subtrees: shift subsequent roots right
    # Our simple leaf approach already gaps; add extra gap for roots >1
    if len(roots) > 1:
        # shift roots to avoid overlap: compute cumulative leaf widths
        # Recompute with offset: for each root after first, add gap
        # Simpler: re-assign leaf_idx without global, per root then offset
        # But leaf method already spaces; just ensure minimal distance between root groups:
        # leaf_idx approach already sequential, so gap is LEAF_STEP, enough.
        # Add explicit root gap by shifting groups
        cur_x = 0
        for r in roots:
            nd = by_idx[r]
            leaves_under = count_leaves(r)
            # group's span: (leaves_under-1)*LEAF_STEP
            group_w = (leaves_under - 1) * LEAF_STEP
            # desired left = cur_x
            current_left = nd.x - group_w / 2 if nd.children else nd.x
            shift = cur_x - current_left
            # shift whole subtree r by shift
            stack2 = [r]
            while stack2:
                cur = stack2.pop()
                by_idx[cur].x += shift
                stack2.extend(by_idx[cur].children)
            cur_x += group_w + ROOT_GAP + LEAF_STEP

    # center whole forest around x=0 for easier fit
    if nodes:
        xs = [n.x for n in nodes]
        min_x, max_x = min(xs), max(xs)
        mid = (min_x + max_x) / 2
        for n in nodes:
            n.x -= mid


# ── виджет вкладки ───────────────────────────────────────────────────

class MindMapView(Gtk.Box):
    """Вкладка Mind Map: граф заголовков H1→H2→H3 с интерактивом."""

    def __init__(self, settings: dict, on_navigate=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self.on_navigate = on_navigate  # callable(line, title, path) или (line,)
        self._current_path: Path | None = None
        self._current_text: str = ""

        self._nodes: list[MindNode] = []
        self._edges: list[tuple[int, int]] = []
        self._roots: list[int] = []

        # view transform
        self._scale: float = 1.0
        self._off_x: float = 0.0
        self._off_y: float = 0.0
        self._drag_node: int | None = None
        self._drag_pan: bool = False
        self._drag_start_x: float = 0.0
        self._drag_start_y: float = 0.0
        self._drag_off_x: float = 0.0
        self._drag_off_y: float = 0.0
        self._hover_idx: int | None = None
        self._hover_cursor = False
        self._fitted = False
        self._last_motion: tuple[float, float] | None = None
        # ui refs
        self._stats: Gtk.Label | None = None  # type: ignore
        self._file_label: Gtk.Label | None = None  # type: ignore
        self._area: Gtk.DrawingArea | None = None  # type: ignore

        self._build_ui()
        self._update_stats()

    # ── публичный API ────────────────────────────────────────────
    def set_current_file(self, path: Path | str | None) -> None:
        """Установить текущую заметку и перестроить mind map."""
        if path is None:
            self._current_path = None
            self._current_text = ""
            self._nodes = []
            self._edges = []
            self._roots = []
            self._fitted = False
            self._update_stats()
            if self._area is not None:
                self._area.queue_draw()
            return
        p = Path(path)
        self._current_path = p
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                text = ""
        self.load_text(text, path=p)

    def load_text(self, text: str, path: Path | str | None = None) -> None:
        """Загрузить raw markdown и построить граф."""
        if path is not None:
            self._current_path = Path(path)
        self._current_text = text or ""
        headings = parse_mindmap_headings(self._current_text)
        nodes, edges, roots = build_mindmap_tree(headings)
        # layout
        layout_mindmap(nodes, roots)
        self._nodes = nodes
        self._edges = edges
        self._roots = roots
        self._fitted = False
        self._hover_idx = None
        self._drag_node = None
        self._update_stats()
        if self._area is not None:
            self._area.queue_draw()

    def reload(self, force: bool = False) -> None:
        """Перечитать файл с диска (force игнорируется, совместимость)."""
        if self._current_path is not None and self._current_path.is_file():
            self.set_current_file(self._current_path)
        elif self._current_text:
            self.load_text(self._current_text, path=self._current_path)

    # ── UI ────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.append(view_header("🗺", "Mind Map", "Авто-граф H1 → H2 → H3 текущей заметки · pan, zoom, drag"))

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["toolbar", "mindmap-toolbar"])
        toolbar.set_margin_start(14)
        toolbar.set_margin_end(14)

        self._stats = Gtk.Label(label="…", css_classes=["dim-hint", "mindmap-stats"], halign=Gtk.Align.START, xalign=0, hexpand=True)
        toolbar.append(self._stats)

        self._file_label = Gtk.Label(label="· нет файла", css_classes=["dim-hint", "mindmap-file"], halign=Gtk.Align.END, xalign=1, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
        toolbar.append(self._file_label)

        # zoom controls
        zoom_out = Gtk.Button(label="−", tooltip_text="Уменьшить (колесо вниз)")
        zoom_out.connect("clicked", lambda *_: self._zoom_step(0.88))
        toolbar.append(zoom_out)
        self._zoom_label = Gtk.Label(label="100%", css_classes=["dim-hint"])
        toolbar.append(self._zoom_label)
        zoom_in = Gtk.Button(label="+", tooltip_text="Увеличить (колесо вверх)")
        zoom_in.connect("clicked", lambda *_: self._zoom_step(1.12))
        toolbar.append(zoom_in)
        fit_btn = Gtk.Button(label="⛶", tooltip_text="Вписать")
        fit_btn.connect("clicked", lambda *_: self._fit_request())
        toolbar.append(fit_btn)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Перестроить")
        refresh.connect("clicked", lambda *_: self.reload(force=True))
        toolbar.append(refresh)

        # open file button (выбрать заметку вручную)
        open_btn = Gtk.Button(label="Открыть", tooltip_text="Выбрать .md вручную")
        open_btn.connect("clicked", self._on_open_file)
        toolbar.append(open_btn)

        self.append(toolbar)

        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        frame.set_margin_start(14)
        frame.set_margin_end(14)
        frame.set_margin_bottom(14)

        self._area = Gtk.DrawingArea(hexpand=True, vexpand=True, css_classes=["mindmap-area", "graph-area"])
        self._area.set_draw_func(self._on_draw, None)
        self._area.set_content_width(900)
        self._area.set_content_height(520)
        self._area.set_can_focus(True)
        self._area.set_focusable(True)
        self._area.set_size_request(600, 400)

        # gesture drag (ЛКМ — drag node или pan)
        drag = Gtk.GestureDrag.new()
        drag.set_button(1)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self._area.add_controller(drag)

        # pan middle-button
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

        # keyboard: +/- for zoom, 0 fit
        key = Gtk.EventControllerKey.new()
        key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key.connect("key-pressed", self._on_key)
        self._area.add_controller(key)

        frame_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        frame_box.append(self._area)
        frame.append(frame_box)
        self.append(frame)

        hint = Gtk.Label(label="ЛКМ — перетаскивание узла · перетаскивание фона — pan · колесо — zoom · клик на узел — переход к заголовку", css_classes=["dim-hint", "mindmap-hint"], halign=Gtk.Align.START, xalign=0, wrap=True)
        hint.set_margin_start(14)
        hint.set_margin_end(14)
        self.append(hint)

    # ── stats ─────────────────────────────────────────────────────
    def _update_stats(self) -> None:
        if not hasattr(self, "_stats") or self._stats is None:
            return
        n = len(self._nodes)
        if self._current_path is not None:
            try:
                rel = self._current_path.relative_to(Path(str(self.settings.get("vault_root") or "")))
                fname = str(rel)
            except Exception:
                fname = self._current_path.name
            if hasattr(self, "_file_label") and self._file_label is not None:
                self._file_label.set_text(fname)
                self._file_label.set_tooltip_text(str(self._current_path))
        else:
            if hasattr(self, "_file_label") and self._file_label is not None:
                self._file_label.set_text("· нет файла · выбери заметку")
        if n == 0:
            if not self._current_text and self._current_path is None:
                self._stats.set_text("Нет данных — открой заметку с # заголовками")
            elif not parse_mindmap_headings(self._current_text):
                self._stats.set_text("В заметке нет H1/H2/H3 заголовков")
            else:
                self._stats.set_text("0 узлов")
        else:
            h1 = sum(1 for nd in self._nodes if nd.level == 1)
            h2 = sum(1 for nd in self._nodes if nd.level == 2)
            h3 = sum(1 for nd in self._nodes if nd.level == 3)
            edges = len(self._edges)
            self._stats.set_text(f"{n} узлов ({h1}·{h2}·{h3}) · {edges} связей · H1→H2→H3")
        if hasattr(self, "_zoom_label") and self._zoom_label is not None:
            self._zoom_label.set_text(f"{int(self._scale*100)}%")

    def _zoom_step(self, factor: float) -> None:
        w = self._area.get_width() if self._area else 900
        h = self._area.get_height() if self._area else 520
        cx, cy = w / 2, h / 2
        if self._last_motion:
            cx, cy = self._last_motion
        ns = max(0.18, min(4.0, self._scale * factor))
        if abs(ns - self._scale) < 0.01:
            return
        wx = (cx - self._off_x) / self._scale
        wy = (cy - self._off_y) / self._scale
        self._scale = ns
        self._off_x = cx - wx * ns
        self._off_y = cy - wy * ns
        self._update_stats()
        if self._area:
            self._area.queue_draw()

    def _fit_request(self) -> None:
        self._fitted = False
        if self._area:
            self._area.queue_draw()

    # ── geometry helpers ──────────────────────────────────────────
    def _node_at(self, sx: float, sy: float) -> int | None:
        if not self._nodes:
            return None
        # hit-test in screen coords: nodes are axis-aligned rects
        best = None
        for nd in self._nodes:
            cx = nd.x * self._scale + self._off_x
            cy = nd.y * self._scale + self._off_y
            hw = (nd.w * self._scale) / 2
            hh = (nd.h * self._scale) / 2
            if (cx - hw - 4 <= sx <= cx + hw + 4) and (cy - hh - 4 <= sy <= cy + hh + 4):
                # closest by center distance
                if best is None:
                    best = nd.idx
                else:
                    # prefer smaller level (H1) if overlap? keep first found closest
                    bx = self._nodes[best].x * self._scale + self._off_x
                    by = self._nodes[best].y * self._scale + self._off_y
                    d_best = (sx - bx) ** 2 + (sy - by) ** 2
                    d_cur = (sx - cx) ** 2 + (sy - cy) ** 2
                    if d_cur < d_best:
                        best = nd.idx
        return best

    def _fit_if_needed(self, width: int, height: int) -> None:
        if self._fitted or not self._nodes:
            return
        xs = [n.x for n in self._nodes]
        ys = [n.y for n in self._nodes]
        ws = [n.w for n in self._nodes]
        hs = [n.h for n in self._nodes]
        # bounding box in world coords (centered nodes)
        min_x = min(x - w / 2 for x, w in zip(xs, ws))
        max_x = max(x + w / 2 for x, w in zip(xs, ws))
        min_y = min(y - h / 2 for y, h in zip(ys, hs))
        max_y = max(y + h / 2 for y, h in zip(ys, hs))
        w = max(60.0, max_x - min_x)
        h = max(60.0, max_y - min_y)
        cx = (min_x + max_x) / 2
        cy = (min_y + max_y) / 2
        pad = 36
        sx = (width - pad * 2) / w if w > 0 else 1
        sy = (height - pad * 2) / h if h > 0 else 1
        s = min(sx, sy, 1.4)
        s = max(0.22, min(2.2, s))
        self._scale = s
        self._off_x = width / 2 - cx * s
        self._off_y = height / 2 - cy * s
        # also offset y so top visible with padding
        # if height large, keep centered; fine
        self._fitted = True
        self._update_stats()

    # ── draw ──────────────────────────────────────────────────────
    def _on_draw(self, area: Gtk.DrawingArea, cr, width: int, height: int, _data) -> None:
        self._fit_if_needed(width, height)
        cr.save()

        if not self._nodes:
            cr.set_source_rgba(0.55, 0.58, 0.66, 1)
            cr.select_font_face("Inter", 0, 0)
            cr.set_font_size(13)
            msg = "Нет заголовков H1/H2/H3 — добавь # Заголовок в заметке"
            ext = cr.text_extents(msg)
            cr.move_to((width - ext.width) / 2, height / 2)
            cr.show_text(msg)
            # secondary hint
            cr.set_source_rgba(0.52, 0.56, 0.64, 0.85)
            cr.set_font_size(10)
            msg2 = "Mind Map строит дерево из текущей заметки · открой файл в «Заметки» и вернись сюда"
            ext2 = cr.text_extents(msg2)
            cr.move_to((width - ext2.width) / 2, height / 2 + 22)
            cr.show_text(msg2)
            cr.restore()
            return

        # clip
        # edges: lines from parent bottom center to child top center (with bezier maybe)
        # simple straight + subtle curve
        for a, b in self._edges:
            if a < 0 or b < 0 or a >= len(self._nodes) or b >= len(self._nodes):
                continue
            pa = self._nodes[a]
            pb = self._nodes[b]
            x1 = pa.x * self._scale + self._off_x
            y1 = (pa.y + pa.h / 2) * self._scale + self._off_y  # bottom
            x2 = pb.x * self._scale + self._off_x
            y2 = (pb.y - pb.h / 2) * self._scale + self._off_y  # top
            # color by parent level
            r, g, b_ = LEVEL_BG_RGB.get(pa.level, (0.6, 0.66, 0.82))
            cr.set_source_rgba(r, g, b_, 0.42)
            cr.set_line_width(1.4 * max(0.7, min(1.4, self._scale)))
            cr.set_line_cap(1)
            # bezier curve: control points 0.5*V_GAP
            mid = (y1 + y2) / 2
            cr.move_to(x1, y1)
            cr.curve_to(x1, mid, x2, mid, x2, y2)
            cr.stroke()
            # arrow dot at child top
            cr.set_source_rgba(r, g, b_, 0.85)
            cr.arc(x2, y2, 2.2 * max(0.7, min(1.3, self._scale)), 0, 2 * math.pi)
            cr.fill()

        # nodes
        for nd in self._nodes:
            cx = nd.x * self._scale + self._off_x
            cy = nd.y * self._scale + self._off_y
            w = nd.w * self._scale
            h = nd.h * self._scale
            # cull off-screen
            if cx + w / 2 < -30 or cx - w / 2 > width + 30 or cy + h / 2 < -30 or cy - h / 2 > height + 30:
                continue
            is_hover = nd.idx == self._hover_idx
            is_drag = nd.idx == self._drag_node
            r, g, b_ = LEVEL_BG_RGB.get(nd.level, (0.6, 0.66, 0.82))
            # bg
            alpha = 0.20 if nd.level == 3 else (0.24 if nd.level == 2 else 0.28)
            if is_hover or is_drag:
                alpha = min(0.45, alpha + 0.14)
            # rounded rect
            radius = 9 * self._scale
            radius = max(4, min(12, radius))
            x0 = cx - w / 2
            y0 = cy - h / 2
            # shadow
            cr.set_source_rgba(0.02, 0.04, 0.10, 0.22)
            self._round_rect(cr, x0 + 1, y0 + 1.5, w, h, radius)
            cr.fill()
            # fill
            cr.set_source_rgba(r, g, b_, alpha)
            self._round_rect(cr, x0, y0, w, h, radius)
            cr.fill_preserve()
            # border
            if is_hover or is_drag:
                cr.set_source_rgba(r, g, b_, 0.95)
                cr.set_line_width(1.6)
            else:
                cr.set_source_rgba(r, g, b_, 0.55)
                cr.set_line_width(1.0)
            cr.stroke()
            # level indicator left stripe
            cr.set_source_rgba(r, g, b_, 0.85)
            self._round_rect(cr, x0, y0, 4 * self._scale, h, radius, left_only=True)
            cr.fill()
            # text
            label = nd.title
            if len(label) > LABEL_MAX:
                label = label[: LABEL_MAX - 1] + "…"
            # prefix by level
            prefix = "■ " if nd.level == 1 else ("▣ " if nd.level == 2 else "▫ ")
            display = prefix + label
            fs = LEVEL_FONT.get(nd.level, 9) * (0.92 + 0.10 * self._scale)
            fs = max(7.5, min(13, fs))
            cr.select_font_face("Inter", 0, 0)
            cr.set_font_size(fs)
            # choose weight
            # measure to center
            ext = cr.text_extents(display)
            # if text wider than node inner width, truncate already; center
            tx = cx - ext.width / 2
            ty = cy + ext.height / 2 - 1  # vertical center adjust
            # keep inside node padding visually already centered, just clamp to screen?
            # Use node inner area: ensure not overflow node clip (we already truncated)
            cr.set_source_rgba(0.92, 0.94, 0.98, 0.97 if nd.level == 1 else (0.90 if nd.level == 2 else 0.86))
            cr.move_to(tx, ty)
            cr.show_text(display)
            # line badge top-right small
            if nd.level == 1 and is_hover:
                cr.set_source_rgba(0.7, 0.76, 0.88, 0.9)
                cr.set_font_size(7)
                sub = f"стр {nd.line+1}"
                se = cr.text_extents(sub)
                cr.move_to(cx + w / 2 - se.width - 6 * self._scale, cy - h / 2 + 10 * self._scale)
                cr.show_text(sub)

        cr.restore()

    def _round_rect(self, cr, x: float, y: float, w: float, h: float, r: float, left_only: bool = False) -> None:
        """Rounded rect path."""
        if w <= 0 or h <= 0:
            return
        r = min(r, w / 2, h / 2)
        if left_only:
            # only left side rounded
            cr.new_path()
            cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
            cr.arc(x + w - r, y + r, r, 3 * math.pi / 2, 0) if w > r * 2 else cr.line_to(x + w, y)
            cr.line_to(x + w, y + h)
            cr.line_to(x + r, y + h)
            cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
            cr.close_path()
            return
        cr.new_path()
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.arc(x + w - r, y + r, r, 3 * math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.close_path()

    # ── interaction ───────────────────────────────────────────────
    def _on_drag_begin(self, gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        if self._area:
            self._area.grab_focus()
        idx = self._node_at(x, y)
        if idx is not None:
            self._drag_node = idx
            self._drag_pan = False
            self._nodes[idx].fixed = True
        else:
            self._drag_node = None
            self._drag_pan = True
            self._drag_start_x = x
            self._drag_start_y = y
            self._drag_off_x = self._off_x
            self._drag_off_y = self._off_y

    def _on_drag_update(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self._drag_node is not None:
            start = gesture.get_start_point()
            if not start[0]:
                return
            sx, sy = start[1], start[2]
            cx = sx + dx
            cy = sy + dy
            wx = (cx - self._off_x) / self._scale
            wy = (cy - self._off_y) / self._scale
            nd = self._nodes[self._drag_node]
            nd.x = wx
            nd.y = wy
            if self._area:
                self._area.queue_draw()
        elif self._drag_pan:
            self._off_x = self._drag_off_x + dx
            self._off_y = self._drag_off_y + dy
            if self._area:
                self._area.queue_draw()

    def _on_drag_end(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        self._drag_node = None
        self._drag_pan = False

    def _on_pan_begin(self, gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        self._drag_pan = True
        self._drag_start_x = x
        self._drag_start_y = y
        self._drag_off_x = self._off_x
        self._drag_off_y = self._off_y

    def _on_pan_update(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self._drag_pan:
            self._off_x = self._drag_off_x + dx
            self._off_y = self._drag_off_y + dy
            if self._area:
                self._area.queue_draw()

    def _on_pan_end(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        self._drag_pan = False

    def _on_click(self, gesture: Gtk.GestureClick, n: int, x: float, y: float) -> None:
        idx = self._node_at(x, y)
        if idx is not None:
            nd = self._nodes[idx]
            # notify
            if self.on_navigate is not None:
                try:
                    # try 3-arg, fallback 1-arg
                    try:
                        self.on_navigate(nd.line, nd.title, self._current_path)
                    except TypeError:
                        self.on_navigate(nd.line)
                except Exception:
                    pass
            else:
                # fallback: toast
                self._notify(f"§ {nd.title} · строка {nd.line+1}")
            # highlight hover
            self._hover_idx = idx
            if self._area:
                self._area.queue_draw()

    def _on_scroll(self, ctrl: Gtk.EventControllerScroll, dx: float, dy: float) -> bool:
        pos = self._last_motion or (self._area.get_width() / 2 if self._area else 450, self._area.get_height() / 2 if self._area else 260)
        mx, my = pos
        factor = 1.12 if dy < 0 else 0.88 if dy > 0 else 1.0
        if factor == 1.0:
            return False
        ns = self._scale * factor
        ns = max(0.18, min(4.0, ns))
        if abs(ns - self._scale) < 0.001:
            return True
        wx = (mx - self._off_x) / self._scale
        wy = (my - self._off_y) / self._scale
        self._scale = ns
        self._off_x = mx - wx * ns
        self._off_y = my - wy * ns
        self._update_stats()
        if self._area:
            self._area.queue_draw()
        return True

    def _on_motion(self, ctrl: Gtk.EventControllerMotion, x: float, y: float) -> None:
        self._last_motion = (x, y)
        idx = self._node_at(x, y)
        if idx != self._hover_idx:
            self._hover_idx = idx
            if self._area:
                self._area.queue_draw()
        if idx is not None:
            if not self._hover_cursor and self._area:
                self._area.set_cursor(Gdk.Cursor.new_from_name("pointer"))
                self._hover_cursor = True
        else:
            if self._hover_cursor and self._area:
                self._area.set_cursor(None)
                self._hover_cursor = False

    def _on_key(self, _ctrl, keyval: int, _code: int, state: Gdk.ModifierType) -> bool:
        if keyval in (Gdk.KEY_plus, Gdk.KEY_KP_Add, Gdk.KEY_equal):
            self._zoom_step(1.12)
            return True
        if keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract, Gdk.KEY_underscore):
            self._zoom_step(0.88)
            return True
        if keyval in (Gdk.KEY_0, Gdk.KEY_KP_0):
            self._fit_request()
            return True
        if keyval == Gdk.KEY_Escape and self._drag_node is not None:
            self._drag_node = None
            return True
        return False

    # ── file picker ──────────────────────────────────────────────
    def _on_open_file(self, *_args) -> None:
        vault_root = Path(str(self.settings.get("vault_root") or Path.home()))
        try:
            dlg = Gtk.FileDialog()
            dlg.set_title("Открыть заметку для Mind Map")
            filt = Gio.ListStore.new(Gtk.FileFilter)
            f = Gtk.FileFilter()
            f.set_name("Markdown (*.md)")
            f.add_pattern("*.md")
            filt.append(f)
            dlg.set_filters(filt)
            try:
                if vault_root.is_dir():
                    dlg.set_initial_folder(Gio.File.new_for_path(str(vault_root)))
            except Exception:
                pass
            dlg.open(self.get_root(), None, self._on_open_done)
            return
        except Exception:
            pass
        self._fallback_open()

    def _on_open_done(self, dlg: Gtk.FileDialog, res) -> None:
        try:
            f = dlg.open_finish(res)
            if f is None:
                return
            path = Path(f.get_path() or "")
            if path:
                self.set_current_file(path)
        except Exception as exc:  # noqa: BLE001
            self._notify(f"ошибка открытия: {exc}")

    def _fallback_open(self) -> None:
        dialog = Adw.Dialog(title="Открыть файл")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
        box.set_size_request(420, -1)
        lbl = Gtk.Label(label="Путь к заметке (от vault или абсолютный):", halign=Gtk.Align.START)
        vault_root = Path(str(self.settings.get("vault_root") or Path.home()))
        entry = Gtk.Entry(placeholder_text="например: Заметки/demo.md")
        if self._current_path is not None:
            try:
                entry.set_text(str(self._current_path.relative_to(vault_root)))
            except Exception:
                entry.set_text(str(self._current_path))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Отмена")
        ok = Gtk.Button(label="Открыть", css_classes=["suggested-action"])
        row.append(cancel)
        row.append(ok)
        box.append(lbl)
        box.append(entry)
        box.append(row)
        dialog.set_child(box)
        cancel.connect("clicked", lambda *_: dialog.close())

        def _do(*_):
            raw = entry.get_text().strip()
            if not raw:
                return
            p = Path(raw)
            if not p.is_absolute():
                p = vault_root / p
            dialog.close()
            self.set_current_file(p)

        ok.connect("clicked", _do)
        entry.connect("activate", _do)
        dialog.present(self.get_root())

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
        if self._stats is not None:
            prev = self._stats.get_text()
            self._stats.set_text(msg)
            GLib.timeout_add(2500, lambda: (self._stats.set_text(prev), False)[1] if self._stats else False)


__all__ = ["MindMapView", "MindNode", "parse_mindmap_headings", "build_mindmap_tree", "layout_mindmap"]
