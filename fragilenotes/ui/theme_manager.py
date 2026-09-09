"""Менеджер темы: тёмная/светлая/авто + кастом CSS из vault/_System/custom.css.

Хранение: settings["theme"] = "auto" | "light" | "dark"
Переключение: Adw.StyleManager (FORCE_LIGHT / FORCE_DARK / DEFAULT)
Кастом CSS: vault/_System/custom.css — отдельный CssProvider поверх базового.

Используется в:
- ui.app.FragileWindow — init и on_settings_saved
- ui.settings_view — dropdown переключения темы
- ui.style — apply_custom_css делегирует сюда (круговой импорт избегается lazy import)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

try:
    gi.require_version("Adw", "1")
except Exception:
    pass

from gi.repository import Gdk, Gtk

log = logging.getLogger(__name__)

# ── Константы темы ───────────────────────────────────────────────
THEME_AUTO = "auto"
THEME_LIGHT = "light"
THEME_DARK = "dark"

THEMES: tuple[str, ...] = (THEME_AUTO, THEME_LIGHT, THEME_DARK)
VALID_THEMES = set(THEMES)

THEME_LABELS: dict[str, str] = {
    THEME_AUTO: "Авто (системная)",
    THEME_LIGHT: "Светлая",
    THEME_DARK: "Тёмная",
}

THEME_NAMES = THEME_LABELS

# Относительный путь кастом CSS внутри vault
CUSTOM_CSS_REL = Path("_System") / "custom.css"
CUSTOM_CSS_REL_STR = "_System/custom.css"

# Провайдер для кастом CSS (отдельный от базового в style.py)
_custom_provider: Gtk.CssProvider | None = None


# ── Нормализация ─────────────────────────────────────────────────
def normalize_theme(value: Any) -> str:
    """Привести любое значение к валидной теме, fallback → auto."""
    try:
        v = str(value).strip().lower()
    except Exception:
        return THEME_AUTO
    return v if v in VALID_THEMES else THEME_AUTO


def get_theme(settings: dict[str, Any]) -> str:
    """Достать тему из settings с нормализацией."""
    return normalize_theme(settings.get("theme", THEME_AUTO))


# ── Adw.StyleManager ─────────────────────────────────────────────
def _color_scheme_for(theme: str):
    """Маппинг темы → Adw.ColorScheme."""
    try:
        from gi.repository import Adw

        # Adw.ColorScheme: DEFAULT=0, FORCE_LIGHT=1, FORCE_DARK=2, PREFER_LIGHT=3, PREFER_DARK=4
        if theme == THEME_LIGHT:
            return Adw.ColorScheme.FORCE_LIGHT
        if theme == THEME_DARK:
            return Adw.ColorScheme.FORCE_DARK
        # auto → следовать системе
        return Adw.ColorScheme.DEFAULT
    except Exception:
        return None


def apply_theme(theme: Any) -> str:
    """Применить тему через Adw.StyleManager + CSS-класс окна, вернуть значение."""
    t = normalize_theme(theme)
    try:
        from gi.repository import Adw

        manager = Adw.StyleManager.get_default()
        if manager is not None:
            scheme = _color_scheme_for(t)
            if scheme is not None:
                manager.set_color_scheme(scheme)
    except Exception as exc:  # noqa: BLE001
        log.debug("apply_theme: StyleManager недоступен: %s", exc)
    # Дополнительно вешаем класс на все окна, чтобы CSS window.light/dark переопределил токены
    try:
        display = Gdk.Display.get_default()
        if display is not None:
            from gi.repository import Gtk as _Gtk

            for win in Gtk.Window.list_toplevels():
                if not hasattr(win, "get_css_classes"):
                    continue
                cls = [c for c in win.get_css_classes() if c not in ("light", "dark", "auto")]
                if t in ("light", "dark"):
                    cls.append(t)
                win.set_css_classes(cls)
    except Exception as exc:  # noqa: BLE001
        log.debug("apply_theme: css class: %s", exc)
    return t


def init_theme(settings: dict[str, Any]) -> str:
    """Инициализировать тему из settings (get + apply)."""
    t = get_theme(settings)
    apply_theme(t)
    return t


# ── Кастом CSS ───────────────────────────────────────────────────
def custom_css_path(settings: dict[str, Any]) -> Path:
    """Путь к vault/_System/custom.css."""
    vault_root = str(settings.get("vault_root") or Path.home() / "desktop")
    return Path(vault_root) / CUSTOM_CSS_REL


# Алиас для совместимости
get_custom_css_path = custom_css_path


def load_custom_css(settings: dict[str, Any]) -> str | None:
    """Прочитать custom.css, вернуть текст или None если нет/ошибка."""
    path = custom_css_path(settings)
    try:
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
        # пустой файл — считаем отсутствием
        if not text.strip():
            return None
        return text
    except OSError as exc:
        log.debug("load_custom_css: %s: %s", path, exc)
        return None


# Алиас
load_custom_css_text = load_custom_css


def is_custom_css_available(settings: dict[str, Any]) -> bool:
    """Есть ли файл custom.css."""
    try:
        return custom_css_path(settings).is_file()
    except Exception:
        return False


def clear_custom_css() -> None:
    """Убрать провайдер кастом CSS с дисплея."""
    global _custom_provider
    if _custom_provider is None:
        return
    try:
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.remove_provider_for_display(display, _custom_provider)
    except Exception as exc:  # noqa: BLE001
        log.debug("clear_custom_css: %s", exc)
    finally:
        _custom_provider = None


def apply_custom_css(settings: dict[str, Any]) -> bool:
    """Загрузить и применить vault/_System/custom.css поверх базовых стилей.

    Возвращает True если файл найден и применён, False если отсутствует/ошибка.
    Использует отдельный CssProvider с приоритетом USER (поверх APPLICATION).
    """
    global _custom_provider
    css_text = load_custom_css(settings)
    if css_text is None:
        clear_custom_css()
        return False

    # Валидация через провайдер — если CSS битый, Gtk залогирует, но не упадёт
    try:
        display = Gdk.Display.get_default()
        if display is None:
            # без дисплея (тесты/headless) — считаем успех если файл читается
            return True
        # Снять старый перед добавлением нового
        if _custom_provider is not None:
            try:
                Gtk.StyleContext.remove_provider_for_display(display, _custom_provider)
            except Exception:
                pass
            _custom_provider = None

        provider = Gtk.CssProvider()
        provider.load_from_string(css_text)
        # USER приоритет = 800, выше APPLICATION (600) — переопределяет базовый CSS
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )
        _custom_provider = provider
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("apply_custom_css: не удалось применить %s: %s", custom_css_path(settings), exc)
        return False


def reload_custom_css(settings: dict[str, Any]) -> bool:
    """Перезагрузить кастом CSS (clear + apply)."""
    clear_custom_css()
    return apply_custom_css(settings)


def apply_theme_and_css(settings: dict[str, Any]) -> tuple[str, bool]:
    """Удобный хелпер: применить тему и кастом CSS."""
    t = apply_theme(get_theme(settings))
    ok = apply_custom_css(settings)
    return t, ok


__all__ = [
    "THEME_AUTO",
    "THEME_LIGHT",
    "THEME_DARK",
    "THEMES",
    "VALID_THEMES",
    "THEME_LABELS",
    "THEME_NAMES",
    "CUSTOM_CSS_REL",
    "CUSTOM_CSS_REL_STR",
    "normalize_theme",
    "get_theme",
    "apply_theme",
    "init_theme",
    "custom_css_path",
    "get_custom_css_path",
    "load_custom_css",
    "load_custom_css_text",
    "is_custom_css_available",
    "apply_custom_css",
    "clear_custom_css",
    "reload_custom_css",
    "apply_theme_and_css",
]
