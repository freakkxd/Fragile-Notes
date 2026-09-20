"""Настройки — рабочее пространство, масштаб, vault, LLM, движок (группы-карточки)."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from pathlib import Path

from gi.repository import Adw, Gtk, Pango  # noqa: E402

from . import scale, theme_manager
from . import workspace as ws
from .widgets import section_title, view_header


def _group(title: str) -> tuple[Gtk.Box, Gtk.Box]:
    group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["settings-group"])
    group.append(section_title(title))
    inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    group.append(inner)
    return group, inner


class SettingsView(Gtk.Box):
    def __init__(self, settings: dict, on_saved,
                 on_ribbon_changed=None, on_window_resize=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self.on_saved = on_saved
        self.on_ribbon_changed = on_ribbon_changed or (lambda: None)
        self.on_window_resize = on_window_resize or (lambda w, h: None)
        self._fields: list[tuple[str, object]] = []
        self._ribbon = ws.normalize(settings)["ribbon"]

        self.append(view_header("⚙️", "Настройки"))

        scroller = Gtk.ScrolledWindow(vexpand=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(14)
        box.set_margin_end(14)
        box.set_margin_bottom(14)
        scroller.set_child(box)

        g, inner = self._build_workspace_group(box)
        box.append(g)

        g, inner = self._build_theme_group()
        box.append(g)

        g, inner = _group("Интерфейс · масштаб")
        self._spin_row(inner, "Масштаб интерфейса", "ui_scale",
                       float(settings.get("ui_scale", 1.0)) * 100.0, 75.0, 200.0,
                       "Увеличивает весь интерфейс (базу шрифтов)")
        self._spin_row(inner, "Зум редактора", "editor_zoom",
                       float(settings.get("editor_zoom", 1.0)) * 100.0, 50.0, 200.0,
                       "CTRL+= / CTRL+- в редакторе, CTRL+0 сброс")
        self._switch_row(inner, "follow_system_scale",
                         bool(settings.get("follow_system_scale", True)),
                         "Следить за системным text-scaling (GNOME)")
        self._system_info = self._info_row(inner, self._system_info_text())
        box.append(g)

        g, inner = _group("Редактор · Vim")
        self._switch_row(inner, "vim_mode",
                         bool(settings.get("vim_mode", False)),
                         "Vim-режим (normal/insert, hjkl, dd/yy/p, i, Esc, :w :q)")
        self._info_row(inner, "В normal: hjkl, dd/yy/p, i, Esc, :w :q/:wq/:q!  В insert — обычный ввод.")
        box.append(g)

        g, inner = _group("E2E шифрование vault")
        self._switch_row(inner, "e2e_enabled",
                         bool(settings.get("e2e_enabled", False)),
                         "E2E шифрование всего vault (AES-GCM, общий ключ из пароля)")
        self._entry_row(inner, "Файл соли E2E", str(settings.get("e2e_salt_file", ".e2e_salt")),
                        "e2e_salt_file", "Имя файла соли в корне vault (по умолчанию .e2e_salt)")
        self._info_row(inner, "Команды: core.e2e.encrypt_vault(vault, пароль) / decrypt_vault(vault, пароль) — каждый файл → .enc с общим ключом PBKDF2.")
        box.append(g)

        g, inner = _group("Vault и движок")
        for key, label, hint in [
            ("vault_root", "Корень vault", "Путь к папке Obsidian-vault"),
            ("manage_llm_script", "Скрипт manage-llm", "Start/stop/status llama-server"),
            ("engine_cli", "CLI движка (от vault)", "Относительный путь к cli.js"),
            ("node_path", "Node executable", ""),
        ]:
            e = self._entry_row(inner, label, str(settings.get(key, "")), key, hint)
        box.append(g)

        g, inner = _group("LLM")
        for key, label, hint in [
            ("llm_day_port", "Порт day", "Qwen3-14B"),
            ("llm_archive_port", "Порт archive", "Gemma-4-26B"),
            ("llm_day_ctx", "ctx day", "Окно контекста day"),
            ("llm_archive_ctx", "ctx archive", "Окно контекста archive"),
        ]:
            e = self._entry_row(inner, label, str(settings.get(key, "")), key, hint)
        box.append(g)

        g, inner = _group("Task Manager")
        for key, label, hint in [
            ("tm_tasks_folder", "Папка задач", ""),
            ("tm_comments_folder", "Папка комментариев", ""),
            ("daily_folder", "Папка daily", ""),
            ("daily_template", "Шаблон daily", ""),
            ("enrich_default_limit", "Лимит enrich", "Заметок за прогон"),
        ]:
            e = self._entry_row(inner, label, str(settings.get(key, "")), key, hint)
        box.append(g)

        save_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["settings-row"])
        save_btn = Gtk.Button(label="Сохранить", css_classes=["suggested-action"])
        save_btn.connect("clicked", self._on_save)
        save_row.append(save_btn)
        self.status = Gtk.Label(label="", css_classes=["dim-label", "dim-hint"])
        save_row.append(self.status)
        box.append(save_row)
        self.append(scroller)

    # ── Тема · кастом CSS ─────────────────────────────────────
    def _build_theme_group(self) -> tuple[Gtk.Box, Gtk.Box]:
        g, inner = _group("Тема · оформление")

        # Dropdown темы
        cur_theme = theme_manager.normalize_theme(self.settings.get("theme", "auto"))
        cur_idx = {"auto": 0, "light": 1, "dark": 2}.get(cur_theme, 0)
        drop = Gtk.DropDown.new_from_strings([
            theme_manager.THEME_LABELS["auto"],
            theme_manager.THEME_LABELS["light"],
            theme_manager.THEME_LABELS["dark"],
        ])
        drop.set_selected(cur_idx)
        drop.connect("notify::selected", lambda *_: self._on_theme_changed())
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["settings-row"])
        row.append(Gtk.Label(label="Тема приложения", halign=Gtk.Align.START, xalign=0, css_classes=["settings-label"]))
        row.append(Gtk.Label(label="Авто — следует системной теме GNOME (Adw.StyleManager)", halign=Gtk.Align.START, xalign=0, css_classes=["dim-label", "dim-hint"]))
        row.append(drop)
        inner.append(row)
        self._theme_drop = drop

        # Кастом CSS info
        try:
            cpath = theme_manager.custom_css_path(self.settings)
        except Exception:
            cpath = Path(str(self.settings.get("vault_root") or "")) / "_System" / "custom.css"
        exists = cpath.is_file()
        hint = "Кастом CSS: vault/_System/custom.css — переопределяет стили поверх темы."
        if exists:
            try:
                sz = cpath.stat().st_size
                hint += f" · найден ({sz} байт)"
            except OSError:
                hint += " · найден"
        else:
            hint += " · файл не найден (создай для кастомизации)"

        inner.append(Gtk.Label(label=f"Путь: {cpath}", halign=Gtk.Align.START, xalign=0, css_classes=["dim-label", "dim-hint"], wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR))
        self._custom_hint = self._info_row(inner, hint)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["settings-row"])
        reload_btn = Gtk.Button(label="↻ Перезагрузить CSS", css_classes=["mod-neutral", "btn-sm"])
        reload_btn.connect("clicked", lambda *_: self._on_reload_custom_css())
        reload_btn.set_tooltip_text("Перечитать vault/_System/custom.css")
        open_btn = Gtk.Button(label="Открыть папку", css_classes=["mod-neutral", "btn-sm"])
        open_btn.connect("clicked", lambda *_: self._on_open_custom_folder())
        open_btn.set_tooltip_text("Открыть папку _System в файловом менеджере")
        btn_row.append(reload_btn)
        btn_row.append(open_btn)
        inner.append(btn_row)
        return g, inner

    def _on_theme_changed(self) -> None:
        idx = self._theme_drop.get_selected() if hasattr(self, "_theme_drop") else 0
        mapping = ["auto", "light", "dark"]
        theme = mapping[idx] if 0 <= idx < len(mapping) else "auto"
        theme_manager.apply_theme(theme)
        # не сохраняем сразу — пользователь нажмёт «Сохранить»; но показываем превью
        self.status.set_label(f"тема: {theme_manager.THEME_LABELS.get(theme, theme)} (нажми Сохранить)")

    def _on_reload_custom_css(self) -> None:
        try:
            # пробуем через style, fallback через theme_manager
            from . import style as _style

            ok = _style.reload_custom_css(self.settings)
            if not ok:
                ok = theme_manager.reload_custom_css(self.settings)
        except Exception:
            try:
                ok = theme_manager.reload_custom_css(self.settings)
            except Exception:
                ok = False
        if ok:
            self.status.set_label("custom.css перезагружен ✓")
        else:
            # проверяем наличие файла
            try:
                p = theme_manager.custom_css_path(self.settings)
                if p.is_file():
                    self.status.set_label("custom.css не применён (ошибка парсинга?)")
                else:
                    self.status.set_label("custom.css не найден — создай vault/_System/custom.css")
            except Exception:
                self.status.set_label("custom.css не найден")

    def _on_open_custom_folder(self) -> None:
        try:
            from gi.repository import Gio

            p = theme_manager.custom_css_path(self.settings).parent
            if not p.is_dir():
                try:
                    p.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
            Gio.AppInfo.launch_default_for_uri(f"file://{p}", None)
        except Exception:
            try:
                import subprocess

                p = theme_manager.custom_css_path(self.settings).parent
                subprocess.Popen(["xdg-open", str(p)])
            except Exception:
                pass

    # ── Рабочее пространство: ribbon и размер окна ──────────
    def _build_workspace_group(self, box: Gtk.Box) -> tuple[Gtk.Box, Gtk.Box]:
        g, inner = _group("Рабочее пространство")

        # Размер окна
        size_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["settings-row"])
        ui_state = self.settings.get("ui_state") or {}
        adj_w = Gtk.Adjustment(value=float(ui_state.get("width") or 1280),
                               lower=200, upper=8000, step_increment=50)
        adj_h = Gtk.Adjustment(value=float(ui_state.get("height") or 820),
                               lower=160, upper=6000, step_increment=50)
        self._win_w = Gtk.SpinButton(adjustment=adj_w, digits=0)
        self._win_h = Gtk.SpinButton(adjustment=adj_h, digits=0)
        apply = Gtk.Button(label="Применить размер", css_classes=["mod-neutral", "btn-sm"])
        apply.connect("clicked", lambda *_: self._apply_window_size())
        for lbl, spin in (("Ширина", self._win_w), ("Высота", self._win_h)):
            r = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            r.append(Gtk.Label(label=lbl, css_classes=["settings-label"], halign=Gtk.Align.START, xalign=0))
            r.append(spin)
            size_row.append(r)
        size_row.append(apply)
        size_row.set_valign(Gtk.Align.CENTER)
        inner.append(size_row)
        self._info_row(inner, "Размер применяется сразу; интерфейс масштабируется пропорционально окну.")

        # Редактор ribbon
        lbl = Gtk.Label(label="Иконки ribbon (порядок и состав)", halign=Gtk.Align.START, xalign=0, css_classes=["settings-label"])
        lbl.set_margin_top(10)
        inner.append(lbl)
        self._ribbon_list = Gtk.ListBox(css_classes=["ws-list"])
        self._ribbon_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._ribbon_list.set_activate_on_single_click(False)
        inner.append(self._ribbon_list)
        self._render_ribbon()

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        add_btn = Gtk.Button(label="＋ Добавить иконку", css_classes=["mod-neutral", "btn-sm"])
        add_btn.connect("clicked", self._on_add_icon)
        reset_btn = Gtk.Button(label="Сбросить", css_classes=["mod-neutral", "btn-sm"])
        reset_btn.connect("clicked", self._on_reset_ribbon)
        btn_row.append(add_btn)
        btn_row.append(reset_btn)
        btn_row.set_margin_top(4)
        inner.append(btn_row)
        return g, inner

    def _render_ribbon(self) -> None:
        while (child := self._ribbon_list.get_first_child()) is not None:
            self._ribbon_list.remove(child)
        for idx, it in enumerate(self._ribbon):
            row = Gtk.ListBoxRow(activatable=False, selectable=False, css_classes=["ws-row"])
            h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, hexpand=True)
            h.set_margin_start(8)
            h.set_margin_top(4)
            h.set_margin_bottom(4)
            icon = Gtk.Label(label=ws.item_char(it), css_classes=["ws-icon"])
            name = Gtk.Label(label=ws.item_label(it), hexpand=True, halign=Gtk.Align.START, xalign=0,
                             ellipsize=Pango.EllipsizeMode.END)
            kind = Gtk.Label(label=ws.item_kind_name(it), css_classes=["dim-hint", "ws-kind"])
            up = Gtk.Button(label="↑", css_classes=["ws-arrow"])
            dn = Gtk.Button(label="↓", css_classes=["ws-arrow"])
            rem = Gtk.Button(label="✕", css_classes=["ws-arrow"])
            up.connect("clicked", lambda *_, i=idx: self._move_ribbon(i, -1))
            dn.connect("clicked", lambda *_, i=idx: self._move_ribbon(i, 1))
            rem.connect("clicked", lambda *_, i=idx: self._remove_ribbon(i))
            h.append(icon)
            h.append(name)
            h.append(kind)
            h.append(up)
            h.append(dn)
            h.append(rem)
            row.set_child(h)
            self._ribbon_list.append(row)

    def _move_ribbon(self, idx: int, delta: int) -> None:
        ws.move(self._ribbon, idx, delta)
        self._commit_workspace()

    def _remove_ribbon(self, idx: int) -> None:
        if 0 <= idx < len(self._ribbon):
            self._ribbon.pop(idx)
            self._commit_workspace()

    def _on_reset_ribbon(self, _btn) -> None:
        self._ribbon = ws.default_ribbon()
        self._commit_workspace()

    def _commit_workspace(self) -> None:
        self._ribbon = ws.normalize({"workspace": {"ribbon": self._ribbon}})["ribbon"]
        self._render_ribbon()
        updated = dict(self.settings)
        updated["workspace"] = {
            **dict(updated.get("workspace") or {}),
            "ribbon": [dict(it) for it in self._ribbon],
        }
        self.settings = updated
        self.on_ribbon_changed()
        self.on_saved(updated)
        self.status.set_label("рабочее пространство обновлено ✓")

    def _apply_window_size(self) -> None:
        self.on_window_resize(self._win_w.get_value_as_int(), self._win_h.get_value_as_int())
        self.status.set_label("размер окна применён ✓")

    def _on_add_icon(self, _btn) -> None:
        dialog = Adw.Dialog(title="Добавить иконку")
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                       css_classes=["dialog-body"], margin_top=8, margin_bottom=8,
                       margin_start=24, margin_end=24)

        drop = Gtk.DropDown.new_from_strings(["Вьюха", "Действие", "Открыть заметку", "Разделитель"])

        target_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        target_box.append(Gtk.Label(label="Тип", css_classes=["settings-label"], halign=Gtk.Align.START, xalign=0))
        target_box.append(drop)

        spec_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._ws_selector_into(drop, spec_holder)
        target_box.append(spec_holder)

        helper = Gtk.Frame(css_classes=["ws-frame"], margin_start=0, margin_top=6)
        helper.set_child(Gtk.Label(
            label="Для «Открыть заметку» — путь .md от корня vault (можно с подпапками).",
            wrap=True, halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"]))
        target_box.append(helper)

        icon_box = self._dialog_entry("Иконка (эмодзи)", "📄", "Для вьюх/действий можно оставить пустым")
        tip_box = self._dialog_entry("Подпись", "", "Тултип и название в редакторе")

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["btn-row"])
        cancel = Gtk.Button(label="Отмена", css_classes=["mod-neutral"])
        create = Gtk.Button(label="Добавить", css_classes=["suggested-action", "mod-cta"])
        cancel.connect("clicked", lambda *_: dialog.close())

        def on_create(*_a):
            try:
                self._add_icon_commit(
                    drop,
                    icon_box._entry.get_text().strip(),
                    tip_box._entry.get_text().strip(),
                )
            except Exception:
                return
            dialog.close()

        create.connect("clicked", on_create)
        actions.append(cancel)
        actions.append(create)
        actions.set_halign(Gtk.Align.END)
        body.append(target_box)
        body.append(icon_box)
        body.append(tip_box)
        body.append(actions)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(body)
        dialog.set_child(content)
        body.set_size_request(340, -1)
        dialog.present(self)
        drop.connect("notify::selected", lambda *_: self._ws_selector_into(drop, spec_holder))

    def _ws_selector_into(self, drop: Gtk.DropDown, holder: Gtk.Box) -> None:
        while (child := holder.get_first_child()) is not None:
            holder.remove(child)
        kind = drop.get_selected()
        if kind in (0, 1):
            items = list(ws.VIEW_TITLES.values()) if kind == 0 else list(ws.CMD_TIPS.values())
            combo = Gtk.DropDown.new_from_strings(items)
            combo.set_selected(0)
            holder.append(combo)
            self._ws_selector_widget = combo
        elif kind == 2:
            e = Gtk.Entry(placeholder_text="путь к заметке (относительный)", hexpand=True)
            holder.append(e)
            self._ws_selector_widget = e

    def _dialog_entry(self, label: str, value: str, hint: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(Gtk.Label(label=label, halign=Gtk.Align.START, xalign=0, css_classes=["settings-label"]))
        if hint:
            box.append(Gtk.Label(label=hint, halign=Gtk.Align.START, xalign=0,
                                 css_classes=["dim-label", "dim-hint"]))
        entry = Gtk.Entry(text=value, hexpand=True)
        box.append(entry)
        box._entry = entry
        return box

    def _add_icon_commit(self, drop: Gtk.DropDown, icon: str, tip: str) -> None:
        kind = drop.get_selected()
        widget = getattr(self, "_ws_selector_widget", None)
        item: dict | None = None
        if kind == 3:
            item = {"t": "sep"}
        elif kind == 0 and widget is not None:
            key = list(ws.VIEW_TITLES.keys())[widget.get_selected()]
            item = {"t": "view", "id": key}
        elif kind == 1 and widget is not None:
            cid = list(ws.CMD_TIPS.keys())[widget.get_selected()]
            item = {"t": "cmd", "id": cid}
        elif kind == 2 and widget is not None:
            path = widget.get_text().strip()
            if path:
                item = {"t": "open", "icon": icon or "📄", "tip": tip or path, "path": path}
        if item is None:
            return
        self._ribbon.append(item)
        self._commit_workspace()

    def _info_row(self, parent: Gtk.Box, text: str) -> Gtk.Label:
        lbl = Gtk.Label(label=text, halign=Gtk.Align.START, xalign=0, wrap=True,
                        css_classes=["dim-label", "dim-hint"])
        parent.append(lbl)
        return lbl

    # ── Строители строк ─────────────────────────────────────
    def _entry_row(self, parent: Gtk.Box, label: str, value: str, key: str, hint: str | None = None) -> Gtk.Entry:
        box = self._field_box(parent, label, hint)
        entry = Gtk.Entry(text=value, hexpand=True)
        box.append(entry)
        self._fields.append((key, entry))
        return entry

    def _spin_row(self, parent: Gtk.Box, label: str, key: str, value_pct: float,
                  lo: float, hi: float, hint: str | None = None) -> Gtk.SpinButton:
        box = self._field_box(parent, label, hint)
        adj = Gtk.Adjustment(value=value_pct, lower=lo, upper=hi, step_increment=5, page_increment=25)
        spin = Gtk.SpinButton(adjustment=adj, digits=0, hexpand=True)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.append(spin)
        row.append(Gtk.Label(label="%", css_classes=["dim-label"], valign=Gtk.Align.CENTER))
        box.append(row)
        self._fields.append((key, spin))
        return spin

    def _field_box(self, parent: Gtk.Box, label: str, hint: str | None) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["settings-row"])
        if label:
            lbl = Gtk.Label(label=label, halign=Gtk.Align.START, xalign=0, css_classes=["settings-label"])
            lbl.set_margin_top(6)
            box.append(lbl)
        if hint:
            h = Gtk.Label(label=hint, halign=Gtk.Align.START, xalign=0, css_classes=["dim-label", "dim-hint"])
            box.append(h)
        parent.append(box)
        return box

    def _switch_row(self, parent: Gtk.Box, key: str, active: bool, label: str) -> Gtk.Switch:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["settings-row"])
        lbl = Gtk.Label(label=label, hexpand=True, halign=Gtk.Align.START, xalign=0, css_classes=["settings-label"])
        sw = Gtk.Switch(active=active, valign=Gtk.Align.CENTER)
        row.append(lbl)
        row.append(sw)
        parent.append(row)
        self._fields.append((key, sw))
        return sw

    # ── Информация о системе ────────────────────────────────
    def _system_info_text(self) -> str:
        mon = scale.monitor_scale()
        mon_txt = f" ×{mon}" if mon > 1 else " (обычная плотность)"
        return (f"Обнаружено: GNOME text-scaling ×{scale.system():g} · "
                f"монитор{mon_txt}. При включённом слежении итоговый масштаб "
                f"= пользовательский × системный.")

    def refresh_scale(self) -> None:
        for key, w in self._fields:
            if isinstance(w, Gtk.SpinButton):
                if key == "ui_scale":
                    w.set_value(scale.ui_scale() * 100.0)
                elif key == "editor_zoom":
                    w.set_value(scale.editor_zoom() * 100.0)
            elif isinstance(w, Gtk.Switch):
                w.set_active(scale.follow_system())
        self._system_info.set_text(self._system_info_text())

    # ── Сохранение ───────────────────────────────────────────
    def _on_save(self, _btn: Gtk.Button) -> None:
        updated = dict(self.settings)
        for key, w in self._fields:
            if isinstance(w, Gtk.Entry):
                updated[key] = w.get_text()
            elif isinstance(w, Gtk.SpinButton):
                updated[key] = round(w.get_value() / 100.0, 2)
            elif isinstance(w, Gtk.Switch):
                updated[key] = w.get_active()
        # Тема
        if hasattr(self, "_theme_drop"):
            idx = self._theme_drop.get_selected()
            mapping = ["auto", "light", "dark"]
            theme = mapping[idx] if 0 <= int(idx) < len(mapping) else "auto"
            updated["theme"] = theme_manager.normalize_theme(theme)
            # немедленное применение (on_saved повторно применит, но превью уже)
            theme_manager.apply_theme(updated["theme"])
        self.settings = updated
        self.on_saved(updated)
        # попытка перезагрузить кастом CSS после сохранения vault_root
        try:
            from . import style as _style
            _style.reload_custom_css(updated)
        except Exception:
            try:
                theme_manager.reload_custom_css(updated)
            except Exception:
                pass
        self.status.set_label("сохранено ✓")
