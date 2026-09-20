"""Шаблоны с переменными — хранение в vault/_System/Templates/, UI создания/редактирования, применение при создании заметки."""

from __future__ import annotations

from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..core.templates import (
    TEMPLATE_VARS,
    delete_template,
    ensure_templates_dir,
    get_templates_dir,
    list_templates,
    load_template,
    render_template,
    save_template,
)
from .markdown import MarkdownView
from .scale import attach_zoom_keys
from .widgets import empty_state, view_header


def _content_clamp(child: Gtk.Widget) -> Adw.Clamp:
    clamp = Adw.Clamp(maximum_size=920, tightening_threshold=720)
    clamp.set_child(child)
    return clamp


def _sanitize_filename(name: str) -> str:
    """Безопасное имя файла: убираем слэши, запрещённые символы, добавляем .md."""
    s = name.strip()
    if not s:
        return ""
    # убрать путь
    s = s.replace("/", "_").replace("\\", "_")
    # запрещённые в файловой системе
    for ch in ('\0', ':', '*', '?', '"', '<', '>', '|'):
        s = s.replace(ch, "_")
    s = s.strip()
    if not s.lower().endswith(".md"):
        s += ".md"
    # не позволяем скрытые и пустые
    if s.startswith("."):
        s = "_" + s
    return s


