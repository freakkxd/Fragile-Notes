"""Tray daemon + фоновый Quick Capture для FragileNotes.

System tray иконка (AyatanaAppIndicator или Gtk.StatusIcon), меню:
  - открыть — показать главное окно
  - Quick Capture — фоновый быстрый захват в vault/Inbox/
  - выход — завершить приложение

Фоновый процесс через Gio.Application --gapplication-service:
  `fragile-notes --gapplication-service` регистрирует D-Bus имя
  dev.fragilich.fragile-notes и удерживает процесс (Gio.Application.hold)
  без видимого окна. Tray остаётся в панели, команды приходят через
  D-Bus activation (Gio.Application.activate) или клик по иконке.
  При закрытии окна (close-request) с активным tray окно скрывается
  (set_visible(False)) вместо destroy, сервис продолжает жить.

Fallback стратегия:
  1) AyatanaAppIndicator3 (современный Ubuntu/GNOME)
  2) AppIndicator3 (legacy)
  3) Gtk.StatusIcon (старый X11 fallback, deprecated в GTK4 но всё ещё доступен)
  Если ни один не доступен — регистрируются только Gio.SimpleAction
  (open / quick-capture / quit) и hold для --gapplication-service.

Интеграция в app.py:
  from fragilenotes.services.tray import setup_tray
  self._tray = setup_tray(app, self, self.settings)

Интеграция в main.py (опционально):
  Tray держит Gio.Application.hold() пока активен.
"""

from __future__ import annotations

import sys
from typing import Any

# ── GI imports с graceful fallback для headless/py_compile ──────────────
try:
    import gi  # type: ignore

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, GLib, Gtk  # type: ignore

    _HAS_GTK = True
except Exception:  # noqa: BLE001
    gi = None  # type: ignore[assignment]
    Gio = GLib = Gtk = Adw = None  # type: ignore[assignment]
    _HAS_GTK = False

try:
    from fragilenotes import APP_ID  # type: ignore
except Exception:
    APP_ID = "dev.fragilich.fragile-notes"

ICON_NAME = "fragile-notes"
FALLBACK_ICON = "document-new-symbolic"

# ── Indicator loader ────────────────────────────────────────────────────


def _load_indicator():
    """Попытаться загрузить AyatanaAppIndicator или AppIndicator3.

    Возвращает (module, kind) или (None, None).
    kind = 'Ayatana' | 'AppIndicator'
    """
    if not _HAS_GTK or gi is None:
        return None, None
    # Ayatana
    try:
        gi.require_version("AyatanaAppIndicator3", "0.1")  # type: ignore[attr-defined]
        from gi.repository import AyatanaAppIndicator3 as _AI  # type: ignore  # noqa: N814

        return _AI, "Ayatana"
    except Exception:
        pass
    # Legacy AppIndicator3
    try:
        gi.require_version("AppIndicator3", "0.1")  # type: ignore[attr-defined]
        from gi.repository import AppIndicator3 as _AI2  # type: ignore  # noqa: N814

        return _AI2, "AppIndicator"
    except Exception:
        pass
    return None, None


def _has_status_icon() -> bool:
    if not _HAS_GTK or Gtk is None:
        return False
    return hasattr(Gtk, "StatusIcon")


def is_service_mode(argv: list[str] | None = None) -> bool:
    """True если процесс запущен как Gio.Application --gapplication-service."""
    args = argv if argv is not None else sys.argv
    return "--gapplication-service" in args


def _safe_icon_name() -> str:
    # ICON_NAME может отсутствовать в теме — fallback на символическую
    return ICON_NAME


# ── Tray core ───────────────────────────────────────────────────────────


