"""Заметки — обсидиановское дерево папок + markdown-редактор + мгновенный поиск.

Сайдбар — дерево vault как в Obsidian: папка раскрывается кликом на названии,
заметка — одним кликом (корпус 39k раскрывается лениво и чанками, UI не виснет).
Фильтр сверху переводит панель в плоский поиск по всей базе.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from ..vault import FILE_EMOJI

try:
    from ..core import crypto as _crypto  # AES-GCM .md.enc
except Exception:  # pragma: no cover - fallback если модуль не загружен в тестах
    _crypto = None  # type: ignore

try:
    from ..services import crypto_folder as _crypto_folder  # авто-шифрование vault/Secret/
except Exception:  # pragma: no cover
    _crypto_folder = None  # type: ignore

try:
    from ..core import crdt as _crdt  # CRDT совместное редактирование
except Exception:  # pragma: no cover
    _crdt = None  # type: ignore

try:
    from ..core import snippets as _snippets  # сниппеты trigger->expansion
except Exception:  # pragma: no cover
    _snippets = None  # type: ignore

try:
    from ..services import fts as _fts  # FTS5 полнотекстовый поиск BM25
except Exception:  # pragma: no cover
    _fts = None  # type: ignore

try:
    from ..services import auto_tagger as _auto_tagger  # LLM авто-тегирование
except Exception:  # pragma: no cover
    _auto_tagger = None  # type: ignore

try:
    from ..services import embeddings as _embeddings  # локальные embeddings + TF-IDF fallback
except Exception:  # pragma: no cover
    _embeddings = None  # type: ignore

try:
    from . import pdf_annotate as _pdf_annotate  # PDF highlight аннотации (Poppler/WebView)
except Exception:  # pragma: no cover
    _pdf_annotate = None  # type: ignore


# ── Модель для виртуализованного плоского списка (Gio.ListStore) ─────────
class _NoteItem(GObject.Object):
    __gtype_name__ = "NoteItem"
    name = GObject.Property(type=str, default="")
    path = GObject.Property(type=str, default="")
    lower_name = GObject.Property(type=str, default="")
    lower_path = GObject.Property(type=str, default="")

    def __init__(self, name: str, path: str):
        super().__init__()
        self.name = name
        self.path = path
        self.lower_name = name.lower()
        self.lower_path = path.lower()


from ..services.vault_service import VaultService, collect_notes, count_nodes, index_nodes
from .html_preview import HtmlPreview
from .markdown import MarkdownView
from .scale import attach_zoom_keys
from .widgets import empty_state, view_header

try:
    from .smart_links import SmartLinks  # type: ignore
except Exception:  # pragma: no cover - headless/tests
    SmartLinks = None  # type: ignore

try:
    from .vim_mode import VimController  # type: ignore
except Exception:  # pragma: no cover
    VimController = None  # type: ignore

try:
    from .comments import CommentsGutter  # type: ignore
except Exception:  # pragma: no cover
    CommentsGutter = None  # type: ignore

# re-export для обратной совместимости (внешний API files_view.collect_notes и т.д.)
__all__ = ["FilesView", "collect_notes", "index_nodes", "count_nodes"]


def _content_clamp(child: Gtk.Widget) -> Adw.Clamp:
    """Максимальная ширина колонки текста как в Obsidian (читается по центру)."""
    clamp = Adw.Clamp(maximum_size=920, tightening_threshold=720)
    clamp.set_child(child)
    return clamp


# Сколько строк дерева/списка добавляем за один idle-тик — UI не замирает.
_CHUNK = 500
# Жёсткий потолок строк в плоском поиске; остальное — уточни фильтр.
_RENDER_CAP = 5000
# Дебаунс фильтра плоского списка (мс) — не дергаем FilterListModel на каждый keystroke.
_FILTER_DEBOUNCE_MS = 150
# Автосохранение: debounce после изменения буфера (мс)
_AUTOSAVE_MS = 800
# Поиск в редакторе: debounce подсветки (мс) + лимит видимых совпадений
_SEARCH_DEBOUNCE_MS = 200
_SEARCH_HIGHLIGHT_LIMIT = 1000


class FilesView(Gtk.Box):
    def __init__(self, settings: dict) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self._vault = VaultService(settings)
        # alias для обратной совместимости / тестов
        self.vault_service = self._vault
        self._current: Path | None = None
        self._highlight: str = ""
        self._dirty = False
        self._loading = False
        self._root = Path(self.settings["vault_root"])
        self._token = 0
        self._alive = True
        self._notes: list[tuple[str, str]] = []
        self._node: object | None = None
        self._nodes: dict[str, object] = {}
        self._populated: set[str] = set()
        self._fill_tokens: dict[str, int] = {}
        self._path_to_iter: dict[str, Gtk.TreeIter] = {}
        self._path_to_row: dict[
            str, Gtk.ListBoxRow
        ] = {}  # compat shim (устар., теперь выбор через selection)
        self._render_token = 0
        self._pending_rows: list[tuple[str, str]] = []
        self._filling = False
        self._hit_cap = False
        # ── виртуализованный плоский список (Gio.ListStore + worker FilterListModel + SliceListModel) ──
        self._filter_timer: int | None = None
        self._filter_query: str = ""
        self._tag_matched: set[str] = set()
        self._flat_store: Gio.ListStore | None = None
        self._filtered_store: Gio.ListStore | None = None
        self._flat_filter: Gtk.CustomFilter | None = None  # legacy compat
        self._filter_model: Gtk.FilterListModel | None = None  # legacy compat
        self._slice_model: Gtk.SliceListModel | None = None
        self._selection: Gtk.SingleSelection | None = None
        self._listview: Gtk.ListView | None = None
        self._flat_populate_token = 0
        self._flat_pending: list[tuple[str, str]] = []
        self._filter_token: int = 0
        self._filtered_pending: list[tuple[str, str]] = []
        self._read_cache: OrderedDict[str, tuple[int, str]] = OrderedDict()
        self._selected_tag: str | None = None
        self._outline_timer: int | None = None
        self._outline_headings: list[tuple[int, str, int]] = []
        # ── автосохранение + FileMonitor открытого файла ──
        self._autosave_timer: int | None = None
        self._file_monitor: Gio.FileMonitor | None = None
        self._file_monitor_path: str | None = None
        self._suppress_monitor: bool = False
        self._external_toast_pending: bool = False
        # ── поиск с подсветкой в редакторе ──
        self._search_matches: list[tuple[Gtk.TextIter, Gtk.TextIter]] = []
        self._search_index: int = -1
        self._search_tag: Gtk.TextTag | None = None
        self._search_current_tag: Gtk.TextTag | None = None
        self.search_counter: Gtk.Label | None = None
        self.search_prev_btn: Gtk.Button | None = None
        self.search_next_btn: Gtk.Button | None = None
        # debounce + фоновый поиск
        self._search_timer: int | None = None
        self._search_gen: int = 0
        # ── CRDT совместное редактирование (опция) ──
        self._crdt_doc = None  # LWWDocument | RGAText | None
        self._crdt_sync = None  # FileSync | None
        self._crdt_stub = None  # NetworkSyncStub | None (заглушка)
        # ── сниппеты trigger->expansion ──
        self._snippet_expanding: bool = False
        self._snippets_cache: dict[str, str] | None = None
        # ── auto-tagging LLM ──────────────────────────────
        self._auto_suggested: list[str] | None = None
        self._auto_tag_bar: Gtk.Revealer | None = None
        self._auto_tag_chips: Gtk.FlowBox | None = None
        self._auto_tag_label: Gtk.Label | None = None
        self.connect("destroy", self._on_destroy)
        self._build()

    def _on_destroy(self, _widget) -> None:
        self._alive = False
        if self._outline_timer is not None:
            try:
                GLib.source_remove(self._outline_timer)
            except Exception:
                pass
            self._outline_timer = None
        if getattr(self, "_filter_timer", None) is not None:
            try:
                GLib.source_remove(self._filter_timer)
            except Exception:
                pass
            self._filter_timer = None
        if getattr(self, "_autosave_timer", None) is not None:
            try:
                GLib.source_remove(self._autosave_timer)
            except Exception:
                pass
            self._autosave_timer = None
        if getattr(self, "_search_timer", None) is not None:
            try:
                GLib.source_remove(self._search_timer)
            except Exception:
                pass
            self._search_timer = None
        self._search_gen += 1
        self._cancel_file_monitor()
        # SmartLinks cleanup
        try:
            sl = getattr(self, "_smart_links", None)
            if sl is not None and hasattr(sl, "destroy"):
                sl.destroy()
        except Exception:
            pass

    # ── Сборка ───────────────────────────────────────────────
    def _build(self) -> None:
        self.append(view_header("🗂", "Заметки", "Vault как в Obsidian: папки, один клик — открыть"))

        split = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12, hexpand=True, vexpand=True
        )
        split.set_margin_start(14)
        split.set_margin_end(14)
        split.set_margin_bottom(14)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.set_size_request(280, -1)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        new_btn = Gtk.Button(label="Новый")
        self._new_entry = Gtk.Entry(placeholder_text="Имя файла (.md)", activates_default=True)
        new_pop = Gtk.Popover()
        popbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        popbox.set_margin_top(8)
        popbox.set_margin_bottom(8)
        popbox.set_margin_start(10)
        popbox.set_margin_end(10)
        popbox.append(self._new_entry)
        # выбор шаблона для создания заметки
        self._new_template_drop: Gtk.DropDown | None = None
        try:
            from ..core.templates import list_templates as _lt

            _tpls = _lt(self.settings)
            _names = ["— без шаблона —"] + [p.stem for p in _tpls]
            if len(_names) > 1:
                tpl_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                tpl_box.append(
                    Gtk.Label(
                        label="Шаблон", halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"]
                    )
                )
                self._new_template_drop = Gtk.DropDown.new_from_strings(_names)
                self._new_template_drop.set_selected(0)
                tpl_box.append(self._new_template_drop)
                popbox.append(tpl_box)
        except Exception:
            self._new_template_drop = None
        create = Gtk.Button(label="Создать", css_classes=["suggested-action"])
        create.connect("clicked", self._on_create_note, self._new_entry, new_pop)
        popbox.append(create)
        new_pop.set_child(popbox)
        new_pop.set_parent(new_btn)
        self._new_popover = new_pop
        new_btn.connect("clicked", lambda *_: self._popup_new(new_pop))
        actions.append(new_btn)
        self.filter_entry = Gtk.SearchEntry(
            placeholder_text="Поиск по всем заметкам…",
            hexpand=True,
        )
        self.filter_entry.connect("search-changed", lambda *_: self._on_filter_changed())
        actions.append(self.filter_entry)
        # счётчик вхождений в редакторе — формат "3/12"
        self.search_counter = Gtk.Label(label="", css_classes=["dim-hint", "search-counter"])
        self.search_counter.set_tooltip_text("Совпадений в открытом файле")
        actions.append(self.search_counter)
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Перечитать vault")
        refresh_btn.connect("clicked", lambda *_: self.reload(force=True))
        actions.append(refresh_btn)
        self.count_label = Gtk.Label(label="", css_classes=["dim-hint"])
        actions.append(self.count_label)
        left.append(actions)

        # Дерево (Obsidian): папки раскрываются кликом, файлы открываются одиночным.
        # TODO(GTK4 migration): TreeView/TreeStore deprecated since GTK 4.10.
        #   Planned: Gtk.ColumnView + Gtk.TreeListModel + Gtk.TreeExpander.
        #   Complexity: ленивая чанковая подгрузка _store (39k узлов), кастомные
        #   иконы FILE_EMOJI, Obsidian-UX (клик на папку = expand/collapse).
        #   Полная миграция требует переписывания VaultService.file_tree →
        #   Gio.ListStore + TreeListModel.create_func. Оставлено TreeView с
        #   keyboard typeahead как промежуточный шаг.
        self._store = Gtk.TreeStore(str, str, str, bool)  # emoji, имя, abs-path, is_dir
        self._tree = Gtk.TreeView(model=self._store, css_classes=["file-tree"])
        # keyboard typeahead (инкрементальный поиск по имени файла/папки)
        try:
            self._tree.set_enable_search(True)
            self._tree.set_search_column(1)
        except Exception:
            pass
        icon_col = Gtk.TreeViewColumn(title="", cell_renderer=Gtk.CellRendererText(), text=0)
        name_col = Gtk.TreeViewColumn(title="", cell_renderer=Gtk.CellRendererText(), text=1)
        name_col.set_expand(True)
        self._tree.append_column(icon_col)
        self._tree.append_column(name_col)
        self._tree.set_expander_column(name_col)
        self._tree.set_headers_visible(False)
        self._tree.set_activate_on_single_click(True)
        self._tree.connect("row-activated", self._on_tree_activated)
        self._tree.connect("row-expanded", self._on_tree_expanded)
        self._tree.connect("row-collapsed", self._on_tree_collapsed)
        tree_scroller = Gtk.ScrolledWindow(vexpand=True, css_classes=["editor-frame"])
        tree_scroller.set_child(self._tree)
        # Не раздвигать окно из-за дерева (как в Obsidian — панель сжимает контент)
        try:
            tree_scroller.set_propagate_natural_width(False)
        except AttributeError:
            pass

        # Плоский поиск (фильтр): виртуализованный ListView (Gio.ListStore + worker filtering + SliceListModel)
        self._flat_store = Gio.ListStore(item_type=_NoteItem)
        self._filtered_store = Gio.ListStore(item_type=_NoteItem)
        self._flat_filter = Gtk.CustomFilter.new(self._flat_filter_func)  # legacy compat
        self._filter_model = Gtk.FilterListModel(model=self._flat_store, filter=self._flat_filter)
        self._slice_model = Gtk.SliceListModel(model=self._filtered_store, size=_RENDER_CAP)
        self._filter_token = 0
        self._filtered_pending = []
        self._selection = Gtk.SingleSelection(model=self._slice_model)
        self._selection.set_can_unselect(True)
        # factory для строки файла (иконка + title + parent)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_flat_factory_setup)
        factory.connect("bind", self._on_flat_factory_bind)
        self._listview = Gtk.ListView(
            model=self._selection, factory=factory, css_classes=["file-list"]
        )
        self._listview.set_single_click_activate(True)
        self._listview.connect("activate", self._on_listview_activate)
        flat_scroller = Gtk.ScrolledWindow(vexpand=True, css_classes=["editor-frame"])
        flat_scroller.set_child(self._listview)
        try:
            flat_scroller.set_propagate_natural_width(False)
        except AttributeError:
            pass
        # ── compat shim: старый API listbox ──
        self.listbox = self._listview  # type: ignore[assignment]
        # прокси для кода который вызывает listbox.select_row / unselect_all / get_first_child
        # методы добавлены ниже (_listbox_compat_*); здесь подменяем атрибуты
        self._install_listbox_compat()

        self.side_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.side_stack.add_named(tree_scroller, "tree")
        self.side_stack.add_named(flat_scroller, "find")
        self.side_stack.set_visible_child_name("tree")
        left.append(self.side_stack)

        # Пустые состояния — унифицированные .empty
        self._vault_empty = empty_state(
            "📂",
            "Vault пуст — заметок пока нет",
            hint="Создайте первую заметку кнопкой «Новый»",
            action_label="Создать заметку",
            on_action=lambda: self._popup_new(self._new_popover),
        )
        self._vault_empty.set_visible(False)
        left.append(self._vault_empty)

        self._filter_empty = empty_state(
            "🔍",
            "Ничего не найдено",
            hint="Попробуйте другой запрос",
            action_label="Очистить фильтр",
            on_action=lambda: self.filter_entry.set_text(""),
        )
        self._filter_empty.set_visible(False)
        left.append(self._filter_empty)
        left.set_visible(False)
        split.append(left)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True)
        toolbar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["toolbar"]
        )
        self.file_label = Gtk.Label(
            label="",
            css_classes=["dim-label", "dim-hint"],
            hexpand=True,
            halign=Gtk.Align.START,
            xalign=0,
            ellipsize=Pango.EllipsizeMode.MIDDLE,
        )
        toolbar.append(self.file_label)
        # ── навигация по вхождениям (Ctrl+G / Ctrl+Shift+G) ──
        search_nav = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=2, css_classes=["search-nav"]
        )
        self.search_prev_btn = Gtk.Button(
            icon_name="go-up-symbolic",
            tooltip_text="Предыдущее вхождение (Ctrl+Shift+G)",
            css_classes=["flat", "search-nav-btn"],
        )
        self.search_prev_btn.connect("clicked", lambda *_: self._search_prev())
        self.search_prev_btn.set_sensitive(False)
        search_nav.append(self.search_prev_btn)
        self.search_next_btn = Gtk.Button(
            icon_name="go-down-symbolic",
            tooltip_text="Следующее вхождение (Ctrl+G)",
            css_classes=["flat", "search-nav-btn"],
        )
        self.search_next_btn.connect("clicked", lambda *_: self._search_next())
        self.search_next_btn.set_sensitive(False)
        search_nav.append(self.search_next_btn)
        toolbar.append(search_nav)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher(stack=self.stack, css_classes=["toolbar-switcher"])
        self.del_btn = Gtk.Button(
            icon_name="user-trash-symbolic",
            tooltip_text="Удалить файл безвозвратно",
            css_classes=["destructive-action", "flat"],
        )
        self.del_btn.set_sensitive(False)
        self.del_btn.connect("clicked", self._on_delete)
        toolbar.append(self.del_btn)
        # ── Шифрование AES-GCM .md.enc ─────────────────────
        self.encrypt_btn = Gtk.Button(
            icon_name="system-lock-screen-symbolic",
            tooltip_text="Зашифровать — AES-GCM .md.enc (ключ из пароля PBKDF2)",
            css_classes=["flat"],
        )
        self.encrypt_btn.set_sensitive(False)
        self.encrypt_btn.connect("clicked", self._on_encrypt)
        toolbar.append(self.encrypt_btn)
        self.decrypt_btn = Gtk.Button(
            icon_name="changes-allow-symbolic",
            tooltip_text="Расшифровать .md.enc — AES-GCM",
            css_classes=["flat"],
        )
        self.decrypt_btn.set_sensitive(False)
        self.decrypt_btn.connect("clicked", self._on_decrypt)
        toolbar.append(self.decrypt_btn)
        # ── PDF экспорт ──────────────────────────────────────────
        self.export_btn = Gtk.Button(
            label="Экспорт",
            icon_name="document-save-as-symbolic",
            tooltip_text="Экспорт в PDF (тема AO Glass)",
            css_classes=["flat"],
        )
        self.export_btn.set_sensitive(False)
        self.export_btn.connect("clicked", self._on_export_pdf)
        toolbar.append(self.export_btn)
        # ── Publish: экспорт vault в статический сайт ─────────────
        self.publish_btn = Gtk.Button(
            label="Публикация",
            icon_name="applications-internet-symbolic",
            tooltip_text="Опубликовать сайт — экспорт vault в static HTML (fragile publish)",
            css_classes=["flat"],
        )
        self.publish_btn.connect("clicked", self._on_publish)
        toolbar.append(self.publish_btn)
        # ── CRDT совместное редактирование (опция) ─────────────
        self.crdt_btn = Gtk.ToggleButton(
            label="CRDT",
            tooltip_text="Совместное редактирование (CRDT LWW/RGA) — синхронизация через файл .crdt.json + сеть (заглушка)",
        )
        try:
            self.crdt_btn.set_active(bool(self.settings.get("crdt_enabled", False)))
        except Exception:
            pass
        self.crdt_btn.connect("toggled", self._on_crdt_toggled)
        toolbar.append(self.crdt_btn)
        # ── История версий (git log / diff / restore) ────────────
        self.history_btn = Gtk.ToggleButton(
            icon_name="document-open-recent-symbolic",
            tooltip_text="История версий — git log / diff / восстановление (показать/скрыть панель)",
            css_classes=["flat"],
        )
        try:
            self.history_btn.set_active(bool(self.settings.get("history_panel_visible", False)))
        except Exception:
            pass
        self.history_btn.connect("toggled", self._on_history_toggled)
        toolbar.append(self.history_btn)
        # ── Сниппеты trigger->expansion ──────────────────────────
        self.snippets_btn = Gtk.Button(
            icon_name="text-x-generic-symbolic",
            tooltip_text="Сниппеты — палитра (trigger → expansion), Tab/Space для автозамены",
            css_classes=["flat"],
        )
        self.snippets_btn.connect("clicked", self._on_snippets_palette)
        toolbar.append(self.snippets_btn)
        # ── Mermaid Live — split editor/preview ────────────────
        self.mermaid_live_btn = Gtk.Button(
            label="🧜 Mermaid Live",
            tooltip_text="Mermaid Live — split view: слева редактор, справа live preview (300 мс debounce)",
            css_classes=["flat"],
        )
        self.mermaid_live_btn.connect("clicked", self._on_mermaid_live)
        toolbar.append(self.mermaid_live_btn)
        toolbar.append(switcher)
        # ── Vim-статус ──────────────────────────────────────────
        self._vim_status = Gtk.Label(label="", css_classes=["dim-hint", "vim-status"])
        self._vim_status.set_visible(False)
        toolbar.append(self._vim_status)
        self.save_btn = Gtk.Button(label="Сохранить", css_classes=["suggested-action"])
        self.save_btn.set_sensitive(False)
        self.save_btn.connect("clicked", self._on_save)
        toolbar.append(self.save_btn)
        right.append(toolbar)
        # ── Vim command line (под toolbar, над редактором) ─────
        self._vim_command_entry = Gtk.Entry(
            css_classes=["vim-command"],
            placeholder_text=":w :q :wq — Enter выполнить, Esc отменить",
        )
        self._vim_command_entry.set_visible(True)
        self._vim_revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._vim_revealer.set_child(self._vim_command_entry)
        self._vim_revealer.set_reveal_child(False)
        right.append(self._vim_revealer)

        self.buffer = Gtk.TextBuffer()
        # ── теги подсветки поиска (Gtk.TextBuffer tags, без GtkSourceView) ──
        try:
            bg = Gdk.RGBA()
            bg.parse("rgba(130,168,255,0.3)")
            fg = Gdk.RGBA()
            fg.parse("rgba(228,234,246,1)")
            tag = Gtk.TextTag.new("search_match")
            tag.set_property("background-rgba", bg)
            tag.set_property("foreground-rgba", fg)
            self.buffer.get_tag_table().add(tag)
            self._search_tag = tag
            bg_cur = Gdk.RGBA()
            bg_cur.parse("rgba(130,168,255,0.55)")
            fg_cur = Gdk.RGBA()
            fg_cur.parse("rgba(255,255,255,1)")
            cur_tag = Gtk.TextTag.new("search_match_current")
            cur_tag.set_property("background-rgba", bg_cur)
            cur_tag.set_property("foreground-rgba", fg_cur)
            # чтобы текущее вхождение выделялось сильнее — underline
            cur_tag.set_property("underline", Pango.Underline.SINGLE)
            self.buffer.get_tag_table().add(cur_tag)
            self._search_current_tag = cur_tag
        except Exception:
            # fallback: строки-цвета
            try:
                t = self.buffer.create_tag(
                    "search_match", background="rgba(130,168,255,0.3)", foreground="#e4eaf6"
                )
                self._search_tag = t
                ct = self.buffer.create_tag(
                    "search_match_current",
                    background="rgba(130,168,255,0.55)",
                    foreground="#ffffff",
                    underline=Pango.Underline.SINGLE,
                )
                self._search_current_tag = ct
            except Exception:
                pass
        self.editor = Gtk.TextView(
            buffer=self.buffer,
            wrap_mode=Gtk.WrapMode.WORD,
            hexpand=True,
            vexpand=True,
            css_classes=["editor"],
            top_margin=14,
            bottom_margin=16,
            left_margin=20,
            right_margin=20,
        )
        self.buffer.connect("changed", self._on_changed)
        ed_scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True, css_classes=["editor-frame"])
        ed_scroller.set_child(self.editor)
        self.preview = HtmlPreview()
        try:
            self.preview_html = self.preview
        except Exception:
            pass
        ed_clamp = _content_clamp(ed_scroller)
        pv_clamp = _content_clamp(self.preview)
        self.stack.add_named(ed_clamp, "Редактор")
        self.stack.add_named(pv_clamp, "Просмотр")
        self.stack.set_visible_child_name("Редактор")
        self.stack.connect("notify::visible-child-name", self._on_switch)
        # ── Outline + редактор (горизонтальная зона под toolbar) ──
        editor_area = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8, hexpand=True, vexpand=True
        )
        editor_area.append(self.stack)
        self._build_outline_panel(editor_area)
        try:
            self.outline_panel.set_visible(False)
        except Exception:
            pass
        self._build_comments_gutter(editor_area)
        try:
            if getattr(self, "comments_gutter", None) is not None:
                self.comments_gutter.set_visible(False)
        except Exception:
            pass
        right.append(editor_area)
        # ── Авто-теги LLM (accept/reject бар) ────────────────
        self._build_auto_tag_bar(right)
        self._build_links_panel(right)
        self._build_tags_panel(right)
        try:
            self._tags_panel.set_visible(False)
        except Exception:
            pass
        self._build_history_panel(right)
        try:
            if hasattr(self, "_history_panel"):
                self._history_panel.set_visible(False)
        except Exception:
            pass
        split.append(right)
        self.append(split)
        attach_zoom_keys(self)
        save_key = Gtk.EventControllerKey.new()
        save_key.connect("key-pressed", self._on_key_save)
        self.add_controller(save_key)
        # Ctrl+D в редакторе — перехватываем до дефолтного delete-forward-char
        ed_key = Gtk.EventControllerKey.new()
        ed_key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        ed_key.connect("key-pressed", self._on_editor_key)
        self.editor.add_controller(ed_key)
        # ── Vim-режим ───────────────────────────────────────────
        self._vim: object | None = None
        self._vim_key_ctrl = None
        self._init_vim_mode()
        # ── Smart links [[wikilink]] ────────────────────────────────
        self._smart_links: object | None = None
        if SmartLinks is not None:
            try:
                self._smart_links = SmartLinks(self.editor, self.buffer, self._vault)  # type: ignore[arg-type]
            except Exception:
                self._smart_links = None
        self.reload()

    # ── Связи: исходящие и бэклинки ──────────────────────────
    def _build_links_panel(self, right: Gtk.Box) -> None:
        """Секция 'Связи' под редактором: Исходящие и Входящие/бэклинки."""
        self._links_panel = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["links-panel"]
        )
        title = Gtk.Label(label="Связи", halign=Gtk.Align.START, css_classes=["links-title"])
        self._links_panel.append(title)
        # Исходящие
        out_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        out_hdr.append(
            Gtk.Label(label="Исходящие", css_classes=["links-subtitle"], halign=Gtk.Align.START)
        )
        self._outgoing_count = Gtk.Label(label="", css_classes=["dim-hint"])
        out_hdr.append(self._outgoing_count)
        self._links_panel.append(out_hdr)
        # NOTE: FlowBox не виртуализован (все чипы — живые виджеты).
        # Замена на Gtk.ListView/Gtk.GridView требует кастомной layout-выравнивания
        # и потери естественного flow-переноса. Оценка: сложно, риск регрессии UX.
        # Митигация: homogeneous + max_children_per_line + жёсткий лимит 100 чипов
        # (см. _refresh_links — slice + счётчик "+N ещё").
        self._outgoing_flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            column_spacing=6,
            row_spacing=6,
            max_children_per_line=12,
            homogeneous=True,
            css_classes=["links-flow"],
        )
        self._outgoing_flow.set_halign(Gtk.Align.START)
        self._links_panel.append(self._outgoing_flow)
        self._outgoing_empty = Gtk.Label(label="— нет исходящих ссылок —", css_classes=["dim-hint"])
        self._links_panel.append(self._outgoing_empty)
        # Входящие / Бэклинки
        in_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        in_hdr.append(
            Gtk.Label(
                label="Входящие / Бэклинки", css_classes=["links-subtitle"], halign=Gtk.Align.START
            )
        )
        self._incoming_count = Gtk.Label(label="", css_classes=["dim-hint"])
        in_hdr.append(self._incoming_count)
        self._links_panel.append(in_hdr)
        self._incoming_flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            column_spacing=6,
            row_spacing=6,
            max_children_per_line=12,
            homogeneous=True,
            css_classes=["links-flow"],
        )
        self._incoming_flow.set_halign(Gtk.Align.START)
        self._links_panel.append(self._incoming_flow)
        self._incoming_empty = Gtk.Label(label="— нет входящих ссылок —", css_classes=["dim-hint"])
        self._links_panel.append(self._incoming_empty)
        # панель не растягивается, остаётся под редактором
        self._links_panel.set_visible(False)
        right.append(self._links_panel)

    # ── Теги: FlowBox с чипами ───────────────────────────────
    def _build_tags_panel(self, right: Gtk.Box) -> None:
        """Секция 'Теги' рядом с 'Связи' под редактором: чипы с счётчиками."""
        self._tags_panel = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["tags-panel"]
        )
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title_row.append(
            Gtk.Label(label="Теги", halign=Gtk.Align.START, css_classes=["tags-title"])
        )
        self._tags_count = Gtk.Label(label="", css_classes=["dim-hint"])
        title_row.append(self._tags_count)
        # кнопка сброса фильтра
        self._tags_clear = Gtk.Button(
            label="✕", css_classes=["flat", "tag-clear"], tooltip_text="Сбросить фильтр по тегу"
        )
        self._tags_clear.connect("clicked", lambda *_: self._clear_tag_filter())
        self._tags_clear.set_visible(False)
        title_row.append(self._tags_clear)
        self._tags_panel.append(title_row)
        # NOTE: FlowBox для тегов не виртуализован — замена на ListView/GridView
        # требует flow-layout (перенос по строкам). Оставлен FlowBox с
        # homogeneous + max_children_per_line + лимит 100 (см. _refresh_tags).
        self._tags_flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            column_spacing=6,
            row_spacing=6,
            max_children_per_line=14,
            homogeneous=True,
            css_classes=["tags-flow"],
        )
        self._tags_flow.set_halign(Gtk.Align.START)
        self._tags_panel.append(self._tags_flow)
        self._tags_empty = Gtk.Label(label="— нет тегов —", css_classes=["dim-hint"])
        self._tags_panel.append(self._tags_empty)
        # панель видна всегда (даже без открытого файла) — показывает глобальные теги
        right.append(self._tags_panel)

    # ── Auto-tagging LLM: accept/reject бар ──────────────────
    def _build_auto_tag_bar(self, right: Gtk.Box) -> None:
        """Бар предложенных LLM тегов: #теги + Принять/Отклонить. Хранение — frontmatter."""
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["auto-tag-bar"]
        )
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        self._auto_tag_label = Gtk.Label(
            label="Предложенные теги:",
            css_classes=["dim-hint", "auto-tag-title"],
            halign=Gtk.Align.START,
        )
        box.append(self._auto_tag_label)
        self._auto_tag_chips = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            column_spacing=6,
            row_spacing=6,
            max_children_per_line=10,
            homogeneous=False,
            css_classes=["auto-tag-flow"],
            hexpand=True,
            halign=Gtk.Align.START,
        )
        box.append(self._auto_tag_chips)
        accept = Gtk.Button(
            label="Принять",
            css_classes=["suggested-action", "pill"],
            tooltip_text="Добавить предложенные теги в frontmatter",
        )
        accept.connect("clicked", self._on_auto_tag_accept)
        box.append(accept)
        self._auto_accept_btn = accept
        reject = Gtk.Button(
            label="Отклонить",
            css_classes=["flat", "pill"],
            tooltip_text="Отклонить предложенные теги",
        )
        reject.connect("clicked", self._on_auto_tag_reject)
        box.append(reject)
        self._auto_reject_btn = reject
        revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_child(box)
        revealer.set_reveal_child(False)
        revealer.set_visible(True)
        self._auto_tag_bar = revealer
        self._auto_tag_box = box
        right.append(revealer)

    def _show_auto_tag_bar(self, tags: list[str]) -> None:
        if not tags or self._auto_tag_bar is None or self._auto_tag_chips is None:
            return
        self._auto_suggested = list(tags)
        # очистить чипы
        while (ch := self._auto_tag_chips.get_first_child()) is not None:
            self._auto_tag_chips.remove(ch)
        for t in tags:
            lbl = Gtk.Label(label=f"#{t}", css_classes=["tag-chip", "auto-tag-chip"])
            # FlowBox требует дочерний виджет — оборачиваем в Box
            chip_box = Gtk.Box(css_classes=["chip-wrap"])
            chip_box.append(lbl)
            self._auto_tag_chips.append(chip_box)
        self._auto_tag_bar.set_reveal_child(True)

    def _hide_auto_tag_bar(self) -> None:
        self._auto_suggested = None
        if self._auto_tag_bar is not None:
            try:
                self._auto_tag_bar.set_reveal_child(False)
            except Exception:
                pass

    def _on_auto_tag_accept(self, _btn=None) -> None:
        if not self._auto_suggested or self._current is None:
            self._hide_auto_tag_bar()
            return
        tags = list(self._auto_suggested)
        target = Path(self._current)
        self._hide_auto_tag_bar()

        # применить в фоне чтобы не блокировать UI (диск + инвалидация кэша)
        def work() -> None:
            ok: bool = False
            err: str | None = None
            try:
                if _auto_tagger is not None:
                    svc = _auto_tagger.AutoTaggerService(self.settings)
                    ok, err = svc.apply_tags(target, tags)
                else:
                    # fallback напрямую через vault
                    from ..vault import parse_frontmatter, serialize_frontmatter

                    raw = target.read_text(encoding="utf-8")
                    fm, body = parse_frontmatter(raw)
                    existing = fm.get("tags") if isinstance(fm.get("tags"), list) else []
                    merged = list(existing) if isinstance(existing, list) else []
                    seen = {str(x).lower() for x in merged}
                    for t in tags:
                        if t.lower() not in seen:
                            merged.append(t)
                            seen.add(t.lower())
                    fm["tags"] = merged
                    target.write_text(
                        serialize_frontmatter(fm) + body.lstrip("\n"), encoding="utf-8"
                    )
                    ok = True
            except Exception as exc:  # noqa: BLE001
                ok = False
                err = str(exc)
            GLib.idle_add(lambda: self._on_auto_tag_applied(ok, err, tags, target))

        import threading

        threading.Thread(target=work, daemon=True).start()

    def _on_auto_tag_applied(
        self, ok: bool, err: str | None, tags: list[str], target: Path
    ) -> bool:
        if not self._alive:
            return False
        if ok:
            try:
                self._read_cache.pop(str(target), None)
            except Exception:
                pass
            try:
                self._vault.invalidate_cache()
            except Exception:
                pass
            # обновить buffer frontmatter если открыт тот же файл — вставить tags в видимый текст
            if self._current is not None and Path(self._current) == target:
                try:
                    # перечитать файл и обновить буфер без маркировки dirty
                    new_text = target.read_text(encoding="utf-8")
                    self._loading = True
                    self.buffer.set_text(new_text)
                    self._loading = False
                    self._dirty = False
                    self.save_btn.set_sensitive(False)
                    if self.stack.get_visible_child_name() == "Просмотр":
                        self._render_preview()
                    try:
                        self._refresh_outline()
                    except Exception:
                        pass
                except Exception:
                    pass
            self.file_label.set_text(f"теги добавлены: {', '.join('#' + t for t in tags)}")
            try:
                self._refresh_tags()
            except Exception:
                pass
            try:
                self._refresh_links()
            except Exception:
                pass
            # toast если есть overlay
            try:
                root = self.get_root()
                overlay = getattr(root, "toast_overlay", None) if root is not None else None
                if overlay is not None:
                    toast = Adw.Toast.new(f"Теги добавлены: {', '.join('#' + t for t in tags)}")
                    toast.set_timeout(3)
                    overlay.add_toast(toast)
            except Exception:
                pass
        else:
            self.file_label.set_text(f"не удалось добавить теги: {err or 'ошибка'}")
        return False

    def _on_auto_tag_reject(self, _btn=None) -> None:
        self._hide_auto_tag_bar()
        if self._current is not None:
            self.file_label.set_text("предложенные теги отклонены")

    def _trigger_auto_tag(self) -> None:
        """После сохранения — асинхронно предложить теги через LLM."""
        if self._current is None:
            return
        if self._is_encrypted_path(self._current):
            return
        # не спамим если текст короткий
        try:
            start, end = self.buffer.get_bounds()
            text = self.buffer.get_text(start, end, True)
        except Exception:
            return
        if not text or len(text.strip()) < 40:
            return
        # скрыть предыдущий бар
        self._hide_auto_tag_bar()
        target = Path(self._current)
        snapshot = text
        settings_copy = dict(self.settings) if isinstance(self.settings, dict) else {}

        def work() -> None:
            try:
                if _auto_tagger is None:
                    return
                svc = _auto_tagger.AutoTaggerService(settings_copy)
                # существующие теги из файла (не из buffer — frontmatter уже на диске)
                existing = svc.get_existing_tags(target)
                result = svc.suggest(snapshot, existing_tags=existing)
                GLib.idle_add(lambda: self._on_auto_tag_result(result, target, snapshot))
            except Exception:
                pass

        import threading

        threading.Thread(target=work, daemon=True).start()

    def _on_auto_tag_result(self, result, target: Path, snapshot: str) -> bool:
        if not self._alive or self._current is None or Path(self._current) != target:
            return False
        if not getattr(result, "ok", False) or not getattr(result, "tags", None):
            return False
        tags: list[str] = list(result.tags) if isinstance(result.tags, list) else []
        if not tags:
            return False
        # фильтр дублей с текущим frontmatter (если пользователь успел добавить вручную)
        try:
            if _auto_tagger is not None:
                svc = _auto_tagger.AutoTaggerService(self.settings)
                existing = {t.lower() for t in svc.get_existing_tags(target)}
                tags = [t for t in tags if t.lower() not in existing]
        except Exception:
            pass
        if not tags:
            return False
        # кламп 3-5 уже в сервисе, но на всякий — режем
        if len(tags) > 5:
            tags = tags[:5]
        # если <3 но LLM дал 1-2 — показываем всё равно (эвристика офлайна)
        self._show_auto_tag_bar(tags)
        return False

    # ── Outline: оглавление markdown ──────────────────────────
    def _build_outline_panel(self, parent: Gtk.Box) -> None:
        """Правая панель Outline рядом с редактором (под toolbar).

        Gtk.ListBox с заголовками, клик скроллит редактор к строке
        через TextView scroll_to_iter. Уровни различаются отступом
        (level-1)*12px и font-size (через CSS-классы).
        """
        self.outline_panel = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["outline-panel"]
        )
        self.outline_panel.set_size_request(220, -1)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.append(
            Gtk.Label(label="Оглавление", halign=Gtk.Align.START, css_classes=["outline-title"])
        )
        self.outline_count = Gtk.Label(label="", css_classes=["dim-hint"])
        header.append(self.outline_count)
        self.outline_panel.append(header)

        self.outline_list = Gtk.ListBox(css_classes=["outline-list"])
        self.outline_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.outline_list.connect("row-activated", self._on_outline_activated)
        scroller = Gtk.ScrolledWindow(vexpand=True, css_classes=["outline-scroller"])
        scroller.set_child(self.outline_list)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        try:
            scroller.set_propagate_natural_width(False)
        except AttributeError:
            pass
        self.outline_panel.append(scroller)
        self.outline_empty = Gtk.Label(
            label="— нет заголовков —", css_classes=["dim-hint", "outline-empty"]
        )
        self.outline_panel.append(self.outline_empty)
        parent.append(self.outline_panel)
        self._refresh_outline()

    def _refresh_outline(self) -> None:
        """Перерисовать Outline по текущему содержимому буфера."""
        if not hasattr(self, "outline_list"):
            return
        # очистить список
        while (child := self.outline_list.get_first_child()) is not None:
            self.outline_list.remove(child)
        # парсинг заголовков
        try:
            from .markdown import parse_headings
        except Exception:
            parse_headings = None  # type: ignore
        text = ""
        try:
            start, end = self.buffer.get_bounds()
            text = self.buffer.get_text(start, end, True)
        except Exception:
            text = ""
        headings: list[tuple[int, str, int]] = []
        if parse_headings is not None and text is not None:
            try:
                headings = parse_headings(text)
            except Exception:
                headings = []
        self._outline_headings = headings
        # заполняем ListBox
        for level, title, line in headings:
            row = Gtk.ListBoxRow(css_classes=["outline-row", f"outline-level-{level}"])
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            box.set_margin_start((level - 1) * 12)
            label = Gtk.Label(
                label=title,
                halign=Gtk.Align.START,
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
                css_classes=["outline-label"],
                hexpand=True,
            )
            label.set_tooltip_text(title)
            box.append(label)
            row.set_child(box)
            # сохраняем номер строки для скролла
            row._outline_line = line  # type: ignore[attr-defined]
            self.outline_list.append(row)
        has = bool(headings)
        self.outline_list.set_visible(has)
        self.outline_empty.set_visible(not has)
        if hasattr(self, "outline_count"):
            self.outline_count.set_text(f"· {len(headings)}" if has else "")

    def _schedule_outline_update(self) -> None:
        """Debounce 300 мс для обновления Outline при изменении буфера."""
        if self._outline_timer is not None:
            try:
                GLib.source_remove(self._outline_timer)
            except Exception:
                pass
            self._outline_timer = None
        self._outline_timer = GLib.timeout_add(300, self._do_outline_update)

    def _do_outline_update(self) -> bool:
        self._outline_timer = None
        self._refresh_outline()
        return False  # GLib.SOURCE_REMOVE

    def _on_outline_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        """Клик по заголовку — скроллит редактор к строке."""
        line = getattr(row, "_outline_line", None)
        if line is None:
            return
        try:
            it = self.buffer.get_iter_at_line(int(line))
            # scroll_to_iter + поместить курсор
            self.editor.scroll_to_iter(it, 0.0, False, 0, 0)
            self.buffer.place_cursor(it)
            self.editor.grab_focus()
        except Exception:
            pass

    # ── Комментарии к строкам (gutter справа) ─────────────────────
    def _build_comments_gutter(self, parent: Gtk.Box) -> None:
        """Gutter справа от редактора — CommentsGutter с кликом по номеру строки."""
        self.comments_gutter: Any | None = None
        if CommentsGutter is None:
            return
        try:
            gutter = CommentsGutter(self.settings, parent_view=self)
        except Exception:
            return
        self.comments_gutter = gutter
        parent.append(gutter)
        # сместить outline чтобы gutter был последним справа
        try:
            # если нужно — порядок уже: stack, outline, gutter
            pass
        except Exception:
            pass

    def _refresh_comments(self) -> None:
        """Обновить gutter комментариев для текущего файла."""
        gutter = getattr(self, "comments_gutter", None)
        if gutter is None:
            return
        try:
            if self._current is None:
                gutter.set_file(None)
            else:
                gutter.set_file(self._current)
        except Exception:
            pass

    def _clear_flow(self, flow: Gtk.FlowBox) -> None:
        while (child := flow.get_first_child()) is not None:
            flow.remove(child)

    def _make_tag_chip(self, tag: str, count: int) -> Gtk.Widget:
        # Obsidian-стиль: #tag · count
        label = f"#{tag}  · {count}" if count > 1 else f"#{tag}"
        is_active = self._selected_tag is not None and self._selected_tag.lower() == tag.lower()
        classes = ["tag-chip", "tag-chip--active"] if is_active else ["tag-chip"]
        btn = Gtk.Button(label=label, css_classes=classes)
        btn.set_tooltip_text(f"#{tag} — {count} файлов · клик для фильтра")
        btn.connect("clicked", lambda *_: self._on_tag_clicked(tag))
        return btn

    def _on_tag_clicked(self, tag: str) -> None:
        """Клик по чипу — фильтрует file-list, показывает файлы с тегом."""
        if self._selected_tag is not None and self._selected_tag.lower() == tag.lower():
            self._clear_tag_filter()
            return
        self._selected_tag = tag
        self.filter_entry.set_text(f"#{tag}")
        # _on_filter_changed вызовет _render_list и обновит чип-активность
        self._refresh_tags()

    def _clear_tag_filter(self) -> None:
        self._selected_tag = None
        cur = self.filter_entry.get_text()
        if cur.startswith("#"):
            self.filter_entry.set_text("")
        else:
            self._render_list()
            self._refresh_tags()

    def _refresh_tags(self) -> None:
        """Перерисовать панель Теги: популярные теги vault с счётчиками.

        FlowBox не виртуализован — лимит 100 чипов (де-факто 30 из get_popular_tags).
        """
        try:
            tags = self._vault.scan_tags()
            popular = self._vault.get_popular_tags(limit=30)
        except Exception:
            tags, popular = {}, []
        # виртуализация-заглушка: лимит 100 (FlowBox создаёт виджеты для всех детей)
        if len(popular) > 100:
            popular = popular[:100]
        self._clear_flow(self._tags_flow)
        total = len(tags)
        self._tags_count.set_text(f"· {total}" if total else "")
        has = bool(popular)
        self._tags_empty.set_visible(not has)
        self._tags_flow.set_visible(has)
        self._tags_clear.set_visible(self._selected_tag is not None)
        for tag, count in popular:
            self._tags_flow.append(self._make_tag_chip(tag, count))
        # если срезали — показать hint в счётчике
        if len(popular) == 100 and total > 100:
            self._tags_count.set_text(f"· {total} · показаны 100")

    def _make_link_chip(self, path: Path) -> Gtk.Widget:
        title = self._vault.note_title_cached(Path(path)) or Path(path).stem
        btn = Gtk.Button(label=title, css_classes=["link-chip"])
        btn.set_tooltip_text(str(path))
        # клик открывает файл
        btn.connect("clicked", lambda *_: self._open(Path(path)))
        # FlowBox требует обёртку FlowBoxChild, но append сам оборачивает
        return btn

    def _refresh_links(self) -> None:
        """Перерисовать секцию Связи для текущего файла (кэш граф)."""
        if self._current is None:
            self._links_panel.set_visible(False)
            return
        self._links_panel.set_visible(True)
        try:
            outgoing = self._vault.get_outgoing_links(self._current)
            incoming = self._vault.get_backlinks(self._current)
        except Exception:
            outgoing, incoming = [], []
        # исходящие — лимит 100 (FlowBox не виртуализован)
        _FLOW_CAP = 100
        self._clear_flow(self._outgoing_flow)
        out_capped = len(outgoing) > _FLOW_CAP
        out_shown = sorted(outgoing, key=lambda pp: pp.stem.lower())[:_FLOW_CAP]
        extra_out = len(outgoing) - len(out_shown) if out_capped else 0
        lbl_out = f"· {len(outgoing)}" + (
            f" · +{extra_out} скрыто (лимит {_FLOW_CAP})" if out_capped else ""
        )
        self._outgoing_count.set_text(lbl_out if outgoing else "")
        self._outgoing_empty.set_visible(not bool(outgoing))
        self._outgoing_flow.set_visible(bool(outgoing))
        for p in out_shown:
            self._outgoing_flow.append(self._make_link_chip(p))
        # входящие — лимит 100 (FlowBox не виртуализован)
        self._clear_flow(self._incoming_flow)
        in_capped = len(incoming) > _FLOW_CAP
        in_shown = sorted(incoming, key=lambda pp: pp.stem.lower())[:_FLOW_CAP]
        extra_in = len(incoming) - len(in_shown) if in_capped else 0
        lbl_in = f"· {len(incoming)}" + (
            f" · +{extra_in} скрыто (лимит {_FLOW_CAP})" if in_capped else ""
        )
        self._incoming_count.set_text(lbl_in if incoming else "")
        self._incoming_empty.set_visible(not bool(incoming))
        self._incoming_flow.set_visible(bool(incoming))
        for p in in_shown:
            self._incoming_flow.append(self._make_link_chip(p))

    # ── История версий (git history UI) ────────────────────────
    def _build_history_panel(self, right: Gtk.Box) -> None:
        """Панель истории git под Тегами: HistoryPanel + Revealer."""
        self.history_panel: Gtk.Widget | None = None
        self._history_revealer: Gtk.Revealer | None = None
        try:
            from .history_view import HistoryPanel
        except Exception:
            HistoryPanel = None  # type: ignore
        if HistoryPanel is None:
            return
        try:
            panel = HistoryPanel(self.settings, parent_view=self)
        except Exception:
            return
        self.history_panel = panel
        # Revealer для сворачивания по кнопке в toolbar
        revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_child(panel)
        try:
            visible = bool(self.settings.get("history_panel_visible", False))
        except Exception:
            visible = False
        revealer.set_reveal_child(visible)
        self._history_revealer = revealer
        right.append(revealer)

    def _on_history_toggled(self, btn: Gtk.ToggleButton) -> None:
        visible = bool(btn.get_active())
        try:
            self.settings["history_panel_visible"] = visible
            from ..config import save_settings as _save

            _save(self.settings)
        except Exception:
            pass
        if getattr(self, "_history_revealer", None) is not None:
            try:
                self._history_revealer.set_reveal_child(visible)
            except Exception:
                pass
        # при открытии — сразу обновить историю для текущего файла
        if (
            visible
            and self._current is not None
            and getattr(self, "history_panel", None) is not None
        ):
            try:
                self.history_panel.set_file(self._current)  # type: ignore[union-attr]
            except Exception:
                pass

    def _refresh_history(self) -> None:
        """Обновить панель истории для текущего файла (вызывается из _open/_save)."""
        panel = getattr(self, "history_panel", None)
        if panel is None:
            return
        try:
            if self._current is None:
                panel.set_file(None)
            else:
                # если панель скрыта — не дергаем git, но запомним файл; при раскрытии _on_history_toggled обновит
                revealer = getattr(self, "_history_revealer", None)
                is_visible = True
                try:
                    is_visible = bool(revealer.get_reveal_child()) if revealer is not None else True
                except Exception:
                    is_visible = True
                if is_visible:
                    panel.set_file(self._current)
                else:
                    # лениво: просто сбросить внутренние _current без запроса git
                    # panel.set_file лениво бы запросил git — откладываем
                    # но нужно чтобы при следующем показе был актуальный файл:
                    # сохраняем путь в панели без загрузки коммитов
                    try:
                        panel._current = self._current  # type: ignore[attr-defined]
                    except Exception:
                        pass
        except Exception:
            pass

    # ── Сниппеты trigger->expansion ────────────────────────────
    def _get_snippets(self) -> dict[str, str]:
        """Кэш сниппетов (инвалидируется при сохранении/палитре)."""
        if _snippets is None:
            return {}
        try:
            # если уже есть кэш и не пустой — вернуть
            if self._snippets_cache is not None:
                return self._snippets_cache
            data = _snippets.load_snippets(self.settings)
            self._snippets_cache = data
            return data
        except Exception:
            return {}

    def _invalidate_snippets_cache(self) -> None:
        self._snippets_cache = None

    def _try_expand_snippet(self, add_suffix: str = "") -> bool:
        """Автозамена: слово перед курсором == trigger -> expansion.

        add_suffix: дополнительный суффикс после вставки (например " " для Space-триггера).
        Возвращает True если замена выполнена (событие поглощено).
        """
        if _snippets is None or self._snippet_expanding:
            return False
        if self._current is not None and self._is_encrypted_path(self._current):
            return False
        try:
            buf = self.buffer
            insert = buf.get_insert()
            it = buf.get_iter_at_mark(insert)
            line_start = buf.get_iter_at_line(it.get_line())
            line_text = buf.get_text(line_start, it, True)
            # токен перед курсором — \S+ в конце
            import re as _re

            m = _re.search(r"(\S+)$", line_text)
            if not m:
                return False
            trigger = m.group(1)
            data = self._get_snippets()
            if trigger not in data:
                return False
            raw = data[trigger]
            title = self._current.stem if self._current is not None else ""
            try:
                expansion = _snippets.render_expansion(raw, title=title)
            except Exception:
                expansion = raw
            # удалить trigger и вставить expansion
            start_offset = it.get_offset() - len(trigger)
            if start_offset < 0:
                return False
            start = buf.get_iter_at_offset(start_offset)
            # защита от рекурсии changed->autosave
            self._snippet_expanding = True
            try:
                buf.delete(start, it)
                # вставляем и ставим курсор после
                insert_iter = buf.get_iter_at_offset(start_offset)
                buf.insert(insert_iter, expansion + add_suffix)
                # курсор уже после вставки — переместить явно
                new_iter = buf.get_iter_at_offset(start_offset + len(expansion + add_suffix))
                buf.place_cursor(new_iter)
                self.editor.scroll_to_iter(new_iter, 0.0, False, 0, 0)
            finally:
                self._snippet_expanding = False
            return True
        except Exception:
            try:
                self._snippet_expanding = False
            except Exception:
                pass
            return False

    def _insert_snippet_text(self, expansion: str) -> None:
        """Вставить expansion в позицию курсора (палитра)."""
        try:
            buf = self.buffer
            if self._current is None or self._is_encrypted_path(self._current):
                self.file_label.set_text("открой заметку для вставки сниппета")
                return
            title = self._current.stem if self._current else ""
            text = (
                _snippets.render_expansion(expansion, title=title)
                if _snippets is not None
                else expansion
            )
            insert = buf.get_insert()
            it = buf.get_iter_at_mark(insert)
            self._snippet_expanding = True
            try:
                buf.insert(it, text)
            finally:
                self._snippet_expanding = False
            self.editor.grab_focus()
        except Exception as exc:  # noqa: BLE001
            self.file_label.set_text(f"сниппет: {exc}")

    def _on_snippets_palette(self, _btn=None) -> None:
        """Палитра команд сниппетов: поиск + вставка + управление."""
        if _snippets is None:
            self.file_label.set_text("модуль snippets недоступен")
            return
        try:
            items = _snippets.get_palette_items(self.settings)
        except Exception:
            items = []
        # диалог-палитра (Gtk.Dialog / Adw.Dialog)
        try:
            dlg = Gtk.Dialog(title="Сниппеты — палитра")
            try:
                dlg.set_transient_for(self.get_root())  # type: ignore
            except Exception:
                pass
            dlg.set_default_size(520, 420)
            content = dlg.get_content_area()
            content.set_spacing(8)
            content.set_margin_top(12)
            content.set_margin_bottom(12)
            content.set_margin_start(12)
            content.set_margin_end(12)
            header = Gtk.Label(
                label="Триггер → вставка · Tab/Space в редакторе автозаменяет · Enter вставляет",
                css_classes=["dim-hint"],
                halign=Gtk.Align.START,
                wrap=True,
            )
            content.append(header)
            # поиск по палитре
            search = Gtk.SearchEntry(placeholder_text="Фильтр по триггеру/тексту…", hexpand=True)
            content.append(search)
            # список
            scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True, css_classes=["editor-frame"])
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            listbox = Gtk.ListBox(
                css_classes=["snippets-list"], selection_mode=Gtk.SelectionMode.SINGLE
            )
            scroller.set_child(listbox)
            content.append(scroller)
            # поле добавления нового сниппета
            add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            trig_entry = Gtk.Entry(placeholder_text="триггер (без пробелов)", hexpand=False)
            trig_entry.set_size_request(140, -1)
            exp_entry = Gtk.Entry(
                placeholder_text="развёртывание — поддерживает {{date}} {{time}} {{title}} {{uuid}}",
                hexpand=True,
            )
            add_btn = Gtk.Button(label="Добавить", css_classes=["suggested-action"])
            add_row.append(trig_entry)
            add_row.append(exp_entry)
            add_row.append(add_btn)
            content.append(add_row)
            hint = Gtk.Label(
                label="Подсказка: {{date:YYYY-MM-DD}} {{time:HH:mm}} — формат как в шаблонах",
                css_classes=["dim-hint"],
                halign=Gtk.Align.START,
            )
            content.append(hint)

            def _populate(filter_text: str = "") -> None:
                while (ch := listbox.get_first_child()) is not None:
                    listbox.remove(ch)
                q = filter_text.strip().lower()
                for it in items:
                    trig = it.get("trigger", "")
                    exp = it.get("expansion", "")
                    label = it.get("label", f"{trig} → {exp}")
                    if q and q not in trig.lower() and q not in exp.lower():
                        continue
                    row = Gtk.ListBoxRow(activatable=True, css_classes=["snippet-row"])
                    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, hexpand=True)
                    box.set_margin_top(4)
                    box.set_margin_bottom(4)
                    box.set_margin_start(6)
                    box.set_margin_end(6)
                    trig_lbl = Gtk.Label(
                        label=trig,
                        css_classes=["snippet-trigger"],
                        halign=Gtk.Align.START,
                        xalign=0,
                    )
                    trig_lbl.set_size_request(120, -1)
                    trig_lbl.set_ellipsize(Pango.EllipsizeMode.END)
                    exp_lbl = Gtk.Label(
                        label=exp.replace("\n", " ⏎ "),
                        halign=Gtk.Align.START,
                        xalign=0,
                        hexpand=True,
                        ellipsize=Pango.EllipsizeMode.END,
                    )
                    exp_lbl.set_tooltip_text(exp)
                    del_btn = Gtk.Button(
                        icon_name="user-trash-symbolic",
                        css_classes=["flat", "destructive-action"],
                        tooltip_text="Удалить сниппет",
                    )
                    # capture trig in closure
                    _t = trig
                    del_btn.connect("clicked", lambda _b, t=_t: _delete_snippet(t))
                    box.append(trig_lbl)
                    box.append(exp_lbl)
                    box.append(del_btn)
                    row.set_child(box)
                    row._trigger = trig  # type: ignore
                    row._expansion = exp  # type: ignore
                    listbox.append(row)
                if not items:
                    row = Gtk.ListBoxRow(activatable=False, selectable=False)
                    row.set_child(
                        Gtk.Label(
                            label="— нет сниппетов — добавьте первый", css_classes=["dim-hint"]
                        )
                    )
                    listbox.append(row)

            def _delete_snippet(trig: str) -> None:
                try:
                    _snippets.delete_snippet(self.settings, trig)
                    self._invalidate_snippets_cache()
                    # обновить локальный список
                    nonlocal items
                    items = _snippets.get_palette_items(self.settings)
                    _populate(search.get_text())
                    self.file_label.set_text(f"сниппет «{trig}» удалён")
                except Exception as exc:  # noqa: BLE001
                    self.file_label.set_text(f"удаление: {exc}")

            def _on_add(*_a) -> None:
                trig = trig_entry.get_text().strip()
                exp = exp_entry.get_text()
                if not trig:
                    trig_entry.grab_focus()
                    return
                if not exp:
                    exp_entry.grab_focus()
                    return
                try:
                    _snippets.add_snippet(self.settings, trig, exp)
                    self._invalidate_snippets_cache()
                    nonlocal items
                    items = _snippets.get_palette_items(self.settings)
                    trig_entry.set_text("")
                    exp_entry.set_text("")
                    _populate(search.get_text())
                    self.file_label.set_text(f"сниппет «{trig}» сохранён")
                except Exception as exc:  # noqa: BLE001
                    self.file_label.set_text(f"сниппет: {exc}")

            add_btn.connect("clicked", _on_add)
            exp_entry.connect("activate", _on_add)
            trig_entry.connect("activate", lambda *_: exp_entry.grab_focus())

            def _on_row_activated(_lb, row) -> None:
                trig = getattr(row, "_trigger", None)
                exp = getattr(row, "_expansion", None)
                if exp is None:
                    return
                dlg.close()
                self._insert_snippet_text(exp)
                self.file_label.set_text(f"сниппет «{trig}» вставлен")

            listbox.connect("row-activated", _on_row_activated)
            search.connect("search-changed", lambda *_: _populate(search.get_text()))
            # кнопки диалога
            dlg.add_button("Закрыть", Gtk.ResponseType.CLOSE)
            dlg.connect("response", lambda *_: dlg.close())
            _populate("")
            dlg.present()
            # фокус на поиск
            search.grab_focus()
            return
        except Exception as exc:  # noqa: BLE001
            # fallback: простой AlertDialog со списком
            try:
                body = (
                    "\n".join(f"{it['trigger']} → {it['expansion'][:40]}" for it in items[:12])
                    or "нет сниппетов"
                )
                ad = Adw.AlertDialog(heading="Сниппеты", body=body)
                ad.add_response("ok", "OK")
                ad.present(self)
            except Exception:
                self.file_label.set_text(f"палитра: {exc}")

    def _on_mermaid_live(self, _btn=None) -> None:
        """Открыть Mermaid Live — split editor/preview (300 мс debounce)."""
        try:
            start, end = self.buffer.get_bounds()
            text = self.buffer.get_text(start, end, True)
        except Exception:
            text = ""
        # вытащить mermaid-код из текущего файла/буфера
        code = ""
        try:
            from .mermaid import extract_mermaid_blocks, has_mermaid, sanitize_mermaid_code

            if has_mermaid(text):
                blocks = extract_mermaid_blocks(text)
                code = blocks[0] if blocks else text
            elif text.strip():
                # если открыт .mmd или просто mermaid — берём как есть
                low = text.strip().lower()
                if any(
                    k in low
                    for k in (
                        "graph",
                        "sequencediagram",
                        "classdiagram",
                        "statediagram",
                        "gantt",
                        "pie",
                        "erdiagram",
                        "mindmap",
                        "flowchart",
                    )
                ):
                    code = text
                else:
                    # нет mermaid — пример
                    code = text if len(text.strip()) < 400 else ""
            if not code.strip():
                code = "graph TD\n    A[Вставьте диаграмму] --> B[Preview справа 300 мс]"
            code = sanitize_mermaid_code(code)
        except Exception:
            code = text or "graph TD\n    A[Вставьте диаграмму] --> B[Preview]"

        # 1) пробуем отдельную вкладку mermaid_live в главном окне
        try:
            root = self.get_root()
            if root is not None and hasattr(root, "_on_nav") and hasattr(root, "_ensure_view"):
                # перейти на вкладку
                try:
                    root._on_nav("mermaid_live")  # type: ignore[attr-defined]
                    view = root._ensure_view("mermaid_live")  # type: ignore[attr-defined]
                    if hasattr(view, "set_code"):
                        view.set_code(code)  # type: ignore[attr-defined]
                    if hasattr(root, "toast_overlay"):
                        try:
                            t = Adw.Toast.new("Mermaid Live — код из редактора загружен")
                            t.set_timeout(2)
                            root.toast_overlay.add_toast(t)  # type: ignore
                        except Exception:
                            pass
                    return
                except Exception:
                    pass
        except Exception:
            pass

        # 2) fallback — диалог Adw.Dialog с live view
        try:
            from .mermaid_live import create_dialog

            dlg = create_dialog(self, self.settings, initial_code=code)
            if dlg is not None:
                return
        except Exception as e:
            log = __import__("logging").getLogger(__name__)
            log.debug("mermaid_live dialog failed: %s", e)

        # 3) fallback — простой диалог с копированием
        try:
            ad = Adw.AlertDialog(
                heading="Mermaid Live",
                body=f"Код скопирован в буфер. Открой вкладку 🧜 Mermaid Live.\n\n{code[:400]}",
            )
            ad.add_response("ok", "OK")
            ad.present(self)
            # copy to clipboard
            try:
                disp = Gdk.Display.get_default()
                if disp is not None:
                    disp.get_clipboard().set(code)
            except Exception:
                pass
        except Exception:
            try:
                self.file_label.set_text("Mermaid Live — установите WebKitGTK для preview")
            except Exception:
                pass

    def focus_filter(self) -> bool:
        """Фокус на глобальный поиск (filter_entry) — для Ctrl+Shift+F."""
        self.filter_entry.grab_focus()
        # выделить существующий текст для быстрого переопределения
        try:
            self.filter_entry.select_region(0, -1)
        except Exception:  # noqa: BLE001
            pass
        return True

    def duplicate_line(self) -> bool:
        """Ctrl+D — дублировать текущую строку под курсором (Obsidian/CodeMirror)."""
        if self._current is None:
            return False
        # только когда открыт редактор (не preview); но для удобства — всегда
        buf = self.buffer
        insert = buf.get_insert()
        it = buf.get_iter_at_mark(insert)
        line = it.get_line()
        line_offset = it.get_line_offset()
        start = buf.get_iter_at_line(line)
        # конец строки — начало следующей или конец буфера
        if line + 1 < buf.get_line_count():
            end = buf.get_iter_at_line(line + 1)
            # end включает \n следующей строки? get_iter_at_line(line+1) — после \n
            # текст строки включая \n
            text = buf.get_text(start, end, True)
            # вставляем перед началом следующей строки — уже отделено \n
            # если text уже оканчивается \n, дублирование даст корректный перенос
            buf.insert(end, text)
        else:
            end = buf.get_end_iter()
            text = buf.get_text(start, end, True)
            # последняя строка без \n — добавляем перенос
            if text:
                buf.insert(end, "\n" + text)
            else:
                # пустая последняя строка — просто \n
                buf.insert(end, "\n")
        # вернуть курсор на дублированную строку, тот же столбец
        new_line = line + 1
        if new_line < buf.get_line_count():
            nit = buf.get_iter_at_line(new_line)
            # clamp offset
            line_end = (
                buf.get_iter_at_line(new_line + 1)
                if new_line + 1 < buf.get_line_count()
                else buf.get_end_iter()
            )
            line_text = buf.get_text(nit, line_end, True)
            # длина без \n
            line_len = len(line_text.rstrip("\n"))
            col = min(line_offset, line_len)
            nit.set_line_offset(col) if hasattr(nit, "set_line_offset") else None  # pyright
            # fallback через forward_chars если нет set_line_offset
            try:
                nit = buf.get_iter_at_line_offset(new_line, col)
            except Exception:  # noqa: BLE001
                pass
            buf.place_cursor(nit)
            self.editor.scroll_to_iter(nit, 0.0, False, 0, 0)
        return True

    def _on_editor_key(self, _ctrl, keyval, _keycode, state) -> bool:
        # ── сниппеты: Tab автозамена trigger -> expansion ──────────
        if not (state & Gdk.ModifierType.CONTROL_MASK) and not (state & Gdk.ModifierType.ALT_MASK):
            if keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab, Gdk.KEY_KP_Tab):
                if self._try_expand_snippet():
                    return True
            if keyval == Gdk.KEY_space and not (state & Gdk.ModifierType.SHIFT_MASK):
                # Space как триггер автозамены: "omw " -> "On my way! "
                if self._try_expand_snippet(add_suffix=" "):
                    return True
        if state & Gdk.ModifierType.CONTROL_MASK and keyval in (Gdk.KEY_d, Gdk.KEY_D):
            # Shift не должен быть нажат
            if not (state & Gdk.ModifierType.SHIFT_MASK):
                return self.duplicate_line()
        # навигация по вхождениям поиска
        if state & Gdk.ModifierType.CONTROL_MASK and keyval in (Gdk.KEY_g, Gdk.KEY_G):
            if state & Gdk.ModifierType.SHIFT_MASK:
                self._search_prev()
            else:
                self._search_next()
            return True
        return False

    def _on_key_save(self, _ctrl, keyval, _keycode, state) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if ctrl and keyval in (Gdk.KEY_s, Gdk.KEY_S) and not shift:
            self._on_save(None)
            return True
        if ctrl and keyval in (Gdk.KEY_d, Gdk.KEY_D) and not shift:
            return self.duplicate_line()
        if ctrl and keyval in (Gdk.KEY_g, Gdk.KEY_G):
            if shift:
                self._search_prev()
            else:
                self._search_next()
            return True
        return False

    # ── Vim-режим ────────────────────────────────────────────
    def _init_vim_mode(self) -> None:
        if VimController is None:
            return
        try:
            self._vim = VimController(
                editor=self.editor,
                buffer=self.buffer,
                on_save=lambda: self._on_save(None)
                if getattr(self, "_current", None) and getattr(self, "_dirty", False)
                else None,
                on_quit=self._vim_quit,
                status_label=getattr(self, "_vim_status", None),
                command_entry=getattr(self, "_vim_command_entry", None),
                command_revealer=getattr(self, "_vim_revealer", None),
            )
            enabled = bool(
                self.settings.get("vim_mode", False) or self.settings.get("vim_enabled", False)
            )
            self._vim.set_enabled(enabled)
            # key controller с высшим приоритетом (CAPTURE)
            vim_key = Gtk.EventControllerKey.new()
            vim_key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            vim_key.connect("key-pressed", self._on_vim_key)
            self.editor.add_controller(vim_key)
            self._vim_key_ctrl = vim_key
            # command entry activate уже внутри VimController, но дополнительно свяжем
            try:
                self._vim_command_entry.connect(
                    "activate",
                    lambda *_: self._vim
                    and self._vim._on_command_activate(self._vim_command_entry),
                )
            except Exception:
                pass
        except Exception:
            self._vim = None

    def _on_vim_key(self, _ctrl, keyval, keycode, state) -> bool:
        if getattr(self, "_vim", None) is None:
            return False
        try:
            # не перехватываем Ctrl-комбинации — отдаём обычным хендлерам
            if state & Gdk.ModifierType.CONTROL_MASK:
                return False
            return bool(self._vim.handle_key(keyval, keycode, state))
        except Exception:
            return False

    def _vim_quit(self) -> None:
        """Обработка :q / :wq — закрыть текущий файл (очистить редактор)."""
        try:
            if self._dirty:
                # :q без ! — если dirty, не закрываем? Но :q! и :wq уже сохранили
                # Для простоты :q закрывает только если не dirty, иначе подсказка
                # Однако наш _execute_command вызывает on_save перед on_quit для :wq,
                # так что dirty уже False. Для :q — проверим.
                if self._current is not None:
                    # если :q вызвали c dirty — требуем сохранение или :q!
                    # Прямо сейчас :q без сохранения — показываем подсказку
                    self.file_label.set_text("нет сохранения — используй :q! или :wq")
                    return
            # закрыть: сбросить состояние
            self._current = None
            self._dirty = False
            self.save_btn.set_sensitive(False)
            self.del_btn.set_sensitive(False)
            self.file_label.set_text(":q — файл закрыт")
            # очистить буфер
            try:
                self._loading = True
                self.buffer.set_text("")
                self._loading = False
            except Exception:
                pass
            try:
                self._links_panel.set_visible(False)
            except Exception:
                pass
            try:
                self._refresh_history()
            except Exception:
                pass
            try:
                self._refresh_comments()
            except Exception:
                pass
            try:
                self._update_crypto_buttons()
            except Exception:
                pass
        except Exception:
            pass

    def set_vim_enabled(self, enabled: bool) -> None:
        self.settings["vim_mode"] = bool(enabled)
        if getattr(self, "_vim", None) is not None:
            try:
                self._vim.set_enabled(bool(enabled))
            except Exception:
                pass

    # ── Поиск с подсветкой в редакторе ─────────────────────
    def _get_search_query(self) -> str:
        """Текст для подсветки: filter_entry без # , минимум 2 символа."""
        raw = self.filter_entry.get_text().strip() if hasattr(self, "filter_entry") else ""
        if raw.startswith("#"):
            raw = raw[1:].strip()
        return raw

    def _clear_search_highlight(self) -> None:
        """Снять оба тега подсветки со всего буфера."""
        if self._search_tag is None:
            return
        try:
            start, end = self.buffer.get_bounds()
            self.buffer.remove_tag(self._search_tag, start, end)
            if self._search_current_tag is not None:
                self.buffer.remove_tag(self._search_current_tag, start, end)
        except Exception:
            pass
        self._search_matches = []
        self._search_index = -1

    def _update_search_counter(self) -> None:
        """Обновить счётчик '3/12' рядом с filter_entry и кнопки навигации."""
        total = len(self._search_matches)
        if hasattr(self, "search_counter") and self.search_counter is not None:
            if total == 0:
                query = self._get_search_query()
                if len(query) >= 2:
                    self.search_counter.set_text("0/0")
                else:
                    self.search_counter.set_text("")
            else:
                cur = self._search_index + 1 if self._search_index >= 0 else 0
                # если ещё не выбран индекс — показывать 1/total после подсветки
                if cur == 0 and total > 0:
                    cur = 1
                self.search_counter.set_text(f"{cur}/{total}")
        # кнопки навигации активны только если есть совпадения
        has = total > 0
        try:
            if self.search_prev_btn is not None:
                self.search_prev_btn.set_sensitive(has)
            if self.search_next_btn is not None:
                self.search_next_btn.set_sensitive(has)
        except Exception:
            pass

    def _update_search_highlight(self) -> None:
        """Подсветить все вхождения поискового запроса (≥2 символов) в TextBuffer.

        Синхронный API сохранён для совместимости. Лимит 1000 совпадений,
        чтобы не вешать главный поток apply_tag.
        """
        query = self._get_search_query()
        self._clear_search_highlight()
        if len(query) < 2 or self._search_tag is None:
            self._update_search_counter()
            return
        try:
            start, end = self.buffer.get_bounds()
            text = self.buffer.get_text(start, end, True)
            if not text:
                self._update_search_counter()
                return
            lower_text = text.lower()
            lower_q = query.lower()
            qlen = len(query)
            matches: list[tuple[Gtk.TextIter, Gtk.TextIter]] = []
            pos = 0
            while True:
                idx = lower_text.find(lower_q, pos)
                if idx == -1:
                    break
                try:
                    s = self.buffer.get_iter_at_offset(idx)
                    e = self.buffer.get_iter_at_offset(idx + qlen)
                    self.buffer.apply_tag(self._search_tag, s, e)
                    matches.append((s, e))
                except Exception:
                    pass
                pos = idx + qlen
                # защита от огромного числа совпадений — лимит видимых 1000
                if len(matches) >= _SEARCH_HIGHLIGHT_LIMIT:
                    break
            self._search_matches = matches
            if matches:
                self._search_index = 0
                # выделить текущее вхождение отдельным тегом
                if self._search_current_tag is not None:
                    try:
                        s, e = matches[0]
                        # get fresh iters (apply_tag may invalidate old iters after buffer changes)
                        s2 = self.buffer.get_iter_at_offset(s.get_offset())
                        e2 = self.buffer.get_iter_at_offset(e.get_offset())
                        self.buffer.apply_tag(self._search_current_tag, s2, e2)
                        self.editor.scroll_to_iter(s2, 0.2, False, 0, 0)
                    except Exception:
                        pass
            self._update_search_counter()
        except Exception:
            self._update_search_counter()

    # ── debounce 200ms + фоновый поток для buffer.changed ─────
    def _schedule_search_highlight(self) -> None:
        """Debounce 200мс: отменяет предыдущий таймер, ставит новый.

        Тяжёлый поиск подсветки уходит в фоновый поток, apply_tag — в idle.
        """
        if getattr(self, "_search_timer", None) is not None:
            try:
                GLib.source_remove(self._search_timer)
            except Exception:
                pass
            self._search_timer = None
        # инкремент поколения инвалидирует pending фоновые задачи
        self._search_gen += 1
        self._search_timer = GLib.timeout_add(_SEARCH_DEBOUNCE_MS, self._on_search_debounced)

    def _on_search_debounced(self) -> bool:
        self._search_timer = None
        query = self._get_search_query()
        if len(query) < 2 or self._search_tag is None:
            self._clear_search_highlight()
            self._update_search_counter()
            return False
        # snapshot текста в главном потоке — безопасно для thread
        try:
            start, end = self.buffer.get_bounds()
            text = self.buffer.get_text(start, end, True)
        except Exception:
            return False
        if not text:
            self._clear_search_highlight()
            self._update_search_counter()
            return False
        gen = self._search_gen
        qlen = len(query)
        lower_q = query.lower()
        # для очень маленьких файлов синхронно быстрее, без thread overhead
        if len(text) < 50000:
            # лёгкий путь — синхронно но через idle чтобы не реентерить buffer.changed
            self._update_search_highlight()
            return False

        def work() -> None:
            try:
                lower_text = text.lower()
                offsets: list[int] = []
                pos = 0
                while True:
                    idx = lower_text.find(lower_q, pos)
                    if idx == -1:
                        break
                    offsets.append(idx)
                    pos = idx + qlen
                    if len(offsets) >= _SEARCH_HIGHLIGHT_LIMIT:
                        break
            except Exception:
                offsets = []  # type: ignore[assignment]
            GLib.idle_add(lambda: self._apply_search_results(gen, query, offsets))

        threading.Thread(target=work, daemon=True).start()
        return False

    def _apply_search_results(self, gen: int, query: str, offsets: list[int]) -> bool:
        """Применить результаты фонового поиска в главном потоке."""
        if gen != self._search_gen or not self._alive:
            return False
        # если запрос уже изменился — отбросить устаревший результат
        if query != self._get_search_query():
            return False
        try:
            self._clear_search_highlight()
            if not offsets or self._search_tag is None:
                self._update_search_counter()
                return False
            qlen = len(query)
            matches: list[tuple[Gtk.TextIter, Gtk.TextIter]] = []
            for idx in offsets:
                try:
                    s = self.buffer.get_iter_at_offset(idx)
                    e = self.buffer.get_iter_at_offset(idx + qlen)
                    self.buffer.apply_tag(self._search_tag, s, e)
                    matches.append((s, e))
                except Exception:
                    continue
            self._search_matches = matches
            if matches and self._search_current_tag is not None:
                try:
                    s, e = matches[0]
                    s2 = self.buffer.get_iter_at_offset(s.get_offset())
                    e2 = self.buffer.get_iter_at_offset(e.get_offset())
                    self.buffer.apply_tag(self._search_current_tag, s2, e2)
                    self.editor.scroll_to_iter(s2, 0.2, False, 0, 0)
                except Exception:
                    pass
            self._search_index = 0 if matches else -1
            self._update_search_counter()
        except Exception:
            try:
                self._update_search_counter()
            except Exception:
                pass
        return False

    def _goto_search_match(self, index: int) -> None:
        if not self._search_matches:
            return
        total = len(self._search_matches)
        index %= total
        self._search_index = index
        # снять старый current
        try:
            if self._search_current_tag is not None:
                start, end = self.buffer.get_bounds()
                self.buffer.remove_tag(self._search_current_tag, start, end)
            s, e = self._search_matches[index]
            s2 = self.buffer.get_iter_at_offset(s.get_offset())
            e2 = self.buffer.get_iter_at_offset(e.get_offset())
            if self._search_current_tag is not None:
                self.buffer.apply_tag(self._search_current_tag, s2, e2)
            self.editor.scroll_to_iter(s2, 0.2, False, 0, 0)
            self.buffer.place_cursor(s2)
            self.editor.grab_focus()
        except Exception:
            pass
        self._update_search_counter()

    def _search_next(self) -> bool:
        """Ctrl+G — следующее вхождение."""
        if not self._search_matches:
            # попытка подсветить если ещё не было
            self._update_search_highlight()
            if not self._search_matches:
                return False
            return True
        self._goto_search_match(self._search_index + 1)
        return True

    def _search_prev(self) -> bool:
        """Ctrl+Shift+G — предыдущее вхождение."""
        if not self._search_matches:
            self._update_search_highlight()
            if not self._search_matches:
                return False
            return True
        self._goto_search_match(self._search_index - 1)
        return True

    # ── Vault: дерево ──────────────────────────────────────
    def reload(self, force: bool = False) -> None:
        """Дерево строится в фоне из структурного tree-scan, рендер — лениво."""
        if not self._alive:
            return
        self._token += 1
        token = self._token
        if self._current is None:
            self.file_label.set_text("Загружаю vault…")
        settings = dict(self.settings)
        vault_svc = self._vault

        def work() -> None:
            try:
                node = vault_svc.ensure_file_tree(force=force, settings=settings)
            except Exception:  # noqa: BLE001
                node = None
            GLib.idle_add(self._tree_result, token, node)

        threading.Thread(target=work, daemon=True).start()

    def _tree_result(self, token: int, node) -> None:
        if token != self._token or not self._alive:
            return
        if node is None:
            self.file_label.set_text("не удалось построить дерево vault")
            return
        self._node = node
        # модульные хелперы из vault_service (без дублирования в классе)
        self._nodes = index_nodes(node)
        self._populated = set()
        self._store.clear()
        self._path_to_iter = {}
        self._notes = collect_notes(node)
        self._fill_root()
        # ── виртуализованный плоский список: заполнить Gio.ListStore чанками (39k без фриза) ──
        self._populate_flat_store()
        self._render_list()
        self._sync_empty_state()
        # обновить глобальные теги после построения дерева
        try:
            self._refresh_tags()
        except Exception:
            pass

    def _populate_flat_store(self) -> None:
        if self._flat_store is None:
            return
        self._flat_populate_token += 1
        token = self._flat_populate_token
        # очистить и подготовить очередь
        try:
            self._flat_store.remove_all()
        except Exception:
            # Gio.ListStore < 4.8 fallback: по одному
            try:
                while self._flat_store.get_n_items() > 0:
                    self._flat_store.remove(0)
            except Exception:
                pass
        self._flat_pending = list(self._notes)
        # чанковый idle-филл — не блокирует UI при 39k
        GLib.idle_add(lambda: self._populate_flat_chunk(token))

    def _populate_flat_chunk(self, token: int) -> bool:
        if not self._alive or token != self._flat_populate_token:
            return False
        if self._flat_store is None:
            return False
        take = self._flat_pending[:_CHUNK]
        del self._flat_pending[:_CHUNK]
        for name, path in take:
            try:
                self._flat_store.append(_NoteItem(name, path))
            except Exception:
                pass
        if self._flat_pending:
            GLib.idle_add(lambda: self._populate_flat_chunk(token))
            return False
        # готово — если есть активный фильтр, пересчитать в воркере
        if self._filter_query:
            try:
                GLib.idle_add(self._apply_filter_worker)
            except Exception:
                pass
        return False

    def _tree_append(
        self, parent, emoji: str, name: str, path: str, is_dir: bool, placeholder: bool = False
    ):
        it = self._store.append(parent, [emoji, name, path, is_dir])
        if placeholder:
            self._store.append(it, ["", "", "", False])
        if not is_dir:
            self._path_to_iter[path] = it
        return it

    def _fill_root(self) -> None:
        node = self._node
        if node is None:
            return
        self._populated.add(node.path)
        for d in node.dirs:
            sub = self._nodes.get(d.path)
            has = bool(sub and (sub.dirs or sub.files))
            self._tree_append(None, "📁", d.name, d.path, True, placeholder=has)
        for fname, fpath in node.files:
            self._tree_append(
                None,
                FILE_EMOJI.get(Path(fname).suffix.lower(), "📄"),
                Path(fname).stem,
                fpath,
                False,
            )
        self._update_tree_counts()

    def _update_tree_counts(self) -> None:
        if self._node is not None:
            dirs, files = self._vault.count_nodes(self._node)
            self.count_label.set_text(f"{dirs} папок · {files} заметок")

    def _sync_empty_state(self) -> None:
        if self._node is None:
            return
        if not self._notes:
            self.side_stack.set_visible(False)
            self._vault_empty.set_visible(True)
            self._filter_empty.set_visible(False)
            return
        query = self.filter_entry.get_text().strip().lower()
        if query:
            # во время дебаунса/фильтра не показываем empty преждевременно
            if self._filter_timer is not None:
                self.side_stack.set_visible(True)
                self._vault_empty.set_visible(False)
                self._filter_empty.set_visible(False)
                return
            # виртуализованная модель (worker): считаем отфильтрованные элементы
            n = 0
            try:
                if self._filtered_store is not None:
                    n = self._filtered_store.get_n_items()
                elif self._filter_model is not None:
                    n = self._filter_model.get_n_items()
                else:
                    n = len(self._path_to_row)
            except Exception:
                n = len(self._path_to_row) if hasattr(self, "_path_to_row") else 0
            if n == 0:
                self.side_stack.set_visible(False)
                self._vault_empty.set_visible(False)
                self._filter_empty.set_visible(True)
                return
        self.side_stack.set_visible(True)
        self._vault_empty.set_visible(False)
        self._filter_empty.set_visible(False)

    # ── Flat ListView factory + compat ──────────────────────────
    def _install_listbox_compat(self) -> None:
        """Совместимость со старым API self.listbox (Gtk.ListBox)."""
        lv = self._listview
        if lv is None:
            return

        # select_row(row) -> найти позицию по пути и выбрать
        def _compat_select_row(row) -> None:  # row может быть path str или ListBoxRow shim
            if row is None or self._selection is None:
                return
            # если передали Gtk.ListBoxRow (старый путь) — извлечь path
            path_str = None
            if isinstance(row, str):
                path_str = row
            elif hasattr(row, "get_child"):
                # пробуем найти путь через _path_to_row обратную карту (устар.)
                for p, r in self._path_to_row.items():
                    if r is row:
                        path_str = p
                        break
            if path_str is None:
                # row может быть уже путём из _open
                return
            self._select_flat_path(path_str)

        def _compat_unselect_all() -> None:
            try:
                if self._selection is not None:
                    self._selection.set_selected(Gtk.INVALID_LIST_POSITION)
            except Exception:
                pass

        def _compat_get_first_child():
            return None  # виртуализован — детей нет в виджете

        def _compat_remove(child):
            return None

        def _compat_append(row):
            return None

        # обезьяний патч на экземпляр ListView чтобы старый код не падал
        try:
            lv.select_row = _compat_select_row  # type: ignore[attr-defined]
            lv.unselect_all = _compat_unselect_all  # type: ignore[attr-defined]
            lv.get_first_child = _compat_get_first_child  # type: ignore[attr-defined]
            lv.remove = _compat_remove  # type: ignore[attr-defined]
            lv.append = _compat_append  # type: ignore[attr-defined]
        except Exception:
            pass

    def _on_flat_factory_setup(
        self, _factory: Gtk.SignalListItemFactory, item: Gtk.ListItem
    ) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(5)
        box.set_margin_bottom(5)
        box.set_margin_start(4)
        box.set_margin_end(4)
        icon = Gtk.Label(css_classes=["file-icon"])
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        title = Gtk.Label(halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        rel = Gtk.Label(
            css_classes=["dim-hint"],
            halign=Gtk.Align.START,
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
        )
        info.append(title)
        info.append(rel)
        box.append(icon)
        box.append(info)
        item.set_child(box)

    def _on_flat_factory_bind(
        self, _factory: Gtk.SignalListItemFactory, item: Gtk.ListItem
    ) -> None:
        note = item.get_item()
        box = item.get_child()
        if note is None or box is None:
            return
        try:
            # box: [icon, info[title, rel]]
            icon = box.get_first_child()
            info = box.get_last_child()
            title_w = info.get_first_child() if info else None
            rel_w = title_w.get_next_sibling() if title_w else None
            name = getattr(note, "name", "")
            path = getattr(note, "path", "")
            if icon is not None:
                icon.set_label(FILE_EMOJI.get(Path(name).suffix.lower(), "📄"))
            title = self._vault.note_title_cached(Path(path)) or Path(path).stem
            if title_w is not None:
                title_w.set_label(title)
            if rel_w is not None:
                parent = Path(path).parent
                rel = parent.name if parent != self._root else ""
                rel_w.set_label(rel)
        except Exception:
            pass

    def _select_flat_path(self, path_str: str) -> None:
        if self._selection is None or self._slice_model is None:
            return
        n = self._slice_model.get_n_items()
        for i in range(n):
            obj = self._slice_model.get_item(i)
            if obj is not None and getattr(obj, "path", None) == path_str:
                self._selection.set_selected(i)
                return

    def _on_tree_expanded(
        self, _tree: Gtk.TreeView, _it: Gtk.TreeIter, _path: Gtk.TreePath
    ) -> None:
        it = self._store.get_iter(_path)
        self._store.set_value(it, 0, "📂")
        node = self._nodes.get(self._store.get_value(it, 2))
        if node is None or node.path in self._populated:
            return
        self._populated.add(node.path)
        # сбросить заглушку
        child = self._store.iter_children(it)
        if child is not None:
            self._store.remove(child)
        # наполняем чанками, деревья с 39k файлов не вешают UI
        pending = _CHUNK
        for d in node.dirs:
            sub = self._nodes.get(d.path)
            has = bool(sub and (sub.dirs or sub.files))
            self._tree_append(it, "📁", d.name, d.path, True, placeholder=has)
        for fname, fpath in node.files:
            self._tree_append(
                it, FILE_EMOJI.get(Path(fname).suffix.lower(), "📄"), Path(fname).stem, fpath, False
            )

    def _on_tree_collapsed(self, _tree: Gtk.TreeView, _path: Gtk.TreePath) -> None:
        it = self._store.get_iter(_path)
        if it:
            self._store.set_value(it, 0, "📁")

    def _on_tree_activated(self, _tree: Gtk.TreeView, path: Gtk.TreePath, _col) -> None:
        it = self._store.get_iter(path)
        if not it:
            return
        is_dir = bool(self._store.get_value(it, 3))
        p = self._store.get_value(it, 2)
        if is_dir:
            if self._tree.row_expanded(path):
                self._tree.collapse_row(path)
            else:
                self._tree.expand_row(path, False)
        elif p:
            self._highlight = ""
            self._open(Path(p))

    # ── Плоский поиск (фильтр) ─────────────────────────────
    # Виртуализован: Gio.ListStore + worker filtering + SliceListModel + ListView
    # Дебаунс 150 мс, лимит 5000 (SliceListModel), фильтр в воркере — 39k не фризит.
    def _flat_filter_func(self, item: _NoteItem) -> bool:
        q = self._filter_query
        if not q:
            return False
        if q.startswith("#"):
            # тег-режим: item.path должен быть в _tag_matched
            return getattr(item, "path", "") in self._tag_matched
        # обычный поиск по имени/пути (lower заранее)
        try:
            return q in getattr(item, "lower_name", "") or q in getattr(item, "lower_path", "")
        except Exception:
            return False

    def _on_filter_changed(self) -> None:
        raw = self.filter_entry.get_text().strip()
        query = raw.lower()
        # синхронизируем выбранный тег с полем ввода
        if raw.startswith("#"):
            tq = raw[1:].strip()
            self._selected_tag = tq if tq else None
        else:
            if not query:
                self._selected_tag = None
            elif self._selected_tag is not None and query != f"#{self._selected_tag.lower()}":
                if not query.startswith(f"#{self._selected_tag.lower()}"):
                    self._selected_tag = None
        if query:
            self.side_stack.set_visible_child_name("find")
        else:
            self.side_stack.set_visible_child_name("tree")
            # сброс фильтра — очистить виртуализованную модель (worker)
            self._filter_query = ""
            self._tag_matched = set()
            self._filter_token += 1
            try:
                if self._filtered_store is not None:
                    self._filtered_store.remove_all()
                elif self._flat_filter is not None:
                    self._flat_filter.changed(Gtk.FilterChange.DIFFERENT)
            except Exception:
                pass
            self._filtered_pending = []
            self._update_tree_counts()
            try:
                self._update_flat_counts()
            except Exception:
                pass
            self._sync_empty_state()
        # дебаунс 150 мс: не дергаем FilterListModel на каждый keystroke при 39k
        if self._filter_timer is not None:
            try:
                GLib.source_remove(self._filter_timer)
            except Exception:
                pass
            self._filter_timer = None
        if not query:
            # подсветку чипов и редактора — сразу (clear дешёвый), но поиск — через debounce
            try:
                self._refresh_tags()
            except Exception:
                pass
            try:
                self._schedule_search_highlight()
            except Exception:
                pass
            return
        self._filter_timer = GLib.timeout_add(_FILTER_DEBOUNCE_MS, self._on_filter_debounced)

    def _on_filter_debounced(self) -> bool:
        self._filter_timer = None
        self._apply_filter()
        # обновить подсветку чипов и редактора после применения фильтра
        try:
            self._refresh_tags()
        except Exception:
            pass
        try:
            self._schedule_search_highlight()
        except Exception:
            pass
        return False

    def _apply_filter(self) -> None:
        """Делегирует в воркер — синхронный вход для compat/debounce."""
        self._apply_filter_worker()

    def _apply_filter_worker(self) -> bool:
        if not self._alive:
            return False
        raw = self.filter_entry.get_text().strip()
        query = raw.lower()
        if not query:
            return False
        # ── тег-режим: пустой тег ──
        if raw.startswith("#"):
            tq = raw[1:].strip().lower()
            if not tq:
                self._filter_query = raw.lower()
                self._tag_matched = set()
                self._filter_token += 1
                try:
                    if self._filtered_store is not None:
                        self._filtered_store.remove_all()
                except Exception:
                    pass
                self._filtered_pending = []
                self.count_label.set_text(f"введи тег после # · {len(self._notes)} заметок")
                self._hit_cap = False
                self._sync_empty_state()
                return False
        # подготовка воркера
        self._filter_token += 1
        token = self._filter_token
        self._filter_query = query
        # снапшот данных для фона (не трогаем GTK-объекты в потоке)
        notes_snapshot = list(self._notes)
        raw_snapshot = raw
        query_snapshot = query
        vault_ref = self._vault
        settings_snapshot = (
            dict(self.settings) if isinstance(getattr(self, "settings", None), dict) else {}
        )

        # воркер: FTS5 полнотекстовый поиск (BM25) вместо substring-поиска, с fallback
        def work() -> None:
            matched_set: set[str] = set()
            filtered: list[tuple[str, str]] = []
            try:
                if raw_snapshot.startswith("#"):
                    tq2 = raw_snapshot[1:].strip().lower()
                    try:
                        tags = vault_ref.scan_tags()
                    except Exception:
                        tags = {}
                    for tag, paths in tags.items():
                        try:
                            if tq2 == tag.lower() or tq2 in tag.lower():
                                for p in paths:
                                    matched_set.add(str(p))
                        except Exception:
                            continue
                    if not matched_set:
                        try:
                            exact = vault_ref.get_files_by_tag(tq2)
                            for p in exact:
                                matched_set.add(str(p))
                        except Exception:
                            pass
                    # фильтр по множеству
                    for name, path in notes_snapshot:
                        if path in matched_set:
                            filtered.append((name, path))
                else:
                    # Гибрид: FTS5 BM25 + семантический embeddings (sentence-transformers / TF-IDF fallback)
                    # Семантика ловит парафразы и рус/англ синонимы, FTS — точные токены.
                    q = query_snapshot
                    used_fts = False
                    fts_seen: set[str] = set()
                    if _fts is not None and len(q) >= 1:
                        try:
                            fts_hits = _fts.search(
                                settings_snapshot, raw_snapshot, limit=_RENDER_CAP
                            )
                            if fts_hits is not None:
                                used_fts = True
                                name_map = {p: n for n, p in notes_snapshot}
                                seen: set[str] = set()
                                for h in fts_hits:
                                    p = h.get("path")
                                    if not p or p in seen:
                                        continue
                                    seen.add(p)
                                    n = name_map.get(p)
                                    if n is None:
                                        try:
                                            n = Path(p).name
                                        except Exception:
                                            n = p
                                    filtered.append((n, p))
                                fts_seen = set(seen)
                                # дополнить filename/path совпадениями, не покрытыми FTS (папки)
                                if len(filtered) < _RENDER_CAP:
                                    for name, path in notes_snapshot:
                                        if path in seen:
                                            continue
                                        try:
                                            if q in name.lower() or q in path.lower():
                                                filtered.append((name, path))
                                                seen.add(path)
                                                if len(filtered) >= _RENDER_CAP:
                                                    break
                                        except Exception:
                                            continue
                        except Exception:
                            used_fts = False
                    # ── embeddings: семантический поиск поверх FTS (hybrid rerank) ──
                    emb_hits: list[dict[str, Any]] = []
                    if _embeddings is not None and len(q) >= 2:
                        try:
                            # semantic_search сам делает ensure_index (WAL sqlite)
                            emb_hits = (
                                _embeddings.search(
                                    settings_snapshot, raw_snapshot, limit=_RENDER_CAP
                                )
                                or []
                            )
                        except Exception:
                            emb_hits = []
                    if emb_hits:
                        try:
                            name_map_e = {p: n for n, p in notes_snapshot}
                            # гибридный merge: если уже есть FTS, объединяем по weighted score
                            if used_fts and filtered:
                                # карты путей -> позиции/скоры
                                fts_pos = {p: i for i, (_n, p) in enumerate(filtered)}
                                emb_score = {
                                    h.get("path"): float(h.get("score") or 0) for h in emb_hits
                                }
                                all_paths = set(fts_pos.keys()) | set(emb_score.keys())
                                merged: list[tuple[float, str]] = []
                                w_emb = (
                                    0.6 if " " in q else 0.35
                                )  # естественный язык -> вес семантики выше
                                w_fts = 1.0 - w_emb
                                for p in all_paths:
                                    fts_norm = 0.0
                                    if p in fts_pos:
                                        # нормируем позицией: 1/(1+pos)
                                        fts_norm = 1.0 / (1.0 + fts_pos[p])
                                    e_sc = emb_score.get(p, 0.0)
                                    combined = w_fts * fts_norm + w_emb * e_sc
                                    merged.append((combined, p))
                                merged.sort(key=lambda x: x[0], reverse=True)
                                new_filtered: list[tuple[str, str]] = []
                                seen_m: set[str] = set()
                                for _sc, p in merged[:_RENDER_CAP]:
                                    if p in seen_m:
                                        continue
                                    seen_m.add(p)
                                    n = name_map_e.get(p)
                                    if n is None:
                                        try:
                                            n = Path(p).name
                                        except Exception:
                                            n = p
                                    new_filtered.append((n, p))
                                # если merge дал результаты — заменяем
                                if new_filtered:
                                    filtered = new_filtered
                            else:
                                # FTS не дал результатов — чистый семантический ранжированный список
                                seen = set(fts_seen) if used_fts else set()
                                # если FTS уже дал что-то — добавляем только новые из embeddings
                                if filtered and used_fts:
                                    seen = {p for _, p in filtered}
                                    for h in emb_hits:
                                        p = h.get("path")
                                        if not p or p in seen:
                                            continue
                                        seen.add(p)
                                        n = name_map_e.get(p)
                                        if n is None:
                                            try:
                                                n = Path(p).name
                                            except Exception:
                                                n = p
                                        filtered.append((n, p))
                                        if len(filtered) >= _RENDER_CAP:
                                            break
                                elif not filtered:
                                    for h in emb_hits:
                                        p = h.get("path")
                                        if not p:
                                            continue
                                        n = name_map_e.get(p)
                                        if n is None:
                                            try:
                                                n = Path(p).name
                                            except Exception:
                                                n = p
                                        filtered.append((n, p))
                                        if len(filtered) >= _RENDER_CAP:
                                            break
                        except Exception:
                            pass
                    if not used_fts and not emb_hits:
                        # fallback — прежний substring по имени/пути
                        for name, path in notes_snapshot:
                            try:
                                if q in name.lower() or q in path.lower():
                                    filtered.append((name, path))
                            except Exception:
                                continue
            except Exception:
                filtered = []
                matched_set = set()
            GLib.idle_add(
                lambda: self._apply_filter_results(
                    token, filtered, matched_set, raw_snapshot, query_snapshot
                )
            )

        threading.Thread(target=work, daemon=True).start()
        return False

    def _apply_filter_results(
        self,
        token: int,
        filtered: list[tuple[str, str]],
        matched_set: set[str],
        raw: str,
        query: str,
    ) -> bool:
        if not self._alive or token != self._filter_token:
            return False
        # проверка что запрос не устарел
        cur_raw = self.filter_entry.get_text().strip()
        if cur_raw.lower() != query:
            return False
        self._tag_matched = matched_set if raw.startswith("#") else set()
        # чанковое заполнение filtered_store (не блокирует UI)
        try:
            if self._filtered_store is not None:
                try:
                    self._filtered_store.remove_all()
                except Exception:
                    try:
                        while self._filtered_store.get_n_items() > 0:
                            self._filtered_store.remove(0)
                    except Exception:
                        pass
        except Exception:
            pass
        self._filtered_pending = filtered
        if not filtered:
            GLib.idle_add(self._update_flat_counts)
            return False
        GLib.idle_add(lambda: self._populate_filtered_chunk(token))
        return False

    def _populate_filtered_chunk(self, token: int) -> bool:
        if not self._alive or token != self._filter_token:
            return False
        if self._filtered_store is None:
            return False
        take = self._filtered_pending[:_CHUNK]
        del self._filtered_pending[:_CHUNK]
        for name, path in take:
            try:
                self._filtered_store.append(_NoteItem(name, path))
            except Exception:
                pass
        if self._filtered_pending:
            GLib.idle_add(lambda: self._populate_filtered_chunk(token))
            return False
        GLib.idle_add(self._update_flat_counts)
        return False

    def _update_flat_counts(self) -> bool:
        if not self._alive:
            return False
        if not self._filter_query:
            return False
        try:
            total = len(self._notes)
            if self._filtered_store is not None:
                shown_filter = self._filtered_store.get_n_items()
            elif self._filter_model is not None:
                shown_filter = self._filter_model.get_n_items()
            else:
                shown_filter = 0
            shown = self._slice_model.get_n_items() if self._slice_model else shown_filter
            self._hit_cap = shown_filter > _RENDER_CAP
            text = (
                f"найдено {shown} из {total}"
                if not self._filter_query.startswith("#")
                else f"найдено {shown} из {total} · #{self._filter_query[1:]}"
            )
            if self._hit_cap:
                text += f" · показаны первые {_RENDER_CAP} — уточни запрос"
            self.count_label.set_text(text)
        except Exception:
            pass
        self._sync_empty_state()
        # подсветить выбранный файл если он в фильтре
        if self._current is not None:
            try:
                self._select_flat_path(str(self._current))
            except Exception:
                pass
        return False

    # ── legacy compat: старые методы теперь делегируют в виртуализованную модель (worker) ──
    def _render_list(self) -> None:
        """Совместимость: раньше чанковый рендер ListBox, теперь дебаунс + worker FilterListModel."""
        self._apply_filter_worker()

    def _fill_tick(self, token: int) -> bool:
        # устарело: ListView виртуализован, заливка не нужна
        self._filling = False
        return False

    def _after_render(self) -> None:
        self._update_flat_counts()

    def _make_row(self, name: str, path: str) -> Gtk.ListBoxRow:
        # устарело: ListView использует factory, но оставляем для тестов/совместимости
        row = Gtk.ListBoxRow(activatable=True)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(5)
        box.set_margin_bottom(5)
        box.set_margin_start(4)
        box.set_margin_end(4)
        box.append(Gtk.Label(label=FILE_EMOJI.get(Path(name).suffix.lower(), "📄")))
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        title = self._vault.note_title_cached(Path(path)) or Path(path).stem
        info.append(
            Gtk.Label(
                label=title,
                halign=Gtk.Align.START,
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
            )
        )
        parent = Path(path).parent
        rel = parent.name if parent != self._root else ""
        info.append(
            Gtk.Label(
                label=rel,
                css_classes=["dim-hint"],
                halign=Gtk.Align.START,
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
            )
        )
        box.append(info)
        row.set_child(box)
        return row

    def _on_row_activate(self, _list, row) -> None:  # type: ignore[no-untyped-def]
        # legacy ListBox signal
        path = None
        if hasattr(row, "get_child"):
            for p, r in self._path_to_row.items():
                if r is row:
                    path = p
                    break
        if path:
            self._highlight = ""
            self._open(Path(path))

    def _on_listview_activate(self, _lv: Gtk.ListView, pos: int) -> None:
        if self._slice_model is None:
            return
        obj = self._slice_model.get_item(pos)
        if obj is None:
            return
        path = getattr(obj, "path", None)
        if path:
            self._highlight = ""
            self._open(Path(path))

    # ── Редактор ─────────────────────────────────────────────
    def open_path(self, p: Path, highlight: str | None = None) -> None:
        """Открыть файл по абсолютному пути (внешний вход: поиск, wikilinks).

        При `highlight` — фразе из полнотекстового поиска — сразу включить
        режим просмотра и подсветить первое вхождение.
        """
        if not p.is_file():
            return
        self._highlight = (highlight or "").strip()
        self._open(p)
        if self._highlight and self.stack.get_visible_child_name() != "Просмотр":
            self.stack.set_visible_child_name("Просмотр")
            self._render_preview()

    def _is_encrypted_path(self, p: Path | str) -> bool:
        try:
            if _crypto is not None and hasattr(_crypto, "is_encrypted"):
                return bool(_crypto.is_encrypted(Path(p)))
        except Exception:
            pass
        return str(p).endswith(".enc")

    def _update_crypto_buttons(self) -> None:
        cur = self._current
        is_enc = bool(cur is not None and self._is_encrypted_path(cur))
        try:
            if hasattr(self, "encrypt_btn"):
                self.encrypt_btn.set_sensitive(cur is not None and not is_enc)
                self.encrypt_btn.set_visible(not is_enc)
            if hasattr(self, "decrypt_btn"):
                self.decrypt_btn.set_sensitive(cur is not None and is_enc)
                self.decrypt_btn.set_visible(is_enc)
        except Exception:
            pass
        # редактор и save недоступны для зашифрованного (пока не расшифрован)
        try:
            self.editor.set_editable(not is_enc)
            self.save_btn.set_sensitive(False if is_enc else self._dirty)
        except Exception:
            pass
        self._update_export_button()

    def _update_export_button(self) -> None:
        """Синхронизировать чувствительность кнопки Экспорт."""
        try:
            if not hasattr(self, "export_btn"):
                return
            cur = getattr(self, "_current", None)
            # Экспорт доступен только для открытого незашифрованного файла
            can = cur is not None and not self._is_encrypted_path(cur)
            self.export_btn.set_sensitive(bool(can))
        except Exception:
            pass

    # ── Крипто-папка Secret/: авто-шифрование ──────────────────
    def _crypto_prompt_decrypt(self, p: Path) -> bool:
        """Авто-промпт пароля для .md.enc внутри Secret/ и повторный _open."""
        if _crypto_folder is None or _crypto is None:
            return False
        # не спамим если уже есть кэш
        if _crypto_folder.resolve_password(self.settings):
            try:
                txt = _crypto_folder.read_encrypted_text(
                    p, _crypto_folder.resolve_password(self.settings)
                )  # type: ignore[arg-type]
            except Exception:
                pass
            else:
                self._open(p)
                return False
            return False

        def _do(pwd: str) -> None:
            try:
                _crypto_folder.set_cached_password(pwd)
            except Exception:
                pass
            # также сохранить в settings для последующих авто-операций (in-memory сессия)
            try:
                self.settings["crypto_password"] = pwd
            except Exception:
                pass
            # повторно открыть
            try:
                self._open(p)
            except Exception:
                pass

        try:
            self._prompt_password(
                "Авто-дешифрование",
                "Расшифровать Secret?",
                f"«{p.name}» в {_crypto_folder.get_crypto_folder_relative(self.settings)}/ — введите пароль для AES-GCM.",
                "Расшифровать",
                _do,
            )
        except Exception:
            pass
        return False

    def _crypto_handle_save(self, path: Path, text: str) -> bool:
        """Попытаться авто-зашифровать при сохранении.

        Возвращает True если обработка выполнена (файл записан зашифрованно),
        False если требуется обычный plain-write.
        Если пароль отсутствует — показывает prompt и возвращает True (отложено).
        """
        if _crypto_folder is None or _crypto is None:
            return False
        # случай: файл уже .enc внутри Secret — перешифровать тем же паролем
        is_enc = self._is_encrypted_path(path)
        in_secret = False
        try:
            in_secret = _crypto_folder.is_in_crypto_folder(path, self.settings)
        except Exception:
            in_secret = False
        auto = False
        try:
            auto = _crypto_folder.is_auto_enabled(self.settings)
        except Exception:
            auto = True
        if not auto or not in_secret:
            return False
        pwd = _crypto_folder.resolve_password(self.settings)
        if not pwd:
            # нет пароля — запрос
            def _do(pwd2: str) -> None:
                try:
                    _crypto_folder.set_cached_password(pwd2)
                except Exception:
                    pass
                try:
                    self.settings["crypto_password"] = pwd2
                except Exception:
                    pass
                # повтор сохранения
                try:
                    self._on_save(None)
                except Exception:
                    pass

            try:
                # определяем заголовок
                is_new_enc = not is_enc
                title = "Авто-шифрование Secret" if is_new_enc else "Перешифровать Secret"
                body = (
                    f"«{path.name}» → {path.name}.enc — введите пароль для AES-GCM."
                    if is_new_enc
                    else f"«{path.name}» — введите пароль для перезаписи AES-GCM."
                )
                self._prompt_password(title, title + "?", body, "Зашифровать", _do)
            except Exception:
                pass
            # отложено — не делаем plain write
            self.file_label.set_text("требуется пароль для Secret/ — введите пароль")
            return True
        # есть пароль — шифруем
        try:
            self._suppress_monitor = True
            if is_enc:
                # перезапись существующего .enc
                _crypto_folder.write_encrypted_text(path, text, pwd, delete_original=False)
                # обновить кэш
                try:
                    self._read_cache[str(path)] = (path.stat().st_mtime_ns, text)
                except OSError:
                    pass
            else:
                # plain .md внутри Secret -> конвертировать в .md.enc
                dst = _crypto_folder.write_encrypted_text(path, text, pwd, delete_original=True)
                # обновить _current на .enc
                try:
                    self._read_cache.pop(str(path), None)
                except Exception:
                    pass
                self._current = Path(dst)
                self._setup_file_monitor(self._current)
                try:
                    self._read_cache[str(dst)] = (Path(dst).stat().st_mtime_ns, text)
                except OSError:
                    pass
                # инвалидировать дерево — появился .enc, исчез .md
                try:
                    self._invalidate_tree()
                except Exception:
                    pass
                try:
                    self.reload(force=True)
                except Exception:
                    pass
            try:
                GLib.timeout_add(1200, self._reset_suppress)
            except Exception:
                self._suppress_monitor = False
            return True
        except Exception as exc:
            try:
                self._suppress_monitor = False
            except Exception:
                pass
            self.file_label.set_text(f"ошибка авто-шифрования: {exc}")
            return True

    # ── CRDT совместное редактирование (опция) ─────────────────
    def _crdt_is_enabled(self) -> bool:
        try:
            return bool(self.settings.get("crdt_enabled", False))
        except Exception:
            return False

    def _crdt_kind(self) -> str:
        try:
            k = str(self.settings.get("crdt_kind", "lww")).lower()
            return k if k in ("lww", "rga", "yjs", "seq") else "lww"
        except Exception:
            return "lww"

    def _crdt_replica(self) -> str:
        if _crdt is not None and hasattr(_crdt, "get_replica_id"):
            try:
                return _crdt.get_replica_id(self.settings)  # type: ignore
            except Exception:
                pass
        return "local"

    def _crdt_init_for_path(self, p: Path) -> None:
        """Инициализировать CRDT-документ для пути p (FileSync + stub)."""
        if not self._crdt_is_enabled() or _crdt is None:
            self._crdt_doc = None
            self._crdt_sync = None
            self._crdt_stub = None
            return
        try:
            doc_id = str(p)
            kind = self._crdt_kind()
            replica = self._crdt_replica()
            doc = _crdt.create_document(doc_id, replica_id=replica, kind=kind)  # type: ignore
            sync = _crdt.FileSync(doc, p)  # type: ignore
            # попытаться слить существующий sidecar
            try:
                sync.sync()
            except Exception:
                pass
            stub = _crdt.NetworkSyncStub(doc)  # type: ignore
            self._crdt_doc = doc
            self._crdt_sync = sync
            self._crdt_stub = stub
        except Exception:
            self._crdt_doc = None
            self._crdt_sync = None
            self._crdt_stub = None

    def _crdt_after_open(self, p: Path, disk_text: str) -> str:
        """После чтения диска — слить с CRDT, вернуть итоговый текст."""
        if not self._crdt_is_enabled() or _crdt is None or self._crdt_doc is None:
            return disk_text
        try:
            # если doc ещё не для этого пути — переинициализировать
            if getattr(self._crdt_doc, "doc_id", None) != str(p):
                self._crdt_init_for_path(p)
            if self._crdt_sync is not None:
                try:
                    self._crdt_sync.sync()
                except Exception:
                    pass
            doc = self._crdt_doc
            if doc is None:
                return disk_text
            # сравнить disk_text с CRDT текстом
            try:
                crdt_text = doc.text if hasattr(doc, "text") else ""
            except Exception:
                crdt_text = ""
            if crdt_text != disk_text:
                # есть расхождение: сливаем через LWW/RGA
                if hasattr(doc, "set_text") and disk_text != crdt_text:
                    # для LWW — set_text выиграет по времени; для RGA — set_text перезапишет
                    # но чтобы не терять concurrent правки, делаем merge через временный doc
                    try:
                        tmp = _crdt.create_document(
                            str(p), replica_id="disk", kind=self._crdt_kind()
                        )  # type: ignore
                        tmp.set_text(disk_text)  # type: ignore
                        doc.merge(tmp)  # type: ignore
                    except Exception:
                        try:
                            doc.set_text(disk_text)  # type: ignore
                        except Exception:
                            pass
                # сохранить sidecar
                try:
                    if self._crdt_sync is not None:
                        self._crdt_sync.save()
                except Exception:
                    pass
                try:
                    # сеть-заглушка — push
                    if self._crdt_stub is not None:
                        self._crdt_stub.push()
                except Exception:
                    pass
                try:
                    return doc.text if hasattr(doc, "text") else disk_text
                except Exception:
                    return disk_text
            return disk_text
        except Exception:
            return disk_text

    def _crdt_before_save(self, p: Path, text: str) -> None:
        """Перед записью на диск — обновить CRDT и sidecar."""
        if not self._crdt_is_enabled() or _crdt is None:
            return
        try:
            if self._crdt_doc is None or getattr(self._crdt_doc, "doc_id", None) != str(p):
                self._crdt_init_for_path(p)
            doc = self._crdt_doc
            if doc is None:
                return
            # обновить CRDT текст
            try:
                if hasattr(doc, "text") and doc.text != text:  # type: ignore
                    doc.set_text(text)  # type: ignore
            except Exception:
                pass
            if self._crdt_sync is not None:
                try:
                    self._crdt_sync.save()
                except Exception:
                    pass
            if self._crdt_stub is not None:
                try:
                    self._crdt_stub.push()
                except Exception:
                    pass
        except Exception:
            pass

    def _on_crdt_toggled(self, btn: Gtk.ToggleButton) -> None:
        enabled = bool(btn.get_active())
        self.settings["crdt_enabled"] = enabled
        # persist
        try:
            from ..config import save_settings as _save

            _save(self.settings)
        except Exception:
            pass
        # переинициализировать для текущего файла
        if enabled and self._current is not None:
            try:
                self._crdt_init_for_path(self._current)
                if self._crdt_sync is not None:
                    self._crdt_sync.sync()
                self.file_label.set_text(f"CRDT включён ({self._crdt_kind()}) · {self._current}")
            except Exception as exc:
                self.file_label.set_text(f"CRDT: {exc}")
        elif not enabled:
            self._crdt_doc = None
            self._crdt_sync = None
            self._crdt_stub = None
            if self._current is not None:
                self.file_label.set_text(str(self._current))
        # toast
        try:
            root = self.get_root()
            overlay = getattr(root, "toast_overlay", None) if root is not None else None
            if overlay is not None:
                msg = "CRDT включён — синхронизация .crdt.json" if enabled else "CRDT выключен"
                toast = Adw.Toast.new(msg)
                toast.set_timeout(2)
                overlay.add_toast(toast)
        except Exception:
            pass

    def _open_pdf(self, p: Path) -> bool:
        """Попытаться открыть PDF через pdf_annotate (Poppler/WebView).

        Возвращает True если PDF обработан (диалог показан или fallback).
        """
        if _pdf_annotate is None:
            return False
        try:
            if not _pdf_annotate.is_pdf_path(p):
                return False
        except Exception:
            return False
        try:
            # подсветить выбор в дереве/списке перед открытием диалога
            try:
                self._select_flat_path(str(p))
            except Exception:
                pass
            try:
                it = self._path_to_iter.get(str(p))
                if it is not None:
                    self._tree.set_cursor(self._store.get_path(it))
            except Exception:
                pass
            self._current = p
            self._setup_file_monitor(p)
            self.file_label.set_text(f"📕 {p} — PDF аннотации (highlight → .pdf.ann.json)")
            try:
                self.editor.set_editable(False)
            except Exception:
                pass
            try:
                self.del_btn.set_sensitive(True)
            except Exception:
                pass
            try:
                self.save_btn.set_sensitive(False)
            except Exception:
                pass
            self._update_crypto_buttons()
            # открыть диалог
            _pdf_annotate.open_pdf_annotate(self, self.settings, p)
            return True
        except Exception as e:
            try:
                self.file_label.set_text(f"PDF ошибка: {e}")
            except Exception:
                pass
            return False

    def _open(self, p: Path) -> None:
        # ── PDF highlight аннотации (Poppler/WebView → .pdf.ann.json) ──
        try:
            if _pdf_annotate is not None and _pdf_annotate.is_pdf_path(p):
                if self._open_pdf(p):
                    return
        except Exception:
            pass
        if self._dirty and self._current != p:
            self._on_save(None)
        else:
            self._cancel_autosave()
        # ── зашифрованные .md.enc — не читаем как текст ──
        if self._is_encrypted_path(p):
            # ── крипто-папка авто-дешифрование (vault/Secret/) ──
            if _crypto_folder is not None:
                try:
                    if _crypto_folder.should_auto_decrypt(p, self.settings):
                        pwd = _crypto_folder.resolve_password(self.settings)
                        if pwd:
                            try:
                                dec_text = _crypto_folder.read_encrypted_text(p, pwd)
                            except Exception:
                                pass
                            else:
                                # успешно дешифровали — открываем как обычный файл (но _current остаётся .enc)
                                # CRDT слияние
                                try:
                                    if self._crdt_is_enabled():
                                        if self._crdt_doc is None or getattr(
                                            self._crdt_doc, "doc_id", None
                                        ) != str(p):
                                            self._crdt_init_for_path(p)
                                        dec_text = self._crdt_after_open(p, dec_text)
                                except Exception:
                                    pass
                                first = self._current is None
                                self._current = p
                                self._setup_file_monitor(p)
                                self._loading = True
                                self.buffer.set_text(dec_text)
                                self._loading = False
                                self._dirty = False
                                self.save_btn.set_sensitive(False)
                                self.del_btn.set_sensitive(True)
                                self.file_label.set_text(f"🔓 {p} — авто-дешифровано (Secret)")
                                try:
                                    self.editor.set_editable(True)
                                except Exception:
                                    pass
                                self._update_crypto_buttons()
                                try:
                                    self._select_flat_path(str(p))
                                except Exception:
                                    pass
                                it = self._path_to_iter.get(str(p))
                                if it is not None:
                                    try:
                                        self._tree.set_cursor(self._store.get_path(it))
                                    except Exception:
                                        pass
                                if first:
                                    self.stack.set_visible_child_name("Редактор")
                                elif self.stack.get_visible_child_name() == "Просмотр":
                                    self._render_preview()
                                self._refresh_links()
                                try:
                                    self._refresh_outline()
                                except Exception:
                                    pass
                                try:
                                    self._update_search_highlight()
                                except Exception:
                                    pass
                                try:
                                    self._refresh_history()
                                except Exception:
                                    pass
                                try:
                                    self._refresh_comments()
                                except Exception:
                                    pass
                                return
                        else:
                            # нет пароля — попытаемся автозапрос (не блокируем, покажем locked но предложим диалог)
                            # отложенный prompt чтобы не реентерить _open
                            try:
                                GLib.idle_add(lambda: self._crypto_prompt_decrypt(p))
                            except Exception:
                                pass
                except Exception:
                    pass
            first = self._current is None
            self._current = p
            self._setup_file_monitor(p)
            self._loading = True
            try:
                self.buffer.set_text(
                    "🔒 Файл зашифрован (AES-GCM .md.enc)\n\nНажмите «Расшифровать» и введите пароль.\n"
                )
            except Exception:
                pass
            self._loading = False
            self._dirty = False
            self.save_btn.set_sensitive(False)
            self.del_btn.set_sensitive(True)
            self.file_label.set_text(f"🔒 {p} — зашифрован")
            try:
                self.editor.set_editable(False)
            except Exception:
                pass
            self._update_crypto_buttons()
            # выбор в списках как обычно
            try:
                self._select_flat_path(str(p))
            except Exception:
                pass
            it = self._path_to_iter.get(str(p))
            if it is not None:
                try:
                    self._tree.set_cursor(self._store.get_path(it))
                except Exception:
                    pass
            if first:
                self.stack.set_visible_child_name("Редактор")
            self._refresh_links()
            try:
                self._refresh_outline()
            except Exception:
                pass
            try:
                self._refresh_history()
            except Exception:
                pass
            try:
                self._refresh_comments()
            except Exception:
                pass
            return
        try:
            text = self._read_cached(p)
        except OSError as exc:
            self.file_label.set_text(f"не удалось прочитать: {exc}")
            return
        # ── CRDT: слить disk_text с sidecar перед показом ──
        try:
            if self._crdt_is_enabled() and not self._is_encrypted_path(p):
                # инициализация если нужно
                if self._crdt_doc is None or getattr(self._crdt_doc, "doc_id", None) != str(p):
                    self._crdt_init_for_path(p)
                text = self._crdt_after_open(p, text)
        except Exception:
            pass
        first = self._current is None
        self._current = p
        self._setup_file_monitor(p)
        self._loading = True
        self.buffer.set_text(text)
        self._loading = False
        self._dirty = False
        self.save_btn.set_sensitive(False)
        self.del_btn.set_sensitive(True)
        self.file_label.set_text(str(p))
        try:
            self.editor.set_editable(True)
        except Exception:
            pass
        self._update_crypto_buttons()
        # виртуализованный выбор: ListView SingleSelection
        try:
            self._select_flat_path(str(p))
        except Exception:
            # fallback старый шим
            try:
                row = self._path_to_row.get(str(p))
                if row is not None and hasattr(self.listbox, "select_row"):
                    self.listbox.select_row(row)  # type: ignore
            except Exception:
                pass
        it = self._path_to_iter.get(str(p))
        if it is not None:
            try:
                self._tree.set_cursor(self._store.get_path(it))
            except Exception:
                pass
        if first:
            self.stack.set_visible_child_name("Просмотр")
            self._render_preview()
        elif self.stack.get_visible_child_name() == "Просмотр":
            self._render_preview()
        self._refresh_links()
        try:
            self._refresh_outline()
        except Exception:
            pass
        # подсветка поиска при открытии файла
        try:
            self._update_search_highlight()
        except Exception:
            pass
        try:
            self._refresh_history()
        except Exception:
            pass
        try:
            self._refresh_comments()
        except Exception:
            pass

    def _read_cached(self, p: Path) -> str:
        """Чтение файла с mtime-кэшем: повторное открытие заметки — без диска."""
        key = str(p)
        mt = p.stat().st_mtime_ns
        got = self._read_cache.get(key)
        if got is not None and got[0] == mt:
            self._read_cache.move_to_end(key)
            return got[1]
        text = p.read_text(encoding="utf-8")
        self._read_cache[key] = (mt, text)
        self._read_cache.move_to_end(key)
        while len(self._read_cache) > 32:
            self._read_cache.popitem(last=False)
        return text

    # ── Шифрование AES-GCM .md.enc (PBKDF2) ─────────────────────
    def _prompt_password(self, title: str, heading: str, body: str, ok_label: str, on_ok) -> None:
        """Диалог ввода пароля (Adw.AlertDialog + Gtk.PasswordEntry)."""
        dlg = Adw.AlertDialog(heading=heading, body=body)
        dlg.add_response("cancel", "Отмена")
        dlg.add_response("ok", ok_label)
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("ok")
        dlg.set_close_response("cancel")
        entry = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text="Пароль")
        entry.set_margin_top(8)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(entry)
        # AlertDialog extra_child для кастомного виджета (Adw 1.4+)
        try:
            dlg.set_extra_child(box)
        except Exception:
            pass

        def _resp(_d, resp: str) -> None:
            if resp != "ok":
                return
            pwd = entry.get_text()
            if not pwd:
                self.file_label.set_text("пароль не может быть пустым")
                return
            on_ok(pwd)

        dlg.connect("response", _resp)
        dlg.present(self)

    def _on_encrypt(self, _btn) -> None:
        if self._current is None:
            self.file_label.set_text("нет открытого файла для шифрования")
            return
        if self._is_encrypted_path(self._current):
            self.file_label.set_text("файл уже зашифрован")
            return
        if self._dirty:
            # автосохранение перед шифрованием
            try:
                self._on_save(None)
            except Exception:
                pass
        target = self._current

        def _do(pwd: str) -> None:
            if _crypto is None:
                self.file_label.set_text("модуль crypto недоступен")
                return
            try:
                enc_path = _crypto.encrypt_file(target, pwd, delete_original=True)
            except ValueError as exc:
                self.file_label.set_text(f"шифрование: {exc}")
                return
            except ImportError as exc:
                self.file_label.set_text(str(exc))
                return
            except OSError as exc:
                self.file_label.set_text(f"ошибка шифрования: {exc}")
                return
            except Exception as exc:
                self.file_label.set_text(f"ошибка шифрования: {exc}")
                return
            # сброс кэша и текучки
            try:
                self._read_cache.pop(str(target), None)
            except Exception:
                pass
            self._current = Path(enc_path)
            self._cancel_file_monitor()
            self._dirty = False
            self._loading = True
            try:
                self.buffer.set_text(
                    "🔒 Файл зашифрован (AES-GCM .md.enc)\n\nНажмите «Расшифровать» и введите пароль.\n"
                )
            except Exception:
                pass
            self._loading = False
            self.file_label.set_text(f"🔒 зашифровано: {enc_path}")
            self._update_crypto_buttons()
            self._invalidate_tree()
            self.reload(force=True)

        self._prompt_password(
            "Шифрование",
            "Зашифровать заметку?",
            f"«{target.name}» → {target.name}.enc\nAES-GCM, ключ PBKDF2 из пароля.",
            "Зашифровать",
            _do,
        )

    def _on_decrypt(self, _btn) -> None:
        if self._current is None:
            self.file_label.set_text("нет открытого файла для расшифровки")
            return
        if not self._is_encrypted_path(self._current):
            self.file_label.set_text("файл не зашифрован (.enc)")
            return
        target = self._current

        def _do(pwd: str) -> None:
            if _crypto is None:
                self.file_label.set_text("модуль crypto недоступен")
                return
            try:
                dec_path = _crypto.decrypt_file(target, pwd, delete_original=True)
            except ValueError as exc:
                self.file_label.set_text(f"расшифровка: {exc}")
                return
            except ImportError as exc:
                self.file_label.set_text(str(exc))
                return
            except OSError as exc:
                self.file_label.set_text(f"ошибка расшифровки: {exc}")
                return
            except Exception as exc:
                self.file_label.set_text(f"ошибка расшифровки: {exc}")
                return
            try:
                self._read_cache.pop(str(target), None)
            except Exception:
                pass
            self._cancel_file_monitor()
            self._invalidate_tree()
            self.reload(force=True)
            # открыть расшифрованный
            try:
                self._open(Path(dec_path))
            except Exception:
                self.file_label.set_text(f"расшифровано: {dec_path}")

        self._prompt_password(
            "Расшифровка",
            "Расшифровать заметку?",
            f"«{target.name}» — введите пароль для AES-GCM.",
            "Расшифровать",
            _do,
        )

    # ── PDF экспорт (fragilenotes.core.pdf_export) ─────────────────
    def _on_export_pdf(self, _btn) -> None:
        """Кнопка Экспорт — диалог сохранения PDF с темой."""
        if self._current is None:
            self.file_label.set_text("нет открытого файла для экспорта")
            return
        if self._is_encrypted_path(self._current):
            self.file_label.set_text("зашифрованный файл — расшифруйте перед экспортом")
            return
        # автосохранение dirty перед экспортом
        if self._dirty:
            try:
                self._on_save(None)
            except Exception:
                pass
        # Определяем тему: dark по умолчанию, можно взять из settings
        theme = str(self.settings.get("pdf_theme") or self.settings.get("theme") or "dark")
        # Предложить имя: stem.pdf рядом с исходником
        default_name = self._current.with_suffix(".pdf").name
        try:
            dlg = Gtk.FileDialog()
            dlg.set_title("Экспорт в PDF")
            # фильтр PDF
            filt = Gio.ListStore.new(Gtk.FileFilter)
            f = Gtk.FileFilter()
            f.set_name("PDF (*.pdf)")
            f.add_pattern("*.pdf")
            filt.append(f)
            dlg.set_filters(filt)
            try:
                dlg.set_initial_folder(Gio.File.new_for_path(str(self._current.parent)))
            except Exception:
                pass
            try:
                dlg.set_initial_name(default_name)
            except Exception:
                pass
            dlg.save(self.get_root(), None, self._on_export_pdf_done, theme)
            return
        except Exception:
            pass
        # fallback: простой entry-диалог
        self._fallback_export_dialog(theme, default_name)

    def _fallback_export_dialog(self, theme: str, default_name: str) -> None:
        dialog = Adw.Dialog(title="Экспорт в PDF")
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=16,
            margin_end=16,
        )
        box.set_size_request(460, -1)
        lbl = Gtk.Label(
            label=f"Имя PDF (сохранится рядом с {self._current.name if self._current else 'файлом'}):",
            halign=Gtk.Align.START,
            wrap=True,
        )
        entry = Gtk.Entry(text=default_name)
        entry.set_hexpand(True)
        # выбор темы
        theme_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        theme_row.append(Gtk.Label(label="Тема:", css_classes=["dim-hint"]))
        theme_drop = Gtk.DropDown.new_from_strings(["dark", "light", "glass"])
        cur_idx = {"dark": 0, "light": 1, "glass": 2}.get(theme.lower(), 0)
        theme_drop.set_selected(cur_idx)
        theme_row.append(theme_drop)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Отмена")
        ok = Gtk.Button(label="Экспорт", css_classes=["suggested-action"])
        row.append(cancel)
        row.append(ok)
        box.append(lbl)
        box.append(entry)
        box.append(theme_row)
        box.append(row)
        dialog.set_child(box)
        cancel.connect("clicked", lambda *_: dialog.close())

        def _do(*_):
            raw = entry.get_text().strip() or default_name
            if not raw.lower().endswith(".pdf"):
                raw += ".pdf"
            # тема из dropdown
            try:
                sel = int(theme_drop.get_selected())
                tmap = ["dark", "light", "glass"]
                theme_val = tmap[sel] if 0 <= sel < len(tmap) else theme
            except Exception:
                theme_val = theme
            target = self._current.parent / raw if self._current is not None else Path.home() / raw
            dialog.close()
            self._do_export_pdf(self._current, target, theme_val)

        ok.connect("clicked", _do)
        entry.connect("activate", _do)
        dialog.present(self.get_root())

    def _on_export_pdf_done(self, dlg: Gtk.FileDialog, res, theme: str) -> None:
        try:
            f = dlg.save_finish(res)
            if f is None:
                return
            path = Path(f.get_path() or "")
            if not path:
                return
            if not path.suffix.lower() == ".pdf":
                path = path.with_suffix(".pdf")
            self._do_export_pdf(self._current, path, theme)
        except Exception as exc:  # noqa: BLE001
            self.file_label.set_text(f"ошибка диалога экспорта: {exc}")

    def _do_export_pdf(self, md_path: Path | None, pdf_path: Path, theme: str) -> None:
        if md_path is None:
            self.file_label.set_text("нет исходного файла")
            return
        try:
            from ..core.pdf_export import export_to_pdf as _export  # type: ignore
        except Exception as exc:  # noqa: BLE001
            self.file_label.set_text(f"модуль pdf_export недоступен: {exc}")
            return
        try:
            out = _export(md_path, pdf_path, theme)
            self.file_label.set_text(f"PDF экспортирован: {out}")
            # toast если есть overlay
            try:
                root = self.get_root()
                overlay = getattr(root, "toast_overlay", None) if root is not None else None
                if overlay is not None:
                    toast = Adw.Toast.new(f"Экспорт: {Path(out).name}")
                    toast.set_timeout(3)
                    overlay.add_toast(toast)
            except Exception:
                pass
        except FileNotFoundError as exc:
            self.file_label.set_text(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.file_label.set_text(f"ошибка экспорта PDF: {exc}")

    # ── Publish: экспорт vault в статический сайт (fragile publish) ─
    def _on_publish(self, _btn) -> None:
        """Кнопка Публикация — диалог выбора папки и запуск publish в фоне."""
        # автосохранение dirty перед публикацией
        if getattr(self, "_dirty", False) and getattr(self, "_current", None) is not None:
            try:
                self._on_save(None)
            except Exception:
                pass
        vault_root = Path(str(self.settings.get("vault_root") or Path.home()))
        default_out = vault_root / "public"
        # пробуем FileDialog (GTK 4.10+)
        try:
            dlg = Gtk.FileDialog()
            dlg.set_title("Опубликовать сайт — выбери папку (будет очищена)")
            try:
                dlg.set_initial_folder(Gio.File.new_for_path(str(default_out.parent)))
            except Exception:
                pass
            try:
                dlg.set_initial_name(default_out.name)
            except Exception:
                pass
            dlg.select_folder(self.get_root(), None, self._on_publish_folder_chosen)
            return
        except Exception:
            pass
        # fallback: сразу публикуем в default_out
        self._do_publish(default_out)

    def _on_publish_folder_chosen(self, dlg: Gtk.FileDialog, res) -> None:
        try:
            f = dlg.select_folder_finish(res)
            if f is None:
                return
            p = Path(f.get_path() or "")
            if not p:
                return
            # если выбрана существующая папка — используем её; если выбрана родительская + имя — уже в p
            self._do_publish(p)
        except Exception as exc:  # noqa: BLE001
            self.file_label.set_text(f"ошибка выбора папки публикации: {exc}")

    def _do_publish(self, output_dir: Path) -> None:
        self.file_label.set_text(f"публикация → {output_dir} …")
        try:
            self.publish_btn.set_sensitive(False)
        except Exception:
            pass
        settings_copy = dict(self.settings)

        def work() -> None:
            try:
                from fragilenotes.core.publish import build_site  # type: ignore

                # vault_root из настроек
                vr = Path(str(settings_copy.get("vault_root") or Path.home()))
                result = build_site(vr, output_dir, settings=settings_copy, clean=True)
                GLib.idle_add(lambda: self._on_publish_done(result, output_dir))
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)[:500]
                GLib.idle_add(lambda: self._on_publish_error(msg))

        import threading

        threading.Thread(target=work, daemon=True).start()

    def _on_publish_done(self, result: dict, output_dir: Path) -> bool:
        try:
            self.publish_btn.set_sensitive(True)
        except Exception:
            pass
        cnt = result.get("count", 0)
        edges = result.get("edges", 0)
        self.file_label.set_text(f"опубликовано: {cnt} заметок, {edges} связей → {output_dir}")
        try:
            root = self.get_root()
            overlay = getattr(root, "toast_overlay", None) if root is not None else None
            if overlay is not None:
                toast = Adw.Toast.new(f"Публикация: {cnt} заметок → {output_dir.name}/")
                toast.set_timeout(4)
                overlay.add_toast(toast)
        except Exception:
            pass
        # предложить открыть index.html
        try:
            idx = output_dir / "index.html"
            if idx.is_file():
                # небольшая подсказка с кнопкой "Открыть папку"
                pass
        except Exception:
            pass
        return False

    def _on_publish_error(self, msg: str) -> bool:
        try:
            self.publish_btn.set_sensitive(True)
        except Exception:
            pass
        self.file_label.set_text(f"ошибка публикации: {msg}")
        return False

    def _render_preview(self) -> None:
        start, end = self.buffer.get_bounds()
        try:
            self.preview.set_markdown(
                self.buffer.get_text(start, end, True),
                self._highlight,
                vault_root=str(self._root) if hasattr(self, "_root") else None,
                settings=self.settings if hasattr(self, "settings") else None,
            )
        except TypeError:
            # fallback для старой сигнатуры set_markdown(text, highlight)
            self.preview.set_markdown(self.buffer.get_text(start, end, True), self._highlight)

    def _on_switch(self, _stack: Gtk.Stack, _pspec) -> None:
        if self.stack.get_visible_child_name() == "Просмотр":
            self._render_preview()

    def _on_wikilink(self, target: str) -> None:
        p = self._vault.resolve_wikilink(target)
        if p is None:
            self.file_label.set_text(f"[[{target}]] — заметка не найдена")
            return
        if self._current == p and self.stack.get_visible_child_name() == "Просмотр":
            self.stack.set_visible_child_name("Редактор")
            return
        self._highlight = ""
        self._open(p)

    def _on_changed(self, _buf: Gtk.TextBuffer) -> None:
        if self._loading or self._current is None:
            return
        if self._is_encrypted_path(self._current):
            return
        self._dirty = True
        self.save_btn.set_sensitive(True)
        try:
            self._schedule_outline_update()
        except Exception:
            pass
        # живой апдейт подсветки при правке текста — debounce 200мс + фоновый поток
        try:
            if len(self._get_search_query()) >= 2:
                self._schedule_search_highlight()
            else:
                # запрос <2 символов — снять подсветку (debounced, чтобы не мигать)
                # но можно и сразу очистить если было подсвечено
                if self._search_matches:
                    self._schedule_search_highlight()
        except Exception:
            pass
        self._schedule_autosave()

    # ── Автосохранение (debounce 800мс) ──────────────────
    def _schedule_autosave(self) -> None:
        """Сбросить таймер и запустить новый debounce 800мс."""
        if self._autosave_timer is not None:
            try:
                GLib.source_remove(self._autosave_timer)
            except Exception:
                pass
            self._autosave_timer = None
        # не ставим таймер если загрузка или нет файла
        if self._current is None or self._loading:
            return
        self._autosave_timer = GLib.timeout_add(_AUTOSAVE_MS, self._do_autosave)

    def _cancel_autosave(self) -> None:
        if self._autosave_timer is not None:
            try:
                GLib.source_remove(self._autosave_timer)
            except Exception:
                pass
            self._autosave_timer = None

    def _do_autosave(self) -> bool:
        self._autosave_timer = None
        if not self._alive or not self._dirty or self._current is None:
            return False
        # файл должен существовать (иначе _on_save не вызовется)
        try:
            if not self._current.exists() or not self._current.is_file():
                return False
        except Exception:
            return False
        try:
            self._on_save(None)
        except Exception:
            pass
        return False

    # ── FileMonitor открытого файла ───────────────────────
    def _setup_file_monitor(self, p: Path) -> None:
        self._cancel_file_monitor()
        try:
            gfile = Gio.File.new_for_path(str(p))
            mon = gfile.monitor_file(Gio.FileMonitorFlags.NONE, None)
            mon.connect("changed", self._on_external_changed)
            self._file_monitor = mon
            self._file_monitor_path = str(p)
            self._external_toast_pending = False
        except Exception:
            self._file_monitor = None
            self._file_monitor_path = None

    def _cancel_file_monitor(self) -> None:
        if self._file_monitor is not None:
            try:
                self._file_monitor.cancel()
            except Exception:
                pass
            self._file_monitor = None
        self._file_monitor_path = None
        self._external_toast_pending = False

    def _on_external_changed(
        self,
        _mon: Gio.FileMonitor,
        _file: Gio.File,
        _other: Gio.File | None,
        event: Gio.FileMonitorEvent,
    ) -> None:
        # интересуют только изменения содержимого
        if event not in (
            Gio.FileMonitorEvent.CHANGED,
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.CREATED,
        ):
            return
        if self._suppress_monitor:
            return
        if self._current is None:
            return
        # если путь монитора не совпадает — игнорируем
        if self._file_monitor_path is not None and self._file_monitor_path != str(self._current):
            return
        # дебаунс toast — не спамить при множественных CHANGED
        if self._external_toast_pending:
            return
        # если dirty — всё равно показываем toast, но пользователь решит
        # проверяем что файл реально изменился на диске (mtime отличается от кэша)
        try:
            if not self._current.exists():
                return
        except Exception:
            return
        self._external_toast_pending = True
        # показать в следующем idle чтобы не реентерить монитор
        GLib.idle_add(self._show_external_toast)

    def _show_external_toast(self) -> bool:
        # сбросить флаг через небольшую задержку чтобы не спамить
        GLib.timeout_add(1500, self._reset_external_pending)
        # найти ToastOverlay у окна
        overlay = None
        try:
            root = self.get_root()
            if root is not None and hasattr(root, "toast_overlay"):
                overlay = getattr(root, "toast_overlay")
            else:
                # поиск через get_ancestor
                parent = self.get_parent()
                while parent is not None:
                    if hasattr(parent, "toast_overlay"):
                        overlay = getattr(parent, "toast_overlay")
                        break
                    parent = parent.get_parent() if hasattr(parent, "get_parent") else None
        except Exception:
            overlay = None
        if overlay is None:
            # fallback: просто пометить label
            try:
                self.file_label.set_text(
                    f"⚠ файл изменён извне: {self._current.name if self._current else ''}"
                )
            except Exception:
                pass
            return False
        try:
            toast = Adw.Toast.new("Файл изменён извне — перезагрузить?")
            toast.set_button_label("Перезагрузить")
            toast.set_timeout(8)
            # по клику — перечитать с диска
            toast.connect("button-clicked", lambda *_: self._reload_external())
            overlay.add_toast(toast)
        except Exception:
            pass
        return False

    def _reset_external_pending(self) -> bool:
        self._external_toast_pending = False
        return False

    def _reload_external(self) -> None:
        if self._current is None:
            return
        self._suppress_monitor = True
        try:
            # сбросить кэш для этого файла чтобы _read_cached не вернул старое
            try:
                self._read_cache.pop(str(self._current), None)
            except Exception:
                pass
            text = self._read_cached(self._current)
        except OSError as exc:
            self.file_label.set_text(f"не удалось перечитать: {exc}")
            self._suppress_monitor = False
            return
        except Exception as exc:
            self.file_label.set_text(f"ошибка перезагрузки: {exc}")
            self._suppress_monitor = False
            return
        self._loading = True
        try:
            self.buffer.set_text(text)
        finally:
            self._loading = False
        self._dirty = False
        self.save_btn.set_sensitive(False)
        self._cancel_autosave()
        try:
            self._refresh_links()
        except Exception:
            pass
        try:
            self._refresh_outline()
        except Exception:
            pass
        try:
            self._update_search_highlight()
        except Exception:
            pass
        if self.stack.get_visible_child_name() == "Просмотр":
            self._render_preview()
        self.file_label.set_text(str(self._current))
        try:
            self._refresh_comments()
        except Exception:
            pass
        # FTS5: обновить индекс после внешней перезагрузки
        try:
            if _fts is not None and self._current is not None:
                _p = Path(self._current)
                threading.Thread(target=lambda: _fts.upsert_file(_p), daemon=True).start()
        except Exception:
            pass
        # снять подавление после паузы чтобы файловый монитор не сработал на наш же reload
        GLib.timeout_add(1000, self._reset_suppress)

    def _reset_suppress(self) -> bool:
        self._suppress_monitor = False
        return False

    def _on_save(self, _btn: Gtk.Button | None) -> None:
        if self._current is None or not self._dirty:
            return
        if self._is_encrypted_path(self._current):
            # крипто-папка: .md.enc внутри Secret/ — перешифровать авто
            if _crypto_folder is not None:
                try:
                    if _crypto_folder.is_in_crypto_folder(
                        self._current, self.settings
                    ) and _crypto_folder.is_auto_enabled(self.settings):
                        start, end = self.buffer.get_bounds()
                        text = self.buffer.get_text(start, end, True)
                        try:
                            self._crdt_before_save(self._current, text)
                        except Exception:
                            pass
                        if self._crypto_handle_save(self._current, text):
                            # _crypto_handle_save уже записал (или запросил пароль)
                            # если пароль был — продолжим пост-обработку как обычный save
                            # проверяем: если вернулся True и пароль был, dirty сбросим
                            pwd = _crypto_folder.resolve_password(self.settings)
                            if pwd:
                                self._cancel_autosave()
                                self._dirty = False
                                self.save_btn.set_sensitive(False)
                                try:
                                    self._vault.invalidate_cache()
                                except Exception:
                                    pass
                                self._refresh_links()
                                try:
                                    self._refresh_tags()
                                except Exception:
                                    pass
                                try:
                                    self._refresh_history()
                                except Exception:
                                    pass
                                try:
                                    if _fts is not None:
                                        _p = Path(self._current)
                                        threading.Thread(
                                            target=lambda: _fts.upsert_file(_p), daemon=True
                                        ).start()
                                except Exception:
                                    pass
                                try:
                                    root = self.get_root() if hasattr(self, "get_root") else None
                                    pm = (
                                        getattr(root, "plugin_manager", None)
                                        if root is not None
                                        else None
                                    )
                                    if pm is not None and hasattr(pm, "trigger"):
                                        try:
                                            pm.trigger("on_save", self._current, text)
                                        except TypeError:
                                            pm.trigger("on_save", self._current)
                                    else:
                                        from ..core.plugins import trigger as _plug_trigger

                                        try:
                                            _plug_trigger("on_save", self._current, text)
                                        except TypeError:
                                            _plug_trigger("on_save", self._current)
                                except Exception:
                                    pass
                                try:
                                    self._trigger_auto_tag()
                                except Exception:
                                    pass
                                # ensure suppress reset
                                GLib.timeout_add(1200, self._reset_suppress)
                                return
                            else:
                                # пароль запрошен — ждём
                                return
                except Exception:
                    pass
            self.file_label.set_text("файл зашифрован — расшифруйте перед сохранением")
            return
        self._cancel_autosave()
        start, end = self.buffer.get_bounds()
        text = self.buffer.get_text(start, end, True)
        # ── CRDT: обновить документ и sidecar перед записью ──
        try:
            self._crdt_before_save(self._current, text)
        except Exception:
            pass
        # ── крипто-папка авто-шифрование: .md внутри Secret/ -> .md.enc ──
        if _crypto_folder is not None:
            try:
                if _crypto_folder.should_auto_encrypt(self._current, self.settings):
                    if self._crypto_handle_save(self._current, text):
                        pwd = _crypto_folder.resolve_password(self.settings)
                        if pwd:
                            # успешно зашифровано — пост-обработка как в обычном save
                            self._dirty = False
                            self.save_btn.set_sensitive(False)
                            try:
                                self._vault.invalidate_cache()
                            except Exception:
                                pass
                            self._refresh_links()
                            try:
                                self._refresh_tags()
                            except Exception:
                                pass
                            try:
                                self._refresh_history()
                            except Exception:
                                pass
                            try:
                                if _fts is not None and self._current is not None:
                                    _p = Path(self._current)
                                    threading.Thread(
                                        target=lambda: _fts.upsert_file(_p), daemon=True
                                    ).start()
                            except Exception:
                                pass
                            try:
                                root = self.get_root() if hasattr(self, "get_root") else None
                                pm = (
                                    getattr(root, "plugin_manager", None)
                                    if root is not None
                                    else None
                                )
                                if pm is not None and hasattr(pm, "trigger"):
                                    try:
                                        pm.trigger("on_save", self._current, text)
                                    except TypeError:
                                        pm.trigger("on_save", self._current)
                                else:
                                    from ..core.plugins import trigger as _plug_trigger

                                    try:
                                        _plug_trigger("on_save", self._current, text)
                                    except TypeError:
                                        _plug_trigger("on_save", self._current)
                            except Exception:
                                pass
                            try:
                                self._trigger_auto_tag()
                            except Exception:
                                pass
                            GLib.timeout_add(1200, self._reset_suppress)
                            return
                        else:
                            return
            except Exception:
                pass
        self._suppress_monitor = True
        try:
            self._current.write_text(text, encoding="utf-8")
        except Exception:
            self._suppress_monitor = False
            raise
        self._dirty = False
        self.save_btn.set_sensitive(False)
        try:
            self._read_cache[str(self._current)] = (self._current.stat().st_mtime_ns, text)
        except OSError:
            pass
        # снять подавление через 1с — CHANGED от нашей записи проигнорируется
        GLib.timeout_add(1200, self._reset_suppress)
        # wikilink граф мог измениться — сбросить кэш и обновить панели
        try:
            self._vault.invalidate_cache()
        except Exception:
            pass
        self._refresh_links()
        try:
            self._refresh_tags()
        except Exception:
            pass
        try:
            self._refresh_history()
        except Exception:
            pass
        # FTS5: инкрементальное обновление индекса при изменении файла
        try:
            if _fts is not None and self._current is not None:
                # фоновое обновление чтобы не блокировать UI
                _p = Path(self._current)
                threading.Thread(target=lambda: _fts.upsert_file(_p), daemon=True).start()
        except Exception:
            pass
        # plugin hook on_save (изолирован, поддерживает autosave)
        try:
            root = self.get_root() if hasattr(self, "get_root") else None
            pm = getattr(root, "plugin_manager", None) if root is not None else None
            if pm is not None and hasattr(pm, "trigger"):
                try:
                    pm.trigger("on_save", self._current, text)
                except TypeError:
                    pm.trigger("on_save", self._current)
            else:
                from ..core.plugins import trigger as _plug_trigger

                try:
                    _plug_trigger("on_save", self._current, text)
                except TypeError:
                    _plug_trigger("on_save", self._current)
        except Exception:
            pass
        # ── Auto-tagging LLM: предложить теги после сохранения ──
        try:
            self._trigger_auto_tag()
        except Exception:
            pass

    def _on_delete(self, _btn: Gtk.Button) -> None:
        if self._current is None:
            return
        target = self._current
        dialog = Adw.AlertDialog(
            heading="Удалить заметку?",
            body=f"«{target.name}» будет удалена безвозвратно.",
        )
        dialog.add_response("cancel", "Отмена")
        dialog.add_response("delete", "Удалить")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._delete_response, target)
        dialog.present(self)

    def _delete_response(self, dialog: Adw.AlertDialog, response: str, target: Path) -> None:
        if response != "delete":
            return
        try:
            target.unlink()
        except OSError as exc:
            self.file_label.set_text(f"не удалось удалить: {exc}")
            return
        # удалить sidecar комментариев если есть
        try:
            from .comments import get_comments_path as _cpath

            cp = _cpath(target)
            if cp.is_file():
                cp.unlink()
        except Exception:
            pass
        if self._current == target:
            self._current = None
            self._dirty = False
            self._cancel_autosave()
            self._cancel_file_monitor()
            self.file_label.set_text("Файл удалён")
            self.save_btn.set_sensitive(False)
            self.del_btn.set_sensitive(False)
            try:
                if hasattr(self.listbox, "unselect_all"):
                    self.listbox.unselect_all()  # type: ignore
                elif self._selection is not None:
                    self._selection.set_selected(Gtk.INVALID_LIST_POSITION)
            except Exception:
                pass
            # скрыть панель связей
            try:
                self._links_panel.set_visible(False)
            except Exception:
                pass
            try:
                self._update_crypto_buttons()
            except Exception:
                pass
            try:
                self._refresh_history()
            except Exception:
                pass
            try:
                self._refresh_comments()
            except Exception:
                pass
        # FTS5: удалить из индекса
        try:
            if _fts is not None:
                _p = Path(target)
                threading.Thread(target=lambda: _fts.remove_file(_p), daemon=True).start()
        except Exception:
            pass
        self._invalidate_tree()
        self.reload(force=True)

    # ── Новый файл ───────────────────────────────────────────
    def _popup_new(self, pop: Gtk.Popover) -> None:
        self._new_entry.set_text("")
        # обновить список шаблонов в dropdown (vault мог измениться)
        try:
            from ..core.templates import list_templates as _lt

            if getattr(self, "_new_template_drop", None) is not None:
                tpls = _lt(self.settings)
                names = ["— без шаблона —"] + [p.stem for p in tpls]
                # пересобрать модель
                strings = Gtk.StringList.new(names)
                self._new_template_drop.set_model(strings)
                self._new_template_drop.set_selected(0)
        except Exception:
            pass
        pop.popup()
        self._new_entry.grab_focus()

    def _on_create_note(self, _btn: Gtk.Button, entry: Gtk.Entry, pop: Gtk.Popover) -> None:
        name = entry.get_text().strip()
        # запомним выбранный шаблон до закрытия popover
        tpl_name: str | None = None
        try:
            drop = getattr(self, "_new_template_drop", None)
            if drop is not None:
                idx = int(drop.get_selected())
                if idx > 0:
                    # достать текст из модели
                    model = drop.get_model()
                    if model is not None:
                        tpl_name = model.get_string(idx)  # type: ignore[attr-defined]
        except Exception:
            tpl_name = None
        pop.popdown()
        if not name:
            return
        if not name.lower().endswith(".md"):
            name += ".md"
        base = self._current.parent if self._current else self._root
        target = base / name
        if target.exists():
            self.file_label.set_text("файл уже существует")
            return
        # применение шаблона если выбран
        content = f"# {target.stem}\n"
        if tpl_name:
            try:
                from ..core.templates import render_for_new_note

                rendered = render_for_new_note(self.settings, tpl_name, target.stem)
                if rendered:
                    content = rendered
            except Exception:
                pass
        target.write_text(content, encoding="utf-8")
        # ── крипто-папка: если в Secret/ и auto_encrypt — сразу шифруем (AES-GCM) ──
        if _crypto_folder is not None:
            try:
                if _crypto_folder.should_auto_encrypt(target, self.settings):
                    pwd = _crypto_folder.resolve_password(self.settings)
                    if pwd:
                        try:
                            dst = _crypto_folder.write_encrypted_text(
                                target, content, pwd, delete_original=True
                            )
                            target = Path(dst)
                        except Exception:
                            pass
            except Exception:
                pass
        # FTS5: индексация нового файла
        try:
            if _fts is not None:
                _p = Path(target)
                threading.Thread(target=lambda: _fts.upsert_file(_p), daemon=True).start()
        except Exception:
            pass
        # plugin hook on_new
        try:
            root = self.get_root() if hasattr(self, "get_root") else None
            pm = getattr(root, "plugin_manager", None) if root is not None else None
            if pm is not None and hasattr(pm, "trigger"):
                try:
                    pm.trigger("on_new", target)
                except TypeError:
                    pm.trigger("on_new", target)
            else:
                from ..core.plugins import trigger as _plug_trigger

                try:
                    _plug_trigger("on_new", target)
                except TypeError:
                    _plug_trigger("on_new", target)
        except Exception:
            pass
        self._invalidate_tree()
        self.reload(force=True)
        self._open(target)

    def _invalidate_tree(self) -> None:
        try:
            self._vault.invalidate_cache()
        except Exception:
            from ..services import vault

            vault.invalidate_vault_cache()
