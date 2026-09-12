"""Сайдбар навигации: шапка приложения, секции, элементы."""

from __future__ import annotations

from gi.repository import Gtk, Pango

# Совместимость: принимаем одиночный AccessibleProperty как в ТЗ (идемпотентно)
try:
    if not getattr(Gtk.Widget.update_property, "_fragile_patched", False):  # type: ignore
        _orig_upd_sb = Gtk.Widget.update_property  # type: ignore[attr-defined]

        def _compat_update_sb(self, prop, val, *a, **kw):  # type: ignore[no-untyped-def]
            try:
                if isinstance(prop, Gtk.AccessibleProperty):
                    return _orig_upd_sb(self, [prop], [val])  # type: ignore
                return _orig_upd_sb(self, prop, val, *a, **kw)
            except Exception:
                try:
                    return _orig_upd_sb(self, [prop], [val])  # type: ignore
                except Exception:
                    return None

        _compat_update_sb._fragile_patched = True  # type: ignore
        Gtk.Widget.update_property = _compat_update_sb  # type: ignore
except Exception:
    pass

SECTIONS: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("Ядро", [("home", "🧬", "Рабочий стол"), ("runner", "🔧", "AO Tasks"), ("tasks", "✅", "Сегодня"), ("habits", "🌱", "Привычки")]),
    ("Vault", [("daily", "📅", "Daily"), ("calendar", "🗓", "Календарь"), ("review", "🔍", "Обзор"), ("srs", "🧠", "Повторение"), ("media", "🎬", "Media"), ("voice", "🎙️", "Голосовые"), ("video", "📹", "Видео"), ("templates", "📑", "Шаблоны"), ("graph", "🕸", "Граф"), ("canvas", "🎨", "Canvas"), ("whiteboard", "🧊", "Whiteboard"), ("kanban", "📋", "Kanban"), ("database", "🗄️", "База данных"), ("slides", "🎞️", "Презентация"), ("mindmap", "🗺", "Mind Map"), ("mermaid_live", "🧜", "Mermaid Live"), ("latex_live", "∑", "LaTeX Live"), ("tags", "#", "Теги")]),
    ("AI", [("ai_chat", "🤖", "AI Чат")]),
    ("Аналитика", [("analytics", "📊", "Аналитика")]),
    ("Система", [("plugin_store", "🧩", "Магазин плагинов"), ("theme_editor", "🎨", "Редактор темы"), ("settings", "⚙️", "Настройки")]),
]