class FragileTray:
    """System tray иконка + меню + hold для фонового сервиса.

    Args:
        app: Adw.Application / Gio.Application instance (для hold/release и actions)
        window: FragileWindow или любой Gtk.Window (может быть None при старте сервиса)
        settings: dict настроек (vault_root и т.д.)
    """

    def __init__(self, app: Any, window: Any | None, settings: dict[str, Any] | None) -> None:
        self.app = app
        self.window = window
        self.settings: dict[str, Any] = dict(settings) if settings is not None else {}
        self._indicator: Any | None = None
        self._status_icon: Any | None = None
        self._gtk_menu: Any | None = None
        self._indicator_kind: str | None = None
        self._hold_acquired = False
        self._actions_registered = False
        self._active = False

        self._register_actions()
        self._setup_indicator()
        self._maybe_hold()

    # ── Gio actions (D-Bus активируемые) ───────────────────────────────
    def _register_actions(self) -> None:
        if not _HAS_GTK or Gio is None or self.app is None:
            return
        # Предотвращаем дубль при повторном setup
        if self._actions_registered:
            return
        try:
            # open
            act_open = Gio.SimpleAction.new("tray-open", None)
            act_open.connect("activate", lambda *_: self._on_open())
            self.app.add_action(act_open)
            # quick capture
            act_qc = Gio.SimpleAction.new("tray-quick-capture", None)
            act_qc.connect("activate", lambda *_: self._on_quick_capture())
            self.app.add_action(act_qc)
            # also expose as 'quick-capture' for CLI
            act_qc2 = Gio.SimpleAction.new("quick-capture", None)
            act_qc2.connect("activate", lambda *_: self._on_quick_capture())
            self.app.add_action(act_qc2)
            # quit
            act_quit = Gio.SimpleAction.new("tray-quit", None)
            act_quit.connect("activate", lambda *_: self._on_quit())
            self.app.add_action(act_quit)
            self._actions_registered = True
        except Exception:
            pass
        # Также legacy имена без префикса для совместимости
        for name, cb in (("open", self._on_open), ("quit", self._on_quit)):
            try:
                if self.app.lookup_action(name) is None:
                    a = Gio.SimpleAction.new(name, None)
                    a.connect("activate", lambda _a, _p, _cb=cb: _cb())
                    self.app.add_action(a)
            except Exception:
                pass

    # ── Indicator setup ────────────────────────────────────────────────
    def _setup_indicator(self) -> None:
        if not _HAS_GTK or Gtk is None:
            return
        # Пробуем Ayatana/AppIndicator
        mod, kind = _load_indicator()
        if mod is not None:
            try:
                self._setup_appindicator(mod, kind)
                self._active = True
                return
            except Exception:
                self._indicator = None
        # Fallback Gtk.StatusIcon
        if _has_status_icon():
            try:
                self._setup_status_icon()
                self._active = True
                return
            except Exception:
                self._status_icon = None
        # Если оба недоступны — считаем tray активным в режиме service (hold + actions)
        # чтобы close-request всё равно хайдил окно
        if is_service_mode():
            self._active = True

    def _create_gtk_menu(self) -> Any:
        """Gtk.Menu с тремя пунктами: открыть, Quick Capture, выход."""
        if not _HAS_GTK or Gtk is None:
            return None
        try:
            menu = Gtk.Menu()
        except Exception:
            # GTK4: Gtk.Menu может отсутствовать — fallback на Gio.Menu (не для AppIndicator)
            return None

        # открыть
        try:
            item_open = Gtk.MenuItem.new_with_label("открыть")
            item_open.connect("activate", lambda *_: self._on_open())
            menu.append(item_open)
        except Exception:
            pass

        # Quick Capture
        try:
            item_qc = Gtk.MenuItem.new_with_label("Quick Capture")
            item_qc.connect("activate", lambda *_: self._on_quick_capture())
            menu.append(item_qc)
        except Exception:
            pass

        # separator
        try:
            sep = Gtk.SeparatorMenuItem.new()
            menu.append(sep)
        except Exception:
            pass

        # выход
        try:
            item_quit = Gtk.MenuItem.new_with_label("выход")
            item_quit.connect("activate", lambda *_: self._on_quit())
            menu.append(item_quit)
        except Exception:
            pass

        try:
            menu.show_all()
        except Exception:
            pass
        self._gtk_menu = menu
        return menu

    def _setup_appindicator(self, mod: Any, kind: str) -> None:
        icon = _safe_icon_name()
        # Indicator.new(id, icon, category)
        try:
            # AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS
            cat = mod.IndicatorCategory.APPLICATION_STATUS
        except Exception:
            cat = 0
        try:
            indicator = mod.Indicator.new(APP_ID, icon, cat)
        except Exception:
            # fallback с FALLBACK_ICON
            indicator = mod.Indicator.new(APP_ID, FALLBACK_ICON, cat)
        try:
            indicator.set_title("Fragile Notes")
        except Exception:
            pass
        # Пытаемся поставить FALLBACK если иконка не найдена (не критично)
        try:
            indicator.set_icon_full(icon, "Fragile Notes")
        except Exception:
            pass
        menu = self._create_gtk_menu()
        if menu is not None:
            try:
                indicator.set_menu(menu)
            except Exception:
                pass
        try:
            indicator.set_status(mod.IndicatorStatus.ACTIVE)
        except Exception:
            pass
        self._indicator = indicator
        self._indicator_kind = kind

    def _setup_status_icon(self) -> None:
        # Gtk.StatusIcon deprecated в GTK4, но доступен в GTK3/X11
        icon = _safe_icon_name()
        try:
            si = Gtk.StatusIcon.new_from_icon_name(icon)  # type: ignore[attr-defined]
        except Exception:
            si = Gtk.StatusIcon.new_from_icon_name(FALLBACK_ICON)  # type: ignore[attr-defined]
        try:
            si.set_title("Fragile Notes")
            si.set_tooltip_text("Fragile Notes — открыть / Quick Capture / выход")
            si.set_visible(True)
        except Exception:
            pass
        # activate — левый клик → открыть
        try:
            si.connect("activate", lambda *_: self._on_open())
        except Exception:
            pass
        # popup-menu — правый клик → показать меню
        try:
            si.connect("popup-menu", self._on_status_icon_popup)
        except Exception:
            pass
        # ensure menu created
        if self._gtk_menu is None:
            self._create_gtk_menu()
        self._status_icon = si
        self._indicator_kind = "StatusIcon"

    def _on_status_icon_popup(self, _icon: Any, button: int, time: int) -> None:
        if self._gtk_menu is None:
            self._create_gtk_menu()
        if self._gtk_menu is None:
            return
        try:
            # GTK3 signature: popup(None, None, position_func, data, button, time)
            self._gtk_menu.popup(None, None, None, None, button, time)  # type: ignore[attr-defined]
        except Exception:
            try:
                self._gtk_menu.popup_at_pointer(None)  # type: ignore[attr-defined]
            except Exception:
                pass

    # ── hold для --gapplication-service ─────────────────────────────────
    def _maybe_hold(self) -> None:
        """Удержать приложение для фонового сервиса.

        Держит Gio.Application.hold() пока tray активен, чтобы процесс
        не завершался при скрытии окна. Освобождается в destroy()/quit.
        """
        if self.app is None or Gio is None:
            return
        should_hold = is_service_mode() or self._active
        # Также держим если настройка явно включает tray daemon
        try:
            if bool(self.settings.get("tray_daemon", True)) and self._active:
                should_hold = True
        except Exception:
            pass
        if should_hold and not self._hold_acquired:
            try:
                self.app.hold()
                self._hold_acquired = True
            except Exception:
                pass

    def _maybe_release(self) -> None:
        if self._hold_acquired and self.app is not None:
            try:
                self.app.release()
            except Exception:
                pass
            self._hold_acquired = False

    # ── actions ─────────────────────────────────────────────────────────
    def _resolve_window(self) -> Any | None:
        # Приоритет: self.window → app.get_active_window() → app.window
        w = self.window
        if w is not None:
            return w
        if self.app is not None and hasattr(self.app, "get_active_window"):
            try:
                aw = self.app.get_active_window()
                if aw is not None:
                    return aw
            except Exception:
                pass
        if self.app is not None and hasattr(self.app, "window"):
            try:
                aw2 = getattr(self.app, "window", None)
                if aw2 is not None:
                    return aw2
            except Exception:
                pass
        return None

    def _on_open(self, *_args: Any) -> None:
        """Показать главное окно (открыть)."""
        w = self._resolve_window()
        if w is not None:
            try:
                # если была hidden через set_visible(False), показать
                w.set_visible(True)
            except Exception:
                pass
            try:
                w.present()
            except Exception:
                pass
            return
        # окна нет — активировать приложение (создаст FragileWindow в do_activate)
        if self.app is not None:
            try:
                self.app.activate()
            except Exception:
                pass
            # deferred present если окно создастся асинхронно
            if _HAS_GTK and GLib is not None:
                try:
                    def _deferred() -> bool:
                        w2 = self._resolve_window()
                        if w2 is not None:
                            try:
                                w2.set_visible(True)
                                w2.present()
                            except Exception:
                                pass
                        return False

                    GLib.timeout_add(300, _deferred)
                except Exception:
                    pass

    def _on_quick_capture(self, *_args: Any) -> None:
        """Фоновый Quick Capture — открыть попап без фокуса на главном окне."""
        w = self._resolve_window()
        # Если есть окно с QuickCapture — используем его
        if w is not None and hasattr(w, "quick_capture"):
            try:
                # гарантируем видимость родителя для Dialog.present
                try:
                    w.set_visible(True)
                except Exception:
                    pass
                try:
                    w.present()
                except Exception:
                    pass
                qc = getattr(w, "quick_capture", None)
                if qc is not None and hasattr(qc, "open"):
                    # обновим settings если окно уже с синхронизированным
                    try:
                        if hasattr(qc, "update_settings"):
                            qc.update_settings(self.settings)
                    except Exception:
                        pass
                    qc.open()
                    return
            except Exception:
                pass
        # Fallback: если окна нет — активируем приложение и затем откроем
        if self.app is not None:
            try:
                self.app.activate()
            except Exception:
                pass
            if _HAS_GTK and GLib is not None:
                try:
                    def _deferred_qc() -> bool:
                        w2 = self._resolve_window()
                        if w2 is not None and hasattr(w2, "quick_capture"):
                            try:
                                w2.set_visible(True)
                                w2.present()
                                qc2 = getattr(w2, "quick_capture", None)
                                if qc2 is not None and hasattr(qc2, "open"):
                                    qc2.open()
                            except Exception:
                                pass
                            return False
                        # если окно так и не создалось — пробуем standalone
                        try:
                            self._show_standalone_quick_capture()
                        except Exception:
                            pass
                        return False

                    GLib.timeout_add(500, _deferred_qc)
                    return
                except Exception:
                    pass
        # Последний fallback — standalone диалог без главного окна
        try:
            self._show_standalone_quick_capture()
        except Exception:
            pass

    def _show_standalone_quick_capture(self) -> None:
        """Standalone Quick Capture без FragileWindow (для --gapplication-service без окна).

        Создаёт минимальный Adw.Dialog/Gtk.Window с полями заголовок+текст и
        сохраняет через fragilenotes.ui.quick_capture.save_quick_note.
        """
        if not _HAS_GTK or Gtk is None or Adw is None:
            return
        try:
            from fragilenotes.ui.quick_capture import save_quick_note  # type: ignore
        except Exception:
            return

        # Пытаемся найти parent для Dialog (app.get_active_window или временное окно)
        parent: Any | None = self._resolve_window()
        # Если parent нет — создаём временное скрытое окно как transient parent
        temp_win: Any | None = None
        if parent is None and self.app is not None:
            try:
                temp_win = Gtk.Window(application=self.app)  # type: ignore[call-arg]
                temp_win.set_default_size(1, 1)
                temp_win.set_visible(False)
            except Exception:
                temp_win = None

        dialog_parent = parent if parent is not None else temp_win

        # Простой диалог: используем Adw.Dialog если есть parent+Adw, иначе Gtk.Window
        try:
            if isinstance(dialog_parent, Adw.ApplicationWindow) or (parent is not None and hasattr(Adw, "Dialog")):
                # Adw.Dialog путь
                dialog = Adw.Dialog(title="Quick Capture")  # type: ignore[attr-defined]
                dialog.set_content_width(480)
                dialog.set_content_height(360)
            else:
                raise TypeError("no Adw parent")
            # build content similar to QuickCapture.open but simplified для standalone
            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            header.set_margin_top(16)
            header.set_margin_start(16)
            header.set_margin_end(16)
            header.set_margin_bottom(4)
            header.append(Gtk.Label(label="⚡"))
            header.append(Gtk.Label(label="Quick Capture", hexpand=True, halign=Gtk.Align.START, xalign=0))
            root.append(header)
            body_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            body_box.set_margin_start(16)
            body_box.set_margin_end(16)
            body_box.set_margin_bottom(12)
            title_entry = Gtk.Entry(placeholder_text="Заголовок (опционально)", hexpand=True)
            body_box.append(title_entry)
            scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
            scroller.set_min_content_height(180)
            text_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD, top_margin=8, bottom_margin=8, left_margin=10, right_margin=10)
            text_view.set_vexpand(True)
            buf = text_view.get_buffer()
            scroller.set_child(text_view)
            body_box.append(scroller)
            root.append(body_box)
            btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
            btn_row.set_margin_start(16)
            btn_row.set_margin_end(16)
            btn_row.set_margin_bottom(16)
            cancel = Gtk.Button(label="Отмена")
            save = Gtk.Button(label="Сохранить", css_classes=["suggested-action"])
            btn_row.append(cancel)
            btn_row.append(save)
            root.append(btn_row)
            dialog.set_child(root)

            def _close(*_a: Any) -> None:
                try:
                    dialog.close()
                except Exception:
                    pass
                if temp_win is not None:
                    try:
                        temp_win.close()
                    except Exception:
                        pass

            def _do_save(*_a: Any) -> None:
                title = title_entry.get_text().strip()
                s, e = buf.get_bounds()
                body = buf.get_text(s, e, True)
                if not title and not body.strip():
                    title_entry.grab_focus()
                    return
                try:
                    path = save_quick_note(self.settings, title, body)
                    # toast если есть parent с toast_overlay
                    try:
                        par = dialog_parent
                        if par is not None and hasattr(par, "toast_overlay"):
                            toast = Adw.Toast.new(f"Сохранено: Inbox/{path.name}")
                            par.toast_overlay.add_toast(toast)
                    except Exception:
                        pass
                except Exception:
                    pass
                _close()

            cancel.connect("clicked", _close)
            save.connect("clicked", _do_save)
            key_ctrl = Gtk.EventControllerKey.new()
            key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

            def _on_key(_c: Any, keyval: int, _kc: int, state: Any) -> bool:
                ctrl = False
                try:
                    from gi.repository import Gdk  # type: ignore

                    ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
                except Exception:
                    ctrl = False
                # Ctrl+Enter
                try:
                    from gi.repository import Gdk as _Gdk2  # type: ignore

                    if ctrl and keyval in (_Gdk2.KEY_Return, _Gdk2.KEY_KP_Enter, _Gdk2.KEY_ISO_Enter):
                        _do_save()
                        return True
                    if keyval == _Gdk2.KEY_Escape:
                        _close()
                        return True
                except Exception:
                    pass
                return False

            try:
                dialog.add_controller(key_ctrl)
                key_ctrl.connect("key-pressed", _on_key)
            except Exception:
                pass
            dialog.connect("closed", lambda *_: (temp_win.close() if temp_win is not None else None))
            # present
            try:
                if parent is not None:
                    dialog.present(parent)
                elif temp_win is not None:
                    # презентуем через temp_win как parent hidden
                    dialog.present(temp_win)
                else:
                    dialog.present()
            except Exception:
                try:
                    dialog.present()
                except Exception:
                    pass
            try:
                title_entry.grab_focus()
            except Exception:
                pass
            return
        except Exception:
            pass

        # Gtk.Window fallback (если Adw.Dialog недоступен)
        try:
            win = Gtk.Window(title="Quick Capture — Fragile Notes")  # type: ignore[call-arg]
            # try set transient
            try:
                if parent is not None:
                    win.set_transient_for(parent)
            except Exception:
                pass
            try:
                win.set_default_size(480, 360)
            except Exception:
                pass
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            vbox.set_margin_top(16)
            vbox.set_margin_bottom(16)
            vbox.set_margin_start(16)
            vbox.set_margin_end(16)
            vbox.append(Gtk.Label(label="⚡ Quick Capture — vault/Inbox/", halign=Gtk.Align.START, xalign=0))
            title_entry = Gtk.Entry(placeholder_text="Заголовок", hexpand=True)
            vbox.append(title_entry)
            scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
            scroller.set_min_content_height(180)
            text_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD)
            text_view.set_vexpand(True)
            buf = text_view.get_buffer()
            scroller.set_child(text_view)
            vbox.append(scroller)
            btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
            cancel = Gtk.Button(label="Отмена")
            save = Gtk.Button(label="Сохранить", css_classes=["suggested-action"])
            btn_row.append(cancel)
            btn_row.append(save)
            vbox.append(btn_row)
            win.set_child(vbox)

            def _close2(*_a: Any) -> None:
                try:
                    win.close()
                except Exception:
                    pass
                if temp_win is not None:
                    try:
                        temp_win.close()
                    except Exception:
                        pass

            def _save2(*_a: Any) -> None:
                t = title_entry.get_text().strip()
                s2, e2 = buf.get_bounds()
                b = buf.get_text(s2, e2, True)
                if not t and not b.strip():
                    return
                try:
                    save_quick_note(self.settings, t, b)
                except Exception:
                    pass
                _close2()

            cancel.connect("clicked", _close2)
            save.connect("clicked", _save2)
            win.present()
            try:
                title_entry.grab_focus()
            except Exception:
                pass
        except Exception:
            pass

    def _on_quit(self, *_args: Any) -> None:
        """Выход — завершить приложение."""
        # Снимем hold после quit
        try:
            w = self._resolve_window()
            if w is not None:
                try:
                    w.destroy()
                except Exception:
                    try:
                        w.close()
                    except Exception:
                        pass
        except Exception:
            pass
        # destroy indicator before quit
        try:
            self.destroy()
        except Exception:
            pass
        if self.app is not None:
            try:
                self.app.quit()
            except Exception:
                try:
                    GLib.idle_add(lambda: self.app.quit() or False)  # type: ignore[attr-defined]
                except Exception:
                    pass
        # fallback hard exit если quit не сработал (не в GTK main loop)
        # не вызываем sys.exit в нормальном режиме

    # ── public ──────────────────────────────────────────────────────────
    def is_active(self) -> bool:
        """Активен ли tray (показана иконка или service hold)."""
        return bool(self._active)

    def update_settings(self, settings: dict[str, Any]) -> None:
        self.settings = dict(settings) if settings is not None else {}
        # при смене vault_root иконка не меняется, но quick capture должен знать новый путь
        # hold может потребоваться если is_service_mode сменился
        self._maybe_hold()

    def destroy(self) -> None:
        """Снять индикатор и освободить hold."""
        try:
            if self._indicator is not None:
                try:
                    # Ayatana: set status passive
                    from gi.repository import AyatanaAppIndicator3 as _AI  # type: ignore  # noqa: N814, I001

                    self._indicator.set_status(_AI.IndicatorStatus.PASSIVE)
                except Exception:
                    try:
                        self._indicator.set_status(0)  # type: ignore
                    except Exception:
                        pass
                self._indicator = None
        except Exception:
            pass
        try:
            if self._status_icon is not None:
                try:
                    self._status_icon.set_visible(False)  # type: ignore[attr-defined]
                except Exception:
                    pass
                self._status_icon = None
        except Exception:
            pass
        self._active = False
        self._maybe_release()

    # Alias для совместимости
    def teardown(self) -> None:
        self.destroy()