class TemplatesView(Gtk.Box):
    """UI для шаблонов: список + редактор + предпросмотр."""

    def __init__(self, settings: dict) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self._current: Path | None = None
        self._dirty = False
        self._loading = False
        self._alive = True
        self._autosave_timer: int | None = None
        self._file_monitor: Gio.FileMonitor | None = None
        self._suppress_monitor = False
        self.connect("destroy", self._on_destroy)
        self._build()

    def _on_destroy(self, _w) -> None:
        self._alive = False
        if self._autosave_timer is not None:
            try:
                GLib.source_remove(self._autosave_timer)
            except Exception:
                pass
            self._autosave_timer = None
        self._cancel_monitor()

    # ── сборка ───────────────────────────────────────────────
    def _build(self) -> None:
        self.append(view_header("📑", "Шаблоны", "Хранение в vault/_System/Templates/ · переменные {{date}} {{time}} {{title}} {{uuid}}"))

        # hint bar с переменными
        hint = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["toolbar"])
        hint.set_margin_start(14)
        hint.set_margin_end(14)
        hint.append(Gtk.Label(label="Переменные:", css_classes=["dim-hint"]))
        for v in TEMPLATE_VARS:
            pill = Gtk.Label(label=v, css_classes=["pill", "pill-run"])
            hint.append(pill)
        hint.append(Gtk.Label(label="· {{date:YYYY-MM-DD}} {{time:HH:mm}} тоже поддерживаются", css_classes=["dim-hint", "dim-label"]))
        self.append(hint)

        main = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, hexpand=True, vexpand=True)
        main.set_margin_start(14)
        main.set_margin_end(14)
        main.set_margin_bottom(14)

        # Левая панель — список шаблонов
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.set_size_request(280, -1)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        new_btn = Gtk.Button(label="＋ Новый", css_classes=["suggested-action"])
        new_btn.connect("clicked", self._on_new_dialog)
        actions.append(new_btn)
        self._filter = Gtk.SearchEntry(placeholder_text="Поиск шаблонов…", hexpand=True)
        self._filter.connect("search-changed", lambda *_: self._render_list())
        actions.append(self._filter)
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Обновить")
        refresh_btn.connect("clicked", lambda *_: self.reload(force=True))
        actions.append(refresh_btn)
        left.append(actions)

        self.listbox = Gtk.ListBox(css_classes=["file-list"])
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.connect("row-activated", self._on_row_activated)
        scroller = Gtk.ScrolledWindow(vexpand=True, css_classes=["editor-frame"])
        scroller.set_child(self.listbox)
        try:
            scroller.set_propagate_natural_width(False)
        except AttributeError:
            pass
        left.append(scroller)

        # пустые состояния
        self._empty = empty_state("📑", "Шаблонов пока нет", hint="Создайте первый шаблон — он появится в vault/_System/Templates/", action_label="Создать шаблон", on_action=self._on_new_dialog)
        self._empty.set_visible(False)
        left.append(self._empty)

        self._filter_empty = empty_state("🔍", "Ничего не найдено", hint="Попробуйте другой запрос", action_label="Очистить", on_action=lambda: self._filter.set_text(""))
        self._filter_empty.set_visible(False)
        left.append(self._filter_empty)

        info = Gtk.Label(label="Хранение: vault/_System/Templates/", css_classes=["dim-hint"], halign=Gtk.Align.START, wrap=True, xalign=0)
        left.append(info)

        self._count = Gtk.Label(label="", css_classes=["dim-hint"])
        left.append(self._count)

        main.append(left)

        # Правая панель — редактор
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True)
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["toolbar"])
        self.file_label = Gtk.Label(label="Выберите шаблон", css_classes=["dim-label", "dim-hint"], hexpand=True, halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        toolbar.append(self.file_label)
        self.rename_btn = Gtk.Button(label="Переименовать")
        self.rename_btn.connect("clicked", self._on_rename)
        self.rename_btn.set_sensitive(False)
        toolbar.append(self.rename_btn)
        self.dup_btn = Gtk.Button(label="Дублировать")
        self.dup_btn.connect("clicked", self._on_duplicate)
        self.dup_btn.set_sensitive(False)
        toolbar.append(self.dup_btn)
        self.apply_btn = Gtk.Button(label="Создать заметку", css_classes=["suggested-action"], tooltip_text="Создать новую заметку из этого шаблона")
        self.apply_btn.connect("clicked", self._on_apply)
        self.apply_btn.set_sensitive(False)
        toolbar.append(self.apply_btn)
        self.del_btn = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Удалить", css_classes=["destructive-action"])
        self.del_btn.connect("clicked", self._on_delete)
        self.del_btn.set_sensitive(False)
        toolbar.append(self.del_btn)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher(stack=self.stack, css_classes=["toolbar-switcher"])
        toolbar.append(switcher)
        self.save_btn = Gtk.Button(label="Сохранить", css_classes=["suggested-action"])
        self.save_btn.set_sensitive(False)
        self.save_btn.connect("clicked", self._on_save)
        toolbar.append(self.save_btn)
        right.append(toolbar)

        self.buffer = Gtk.TextBuffer()
        self.buffer.connect("changed", self._on_changed)
        self.editor = Gtk.TextView(buffer=self.buffer, wrap_mode=Gtk.WrapMode.WORD, hexpand=True, vexpand=True, css_classes=["editor"], top_margin=14, bottom_margin=16, left_margin=20, right_margin=20)

        ed_scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True, css_classes=["editor-frame"])
        ed_scroller.set_child(self.editor)

        self.preview = MarkdownView()
        pv_scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True, css_classes=["editor-frame", "md-read"])
        pv_scroller.set_child(self.preview)

        self.stack.add_named(_content_clamp(ed_scroller), "Редактор")
        self.stack.add_named(_content_clamp(pv_scroller), "Просмотр")
        self.stack.set_visible_child_name("Редактор")
        self.stack.connect("notify::visible-child-name", self._on_switch)

        # область предпросмотра переменных
        var_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["toolbar"])
        var_bar.append(Gtk.Label(label="Предпросмотр переменных:", css_classes=["dim-hint"]))
        self._preview_entry = Gtk.Entry(placeholder_text="Название для {{title}} (опционально)", hexpand=True, css_classes=["flat"])
        self._preview_entry.connect("activate", lambda *_: self._render_preview())
        var_bar.append(self._preview_entry)
        preview_btn = Gtk.Button(label="Обновить превью")
        preview_btn.connect("clicked", lambda *_: self._render_preview())
        var_bar.append(preview_btn)
        right.append(var_bar)

        right.append(self.stack)
        main.append(right)
        self.append(main)

        attach_zoom_keys(self)
        # Ctrl+S
        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.connect("key-pressed", self._on_key_save)
        self.add_controller(key_ctrl)

        self.reload()

    # ── данные ───────────────────────────────────────────────
    def reload(self, force: bool = False) -> None:  # noqa: ARG002
        """Перечитать список шаблонов."""
        try:
            ensure_templates_dir(self.settings)
        except Exception:
            pass
        self._render_list()
        # если текущий файл удалён — сбросить
        if self._current is not None and not self._current.exists():
            self._current = None
            self._set_editor_text("", None)
        self._update_count()

    def _render_list(self) -> None:
        while (child := self.listbox.get_first_child()) is not None:
            self.listbox.remove(child)
        try:
            templates = list_templates(self.settings)
        except Exception:
            templates = []
        q = self._filter.get_text().strip().lower() if hasattr(self, "_filter") else ""
        filtered = [p for p in templates if not q or q in p.stem.lower() or q in p.name.lower()]
        for p in filtered:
            row = Gtk.ListBoxRow(css_classes=["nav-item"])
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_margin_top(4)
            box.set_margin_bottom(4)
            box.set_margin_start(8)
            box.set_margin_end(8)
            icon = Gtk.Label(label="📄", css_classes=["sb-nav-icon"])
            name = Gtk.Label(label=p.stem, hexpand=True, halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            box.append(icon)
            box.append(name)
            # hint path
            hint = Gtk.Label(label=p.name, css_classes=["dim-hint"], ellipsize=Pango.EllipsizeMode.MIDDLE)
            box.append(hint)
            row.set_child(box)
            row._path = p  # type: ignore[attr-defined]
            self.listbox.append(row)
        has = bool(templates)
        filtered_has = bool(filtered)
        self._empty.set_visible(not has)
        self._filter_empty.set_visible(has and not filtered_has)
        self.listbox.set_visible(filtered_has)
        # подсветить выбранный
        if self._current is not None:
            for row in self._iter_rows():
                if getattr(row, "_path", None) == self._current:
                    self.listbox.select_row(row)
                    break
        self._update_count()

    def _iter_rows(self):
        row = self.listbox.get_first_child()
        while row is not None:
            yield row  # type: ignore[misc]
            row = row.get_next_sibling()

    def _update_count(self) -> None:
        try:
            n = len(list_templates(self.settings))
        except Exception:
            n = 0
        self._count.set_text(f"{n} шаблонов" if n else "")

    # ── выбор шаблона ──────────────────────────────────────
    def _on_row_activated(self, _box, row: Gtk.ListBoxRow) -> None:
        p: Path = getattr(row, "_path", None)  # type: ignore
        if p is not None:
            self.open_path(p)

    def open_path(self, path: Path) -> None:
        """Открыть шаблон в редакторе (вызывается извне тоже)."""
        # автосохр перед переключением
        if self._dirty and self._current is not None:
            self._on_save(None)
        self._current = Path(path)
        self._setup_monitor(self._current)
        try:
            text = load_template(self._current)
        except OSError as e:
            text = f"# ошибка чтения: {e}"
        self._set_editor_text(text, self._current)
        # выделить строку
        for row in self._iter_rows():
            if getattr(row, "_path", None) == self._current:
                self.listbox.select_row(row)
                break

    def _set_editor_text(self, text: str, path: Path | None) -> None:
        self._loading = True
        self.buffer.set_text(text or "")
        self._loading = False
        self._dirty = False
        self.save_btn.set_sensitive(False)
        self.rename_btn.set_sensitive(path is not None)
        self.dup_btn.set_sensitive(path is not None)
        self.apply_btn.set_sensitive(path is not None)
        self.del_btn.set_sensitive(path is not None)
        if path is None:
            self.file_label.set_text("Выберите шаблон")
        else:
            rel = path.name
            self.file_label.set_text(rel)
        self._render_preview()
        self._update_preview_vars_hint()

    def _update_preview_vars_hint(self) -> None:
        # noop, placeholder for future
        pass

    # ── редактирование ─────────────────────────────────────
    def _on_changed(self, _buf) -> None:
        if self._loading:
            return
        if self._current is None:
            return
        self._dirty = True
        self.save_btn.set_sensitive(True)
        self.file_label.set_text(f"{self._current.name} · не сохранено")
        self._schedule_autosave()
        self._schedule_preview()

    def _schedule_autosave(self) -> None:
        if self._autosave_timer is not None:
            try:
                GLib.source_remove(self._autosave_timer)
            except Exception:
                pass
        self._autosave_timer = GLib.timeout_add(800, self._do_autosave)

    def _do_autosave(self) -> bool:
        self._autosave_timer = None
        if self._dirty and self._current is not None:
            self._on_save(None)
        return False

    def _schedule_preview(self) -> None:
        GLib.idle_add(self._render_preview)

    def _on_switch(self, *_a) -> None:
        if self.stack.get_visible_child_name() == "Просмотр":
            self._render_preview()

    def _render_preview(self) -> None:
        if not hasattr(self, "preview"):
            return
        try:
            start, end = self.buffer.get_bounds()
            raw = self.buffer.get_text(start, end, True)
            title = self._preview_entry.get_text().strip() if hasattr(self, "_preview_entry") else ""
            # для превью используем title из поля или имя шаблона
            if not title and self._current is not None:
                title = self._current.stem
            rendered = render_template(raw, title or "Пример заметки")
            self.preview.set_markdown(rendered)
        except Exception:
            try:
                start, end = self.buffer.get_bounds()
                self.preview.set_markdown(self.buffer.get_text(start, end, True))
            except Exception:
                pass

    def _on_key_save(self, _ctrl, keyval, _keycode, state) -> bool:
        from gi.repository import Gdk

        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and keyval in (Gdk.KEY_s, Gdk.KEY_S) and not (state & Gdk.ModifierType.SHIFT_MASK):
            self._on_save(None)
            return True
        return False

    # ── действия ───────────────────────────────────────────
    def _on_save(self, _btn) -> None:
        if self._current is None:
            return
        start, end = self.buffer.get_bounds()
        text = self.buffer.get_text(start, end, True)
        self._suppress_monitor = True
        try:
            save_template(self._current, text)
        except OSError as e:
            self._show_toast(f"Ошибка сохранения: {e}")
            self._suppress_monitor = False
            return
        self._dirty = False
        self.save_btn.set_sensitive(False)
        self.file_label.set_text(self._current.name)
        self._show_toast(f"сохранено ✓ {self._current.name}")
        GLib.timeout_add(300, self._clear_suppress)

    def _clear_suppress(self) -> bool:
        self._suppress_monitor = False
        return False

    def _on_delete(self, _btn) -> None:
        if self._current is None:
            return
        path = self._current
        # диалог подтверждения
        dlg = Adw.MessageDialog.new(self.get_root(), f"Удалить шаблон «{path.name}»?")
        dlg.add_response("cancel", "Отмена")
        dlg.add_response("delete", "Удалить")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def on_resp(d, resp: str) -> None:
            if resp == "delete":
                delete_template(path)
                self._current = None
                self._cancel_monitor()
                self._set_editor_text("", None)
                self._render_list()
                self._show_toast(f"удалён {path.name}")

        dlg.connect("response", on_resp)
        dlg.present()

    def _on_new_dialog(self, *_a) -> None:
        dlg = Adw.Dialog(title="Новый шаблон")
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, css_classes=["dialog-body"], margin_top=8, margin_bottom=8, margin_start=24, margin_end=24)
        hint = Gtk.Label(label=f"Создаст файл в {get_templates_dir(self.settings)}", css_classes=["dim-hint"], wrap=True, halign=Gtk.Align.START, xalign=0)
        body.append(hint)
        entry = Gtk.Entry(placeholder_text="Имя шаблона (без .md)", hexpand=True)
        body.append(entry)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["btn-row"])
        cancel = Gtk.Button(label="Отмена", css_classes=["mod-neutral"])
        create = Gtk.Button(label="Создать", css_classes=["suggested-action", "mod-cta"])
        cancel.connect("clicked", lambda *_: dlg.close())
        row.append(cancel)
        row.append(create)
        row.set_halign(Gtk.Align.END)
        body.append(row)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(body)
        dlg.set_child(content)
        body.set_size_request(360, -1)

        def do_create(*_):
            name = _sanitize_filename(entry.get_text().strip())
            if not name:
                return
            target = get_templates_dir(self.settings) / name
            if target.exists():
                self._show_toast(f"уже есть: {name}")
                return
            # дефолтное содержимое с подсказкой переменных
            default = (
                "---\n"
                'title: "{{title}}" \n'
                "date: {{date}}\n"
                "---\n\n"
                "# {{title}}\n\n"
                "Дата: {{date}} · Время: {{time}} · ID: {{uuid}}\n\n"
            )
            try:
                ensure_templates_dir(self.settings)
                save_template(target, default)
            except OSError as e:
                self._show_toast(f"ошибка: {e}")
                return
            dlg.close()
            self._render_list()
            self.open_path(target)
            self._show_toast(f"создан {name}")

        create.connect("clicked", do_create)
        entry.connect("activate", do_create)
        dlg.present(self)
        entry.grab_focus()

    def _on_rename(self, _btn) -> None:
        if self._current is None:
            return
        cur = self._current
        dlg = Adw.Dialog(title="Переименовать")
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, css_classes=["dialog-body"], margin_top=8, margin_bottom=8, margin_start=24, margin_end=24)
        entry = Gtk.Entry(text=cur.stem, hexpand=True)
        body.append(entry)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        cancel = Gtk.Button(label="Отмена", css_classes=["mod-neutral"])
        ok = Gtk.Button(label="Переименовать", css_classes=["suggested-action"])
        cancel.connect("clicked", lambda *_: dlg.close())
        row.append(cancel)
        row.append(ok)
        row.set_halign(Gtk.Align.END)
        body.append(row)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(body)
        dlg.set_child(box)
        body.set_size_request(340, -1)

        def do_ren(*_):
            new = _sanitize_filename(entry.get_text().strip())
            if not new or new == cur.name:
                dlg.close()
                return
            target = cur.parent / new
            if target.exists():
                self._show_toast(f"уже есть: {new}")
                return
            try:
                cur.rename(target)
            except OSError as e:
                self._show_toast(f"ошибка: {e}")
                return
            dlg.close()
            self._current = target
            self._setup_monitor(target)
            self._render_list()
            self.file_label.set_text(target.name)
            self._show_toast(f"переименован → {new}")

        ok.connect("clicked", do_ren)
        entry.connect("activate", do_ren)
        dlg.present(self)
        entry.grab_focus()

    def _on_duplicate(self, _btn) -> None:
        if self._current is None:
            return
        src = self._current
        try:
            text = load_template(src)
        except OSError:
            text = ""
        base = src.stem + " copy"
        name = _sanitize_filename(base)
        target = src.parent / name
        # инкремент если существует
        i = 2
        while target.exists():
            name = _sanitize_filename(f"{src.stem} copy {i}")
            target = src.parent / name
            i += 1
        try:
            save_template(target, text)
        except OSError as e:
            self._show_toast(f"ошибка: {e}")
            return
        self._render_list()
        self.open_path(target)
        self._show_toast(f"дублирован → {target.name}")

    def _on_apply(self, _btn) -> None:
        """Создать заметку из текущего шаблона: диалог имени → render_template → файл."""
        if self._current is None:
            return
        # сохранить перед применением если грязный
        if self._dirty:
            self._on_save(None)
        root = Path(self.settings.get("vault_root") or Path.home() / "desktop")
        # по умолчанию — корень vault, либо предложить папку рядом с открытой заметкой если есть FilesView
        # для простоты — диалог выбора имени и папки (относительно vault)
        dlg = Adw.Dialog(title="Новая заметка из шаблона")
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, css_classes=["dialog-body"], margin_top=8, margin_bottom=8, margin_start=24, margin_end=24)
        body.append(Gtk.Label(label=f"Шаблон: {self._current.name}", halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"]))
        entry = Gtk.Entry(placeholder_text="Название заметки", hexpand=True)
        body.append(entry)
        folder_entry = Gtk.Entry(text="", placeholder_text="Папка (относительно vault, пусто = корень)", hexpand=True)
        body.append(folder_entry)
        hint = Gtk.Label(label="Переменные: {{title}} подставится из имени заметки", halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"])
        hint.set_wrap(True)
        body.append(hint)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        cancel = Gtk.Button(label="Отмена", css_classes=["mod-neutral"])
        ok = Gtk.Button(label="Создать", css_classes=["suggested-action", "mod-cta"])
        cancel.connect("clicked", lambda *_: dlg.close())
        row.append(cancel)
        row.append(ok)
        row.set_halign(Gtk.Align.END)
        body.append(row)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(body)
        dlg.set_child(box)
        body.set_size_request(380, -1)

        def do_create(*_):
            name = entry.get_text().strip()
            if not name:
                return
            folder_rel = folder_entry.get_text().strip().strip("/")
            target_folder = root / folder_rel if folder_rel else root
            # sanitize name inside
            safe = _sanitize_filename(name)
            # _sanitize adds .md, but we already handle
            target = target_folder / safe
            if target.exists():
                self._show_toast(f"уже есть: {target.name}")
                return
            try:
                target_folder.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self._show_toast(f"ошибка папки: {e}")
                return
            try:
                raw = load_template(self._current)  # type: ignore[arg-type]
                rendered = render_template(raw, target.stem)
                target.write_text(rendered, encoding="utf-8")
            except OSError as e:
                self._show_toast(f"ошибка: {e}")
                return
            dlg.close()
            self._show_toast(f"создана {target.name} из {self._current.name}")  # type: ignore
            # попробовать открыть через главное окно если есть
            try:
                win = self.get_root()
                if win is not None and hasattr(win, "_open_note"):
                    win._open_note(str(target))  # type: ignore
            except Exception:
                pass

        ok.connect("clicked", do_create)
        entry.connect("activate", do_create)
        dlg.present(self)
        entry.grab_focus()

    # ── монитор ──────────────────────────────────────────────
    def _setup_monitor(self, path: Path) -> None:
        self._cancel_monitor()
        try:
            gfile = Gio.File.new_for_path(str(path))
            mon = gfile.monitor_file(Gio.FileMonitorFlags.NONE, None)
            mon.connect("changed", self._on_file_changed)
            self._file_monitor = mon
        except Exception:
            self._file_monitor = None

    def _cancel_monitor(self) -> None:
        if self._file_monitor is not None:
            try:
                self._file_monitor.cancel()
            except Exception:
                pass
            self._file_monitor = None

    def _on_file_changed(self, _mon, _f, _other, event) -> None:
        if self._suppress_monitor or self._dirty:
            return
        if event not in (Gio.FileMonitorEvent.CHANGED, Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.CREATED):
            return
        if self._current is None:
            return
        try:
            text = load_template(self._current)
        except OSError:
            return
        # не перетираем несохранённые правки
        GLib.idle_add(lambda: self._apply_external(text) or False)

    def _apply_external(self, text: str) -> bool:
        if self._dirty or self._current is None:
            return False
        cur_text = self.buffer.get_text(*self.buffer.get_bounds(), True)
        if cur_text == text:
            return False
        self._loading = True
        self.buffer.set_text(text)
        self._loading = False
        self._render_preview()
        self._show_toast("внешнее изменение загружено")
        return False

    # ── toast ────────────────────────────────────────────────
    def _show_toast(self, msg: str) -> None:
        # пробуем найти ToastOverlay у окна
        try:
            win = self.get_root()
            if win is not None and hasattr(win, "toast_overlay"):
                t = Adw.Toast.new(msg)
                t.set_timeout(3)
                win.toast_overlay.add_toast(t)  # type: ignore
                return
        except Exception:
            pass
        # fallback: меняем file_label
        old = self.file_label.get_text() if hasattr(self, "file_label") else ""
        self.file_label.set_text(msg)
        GLib.timeout_add(2500, lambda: self.file_label.set_text(old) or False)