class Sidebar(Gtk.Box):
    def __init__(
        self,
        on_select,
        vault_label: str,
        vaults: list[dict] | None = None,
        on_workspace_switch=None,
        on_workspace_add=None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, css_classes=["sidebar"])
        self.on_select = on_select
        self._rows: dict[str, Gtk.ListBoxRow] = {}
        self._on_ws_switch = on_workspace_switch
        self._on_ws_add = on_workspace_add
        self._vaults: list[dict] = list(vaults or [])
        self._current_vault: str = str(vault_label or "")

        # Шапка — высота подогнана под 3 кнопки ribbon + сепаратор, чтобы первая ячейка шла в одну горизонталь
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=10, margin_bottom=8, css_classes=["sb-head"])
        head.set_margin_start(14)
        head.set_margin_end(14)
        app_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        app_row.append(Gtk.Label(label="🧬", css_classes=["sb-logo"]))
        app_row.append(Gtk.Label(label="Fragile Notes", css_classes=["sb-appname"]))
        head.append(app_row)
        vault_lbl = Gtk.Label(
            label=vault_label or "vault", css_classes=["dim-label", "sb-vault"],
            halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE,
        )
        head.append(vault_lbl)
        self._vault_lbl = vault_lbl
        self.append(head)

        # Workspaces — несколько vault в одном окне
        self._ws_box = self._build_workspaces_box()
        self.append(self._ws_box)

        sep_ws = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL, css_classes=["sb-sep"])
        self.append(sep_ws)

        notes_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        notes_hdr.set_margin_start(8)
        notes_hdr.set_margin_end(8)
        notes_hdr.set_margin_top(4)
        notes_hdr.append(Gtk.Label(label="ЗАМЕТКИ", css_classes=["sb-section"], halign=Gtk.Align.START, hexpand=True, xalign=0))
        refresh_notes = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Обновить список заметок", css_classes=["flat", "sb-ws-add"])
        refresh_notes.set_can_focus(False)
        refresh_notes.connect("clicked", lambda *_: self._refresh_notes())
        notes_hdr.append(refresh_notes)
        self.append(notes_hdr)

        self._notes_scroller = Gtk.ScrolledWindow(vexpand=True)
        self._notes_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._notes_scroller.set_min_content_height(160)
        self._notes_scroller.set_max_content_height(380)
        self._notes_list = Gtk.ListBox(css_classes=["sb-notes-list"])
        self._notes_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._notes_list.connect("row-activated", self._on_note_activated)
        self._notes_scroller.set_child(self._notes_list)
        self.append(self._notes_scroller)
        self._refresh_notes()

        tabs_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        tabs_hdr.set_margin_start(8)
        tabs_hdr.set_margin_end(8)
        tabs_hdr.set_margin_top(6)
        tabs_hdr.append(Gtk.Label(label="ВКЛАДКИ", css_classes=["sb-section"], halign=Gtk.Align.START, hexpand=True, xalign=0))
        self._tabs_toggle = Gtk.ToggleButton(label="☰", tooltip_text="Развернуть все смайлики + названия", css_classes=["flat", "sb-ws-add"])
        self._tabs_toggle.set_active(False)
        self._tabs_toggle.connect("toggled", self._on_tabs_toggle)
        tabs_hdr.append(self._tabs_toggle)
        self.append(tabs_hdr)

        self._tabs_revealer = Gtk.Revealer()
        self._tabs_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._tabs_revealer.set_transition_duration(180)
        self._tabs_revealer.set_reveal_child(False)
        self.append(self._tabs_revealer)

        # Список вкладок — внутри revealer, по кнопке раскрываются смайлики + названия + настройки
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.list = Gtk.ListBox(css_classes=["nav-list"])
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.set_can_focus(False)
        self.list.set_focusable(False)
        for section, items in SECTIONS:
            section_row = Gtk.ListBoxRow(activatable=False, selectable=False, css_classes=["sb-section-row"])
            section_row.set_can_focus(False)
            section_row.set_focusable(False)
            section_row.set_size_request(-1, 18)
            label = Gtk.Label(label=section.upper(), css_classes=["sb-section"], halign=Gtk.Align.START, xalign=0)
            label.set_can_focus(False)
            label.set_focusable(False)
            label.set_accessible_role(Gtk.AccessibleRole.HEADING)
            label.update_property(Gtk.AccessibleProperty.LEVEL, 2)
            label.set_margin_start(6)
            section_row.set_child(label)
            self.list.append(section_row)
            for key, icon, text in items:
                row = Gtk.ListBoxRow(css_classes=["nav-item"])
                row.set_size_request(-1, 36)
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10, valign=Gtk.Align.CENTER)
                box.set_margin_top(2)
                box.set_margin_bottom(2)
                box.set_margin_start(10)
                box.set_margin_end(10)
                icon_box = Gtk.Box(css_classes=["sb-nav-icon-box"], valign=Gtk.Align.CENTER)
                icon_box.append(Gtk.Label(label=icon, css_classes=["sb-nav-icon"],
                                          halign=Gtk.Align.CENTER, xalign=0.5, valign=Gtk.Align.CENTER))
                box.append(icon_box)
                box.append(Gtk.Label(label=text, css_classes=["sb-nav-text"],
                                     halign=Gtk.Align.START, xalign=0, valign=Gtk.Align.CENTER,
                                     ellipsize=Pango.EllipsizeMode.END))
                row.set_child(box)
                row.key = key
                self._rows[key] = row
                self.list.append(row)
        self.list.connect("row-selected", self._on_selected)
        scroller.set_child(self.list)
        self._tabs_revealer.set_child(scroller)

        # Низ: подсказка
        footer = Gtk.Label(
            label="Fragile Notes · ядро = AO Runner + Suite",
            css_classes=["dim-label", "sb-footer"], halign=Gtk.Align.CENTER,
        )
        self.append(footer)

    def _on_selected(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is not None and getattr(row, "key", None):
            self.on_select(row.key)

    def select(self, key: str) -> None:
        row = self._rows.get(key)
        if row is not None:
            self.list.select_row(row)

    def set_vault(self, label: str) -> None:
        self._vault_lbl.set_text(label or "vault")
        self._current_vault = str(label or "")
        self._refresh_workspaces()

    # ── Workspaces UI ─────────────────────────────────────────
    def _build_workspaces_box(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["sb-workspaces"])
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_bottom(4)
        # header
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        hdr.append(Gtk.Label(label="WORKSPACES", css_classes=["sb-section"], halign=Gtk.Align.START, hexpand=True, xalign=0))
        add_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Добавить vault (Ctrl+Shift+O)", css_classes=["flat", "sb-ws-add"])
        add_btn.set_can_focus(False)
        try:
            add_btn.update_property(Gtk.AccessibleProperty.LABEL, "Добавить vault")
        except Exception:
            pass
        add_btn.connect("clicked", self._on_ws_add_clicked)
        self._ws_add_btn = add_btn
        hdr.append(add_btn)
        switch_btn = Gtk.Button(icon_name="system-search-symbolic", tooltip_text="Быстрый свитч Ctrl+Shift+O", css_classes=["flat", "sb-ws-add"])
        switch_btn.set_can_focus(False)
        switch_btn.connect("clicked", self._on_ws_switch_clicked)
        hdr.append(switch_btn)
        box.append(hdr)
        # list
        self._ws_list = Gtk.ListBox(css_classes=["sb-ws-list"])
        self._ws_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._ws_list.set_can_focus(False)
        self._ws_rows: dict[str, Gtk.ListBoxRow] = {}
        box.append(self._ws_list)
        self._refresh_workspaces()
        return box

    def _refresh_workspaces(self) -> None:
        lb = getattr(self, "_ws_list", None)
        if lb is None:
            return
        while (child := lb.get_first_child()) is not None:
            lb.remove(child)
        self._ws_rows.clear()
        vaults = getattr(self, "_vaults", []) or []
        # fallback: хотя бы текущий vault
        if not vaults and getattr(self, "_current_vault", ""):
            cur = self._current_vault
            try:
                from pathlib import Path as _P
                name = _P(cur).name or cur
            except Exception:
                name = cur
            vaults = [{"path": cur, "name": name}]
        cur_norm = ""
        try:
            from pathlib import Path as _P

            cur_norm = str(_P(self._current_vault).expanduser().resolve(strict=False)) if self._current_vault else ""
        except Exception:
            cur_norm = str(self._current_vault or "")
        for v in vaults:
            path = str(v.get("path") or "").strip()
            if not path:
                continue
            name = str(v.get("name") or path).strip() or path
            is_current = False
            try:
                from pathlib import Path as _P

                is_current = str(_P(path).expanduser().resolve(strict=False)) == cur_norm
            except Exception:
                is_current = path == self._current_vault
            row = Gtk.ListBoxRow(css_classes=["sb-ws-row"] + (["sb-ws-current"] if is_current else []), activatable=True, selectable=False)
            h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, valign=Gtk.Align.CENTER)
            h.set_margin_top(3)
            h.set_margin_bottom(3)
            h.set_margin_start(6)
            h.set_margin_end(6)
            # индикатор
            icon = "●" if is_current else "○"
            h.append(Gtk.Label(label=icon, css_classes=["sb-ws-icon"], valign=Gtk.Align.CENTER))
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, hexpand=True)
            vbox.append(Gtk.Label(label=name, css_classes=["sb-ws-name"], halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE))
            vbox.append(Gtk.Label(label=path, css_classes=["dim-label", "sb-ws-path"], halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE))
            h.append(vbox)
            row.set_child(h)
            row._ws_path = path  # type: ignore[attr-defined]
            # клик по row — свитч
            try:
                row.connect("activate", self._on_ws_row_activated)
            except Exception:
                pass
            lb.append(row)
            self._ws_rows[path] = row
        # подсказка если пусто
        if not vaults:
            row = Gtk.ListBoxRow(activatable=False, selectable=False)
            row.set_child(Gtk.Label(label="нет воркспейсов — добавь vault", css_classes=["dim-label", "sb-ws-empty"], halign=Gtk.Align.START))
            lb.append(row)

    def _on_ws_row_activated(self, row: Gtk.ListBoxRow) -> None:
        path = getattr(row, "_ws_path", None)
        if path and self._on_ws_switch is not None:
            try:
                self._on_ws_switch(path)
            except Exception:
                pass
        elif path:
            # fallback: просто通知 via on_select? не делаем
            pass

    def _on_ws_add_clicked(self, _btn) -> None:
        if self._on_ws_add is not None:
            try:
                self._on_ws_add()
            except Exception:
                pass

    def _on_ws_switch_clicked(self, _btn) -> None:
        # делегируем на тот же add handler — он покажет полный свитчер
        if self._on_ws_add is not None:
            try:
                self._on_ws_add()
            except Exception:
                pass
        elif self._on_ws_switch is not None:
            # если нет add-хендлера, хотя бы откроем переключение на текущий
            pass

    def _on_tabs_toggle(self, btn: Gtk.ToggleButton) -> None:
        try:
            self._tabs_revealer.set_reveal_child(bool(btn.get_active()))
        except Exception:
            pass
        try:
            btn.set_label("✕" if btn.get_active() else "☰")
        except Exception:
            pass

    def _refresh_notes(self) -> None:
        lb = getattr(self, "_notes_list", None)
        if lb is None:
            return
        while (child := lb.get_first_child()) is not None:
            lb.remove(child)
        try:
            from pathlib import Path as _P

            from ..services.vault import ensure_file_tree

            from ..config import load_settings

            s = load_settings()
            node = ensure_file_tree(s, force=False)
            def walk(n, prefix=""):
                if prefix:
                    hdr = Gtk.ListBoxRow(activatable=False, selectable=False, css_classes=["sb-folder-row"])
                    lbl = Gtk.Label(label=prefix.rstrip("/"), css_classes=["sb-folder-name"], halign=Gtk.Align.START, xalign=0)
                    lbl.set_margin_start(4)
                    hdr.set_child(lbl)
                    lb.append(hdr)
                for fname, fpath in n.files:
                    row = Gtk.ListBoxRow(css_classes=["sb-note-row"], activatable=True)
                    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                    box.set_margin_start(10 if prefix else 6)
                    box.set_margin_end(6)
                    box.set_margin_top(2)
                    box.set_margin_bottom(2)
                    try:
                        from ..vault import FILE_EMOJI as _EM

                        emo = _EM.get(_P(fpath).suffix.lower(), "📄")
                    except Exception:
                        emo = "📄"
                    box.append(Gtk.Label(label=emo, css_classes=["sb-note-emoji"], valign=Gtk.Align.CENTER))
                    box.append(Gtk.Label(label=fname, css_classes=["sb-note-name"], halign=Gtk.Align.START, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE))
                    row.set_child(box)
                    row._note_path = fpath  # type: ignore[attr-defined]
                    lb.append(row)
                for d in n.dirs:
                    walk(d, prefix + d.name + "/" if not prefix else prefix + d.name + "/")
            walk(node)
            if lb.get_first_child() is None:
                row = Gtk.ListBoxRow(activatable=False, selectable=False)
                row.set_child(Gtk.Label(label="нет заметок", css_classes=["dim-label"], halign=Gtk.Align.START))
                lb.append(row)
        except Exception:
            pass

    def _on_note_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        path = getattr(row, "_note_path", None)
        if not path:
            return
        try:
            parent = self.get_ancestor(Gtk.Window)
            if parent and hasattr(parent, "_open_note"):
                parent._open_note(str(path))
            else:
                self.on_select("files")
                GLib.idle_add(lambda: getattr(parent, "_open_note", lambda *_: None)(str(path)) or False)
        except Exception:
            pass

    def set_vaults(self, vaults: list[dict], current_path: str | None = None) -> None:
        self._vaults = [dict(v) for v in (vaults or [])]
        if current_path is not None:
            self._current_vault = str(current_path)
            self._vault_lbl.set_text(str(current_path) or "vault")
        self._refresh_workspaces()
        try:
            self._refresh_notes()
        except Exception:
            pass

    def set_workspace_callbacks(self, on_switch=None, on_add=None) -> None:
        if on_switch is not None:
            self._on_ws_switch = on_switch
        if on_add is not None:
            self._on_ws_add = on_add
