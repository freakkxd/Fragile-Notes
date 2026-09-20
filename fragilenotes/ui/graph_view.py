"""Граф связей — Obsidian Graph View: узлы = файлы, рёбра = [[wikilink]].

Gtk.DrawingArea + простой force-directed layout (60 итераций, затем статика).
Фичи: drag узлов, pan, zoom колесом, клик — открыть файл, фильтры all/local.
"""

from __future__ import annotations

import math
import random
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from ..vault import WIKILINK_RE as VAULT_WIKILINK_RE  # noqa: E402
from .widgets import view_header  # noqa: E402

# Регулярка из ТЗ — перестраховка: если vault regex изменится
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)")

NODE_R = 8
NODE_HOVER_R = 10
LABEL_MAX = 18
LABEL_FONT = 9


@dataclass(slots=True)
class GraphNode:
    path: Path
    title: str
    x: float = 0.0
    y: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    fixed: bool = False


class GraphView(Gtk.Box):
    """Виджет вкладки графа."""

    def __init__(self, settings: dict, on_open) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self.on_open = on_open

        self._nodes: list[GraphNode] = []
        self._edges: list[tuple[int, int]] = []
        self._path_to_idx: dict[str, int] = {}

        # view state
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
        self._fitted = False
        self._current: Path | None = None
        self._filter_mode: str = "all"  # all | local
        self._hover_cursor = False

        self._build_ui()
        self.reload()

    # ── UI ───────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.append(view_header("🕸", "Граф", "Связи между заметками · [[wikilink]]"))

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["toolbar", "graph-toolbar"])
        toolbar.set_margin_start(14)
        toolbar.set_margin_end(14)

        self._stats = Gtk.Label(label="…", css_classes=["dim-hint", "graph-stats"], halign=Gtk.Align.START, xalign=0, hexpand=True)
        toolbar.append(self._stats)

        # фильтры
        self._btn_all = Gtk.ToggleButton(label="Весь vault", active=True, css_classes=["media-filter"])
        self._btn_local = Gtk.ToggleButton(label="Связанные", css_classes=["media-filter"])
        self._btn_all.connect("toggled", self._on_filter_toggle, "all")
        self._btn_local.connect("toggled", self._on_filter_toggle, "local")
        # group behaviour manual (GTK4 ToggleButton has group attr? use manual)
        toolbar.append(self._btn_all)
        toolbar.append(self._btn_local)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Перестроить граф")
        refresh.connect("clicked", lambda *_: self.reload(force=True))
        toolbar.append(refresh)
        self.append(toolbar)

        # canvas frame
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        frame.set_margin_start(14)
        frame.set_margin_end(14)
        frame.set_margin_bottom(14)

        self._area = Gtk.DrawingArea(hexpand=True, vexpand=True, css_classes=["graph-area"])
        self._area.set_draw_func(self._on_draw, None)
        self._area.set_content_width(800)
        self._area.set_content_height(500)
        # need focus for scroll
        self._area.set_can_focus(True)
        self._area.set_focusable(True)

        # controllers
        drag = Gtk.GestureDrag.new()
        drag.set_button(1)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self._area.add_controller(drag)

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

        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True, css_classes=["graph-scroller"])
        # DrawingArea is not scrollable — scroller just gives frame; pan is manual
        # Use overlay box with drawing directly, avoid double scroll
        frame_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        frame_box.append(self._area)
        frame.append(frame_box)
        self.append(frame)

        # empty hint overlay (label shown via stats)
        self._area.set_size_request(600, 400)

    # ── Public API ───────────────────────────────────────────
    def set_current_file(self, path: Path | None) -> None:
        self._current = Path(path) if path is not None else None
        if self._filter_mode == "local":
            self._area.queue_draw()
            self._update_stats()

    def reload(self, force: bool = False) -> None:
        self._stats.set_text("Сканирую vault…")
        settings = dict(self.settings)
        if force:
            try:
                from ..services import vault as svc
                svc.invalidate_vault_cache()
            except Exception:
                pass
        threading.Thread(target=self._work, args=(settings,), daemon=True).start()

    # ── Background work ──────────────────────────────────────
    def _work(self, settings: dict) -> None:
        nodes, edges, p2i = self._build_graph(settings)
        # layout 60 iterations in world coords (800x600 center)
        self._layout(nodes, edges, iterations=60)
        GLib.idle_add(self._apply_graph, nodes, edges, p2i)

    def _build_graph(self, settings: dict) -> tuple[list[GraphNode], list[tuple[int, int]], dict[str, int]]:
        """Строит узлы = файлы, рёбра = wikilinks где цель существует."""
        from ..paths import resolve_paths

        # Use services.vault._notes_index for cached scan (includes title, raw)
        try:
            from ..services import vault as svc
            hits = svc._notes_index(settings)
        except Exception:
            hits = []
        # fallback direct rglob if empty and root exists
        if not hits:
            try:
                root = resolve_paths(settings).root
                if root.is_dir():
                    # fallback minimal scan
                    for p in root.rglob("*.md"):
                        try:
                            raw = p.read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            continue
                        from ..services.vault import note_title as _nt
                        hits.append(type("H", (), {"path": p, "title": _nt(p), "raw": raw})())
            except Exception:
                pass

        # maps for resolve_wikilink: stem lower and title lower
        stem_map: dict[str, Path] = {}
        title_map: dict[str, Path] = {}
        for h in hits:
            try:
                stem = h.path.stem.lower()
                title = (h.title or h.path.stem).lower()
            except Exception:
                continue
            # first wins (exact stem), keep first
            stem_map.setdefault(stem, h.path)
            title_map.setdefault(title, h.path)

        nodes: list[GraphNode] = []
        p2i: dict[str, int] = {}
        for h in hits:
            idx = len(nodes)
            title = getattr(h, "title", None) or h.path.stem
            nodes.append(GraphNode(path=h.path, title=title))
            p2i[str(h.path)] = idx

        # regex: use vault's WIKILINK_RE which already handles alias/#
        wlink_re = VAULT_WIKILINK_RE
        edges: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for h in hits:
            src = p2i.get(str(h.path))
            if src is None:
                continue
            raw = getattr(h, "raw", "") or ""
            # also need raw from file if not present (fallback hits built manually already have raw)
            targets = wlink_re.findall(raw) if raw else []
            # fallback to simple regex if vault regex misses due to edge
            if not targets and raw:
                targets = WIKILINK_RE.findall(raw)
            for t in targets:
                # t may be str from vault regex (already stripped of #|)
                target = (t or "").strip()
                if not target:
                    continue
                low = target.lower()
                dest = stem_map.get(low)
                if dest is None:
                    dest = title_map.get(low)
                if dest is None:
                    continue
                dst = p2i.get(str(dest))
                if dst is None or dst == src:
                    continue
                key = (src, dst)
                if key in seen:
                    continue
                seen.add(key)
                edges.append(key)
        return nodes, edges, p2i

    def _layout(self, nodes: list[GraphNode], edges: list[tuple[int, int]], iterations: int = 60) -> None:
        if not nodes:
            return
        n = len(nodes)
        # world center
        cx, cy = 400.0, 300.0
        area = 900.0
        # init random circular
        random.seed(42)
        for i, nd in enumerate(nodes):
            ang = 2 * math.pi * i / max(1, n)
            r = min(area * 0.38, 260) * (0.6 + 0.4 * random.random())
            nd.x = cx + r * math.cos(ang) + random.uniform(-18, 18)
            nd.y = cy + r * math.sin(ang) + random.uniform(-18, 18)
            nd.vx = 0.0
            nd.vy = 0.0
            nd.fixed = False

        k_rep = 2200.0
        k_attr = 0.045
        ideal = 110.0
        gravity = 0.012
        damping = 0.82

        for _ in range(iterations):
            # repulsion O(n^2) — для >500 узлов может быть тяжело, но 60 итераций ок (<0.5м узлы ~2k)
            # оптимизация: если много узлов (>400) делаем sample? пока brute
            for i in range(n):
                ni = nodes[i]
                for j in range(i + 1, n):
                    nj = nodes[j]
                    dx = ni.x - nj.x
                    dy = ni.y - nj.y
                    d2 = dx * dx + dy * dy + 0.01
                    d = math.sqrt(d2)
                    d = max(1.0, d)
                    f = k_rep / d2
                    if f > 18:
                        f = 18
                    fx = f * dx / d
                    fy = f * dy / d
                    if not ni.fixed:
                        ni.vx += fx
                        ni.vy += fy
                    if not nj.fixed:
                        nj.vx -= fx
                        nj.vy -= fy
            # attraction
            for (a, b) in edges:
                na = nodes[a]
                nb = nodes[b]
                dx = nb.x - na.x
                dy = nb.y - na.y
                d = math.sqrt(dx * dx + dy * dy) + 0.01
                f = k_attr * (d - ideal)
                fx = f * dx / d
                fy = f * dy / d
                if not na.fixed:
                    na.vx += fx
                    na.vy += fy
                if not nb.fixed:
                    nb.vx -= fx
                    nb.vy -= fy
            # gravity to center
            for nd in nodes:
                if nd.fixed:
                    continue
                dx = cx - nd.x
                dy = cy - nd.y
                nd.vx += dx * gravity
                nd.vy += dy * gravity
            # integrate
            for nd in nodes:
                if nd.fixed:
                    continue
                nd.vx *= damping
                nd.vy *= damping
                nd.x += nd.vx
                nd.y += nd.vy

    def _apply_graph(self, nodes: list[GraphNode], edges: list[tuple[int, int]], p2i: dict[str, int]) -> bool:
        self._nodes = nodes
        self._edges = edges
        self._path_to_idx = p2i
        self._fitted = False
        self._drag_node = None
        self._hover_idx = None
        self._update_stats()
        self._area.queue_draw()
        return False

    # ── Filtering ────────────────────────────────────────────
    def _visible_sets(self) -> tuple[set[int], set[tuple[int, int]]]:
        if not self._nodes:
            return set(), set()
        if self._filter_mode == "all":
            return set(range(len(self._nodes))), set(self._edges)
        # local: neighbours of current file
        if self._current is None:
            return set(range(len(self._nodes))), set(self._edges)
        cur_idx = self._path_to_idx.get(str(self._current))
        if cur_idx is None:
            return set(range(len(self._nodes))), set(self._edges)
        neigh: set[int] = {cur_idx}
        for a, b in self._edges:
            if a == cur_idx:
                neigh.add(b)
            elif b == cur_idx:
                neigh.add(a)
        # edges among visible where both ends in neigh
        vis_edges = {(a, b) for (a, b) in self._edges if a in neigh and b in neigh}
        return neigh, vis_edges

    def _update_stats(self) -> None:
        n = len(self._nodes)
        e = len(self._edges)
        if n == 0:
            self._stats.set_text("Vault пуст — нет md файлов")
            return
        vis_n, vis_e = self._visible_sets()
        mode = "весь vault" if self._filter_mode == "all" else "связанные"
        if self._filter_mode == "local" and self._current is not None:
            cur_name = self._current.stem
            self._stats.set_text(f"{len(vis_n)}/{n} узлов · {len(vis_e)}/{e} связей · {mode} · {cur_name}")
        else:
            self._stats.set_text(f"{n} узлов · {e} связей · {mode}")

    def _on_filter_toggle(self, btn: Gtk.ToggleButton, mode: str) -> None:
        if not btn.get_active():
            # ensure at least one stays active
            if mode == "all" and not self._btn_local.get_active():
                btn.set_active(True)
            elif mode == "local" and not self._btn_all.get_active():
                btn.set_active(True)
            return
        # enforce exclusive
        if mode == "all":
            if self._btn_local.get_active():
                self._btn_local.set_active(False)
            self._filter_mode = "all"
        else:
            if self._btn_all.get_active():
                self._btn_all.set_active(False)
            self._filter_mode = "local"
        self._update_stats()
        self._area.queue_draw()

    # ── Geometry helpers ─────────────────────────────────────
    def _node_at(self, sx: float, sy: float) -> int | None:
        """Ближайший узел под экранными координатами (радиус с учётом зума)."""
        if not self._nodes:
            return None
        vis, _ = self._visible_sets()
        r = (NODE_HOVER_R + 4) * max(0.7, min(1.4, self._scale))
        # also allow slightly larger hit area
        r = max(r, 14)
        best = None
        best_d2 = r * r
        for idx in vis:
            nd = self._nodes[idx]
            px = nd.x * self._scale + self._off_x
            py = nd.y * self._scale + self._off_y
            dx = sx - px
            dy = sy - py
            d2 = dx * dx + dy * dy
            if d2 <= best_d2:
                best_d2 = d2
                best = idx
        return best

    def _fit_if_needed(self, width: int, height: int) -> None:
        if self._fitted or not self._nodes:
            return
        vis, _ = self._visible_sets()
        if not vis:
            return
        xs = [self._nodes[i].x for i in vis]
        ys = [self._nodes[i].y for i in vis]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        w = max(40.0, max_x - min_x)
        h = max(40.0, max_y - min_y)
        cx = (min_x + max_x) / 2
        cy = (min_y + max_y) / 2
        # scale to fit with padding
        pad = 48
        sx = (width - pad * 2) / w if w > 0 else 1
        sy = (height - pad * 2) / h if h > 0 else 1
        s = min(sx, sy, 1.6)
        s = max(0.25, min(2.5, s))
        self._scale = s
        self._off_x = width / 2 - cx * s
        self._off_y = height / 2 - cy * s
        self._fitted = True

    # ── Draw ─────────────────────────────────────────────────
    def _on_draw(self, area: Gtk.DrawingArea, cr, width: int, height: int, _data) -> None:
        # background via CSS; draw graph on top
        self._fit_if_needed(width, height)

        # clip to area
        cr.save()
        # slight bg (css already paints, but ensure)
        # cr.set_source_rgba(0.03,0.04,0.08,1)  # skip, rely on css

        if not self._nodes:
            cr.set_source_rgba(0.55, 0.58, 0.66, 1)
            cr.select_font_face("Inter", 0, 0)
            cr.set_font_size(13)
            msg = "Нет данных для графа — создайте заметки с [[wikilink]]"
            ext = cr.text_extents(msg)
            cr.move_to((width - ext.width) / 2, height / 2)
            cr.show_text(msg)
            cr.restore()
            return

        vis_nodes, vis_edges = self._visible_sets()
        # edges
        cr.set_source_rgba(0.52, 0.60, 0.78, 0.18)
        # line width inversely? keep screen constant 1px
        cr.set_line_width(1.1)
        for (a, b) in vis_edges:
            if a not in vis_nodes or b not in vis_nodes:
                continue
            na = self._nodes[a]
            nb = self._nodes[b]
            x1 = na.x * self._scale + self._off_x
            y1 = na.y * self._scale + self._off_y
            x2 = nb.x * self._scale + self._off_x
            y2 = nb.y * self._scale + self._off_y
            cr.move_to(x1, y1)
            cr.line_to(x2, y2)
            cr.stroke()

        # nodes
        cur_idx = self._path_to_idx.get(str(self._current)) if self._current else None
        for idx in vis_nodes:
            nd = self._nodes[idx]
            sx = nd.x * self._scale + self._off_x
            sy = nd.y * self._scale + self._off_y
            # skip off-screen
            if sx < -20 or sx > width + 20 or sy < -20 or sy > height + 20:
                continue
            is_cur = idx == cur_idx
            is_hover = idx == self._hover_idx
            is_drag = idx == self._drag_node
            r = NODE_HOVER_R * self._scale if (is_hover or is_drag) else NODE_R * self._scale
            r = max(4.5, min(18, r))
            # shadow / fill
            if is_cur:
                cr.set_source_rgba(0.51, 0.66, 1.0, 0.95)
            elif is_hover:
                cr.set_source_rgba(0.75, 0.82, 1.0, 0.96)
            else:
                # degree-based tint: more connections -> brighter
                deg = sum(1 for a, b in vis_edges if a == idx or b == idx)
                alpha = 0.55 + min(0.35, deg * 0.07)
                cr.set_source_rgba(0.60, 0.66, 0.82, alpha)
            cr.arc(sx, sy, r, 0, 2 * math.pi)
            cr.fill_preserve()
            # border
            if is_cur:
                cr.set_source_rgba(0.85, 0.90, 1.0, 1.0)
                cr.set_line_width(1.6)
            else:
                cr.set_source_rgba(0.12, 0.16, 0.24, 0.9)
                cr.set_line_width(1.0)
            cr.stroke()

            # label truncated
            label = nd.title or nd.path.stem
            if len(label) > LABEL_MAX:
                label = label[: LABEL_MAX - 1] + "…"
            # text color
            cr.set_source_rgba(0.86, 0.90, 0.98, 0.92)
            cr.select_font_face("Inter", 0, 0)
            # font size screen-fixed slightly scaled
            fs = LABEL_FONT * (0.9 + 0.12 * self._scale)
            fs = max(7.5, min(11.5, fs))
            cr.set_font_size(fs)
            ext = cr.text_extents(label)
            tx = sx - ext.width / 2
            ty = sy + r + fs + 2
            # keep inside
            tx = max(2, min(width - ext.width - 2, tx))
            # bg halo for readability
            cr.save()
            cr.set_source_rgba(0.06, 0.08, 0.14, 0.72)
            pad = 2
            cr.rectangle(tx - pad, ty - fs + 1, ext.width + pad * 2, fs + 3)
            # crude rounded? just fill
            cr.fill()
            cr.restore()
            cr.set_source_rgba(0.91, 0.93, 0.98, 0.96)
            cr.move_to(tx, ty)
            cr.show_text(label)

        cr.restore()

    # ── Interaction ──────────────────────────────────────────
    def _on_drag_begin(self, gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        self._area.grab_focus()
        # x,y are start coords in widget
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
            nd.vx = 0
            nd.vy = 0
            self._area.queue_draw()
        elif self._drag_pan:
            self._off_x = self._drag_off_x + dx
            self._off_y = self._drag_off_y + dy
            self._area.queue_draw()

    def _on_drag_end(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self._drag_node is not None:
            # keep fixed a bit then release? For static after 60 iters, we keep static but allow dragging stay fixed until next reload.
            # Keep fixed flag true so node stays where dropped; alternatively release.
            # We keep fixed=True to preserve user placement.
            pass
        self._drag_node = None
        self._drag_pan = False

    def _on_click(self, gesture: Gtk.GestureClick, n: int, x: float, y: float) -> None:
        # ignore if it was a drag (gesture drag already handled)
        # check if click was actually drag distance small — gesture drag distance is 0 on click
        idx = self._node_at(x, y)
        if idx is not None:
            path = self._nodes[idx].path
            # update current for local filter feedback
            self._current = path
            if self.on_open:
                try:
                    self.on_open(str(path))
                except Exception:
                    pass
            self._update_stats()
            self._area.queue_draw()

    def _on_scroll(self, ctrl: Gtk.EventControllerScroll, dx: float, dy: float) -> bool:
        # dy >0 -> scroll down = zoom out
        # get pointer
        # Gtk.EventControllerScroll doesn't give x,y directly; try get current motion pos via last hover
        # fallback to center
        # Use display pointer
        pos = self._last_motion or (self._area.get_width() / 2, self._area.get_height() / 2)
        mx, my = pos
        factor = 1.12 if dy < 0 else 0.88 if dy > 0 else 1.0
        # also handle dx for horizontal? ignore
        if factor == 1.0:
            return False
        new_scale = self._scale * factor
        new_scale = max(0.18, min(4.0, new_scale))
        if abs(new_scale - self._scale) < 0.001:
            return True
        # zoom around cursor
        wx = (mx - self._off_x) / self._scale
        wy = (my - self._off_y) / self._scale
        self._scale = new_scale
        self._off_x = mx - wx * new_scale
        self._off_y = my - wy * new_scale
        self._area.queue_draw()
        return True

    _last_motion: tuple[float, float] | None = None

    def _on_motion(self, ctrl: Gtk.EventControllerMotion, x: float, y: float) -> None:
        self._last_motion = (x, y)
        idx = self._node_at(x, y)
        if idx != self._hover_idx:
            self._hover_idx = idx
            self._area.queue_draw()
        # cursor
        if idx is not None:
            if not self._hover_cursor:
                self._area.set_cursor(Gdk.Cursor.new_from_name("pointer"))
                self._hover_cursor = True
        else:
            if self._hover_cursor:
                self._area.set_cursor(None)
                self._hover_cursor = False