# ── Public helpers ──────────────────────────────────────────────────────


def setup_tray(app: Any, window: Any | None, settings: dict[str, Any] | None) -> FragileTray | None:
    """Создать и вернуть FragileTray (или None если GTK недоступен).

    Вызывать из FragileWindow.__init__:
        self._tray = setup_tray(app, self, self.settings)
    """
    if not _HAS_GTK:
        # Даже без GTK держим hold для --gapplication-service + actions (если Gio есть)
        try:
            if Gio is not None and app is not None and is_service_mode():
                try:
                    app.hold()
                except Exception:
                    pass
        except Exception:
            pass
        return None
    try:
        tray = FragileTray(app, window, settings or {})
        # Если не активен и не service — не удерживаем, вернём None для чистоты
        if not tray.is_active() and not is_service_mode():
            # но если status_icon fallback неудачен — всё равно можем вернуть tray для hold?
            # вернём только если есть actions (service не нужен — можно скрыть)
            # оставляем tray только если активен
            if not tray.is_active():
                # не держим приложение — отдаём tray только если иконка есть
                # иначе — None чтобы window close не хайдил
                return tray if tray.is_active() else None
        return tray
    except Exception:
        return None


def teardown_tray(tray: FragileTray | None) -> None:
    if tray is not None:
        try:
            tray.destroy()
        except Exception:
            pass


def is_tray_available() -> bool:
    """Доступен ли хотя бы один бэкенд (AyatanaAppIndicator/AppIndicator/StatusIcon)."""
    if not _HAS_GTK:
        return False
    mod, _ = _load_indicator()
    if mod is not None:
        return True
    return _has_status_icon()


# Совместимость: старые импорты могут ждать TrayService/TrayManager
TrayService = FragileTray
TrayManager = FragileTray
TrayDaemon = FragileTray

__all__ = [
    "FragileTray",
    "TrayService",
    "TrayManager",
    "TrayDaemon",
    "setup_tray",
    "teardown_tray",
    "is_tray_available",
    "is_service_mode",
]
