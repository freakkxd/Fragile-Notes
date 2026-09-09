"""Quick Capture — Ctrl+Shift+Q: маленький попап для быстрого создания заметки.

Сохраняет в vault/Inbox/ (создаёт папку если нет). Заголовок → имя файла,
текст → тело заметки. Доступен глобально из окна (EventControllerKey CAPTURE).
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

# ── утилсы имени файла ──────────────────────────────────────────
_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|]')
_WS_RE = re.compile(r"\s+")

def _sanitize_stem(title: str) -> str:
    """Заголовок → безопасный stem файла (без расширения)."""
    raw = (title or "").strip()
    if not raw:
        return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    # заменяем запрещённые символы на -
    raw = _ILLEGAL_RE.sub("-", raw)
    # схлопываем пробелы
    raw = _WS_RE.sub(" ", raw).strip()
    # точки в конце/начале убираем (проблемы на Win)
    raw = raw.strip(" .")
    if not raw:
        return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    # ограничим длину
    if len(raw) > 80:
        raw = raw[:80].rstrip(" .")
    if not raw:
        return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return raw


def _unique_path(inbox: Path, stem: str) -> Path:
    """Уникальный путь в Inbox: stem.md, stem-1.md ..."""
    base = inbox / f"{stem}.md"
    if not base.exists():
        return base
    for i in range(1, 1000):
        cand = inbox / f"{stem}-{i}.md"
        if not cand.exists():
            return cand
    # fallback с таймстампом
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return inbox / f"{stem}-{ts}.md"


def save_quick_note(settings: dict, title: str, body: str) -> Path:
    """Сохранить быстрый захват в vault/Inbox/. Возвращает путь созданного файла.

    Логика:
    - Inbox = Path(settings["vault_root"]) / "Inbox"
    - mkdir(parents=True, exist_ok=True)
    - имя файла из заголовка (санитизация), пустой → timestamp
    - контент: "# title\\n\\nbody" если title есть, иначе body
    - инвалидация кэша vault (если доступно)
    """
    vault_root = Path(str(settings.get("vault_root") or Path.home() / "desktop"))
    inbox = vault_root / "Inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    stem = _sanitize_stem(title)
    target = _unique_path(inbox, stem)

    t = (title or "").strip()
    b = (body or "").rstrip()
    if t and b:
        content = f"# {t}\n\n{b}\n"
    elif t and not b:
        content = f"# {t}\n"
    else:
        content = b + ("\n" if b and not b.endswith("\n") else "")

    target.write_text(content, encoding="utf-8")

    # инвалидация кэша, чтобы новая заметка сразу появилась в списке/графе
    try:
        from ..services import vault as _vault

        _vault.invalidate_vault_cache()
    except Exception:
        pass
    try:
        from ..services.vault_service import VaultService as _VS

        # если сервис синглтон нужен — не критично, кэш уже очищен
        _ = _VS
    except Exception:
        pass

    return target


# ── UI: маленький попап ────────────────────────────────────────
class QuickCapture:
    """Контроллер попапа Quick Capture. Создаётся один раз на окно.

    Использование:
        self.quick_capture = QuickCapture(self, self.settings)
        # затем в _on_window_key:
        if ctrl and shift and keyval in (Gdk.KEY_q, Gdk.KEY_Q):
            self.quick_capture.open()
    """

    def __init__(self, window: Adw.ApplicationWindow, settings: dict) -> None:
        self.window = window
        self.settings = dict(settings) if settings is not None else {}
        self._dialog: Adw.Dialog | None = None

    def update_settings(self, settings: dict) -> None:
        self.settings = dict(settings) if settings is not None else {}

    # ── публичный API ──────────────────────────────────────
    def open(self) -> None:
        """Открыть попап (создаётся заново каждый раз для чистого состояния)."""
        # если уже открыт — не дублировать
        if self._dialog is not None:
            try:
                self._dialog.close()
            except Exception:
                pass
            self._dialog = None

        dialog = Adw.Dialog(title="Quick Capture")
        dialog.set_content_width(480)
        dialog.set_content_height(360)
        self._dialog = dialog

        # ── layout ──
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # header: иконка + заголовок + подсказка
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_top(16)
        header.set_margin_start(16)
        header.set_margin_end(16)
        header.set_margin_bottom(4)
        header.append(Gtk.Label(label="⚡", css_classes=["view-emoji"]))
        title_lbl = Gtk.Label(
            label="Quick Capture",
            halign=Gtk.Align.START,
            css_classes=["view-title"],
            hexpand=True,
            xalign=0,
        )
        header.append(title_lbl)
        hint = Gtk.Label(
            label="Ctrl+Shift+Q",
            css_classes=["dim-hint"],
            halign=Gtk.Align.END,
        )
        header.append(hint)
        root.append(header)

        subtitle = Gtk.Label(
            label="Быстрая заметка → vault/Inbox/",
            halign=Gtk.Align.START,
            xalign=0,
            css_classes=["dim-hint"],
        )
        subtitle.set_margin_start(16)
        subtitle.set_margin_end(16)
        subtitle.set_margin_bottom(12)
        root.append(subtitle)

        # поля
        body_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body_box.set_margin_start(16)
        body_box.set_margin_end(16)
        body_box.set_margin_bottom(12)

        # заголовок
        title_entry = Gtk.Entry(
            placeholder_text="Заголовок (имя файла, опционально)",
            hexpand=True,
        )
        title_entry.set_max_length(120)
        body_box.append(title_entry)

        # текст (многострочный)
        scroller = Gtk.ScrolledWindow(
            hexpand=True, vexpand=True, css_classes=["editor-frame"]
        )
        scroller.set_min_content_height(180)
        scroller.set_max_content_height(260)
        text_view = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD,
            top_margin=8,
            bottom_margin=8,
            left_margin=10,
            right_margin=10,
            css_classes=["editor"],
        )
        text_view.set_vexpand(True)
        text_view.set_hexpand(True)
        # placeholder для TextView — эмулируем через начальный текст + стиль
        buf = text_view.get_buffer()
        scroller.set_child(text_view)
        body_box.append(scroller)
        # хинт под полем
        foot_hint = Gtk.Label(
            label="Enter — новая строка · Ctrl+Enter — сохранить · Esc — закрыть",
            halign=Gtk.Align.START,
            xalign=0,
            css_classes=["dim-hint"],
        )
        body_box.append(foot_hint)

        root.append(body_box)

        # ── кнопки ──
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)
        btn_row.set_margin_start(16)
        btn_row.set_margin_end(16)
        btn_row.set_margin_bottom(16)

        cancel = Gtk.Button(label="Отмена", css_classes=["mod-neutral"])
        save = Gtk.Button(label="Сохранить", css_classes=["suggested-action", "mod-cta"])
        save.set_sensitive(True)
        btn_row.append(cancel)
        btn_row.append(save)
        root.append(btn_row)

        dialog.set_child(root)

        # ── handlers ──
        def _close(*_a) -> None:
            try:
                dialog.close()
            except Exception:
                pass
            self._dialog = None

        def _do_save(*_a) -> None:
            title = title_entry.get_text().strip()
            start, end = buf.get_bounds()
            body = buf.get_text(start, end, True)
            # пустая заметка — не сохраняем, покажем toast
            if not title and not body.strip():
                try:
                    if hasattr(self.window, "toast_overlay"):
                        toast = Adw.Toast.new("Пустая заметка — не сохранена")
                        self.window.toast_overlay.add_toast(toast)
                except Exception:
                    pass
                title_entry.grab_focus()
                return
            try:
                path = save_quick_note(self.settings, title, body)
                # toast
                try:
                    if hasattr(self.window, "toast_overlay"):
                        toast = Adw.Toast.new(f"Сохранено: Inbox/{path.name}")
                        toast.set_timeout(4)
                        self.window.toast_overlay.add_toast(toast)
                except Exception:
                    pass
                # попробовать открыть созданную заметку если есть метод
                try:
                    if hasattr(self.window, "_open_note"):
                        self.window._open_note(str(path))
                except Exception:
                    pass
                _close()
            except Exception as exc:  # noqa: BLE001
                try:
                    if hasattr(self.window, "toast_overlay"):
                        toast = Adw.Toast.new(f"Ошибка сохранения: {exc}")
                        self.window.toast_overlay.add_toast(toast)
                except Exception:
                    pass

        cancel.connect("clicked", _close)
        save.connect("clicked", _do_save)

        # Enter/Ctrl+Enter/Esc внутри полей
        def _on_title_activate(entry: Gtk.Entry) -> None:
            # Enter в заголовке — фокус на текст
            text_view.grab_focus()

        title_entry.connect("activate", _on_title_activate)

        # Горячие клавиши диалога
        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def _on_key(_c, keyval, _kc, state) -> bool:
            ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
            # Ctrl+Enter — сохранить
            if ctrl and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
                _do_save()
                return True
            # Esc — закрыть (Adw.Dialog уже закрывает, но подстрахуемся)
            if keyval == Gdk.KEY_Escape:
                _close()
                return True
            return False

        dialog.add_controller(key_ctrl)
        key_ctrl.connect("key-pressed", _on_key)

        # также локально для text_view (Ctrl+Enter там должен сработать даже если фокус в буфере)
        tv_key = Gtk.EventControllerKey.new()
        tv_key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        tv_key.connect("key-pressed", _on_key)
        text_view.add_controller(tv_key)

        dialog.connect("closed", lambda *_: setattr(self, "_dialog", None))

        dialog.present(self.window)
        # фокус на заголовок, затем на текст если заголовок уже заполнен
        GLib.idle_add(lambda: title_entry.grab_focus() or False)
