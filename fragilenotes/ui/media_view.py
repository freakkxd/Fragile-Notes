"""Media — браузер FreakyDB с виртуализированным списком (38k+ записей без лага).

Gtk.ListView + собственная Gio.ListModel (фильтр/сортировка на Python) —
рендерятся только видимые плитки.
"""

from __future__ import annotations

import subprocess
import threading

from gi.repository import Gio, GLib, GObject, Gtk

from ..core.media import (
    MEDIA_TYPE_LABELS,
    MEDIA_TYPES,
    load_media_lite,
    media_stats,
)
from ..paths import resolve_paths
from .widgets import empty_state, status_pill

COVER_EMOJI = {
    "anime": "🎬",
    "manga": "📖",
    "game": "🎮",
    "movie": "🎞",
    "music": "🎵",
    "book": "📚",
}


class MediaItem(GObject.Object):
    """GObject-обёртка записи корпуса (GListModel требует GObject-элементы)."""

    __gtype_name__ = "MediaItem"

    def __init__(self, record) -> None:
        super().__init__()
        self.record = record


class MediaRecordModel(GObject.GObject, Gio.ListModel):
    """GListModel с фильтрацией и сортировкой в Python."""

    __gtype_name__ = "MediaRecordModel"

    def __init__(self, records: list, query: str = "", kind: str = "all") -> None:
        GObject.GObject.__init__(self)
        self.query = query
        self.kind = kind
        self._raw = list(records)
        self._items: list = []
        self._wrapped: dict[int, MediaItem] = {}
        self._compute()

    def _compute(self) -> None:
        q = self.query
        kind = self.kind
        items = self._raw if kind == "all" else [r for r in self._raw if r.type == kind]
        if q:
            items = [r for r in items if q in (r.title or "").lower()]
        items.sort(key=lambda r: (-(r.rating or 0), (r.title or "").lower()))
        # храним сырые записи; GObject-обёртки создаём лениво в do_get_item
        self._items = items
        self._wrapped = {}

    def set_filter(self, query: str, kind: str) -> None:
        if query == self.query and kind == self.kind:
            return
        old = len(self._items)
        self.query = query
        self.kind = kind
        self._compute()
        self.items_changed(0, old, len(self._items))

    def reset(self, records: list) -> None:
        old = len(self._items)
        self._raw = list(records)
        self._compute()
        self.items_changed(0, old, len(self._items))

    def do_get_item_type(self):
        return MediaItem.__gtype__

    def do_get_n_items(self) -> int:
        return len(self._items)

    def do_get_item(self, position: int):
        if 0 <= position < len(self._items):
            r = self._items[position]
            key = id(r)
            w = self._wrapped.get(key)
            if w is None:
                w = MediaItem(r)
                self._wrapped[key] = w
            return w
        return None


