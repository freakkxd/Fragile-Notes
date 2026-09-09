"""Zotero панель: поиск по библиотеке (API / локальная БД) и импорт как заметок.

Виртуализация не требуется — библиотека обычно <10k, но UI не блокируется:
загрузка идёт в фоне (threading + GLib.idle_add), как в media_view / templates_view.
"""

from __future__ import annotations

import threading
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..services.zotero import ZoteroItem, ZoteroService
from .widgets import empty_state, status_pill, view_header

try:
    from ..services.zotero import DEFAULT_IMPORT_FOLDER
except Exception:
    DEFAULT_IMPORT_FOLDER = "04 FreakyWiki/Zotero"  # type: ignore

_CHUNK = 200


def _creator_label(item: ZoteroItem) -> str:
    c = item.creators_short()
    if c:
        return c + (f" · {item.year}" if item.year else "")
    return item.year or ""


class ZoteroView(Gtk.Box):
    """Панель Zotero: поиск + импорт.

    Использует :class:`fragilenotes.services.zotero.ZoteroService` и
    не требует внешних зависимостей. Поддерживает:

    * поиск по названию/авторам/аннотации/тегам (дебаунс 200 мс);
    * переключение источника ``auto`` / ``db`` / ``api``;
    * импорт выбранных или всех видимых записей в vault.
    """

    def __init__(self, settings: dict, on_open_note=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self.on_open_note = on_open_note
        self._service = ZoteroService(settings)
        self._items: list[ZoteroItem] = []
        self._filtered: list[ZoteroItem] = []
        self._selected: set[str] = set()
        self._alive = True
        self._loading = False
        self._token = 0
        self._search_timer: int | None = None
        self.connect("destroy", self._on_destroy)
        self._build()

    def _on_destroy(self, _w) -> None:
        self._alive = False
        if self._search_timer is not None:
            try:
                GLib.source_remove(self._search_timer)
            except Exception:
                pass
            self._search_timer = None
        self._token += 1

    # ── сборка ───────────────────────────────────────────────────
    def _build(self) -> None:
        self.append(view_header("📚", "Zotero", "Библиотека Zotero — поиск и импорт записей как заметок (API / zotero.sqlite)"))

        # ── конфигурация (свёрнута в expander-подобный блок) ──
        cfg_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["toolbar"])
        cfg_box.set_margin_start(14)
        cfg_box.set_margin_end(14)
        cfg_title = Gtk.Label(label="Настройки Zotero", halign=Gtk.Align.START, xalign=0, css_classes=["settings-label"])
        cfg_box.append(cfg_title)
        grid = Gtk.Grid(column_spacing=8, row_spacing=6, css_classes=["settings-grid"])
        # library id
        grid.attach(Gtk.Label(label="Library ID", halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"]), 0, 0, 1, 1)
        self._lib_id = Gtk.Entry(text=str(self.settings.get("zotero_library_id") or ""), placeholder_text="числовой ID (user) или group ID", hexpand=True)
        grid.attach(self._lib_id, 1, 0, 1, 1)
        # library type
        grid.attach(Gtk.Label(label="Тип", halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"]), 2, 0, 1, 1)
        self._lib_type = Gtk.DropDown.new_from_strings(["user", "group"])
        self._lib_type.set_selected(0 if str(self.settings.get("zotero_library_type") or "user") == "user" else 1)
        grid.attach(self._lib_type, 3, 0, 1, 1)
        # api key
        grid.attach(Gtk.Label(label="API Key", halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"]), 0, 1, 1, 1)
        self._api_key = Gtk.Entry(text=str(self.settings.get("zotero_api_key") or ""), placeholder_text="ключ с api.zotero.org (необязательно для локальной БД)", hexpand=True, visibility=False)
        grid.attach(self._api_key, 1, 1, 1, 1)
        # db path
        grid.attach(Gtk.Label(label="DB path", halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"]), 0, 2, 1, 1)
        self._db_path = Gtk.Entry(text=str(self.settings.get("zotero_db_path") or ""), placeholder_text="путь к zotero.sqlite (пусто = авто-поиск ~/Zotero/)", hexpand=True)
        grid.attach(self._db_path, 1, 2, 1, 1)
        # import folder
        grid.attach(Gtk.Label(label="Папка импорта", halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"]), 0, 3, 1, 1)
        self._import_folder = Gtk.Entry(text=str(self.settings.get("zotero_import_folder") or DEFAULT_IMPORT_FOLDER), hexpand=True)
        grid.attach(self._import_folder, 1, 3, 1, 1)
        cfg_box.append(grid)
        cfg_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        save_cfg = Gtk.Button(label="Сохранить настройки", css_classes=["suggested-action"])
        save_cfg.connect("clicked", self._on_save_config)
        cfg_btns.append(save_cfg)
        self._cfg_status = Gtk.Label(label="", css_classes=["dim-hint"])
        cfg_btns.append(self._cfg_status)
        cfg_box.append(cfg_btns)
        # hint
        hint = Gtk.Label(
            label="Локальная БД: авто-поиск ~/Zotero/zotero.sqlite — ключ не нужен. API: Library ID + API Key для синхронизированной библиотеки.",
            wrap=True, halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"],
        )
        cfg_box.append(hint)
        self.append(cfg_box)

        # ── тулбар поиска ────────────────────────────────────────
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["toolbar"])
        toolbar.set_margin_start(14)
        toolbar.set_margin_end(14)
        self.search_entry = Gtk.SearchEntry(placeholder_text="Поиск по библиотеке — название, автор, тег…", hexpand=True)
        self.search_entry.connect("search-changed", lambda *_: self._schedule_filter())
        toolbar.append(self.search_entry)

        self.source_drop = Gtk.DropDown.new_from_strings(["auto — БД или API", "локальная БД", "Zotero API"])
        self.source_drop.set_selected(0)
        self.source_drop.connect("notify::selected", lambda *_: self._schedule_filter())
        toolbar.append(self.source_drop)

        self.reload_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Перечитать библиотеку")
        self.reload_btn.connect("clicked", lambda *_: self.reload(force=True))
        toolbar.append(self.reload_btn)

        self.count_label = Gtk.Label(label="", css_classes=["dim-hint"])
        toolbar.append(self.count_label)

        self.append(toolbar)

        # ── список + детали (split) ──────────────────────────────
        split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, hexpand=True, vexpand=True)
        split.set_margin_start(14)
        split.set_margin_end(14)
        split.set_margin_bottom(14)

        # левая: список
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.set_size_request(420, -1)

        # действия импорта
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.import_sel_btn = Gtk.Button(label="Импорт выбранных", css_classes=["suggested-action"])
        self.import_sel_btn.connect("clicked", self._on_import_selected)
        self.import_sel_btn.set_sensitive(False)
        actions.append(self.import_sel_btn)
        self.import_all_btn = Gtk.Button(label="Импорт всех видимых")
        self.import_all_btn.connect("clicked", self._on_import_all)
        self.import_all_btn.set_sensitive(False)
        actions.append(self.import_all_btn)
        self.select_all_btn = Gtk.Button(label="Выбрать всё")
        self.select_all_btn.connect("clicked", self._on_select_all)
        actions.append(self.select_all_btn)
        self.clear_sel_btn = Gtk.Button(label="Снять")
        self.clear_sel_btn.connect("clicked", self._on_clear_selection)
        actions.append(self.clear_sel_btn)
        left.append(actions)

        # прогресc/статус
        self.status_label = Gtk.Label(label="Загрузка…", halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"])
        left.append(self.status_label)

        self.listbox = Gtk.ListBox(css_classes=["file-list"])
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.connect("row-activated", self._on_row_activated)
        scroller = Gtk.ScrolledWindow(vexpand=True, css_classes=["editor-frame"])
        scroller.set_child(self.listbox)
        try:
            scroller.set_propagate_natural_width(False)
        except AttributeError:
            pass
        left.append(scroller)

        # пустые состояния
        self._empty = empty_state(
            "📚", "Библиотека пуста",
            hint="Проверь путь к zotero.sqlite или настройки API — нажми «Перечитать»",
            action_label="Перечитать",
            on_action=lambda: self.reload(force=True),
        )
        self._empty.set_visible(False)
        left.append(self._empty)

        self._filter_empty = empty_state(
            "🔍", "Ничего не найдено",
            hint="Попробуй другой запрос или смени источник",
            action_label="Очистить поиск",
            on_action=lambda: self.search_entry.set_text(""),
        )
        self._filter_empty.set_visible(False)
        left.append(self._filter_empty)

        split.append(left)

        # правая: превью / детали
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True)
        self.detail_title = Gtk.Label(label="Выбери запись", css_classes=["view-title"], halign=Gtk.Align.START, xalign=0, wrap=True)
        right.append(self.detail_title)
        self.detail_meta = Gtk.Label(label="", halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"], wrap=True)
        right.append(self.detail_meta)
        self.detail_tags = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, css_classes=["tags-flow"])
        right.append(self.detail_tags)
        # abstract
        self.detail_abstract = Gtk.Label(label="", halign=Gtk.Align.START, xalign=0, wrap=True, css_classes=["dim-label"])
        self.detail_abstract.set_selectable(True)
        abs_scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True, css_classes=["editor-frame"])
        abs_scroller.set_child(self.detail_abstract)
        right.append(abs_scroller)

        # citation + кнопки
        self.citation_label = Gtk.Label(label="", halign=Gtk.Align.START, xalign=0, wrap=True, css_classes=["dim-hint"])
        self.citation_label.set_selectable(True)
        right.append(self.citation_label)
        detail_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.detail_import_btn = Gtk.Button(label="Импортировать эту запись", css_classes=["suggested-action"])
        self.detail_import_btn.connect("clicked", self._on_detail_import)
        self.detail_import_btn.set_sensitive(False)
        detail_btns.append(self.detail_import_btn)
        self.detail_open_btn = Gtk.Button(label="Открыть DOI/URL")
        self.detail_open_btn.connect("clicked", self._on_open_link)
        self.detail_open_btn.set_sensitive(False)
        detail_btns.append(self.detail_open_btn)
        right.append(detail_btns)

        self._current_detail: ZoteroItem | None = None

        split.append(right)
        self.append(split)

        # загрузка при старте (фон)
        self.reload()

    # ── данные ───────────────────────────────────────────────────
    def reload(self, force: bool = False) -> None:  # noqa: ARG002
        """Перечитать библиотеку в фоне (не блокирует UI)."""
        self._token += 1
        token = self._token
        self._loading = True
        self.status_label.set_text("Загрузка библиотеки…")
        self.reload_btn.set_sensitive(False)
        # sync service settings
        try:
            self._service.update_settings(dict(self.settings))
        except Exception:
            pass
        source = self._source_key()
        query = self.search_entry.get_text().strip() if hasattr(self, "search_entry") else ""
        settings = dict(self.settings)
        threading.Thread(target=self._load_work, args=(settings, source, query, token), daemon=True).start()

    def _source_key(self) -> str:
        sel = self.source_drop.get_selected() if hasattr(self, "source_drop") else 0
        return ["auto", "db", "api"][int(sel)] if int(sel) in (0, 1, 2) else "auto"

    def _load_work(self, settings: dict, source: str, query: str, token: int) -> None:
        try:
            # limit адаптивный: если есть query — берём больше для фильтрации
            limit = 300 if query else 200
            items = self._service.list_items(limit=limit, query=query or None, source=source, settings=settings)
            err = self._service.last_error
        except Exception as exc:  # noqa: BLE001
            items, err = [], str(exc)
        GLib.idle_add(self._apply_loaded, items, err, token)

    def _apply_loaded(self, items: list[ZoteroItem], err: str | None, token: int) -> bool:
        if token != self._token or not self._alive:
            return False
        self._loading = False
        self.reload_btn.set_sensitive(True)
        self._items = list(items)
        if err:
            self.status_label.set_text(f"⚠ {err[:120]}")
        elif not items:
            # подсказка в зависимости от источника
            if self._source_key() == "db" and not self._service.is_db_available():
                self.status_label.set_text("Локальная БД не найдена — проверь путь к zotero.sqlite")
            elif self._source_key() == "api" and not self._service.is_api_configured():
                self.status_label.set_text("API не настроен — укажи Library ID + API Key")
            else:
                self.status_label.set_text("Библиотека пуста или нет совпадений")
        else:
            self.status_label.set_text(f"Загружено {len(items)} записей · источник: {self._source_key()}")

        # service кэш
        try:
            self._service.prime_cache(items)
        except Exception:
            pass
        self._apply_filter()
        return False

    # ── фильтрация (локальная, дебаунс) ──────────────────────────
    def _schedule_filter(self) -> None:
        if self._search_timer is not None:
            try:
                GLib.source_remove(self._search_timer)
            except Exception:
                pass
            self._search_timer = None
        # если идёт загрузка с query — перезагрузим с сервера/БД
        # иначе — локальный фильтр (быстро)
        q = self.search_entry.get_text().strip() if hasattr(self, "search_entry") else ""
        # для API/db с q — триггерим reload с новым query (дебаунс 400мс)
        if q and len(q) >= 2:
            self._search_timer = GLib.timeout_add(400, self._do_reload_with_query)
        else:
            self._search_timer = GLib.timeout_add(200, self._do_local_filter)

    def _do_reload_with_query(self) -> bool:
        self._search_timer = None
        # перезагружаем с новым query — серверная фильтрация
        self.reload()
        return False

    def _do_local_filter(self) -> bool:
        self._search_timer = None
        self._apply_filter()
        return False

    def _apply_filter(self) -> None:
        q = self.search_entry.get_text().strip() if hasattr(self, "search_entry") else ""
        src = self._source_key()
        # если есть query и мы на auto/db/api — уже отфильтровано на сервере,
        # но дополнительно ранжируем локально
        if q:
            from ..services.zotero import search_items

            self._filtered = search_items(self._items, q, limit=300)
        else:
            self._filtered = list(self._items)
        self._render_list()
        self._update_counts()

    def _update_counts(self) -> None:
        total = len(self._items)
        vis = len(self._filtered)
        sel = len(self._selected)
        parts: list[str] = []
        if vis != total:
            parts.append(f"{vis} / {total}")
        else:
            parts.append(f"{total} записей")
        if sel:
            parts.append(f"выбрано {sel}")
        self.count_label.set_text(" · ".join(parts) if total else "")
        self.import_sel_btn.set_sensitive(bool(sel))
        self.import_all_btn.set_sensitive(bool(vis))
        # empty states
        has_raw = bool(total)
        has_vis = bool(vis)
        self._empty.set_visible(not has_raw and not self._loading)
        self._filter_empty.set_visible(has_raw and not has_vis)
        self.listbox.set_visible(has_vis)

    # ── рендер списка ────────────────────────────────────────────
    def _render_list(self) -> None:
        while (child := self.listbox.get_first_child()) is not None:
            self.listbox.remove(child)
        for item in self._filtered:
            row = Gtk.ListBoxRow(css_classes=["nav-item"])
            row._item = item  # type: ignore[attr-defined]
            # checkbox
            h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            h.set_margin_top(4)
            h.set_margin_bottom(4)
            h.set_margin_start(8)
            h.set_margin_end(8)
            chk = Gtk.CheckButton(active=(item.key in self._selected))
            chk.connect("toggled", self._on_check_toggled, item)
            h.append(chk)
            # иконка типа
            type_icon = {
                "journalArticle": "📄",
                "book": "📚",
                "bookSection": "📖",
                "conferencePaper": "🎤",
                "thesis": "🎓",
                "report": "📋",
                "webpage": "🌐",
                "preprint": "📝",
            }.get(item.item_type, "📄")
            h.append(Gtk.Label(label=type_icon, css_classes=["sb-nav-icon"]))
            # текст
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            title = Gtk.Label(label=item.title, halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["sb-nav-text"])
            v.append(title)
            meta = _creator_label(item)
            if item.journal:
                meta = (meta + " · " + item.journal) if meta else item.journal
            if meta:
                v.append(Gtk.Label(label=meta, halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"], ellipsize=Pango.EllipsizeMode.END))
            h.append(v)
            # type pill
            pill = status_pill(item.item_type, "idle")
            h.append(pill)
            # key hint
            h.append(Gtk.Label(label=item.key, css_classes=["dim-hint"]))
            row.set_child(h)
            self.listbox.append(row)
        # если есть выбранная детальная — подсветить
        if self._current_detail is not None:
            self._set_detail(self._current_detail)

    def _on_check_toggled(self, btn: Gtk.CheckButton, item: ZoteroItem) -> None:
        if btn.get_active():
            self._selected.add(item.key)
        else:
            self._selected.discard(item.key)
        self._update_counts()

    def _on_row_activated(self, _box, row: Gtk.ListBoxRow) -> None:
        item: ZoteroItem | None = getattr(row, "_item", None)
        if item is not None:
            self._set_detail(item)

    def _set_detail(self, item: ZoteroItem) -> None:
        self._current_detail = item
        self.detail_title.set_text(item.title)
        meta_parts: list[str] = []
        if item.creators:
            meta_parts.append(item.creators_short())
        if item.date:
            meta_parts.append(item.date)
        if item.journal:
            meta_parts.append(item.journal)
        if item.publisher:
            meta_parts.append(item.publisher)
        self.detail_meta.set_text(" · ".join(p for p in meta_parts if p))
        # теги
        while (c := self.detail_tags.get_first_child()) is not None:
            self.detail_tags.remove(c)
        for t in item.tags[:12]:
            self.detail_tags.append(Gtk.Label(label=f"#{t}", css_classes=["pill", "pill-idle"]))
        if item.tags and len(item.tags) > 12:
            self.detail_tags.append(Gtk.Label(label=f"+{len(item.tags)-12}", css_classes=["dim-hint"]))
        self.detail_abstract.set_text(item.abstract or "— нет аннотации —")
        self.citation_label.set_text(item.citation())
        self.detail_import_btn.set_sensitive(True)
        has_link = bool(item.doi or item.url)
        self.detail_open_btn.set_sensitive(has_link)
        if has_link:
            self.detail_open_btn.set_tooltip_text(item.doi or item.url)
        else:
            self.detail_open_btn.set_tooltip_text("нет ссылки")

    # ── выбор ────────────────────────────────────────────────────
    def _on_select_all(self, _b) -> None:
        for it in self._filtered:
            self._selected.add(it.key)
        self._render_list()
        self._update_counts()

    def _on_clear_selection(self, _b) -> None:
        self._selected.clear()
        self._render_list()
        self._update_counts()

    # ── импорт ───────────────────────────────────────────────────
    def _on_import_selected(self, _b) -> None:
        keys = set(self._selected)
        items = [it for it in self._filtered if it.key in keys]
        # fallback: если filtered не содержит (фильтр сменился) — брать из _items
        if not items:
            items = [it for it in self._items if it.key in keys]
        self._do_import(items)

    def _on_import_all(self, _b) -> None:
        self._do_import(list(self._filtered))

    def _on_detail_import(self, _b) -> None:
        if self._current_detail is not None:
            self._do_import([self._current_detail])

    def _do_import(self, items: list[ZoteroItem]) -> None:
        if not items:
            self._show_toast("нечего импортировать")
            return
        self.status_label.set_text(f"Импорт {len(items)} записей…")
        self.import_sel_btn.set_sensitive(False)
        self.import_all_btn.set_sensitive(False)
        settings = dict(self.settings)
        threading.Thread(target=self._import_work, args=(items, settings), daemon=True).start()

    def _import_work(self, items: list[ZoteroItem], settings: dict) -> None:
        try:
            self._service.update_settings(settings)
            paths = self._service.import_items(items, settings=settings)
            err = None
        except Exception as exc:  # noqa: BLE001
            paths, err = [], str(exc)
        GLib.idle_add(self._import_done, paths, err)

    def _import_done(self, paths: list[Path], err: str | None) -> bool:
        if not self._alive:
            return False
        self.import_sel_btn.set_sensitive(bool(self._selected))
        self.import_all_btn.set_sensitive(bool(self._filtered))
        if err:
            self.status_label.set_text(f"Ошибка импорта: {err[:120]}")
            self._show_toast(f"ошибка: {err[:80]}")
        elif not paths:
            self.status_label.set_text("Ничего не импортировано")
        else:
            self.status_label.set_text(f"Импортировано {len(paths)} заметок → {paths[0].parent}")
            self._show_toast(f"импортировано {len(paths)} ✓")
            # открыть первую если есть колбэк
            if self.on_open_note is not None and paths:
                try:
                    self.on_open_note(str(paths[0]))
                except Exception:
                    pass
        return False

    # ── ссылки ───────────────────────────────────────────────────
    def _on_open_link(self, _b) -> None:
        if self._current_detail is None:
            return
        url = ""
        if self._current_detail.doi:
            doi = self._current_detail.doi.strip()
            url = doi if doi.lower().startswith("http") else f"https://doi.org/{doi}" if doi else ""
        if not url:
            url = self._current_detail.url
        if not url:
            return
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except Exception:
            try:
                import subprocess

                subprocess.Popen(["xdg-open", url])
            except Exception:
                self._show_toast(f"не удалось открыть: {url[:60]}")

    # ── конфиг ───────────────────────────────────────────────────
    def _on_save_config(self, _b) -> None:
        updated = dict(self.settings)
        updated["zotero_library_id"] = self._lib_id.get_text().strip()
        # DropDown: 0=user, 1=group
        sel = self._lib_type.get_selected()
        updated["zotero_library_type"] = "group" if int(sel) == 1 else "user"
        updated["zotero_api_key"] = self._api_key.get_text().strip()
        updated["zotero_db_path"] = self._db_path.get_text().strip()
        updated["zotero_import_folder"] = self._import_folder.get_text().strip() or DEFAULT_IMPORT_FOLDER
        self.settings = updated
        try:
            self._service.update_settings(updated)
        except Exception:
            pass
        # persist через config.save_settings если доступно
        try:
            from ..config import save_settings

            save_settings(updated)
            self._cfg_status.set_text("сохранено ✓")
        except Exception as exc:  # noqa: BLE001
            self._cfg_status.set_text(f"ошибка: {exc}")
            return
        self._show_toast("настройки Zotero сохранены ✓")
        GLib.timeout_add(2000, lambda: self._cfg_status.set_text("") or False)
        self.reload(force=True)

    # ── toast ────────────────────────────────────────────────────
    def _show_toast(self, msg: str) -> None:
        try:
            win = self.get_root()
            if win is not None and hasattr(win, "toast_overlay"):
                t = Adw.Toast.new(msg)
                t.set_timeout(3)
                win.toast_overlay.add_toast(t)  # type: ignore
                return
        except Exception:
            pass
        old = self.status_label.get_text() if hasattr(self, "status_label") else ""
        self.status_label.set_text(msg)
        GLib.timeout_add(2500, lambda: self.status_label.set_text(old) or False)