class MediaView(Gtk.Box):
    def __init__(self, settings: dict) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self._ocr_running = False

        self._build_header()
        self._build_toolbar()

        self._model = MediaRecordModel([], query="", kind="all")
        selection = Gtk.SingleSelection(model=self._model)
        self._view = Gtk.ListView(model=selection)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_factory_setup)
        factory.connect("bind", self._on_factory_bind)
        self._view.set_factory(factory)

        self._scroller = Gtk.ScrolledWindow(vexpand=True, css_classes=["media-scroller"])
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_child(self._view)
        self.append(self._scroller)

        # Пустые состояния — унифицированные .empty
        self._media_empty = empty_state(
            "🎬", "Медиатека пуста",
            hint="Корпус не загружен или записей пока нет",
        )
        self._media_empty.set_visible(False)
        self.append(self._media_empty)

        self._filter_empty = empty_state(
            "🔍", "Ничего не найдено",
            hint="Попробуйте другой запрос или фильтр",
            action_label="Сбросить фильтры",
            on_action=self._clear_media_filters,
        )
        self._filter_empty.set_visible(False)
        self.append(self._filter_empty)

        self.reload()

    # ── Сборка ───────────────────────────────────────────────
    def _build_header(self) -> None:
        self._header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["view-header"])
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(Gtk.Label(label="🎬", css_classes=["view-emoji"]))
        row.append(Gtk.Label(label="FreakyDB", css_classes=["view-title"], halign=Gtk.Align.START, xalign=0))
        self._header.append(row)
        self._header_sub = Gtk.Label(label="", css_classes=["dim-label", "view-sub"], halign=Gtk.Align.START, xalign=0, wrap=True)
        self._header.append(self._header_sub)
        self.append(self._header)

    def _build_toolbar(self) -> None:
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["toolbar"])
        toolbar.set_margin_start(14)
        toolbar.set_margin_end(14)
        self.search = Gtk.SearchEntry(hexpand=True, css_classes=["media-search"], placeholder_text="Поиск по названию…")
        self.search.connect("search-changed", self._on_search)
        toolbar.append(self.search)

        self.all_btn = Gtk.ToggleButton(label="Все", active=True, css_classes=["media-filter"])
        self.all_btn.connect("toggled", self._on_filter, None)
        toolbar.append(self.all_btn)
        self.type_btns: dict[str, Gtk.ToggleButton] = {}
        for t in MEDIA_TYPES:
            btn = Gtk.ToggleButton(label=MEDIA_TYPE_LABELS.get(t, t), css_classes=["media-filter"])
            btn.connect("toggled", self._on_filter, t)
            toolbar.append(btn)
            self.type_btns[t] = btn
        # OCR для изображений vault — tesseract, сохранение в .ocr.md рядом
        self.ocr_btn = Gtk.Button(
            label="🔍 OCR",
            tooltip_text="Распознать текст на изображениях vault (tesseract → .ocr.md)",
            css_classes=["media-filter"],
        )
        self.ocr_btn.connect("clicked", self._on_ocr_clicked)
        toolbar.append(self.ocr_btn)
        self.append(toolbar)

    # ── Данные ───────────────────────────────────────────────
    def reload(self) -> None:
        """Чтение корпуса в фоне: UI не блокируется на 38k+ записей."""
        self._header_sub.set_text("Загрузка корпуса…")
        settings = dict(self.settings)
        threading.Thread(target=self._load_work, args=(settings,), daemon=True).start()

    def _load_work(self, settings: dict) -> None:
        try:
            paths = resolve_paths(settings)
            records = load_media_lite(paths.root)
            stats = media_stats(paths.root)
        except Exception:  # noqa: BLE001
            records, stats = [], {}
        GLib.idle_add(self._apply_loaded, records, stats)

    def _apply_loaded(self, records: list, stats: dict) -> bool:
        self._model.reset(records)

        subtitle = f"Корпус: {len(records)} записей"
        if isinstance(stats, dict) and stats.get("counts"):
            parts = [f"{k}: {v}" for k, v in stats["counts"].items() if isinstance(v, int)]
            if parts:
                subtitle += " · " + ", ".join(parts[:8])
        self._header_sub.set_text(subtitle)
        GLib.idle_add(self._sync_media_empty)
        return False

    def _sync_media_empty(self) -> bool:
        n = self._model.get_n_items()
        has_raw = bool(self._model._raw)
        has_query = bool(self._model.query.strip()) or self._model.kind != "all"
        if n == 0:
            if not has_raw:
                self._scroller.set_visible(False)
                self._media_empty.set_visible(True)
                self._filter_empty.set_visible(False)
            else:
                # фильтр ничего не нашёл
                self._scroller.set_visible(False)
                self._media_empty.set_visible(False)
                self._filter_empty.set_visible(True)
        else:
            self._scroller.set_visible(True)
            self._media_empty.set_visible(False)
            self._filter_empty.set_visible(False)
        return False

    def _clear_media_filters(self) -> None:
        self.search.set_text("")
        self.all_btn.set_active(True)

    # ── Фильтры ──────────────────────────────────────────────
    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        self._model.set_filter(entry.get_text().strip().lower(), self._model.kind)
        self._sync_media_empty()

    def _on_filter(self, btn: Gtk.ToggleButton, kind: str | None) -> None:
        if not btn.get_active():
            return
        for k, b in self.type_btns.items():
            if k != kind:
                b.set_active(False)
        self.all_btn.set_active(kind is None)
        self._model.set_filter(self._model.query, kind or "all")
        self._sync_media_empty()

    # ── Фабрика плиток ───────────────────────────────────────
    def _on_factory_setup(self, _factory, item: Gtk.ListItem) -> None:
        tile = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, css_classes=["media-tile"])
        tile.set_margin_top(4)
        tile.set_margin_bottom(4)
        cover = Gtk.Label(label="", css_classes=["media-cover"])
        tile.append(cover)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title = Gtk.Label(label="", css_classes=["media-title"], halign=Gtk.Align.START, xalign=0, wrap=True)
        meta = Gtk.Label(label="", css_classes=["dim-label", "media-meta"], halign=Gtk.Align.START, xalign=0)
        info.append(title)
        info.append(meta)
        tile.append(info)
        tag = status_pill("", "idle")
        tile.append(tag)

        tile._cover = cover
        tile._title = title
        tile._meta = meta
        tile._tag = tag
        gesture = Gtk.GestureClick()
        gesture.connect("released", self._on_tile_click, tile)
        tile.add_controller(gesture)
        item.set_child(tile)

    def _on_tile_click(self, _gesture, _n, _x, _y, tile: Gtk.Box) -> None:
        item = getattr(tile, "_item", None)
        if item is None:
            return
        r = item.record
        paths = resolve_paths(self.settings)
        full = paths.root / r.path
        if full.exists():
            subprocess.Popen(["xdg-open", str(full)])

    def _on_factory_bind(self, _factory, item: Gtk.ListItem) -> None:
        tile: Gtk.Box = item.get_child()
        item_obj = item.get_item()
        r = item_obj.record
        tile._cover.set_text(COVER_EMOJI.get(r.type, "•"))
        for t in MEDIA_TYPES:
            tile._cover.remove_css_class(f"media-cover-{t}")
        tile._cover.add_css_class(f"media-cover-{r.type}")
        tile._title.set_text(r.title or "?")
        meta = []
        if r.year:
            meta.append(str(r.year))
        if r.rating and r.rating > 0:
            meta.append(f"★ {r.rating}")
        tile._meta.set_text(" · ".join(meta))
        tile._tag.set_text(MEDIA_TYPE_LABELS.get(r.type, r.type))
        tile._item = item_obj

    # ── OCR (tesseract → .ocr.md) ─────────────────────────────────
    def _on_ocr_clicked(self, _btn: Gtk.Button) -> None:
        """Кнопка OCR: фоновое распознавание всех изображений vault."""
        if getattr(self, "_ocr_running", False):
            return
        self._ocr_running = True
        try:
            self.ocr_btn.set_sensitive(False)
            self.ocr_btn.set_label("⏳ OCR…")
        except Exception:
            pass
        self._header_sub.set_text("OCR: сканирование изображений…")
        settings = dict(self.settings)
        threading.Thread(target=self._ocr_work, args=(settings,), daemon=True).start()

    def _ocr_work(self, settings: dict) -> None:
        try:
            from ..services.ocr import batch_ocr, is_tesseract_available

            if not is_tesseract_available():
                GLib.idle_add(self._ocr_done, False, "tesseract не установлен — apt install tesseract-ocr", 0, 0)
                return
            from ..paths import resolve_paths

            vault_root = resolve_paths(settings).root
            result = batch_ocr(vault_root, settings=settings, overwrite=False)
            if "error" in result:
                # tesseract не установлен — показываем ошибку (даже если total>0)
                GLib.idle_add(self._ocr_done, False, str(result.get("error", "OCR ошибка")), 0, 0)
                return
            ok = int(result.get("ok", 0) or 0)
            total = int(result.get("total", 0) or 0)
            skipped = int(result.get("skipped", 0) or 0)
            failed = int(result.get("failed", 0) or 0)
            if total == 0:
                msg = "OCR: изображений не найдено"
            else:
                msg = f"OCR: {ok}/{total} распознано"
                if skipped:
                    msg += f" · пропущено {skipped} (уже есть .ocr.md)"
                if failed:
                    msg += f" · ошибок {failed}"
            GLib.idle_add(self._ocr_done, True, msg, ok, total)
        except Exception as exc:  # noqa: BLE001
            GLib.idle_add(self._ocr_done, False, f"OCR ошибка: {exc}", 0, 0)

    def _ocr_done(self, success: bool, message: str, ok: int, total: int) -> bool:
        self._ocr_running = False
        try:
            self.ocr_btn.set_sensitive(True)
            self.ocr_btn.set_label("🔍 OCR")
        except Exception:
            pass
        self._header_sub.set_text(message)
        return False
