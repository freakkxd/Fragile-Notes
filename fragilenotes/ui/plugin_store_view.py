"""Магазин плагинов FragileNotes: список из локального registry JSON, установка/удаление в vault/_System/Plugins/.

Registry — локальный JSON (fragilenotes/data/plugin_registry.json, рядом с модулем или vault).
Установка — копирование .py файла в vault/_System/Plugins/ с проверкой py_compile.
Удаление — unlink из Plugins/. Интегрируется как вкладка ``plugin_store`` (🧩).
"""

from __future__ import annotations

import json
import py_compile
import shutil
from pathlib import Path
from typing import Any

# GTK лениво — для py_compile/headless тестов модуль должен импортироваться без дисплея
try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402
    _GTK_AVAILABLE = True
except Exception:  # noqa: BLE001
    Adw = Gio = GLib = Gtk = Pango = None  # type: ignore[assignment]
    _GTK_AVAILABLE = False

from ..core.plugins import PLUGINS_REL, ensure_plugins_dir, get_plugins_dir  # noqa: E402
from .widgets import empty_state, status_pill, view_header  # noqa: E402

REGISTRY_FILENAME = "plugin_registry.json"

# ── чистые функции (тестируются без GTK) ─────────────────────────────────────

def _registry_candidates(settings: dict | None = None) -> list[Path]:
    """Список путей где ищем registry JSON (порядок приоритета)."""
    cands: list[Path] = []
    # 1. явный путь из настроек
    if isinstance(settings, dict):
        custom = settings.get("plugin_registry") or settings.get("plugin_registry_path")
        if custom:
            try:
                cands.append(Path(str(custom)).expanduser())
            except Exception:
                pass
    # 2. packaged data: fragilenotes/data/plugin_registry.json
    try:
        pkg_data = Path(__file__).resolve().parent.parent / "data" / REGISTRY_FILENAME
        cands.append(pkg_data)
        # также plugins подпапка рядом: fragilenotes/data/plugins/ -> но registry рядом с data
        alt = Path(__file__).resolve().parent / REGISTRY_FILENAME
        if alt not in cands:
            cands.append(alt)
        alt2 = Path(__file__).resolve().parent.parent / REGISTRY_FILENAME
        if alt2 not in cands:
            cands.append(alt2)
    except Exception:
        pass
    # 3. корень проекта (для запуска из git)
    try:
        from ..config import APP_DIR  # noqa: E402

        cands.append(APP_DIR / REGISTRY_FILENAME)
    except Exception:
        pass
    # 4. vault/_System/Plugins/registry.json (если vault хочет свой registry)
    if isinstance(settings, dict):
        try:
            vr = settings.get("vault_root")
            if vr:
                cands.append(Path(str(vr)).expanduser() / "_System" / "Plugins" / REGISTRY_FILENAME)
                cands.append(Path(str(vr)).expanduser() / "_System" / REGISTRY_FILENAME)
        except Exception:
            pass
    # deduplicate preserve order
    seen: set[str] = set()
    out: list[Path] = []
    for p in cands:
        s = str(p)
        if s not in seen:
            seen.add(s)
            out.append(p)
    return out


def get_registry_path(settings: dict | None = None) -> Path | None:
    """Найти существующий registry JSON или None."""
    for p in _registry_candidates(settings):
        try:
            if p.is_file():
                return p
        except Exception:
            continue
    return None


def _resolve_source_file(plugin: dict, registry_path: Path | None = None) -> Path | None:
    """Найти исходный .py файл плагина для копирования.

    Ищет по ключам ``source`` / ``source_path`` / ``filename`` относительно:
    - директории registry
    - fragilenotes/data/plugins/
    - пакетных кандидатов
    """
    fname = plugin.get("filename") or plugin.get("file") or (plugin.get("id") and f"{plugin['id']}.py") or ""
    source = plugin.get("source") or plugin.get("source_path") or fname
    if not source:
        return None
    # если абсолютный и существует — вернуть
    try:
        s = str(source).strip()
        if not s:
            return None
        # inline code вместо пути? если содержит \n и не выглядит как путь — не считаем файлом
        if "\n" in s and (len(s) > 256 or "def " in s or "import " in s):
            return None
        p = Path(s)
        if p.is_absolute() and p.is_file():
            return p
        # относительные кандидаты
        candidates: list[Path] = []
        if registry_path is not None:
            candidates.append(registry_path.parent / s)
            # если registry в data/, а плагины в data/plugins/
            candidates.append(registry_path.parent / "plugins" / s)
            candidates.append(registry_path.parent / "plugins" / Path(s).name)
        # packaged data/plugins/
        try:
            pkg_plugins = Path(__file__).resolve().parent.parent / "data" / "plugins" / Path(s).name
            candidates.append(pkg_plugins)
            pkg_plugins2 = Path(__file__).resolve().parent.parent / "data" / "plugins" / s
            if pkg_plugins2 != pkg_plugins:
                candidates.append(pkg_plugins2)
        except Exception:
            pass
        # рядом с ui
        try:
            ui_plugins = Path(__file__).resolve().parent / "plugins" / Path(s).name
            candidates.append(ui_plugins)
        except Exception:
            pass
        # просто filename
        try:
            if fname:
                for base in [Path.cwd(), Path.cwd() / "fragilenotes" / "data" / "plugins"]:
                    candidates.append(base / fname)
                    candidates.append(base / Path(fname).name)
        except Exception:
            pass
        for c in candidates:
            try:
                if c.is_file():
                    return c
            except Exception:
                continue
        # если source — просто имя файла и есть в candidates по имени
        # fallback: ищем по fname отдельно
        if fname and fname != s:
            return _resolve_source_file({**plugin, "source": fname}, registry_path)
        return None
    except Exception:
        return None


def load_registry(settings: dict | None = None) -> list[dict[str, Any]]:
    """Загрузить список плагинов из локального registry JSON.

    Возвращает список словарей (пустой если не найден/ошибка). Не бросает.
    """
    path = get_registry_path(settings)
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if isinstance(data, dict) and "plugins" in data:
            data = data["plugins"]
        if not isinstance(data, list):
            return []
        out: list[dict[str, Any]] = []
        for it in data:
            if isinstance(it, dict) and (it.get("id") or it.get("filename")):
                # нормализация обязательных полей
                pid = str(it.get("id") or Path(str(it.get("filename"))).stem).strip()
                if not pid:
                    continue
                fname = str(it.get("filename") or it.get("file") or f"{pid}.py").strip()
                # sanitize fname
                fname = fname.replace("/", "_").replace("\\", "_")
                if not fname.lower().endswith(".py"):
                    # registry может содержать без расширения? добавим
                    if "." not in fname:
                        fname += ".py"
                out.append({
                    "id": pid,
                    "name": str(it.get("name") or pid),
                    "version": str(it.get("version") or "0.1.0"),
                    "description": str(it.get("description") or ""),
                    "author": str(it.get("author") or ""),
                    "filename": fname,
                    "source": str(it.get("source") or it.get("source_path") or fname),
                    "hooks": list(it.get("hooks") or []),
                    "category": str(it.get("category") or ""),
                    "code": it.get("code"),  # опционально inline
                    "_raw": it,
                })
        out.sort(key=lambda x: x["name"].lower())
        return out
    except Exception:
        return []


def get_plugin_by_id(plugins: list[dict], plugin_id: str) -> dict | None:
    pid = str(plugin_id).strip().lower()
    for p in plugins:
        if str(p.get("id", "")).strip().lower() == pid:
            return p
        if str(p.get("filename", "")).strip().lower() == pid:
            return p
        if str(p.get("filename", "")).strip().lower() == f"{pid}.py":
            return p
    return None


def is_installed(settings: dict, plugin: dict | str) -> bool:
    """Проверяет наличие файла плагина в vault/_System/Plugins/."""
    try:
        if isinstance(plugin, str):
            # считаем id или filename
            plugins = load_registry(settings)
            found = get_plugin_by_id(plugins, plugin)
            fname = found["filename"] if found else plugin if plugin.endswith(".py") else f"{plugin}.py"
        else:
            fname = str(plugin.get("filename") or plugin.get("id") or "").strip()
            if not fname:
                return False
            if not fname.endswith(".py"):
                fname += ".py"
        pdir = get_plugins_dir(settings)
        return (pdir / fname).is_file()
    except Exception:
        return False


def _sanitize_filename(name: str) -> str:
    s = str(name).strip().replace("/", "_").replace("\\", "_")
    if not s:
        return ""
    for ch in ('\0', ':', '*', '?', '"', '<', '>', '|'):
        s = s.replace(ch, "_")
    s = s.strip()
    if s.startswith("."):
        s = "_" + s
    if not s.lower().endswith(".py"):
        if "." not in s:
            s += ".py"
        elif not s.lower().endswith(".py"):
            # если расширение другое — всё равно .py
            s = s.rsplit(".", 1)[0] + ".py"
    return s


def _validate_py(path: Path) -> tuple[bool, str]:
    """Проверить .py файл через py_compile (doraise=True)."""
    try:
        py_compile.compile(str(path), cfile=None, doraise=True)
        return True, "ok"
    except py_compile.PyCompileError as e:
        return False, f"py_compile: {e}"
    except SyntaxError as e:
        return False, f"SyntaxError: {e}"
    except OSError as e:
        return False, f"OSError: {e}"
    except Exception as e:
        return False, f"error: {e}"


def install_plugin(settings: dict, plugin: dict, plugin_manager: Any | None = None) -> tuple[bool, str]:
    """Установить плагин: скопировать .py из registry в vault/_System/Plugins/.

    Args:
        settings: словарь настроек (vault_root)
        plugin: dict из registry или с ключом id/filename
        plugin_manager: опционально PluginManager для автоперезагрузки

    Returns:
        (ok, message)
    """
    # нормализация plugin dict если передан id
    if isinstance(plugin, str):
        lst = load_registry(settings)
        found = get_plugin_by_id(lst, plugin)
        if found is None:
            return False, f"плагин {plugin!r} не найден в registry"
        plugin = found
    if not isinstance(plugin, dict):
        return False, "неверный формат плагина"
    pid = str(plugin.get("id") or plugin.get("filename") or "").strip()
    if not pid:
        return False, "у плагина нет id/filename"
    filename = _sanitize_filename(str(plugin.get("filename") or f"{pid}.py"))
    if not filename:
        return False, "неверное имя файла"
    # защищаем от path traversal
    if "/" in filename or "\\" in filename or filename.startswith("."):
        return False, "неверное имя файла"

    registry_path = get_registry_path(settings)
    source = _resolve_source_file(plugin, registry_path)
    inline_code = plugin.get("code")
    # если source не найден но есть inline code — используем его
    pdir = ensure_plugins_dir(settings)
    dest = pdir / filename

    try:
        pdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"не могу создать {pdir}: {e}"

    if source is not None and source.is_file():
        # py_compile проверка исходника до копирования
        ok, msg = _validate_py(source)
        if not ok:
            return False, f"исходник не прошёл py_compile: {msg}"
        try:
            shutil.copy2(str(source), str(dest))
        except OSError as e:
            return False, f"ошибка копирования: {e}"
        # проверка скопированного файла
        ok2, msg2 = _validate_py(dest)
        if not ok2:
            try:
                dest.unlink()
            except Exception:
                pass
            return False, f"скопированный файл битый: {msg2}"
    elif inline_code and isinstance(inline_code, str) and inline_code.strip():
        # записать inline код
        try:
            dest.write_text(inline_code, encoding="utf-8")
        except OSError as e:
            return False, f"ошибка записи: {e}"
        ok2, msg2 = _validate_py(dest)
        if not ok2:
            try:
                dest.unlink()
            except Exception:
                pass
            return False, f"код не прошёл py_compile: {msg2}"
    else:
        if source is None:
            # пробуем найти по filename в packaged plugins
            return False, f"исходник не найден для {filename} (registry: {registry_path})"
        return False, f"исходник не найден: {source}"

    # опционально перезагрузить менеджер
    if plugin_manager is not None:
        try:
            # если передан dict с settings — синхронизируем
            if hasattr(plugin_manager, "load_plugins"):
                plugin_manager.load_plugins(settings)
            elif hasattr(plugin_manager, "reload"):
                plugin_manager.reload(settings)
        except Exception:
            # не критично — установка уже состоялась
            pass
    else:
        # пробуем глобальный fallback
        try:
            from ..core.plugins import get_plugin_manager

            mgr = get_plugin_manager(settings)
            if mgr is not None and hasattr(mgr, "load_plugins"):
                mgr.load_plugins(settings)
        except Exception:
            pass

    return True, f"установлен {filename} → {PLUGINS_REL}/"


def uninstall_plugin(settings: dict, plugin: dict | str, plugin_manager: Any | None = None) -> tuple[bool, str]:
    """Удалить плагин из vault/_System/Plugins/ (unlink).

    Args:
        settings: словарь настроек
        plugin: dict или id/filename
        plugin_manager: опционально для перезагрузки

    Returns:
        (ok, message)
    """
    if isinstance(plugin, str):
        # может быть id или filename.maybe
        plugins = load_registry(settings)
        found = get_plugin_by_id(plugins, plugin)
        if found is not None:
            filename = _sanitize_filename(str(found.get("filename") or f"{found['id']}.py"))
        else:
            filename = _sanitize_filename(plugin)
            if not filename.endswith(".py"):
                filename += ".py"
    elif isinstance(plugin, dict):
        filename = _sanitize_filename(str(plugin.get("filename") or plugin.get("id") or ""))
        if not filename:
            return False, "неверное имя файла"
    else:
        return False, "неверный формат плагина"

    pdir = get_plugins_dir(settings)
    dest = pdir / filename
    # защита от traversal
    try:
        dest.resolve().relative_to(pdir.resolve())
    except Exception:
        # если pdir не существует — просто проверяем имя без /
        if "/" in filename or "\\" in filename:
            return False, "неверное имя файла"

    if not dest.is_file():
        return False, f"не установлен: {filename}"

    try:
        dest.unlink()
    except OSError as e:
        return False, f"ошибка удаления: {e}"
    # также удалить __pycache__ если остался
    try:
        cache = pdir / "__pycache__"
        if cache.is_dir():
            for e in cache.glob(f"{Path(filename).stem}.*"):
                try:
                    e.unlink()
                except Exception:
                    pass
    except Exception:
        pass

    if plugin_manager is not None:
        try:
            if hasattr(plugin_manager, "load_plugins"):
                plugin_manager.load_plugins(settings)
            elif hasattr(plugin_manager, "reload"):
                plugin_manager.reload(settings)
        except Exception:
            pass
    else:
        try:
            from ..core.plugins import get_plugin_manager

            mgr = get_plugin_manager(settings)
            if mgr is not None and hasattr(mgr, "load_plugins"):
                mgr.load_plugins(settings)
        except Exception:
            pass

    return True, f"удалён {filename}"


# ── UI ────────────────────────────────────────────────────────────────────────

if _GTK_AVAILABLE:

    def _content_clamp(child: Gtk.Widget) -> Adw.Clamp:
        clamp = Adw.Clamp(maximum_size=960, tightening_threshold=720)
        clamp.set_child(child)
        return clamp

    class PluginStoreView(Gtk.Box):
        """Вкладка Магазин плагинов: registry + установка/удаление в vault/_System/Plugins/."""

        def __init__(self, settings: dict, plugin_manager: Any | None = None) -> None:
            super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            self.settings = dict(settings) if isinstance(settings, dict) else {}
            # пробуем взять глобальный manager если не передан
            self.plugin_manager = plugin_manager
            if self.plugin_manager is None:
                try:
                    from ..core.plugins import get_plugin_manager

                    self.plugin_manager = get_plugin_manager(self.settings)
                except Exception:
                    self.plugin_manager = None
            self._registry: list[dict] = []
            self._filter_text: str = ""
            self._build()
            self.reload()

        # ── сборка ────────────────────────────────────────────────────────
        def _build(self) -> None:
            self.append(view_header("🧩", "Магазин плагинов", "Локальный registry · установка в vault/_System/Plugins/ · проверка py_compile"))

            # toolbar
            bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["toolbar"])
            bar.set_margin_start(14)
            bar.set_margin_end(14)
            self._search = Gtk.SearchEntry(placeholder_text="Поиск плагинов…", hexpand=True)
            self._search.connect("search-changed", self._on_search_changed)
            bar.append(self._search)
            refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Обновить список")
            refresh.connect("clicked", lambda *_: self.reload(force=True))
            bar.append(refresh)
            open_btn = Gtk.Button(label="Открыть папку", tooltip_text="Открыть vault/_System/Plugins/ в файловом менеджере")
            open_btn.connect("clicked", lambda *_: self._open_plugins_folder())
            bar.append(open_btn)
            self.append(bar)

            # info bar
            self._info = Gtk.Label(label="", halign=Gtk.Align.START, xalign=0, wrap=True, css_classes=["dim-hint"])
            self._info.set_margin_start(14)
            self._info.set_margin_end(14)
            self.append(self._info)

            # main scrolled
            scroller = Gtk.ScrolledWindow(vexpand=True)
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            body.set_margin_start(14)
            body.set_margin_end(14)
            body.set_margin_bottom(16)
            body.set_margin_top(4)

            # list
            self._listbox = Gtk.ListBox(css_classes=["plugin-list"])
            self._listbox.set_selection_mode(Gtk.SelectionMode.NONE)
            body.append(self._listbox)

            # empty states
            self._empty = empty_state("🧩", "Плагинов не найдено", hint="Проверь fragilenotes/data/plugin_registry.json", action_label="Обновить", on_action=lambda: self.reload(force=True))
            self._empty.set_visible(False)
            body.append(self._empty)
            self._filter_empty = empty_state("🔍", "Ничего не найдено", hint="Попробуй другой запрос", action_label="Очистить", on_action=lambda: self._search.set_text(""))
            self._filter_empty.set_visible(False)
            body.append(self._filter_empty)

            # hint внизу
            hint = Gtk.Label(label="Плагины — Python файлы из vault/_System/Plugins/*.py · хуки: on_open, on_save, on_new · register_hook(name, fn)", halign=Gtk.Align.START, xalign=0, wrap=True, css_classes=["dim-hint"])
            hint.set_margin_top(8)
            body.append(hint)
            self._count = Gtk.Label(label="", css_classes=["dim-hint"], halign=Gtk.Align.START)
            body.append(self._count)

            clamp = _content_clamp(body)
            scroller.set_child(clamp)
            self.append(scroller)

        # ── данные ────────────────────────────────────────────────────────
        def reload(self, force: bool = False) -> None:  # noqa: ARG002
            """Перечитать registry и состояние установленных."""
            try:
                ensure_plugins_dir(self.settings)
            except Exception:
                pass
            self._registry = load_registry(self.settings)
            self._render_list()
            self._update_info()

        def _update_info(self) -> None:
            reg = get_registry_path(self.settings)
            pdir = get_plugins_dir(self.settings)
            try:
                installed = [p for p in self._registry if is_installed(self.settings, p)]
            except Exception:
                installed = []
            reg_txt = str(reg) if reg is not None else "не найден"
            self._info.set_text(f"registry: {reg_txt} · {len(self._registry)} доступно · {len(installed)} установлено · папка: {pdir}")
            self._count.set_text(f"{len(self._registry)} плагинов · {len(installed)} установлено" if self._registry else "")

        def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
            self._filter_text = entry.get_text().strip().lower()
            self._render_list()

        def _filtered(self) -> list[dict]:
            q = self._filter_text
            if not q:
                return list(self._registry)
            out: list[dict] = []
            for p in self._registry:
                hay = " ".join([str(p.get("id", "")), str(p.get("name", "")), str(p.get("description", "")), str(p.get("author", "")), str(p.get("filename", "")), str(p.get("category", ""))]).lower()
                if q in hay:
                    out.append(p)
            return out

        def _render_list(self) -> None:
            while (child := self._listbox.get_first_child()) is not None:
                self._listbox.remove(child)
            flt = self._filtered()
            has_any = bool(self._registry)
            self._empty.set_visible(not has_any)
            self._filter_empty.set_visible(has_any and not flt)
            self._listbox.set_visible(bool(flt))
            for plugin in flt:
                row = self._row_for(plugin)
                self._listbox.append(row)
            self._update_info()

        def _row_for(self, plugin: dict) -> Gtk.Widget:
            installed = is_installed(self.settings, plugin)
            row = Gtk.ListBoxRow(activatable=False, selectable=False, css_classes=["plugin-row", "glass-card"])
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            card.set_margin_top(8)
            card.set_margin_bottom(8)
            card.set_margin_start(12)
            card.set_margin_end(12)

            # header: icon + name + version + pill
            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, valign=Gtk.Align.CENTER)
            head.set_hexpand(True)
            icon = Gtk.Label(label="🧩", css_classes=["sb-nav-icon"])
            head.append(icon)
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
            title = Gtk.Label(label=str(plugin.get("name", plugin.get("id"))), halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["task-title"])
            vbox.append(title)
            sub = Gtk.Label(label=f"{plugin.get('id')} · v{plugin.get('version')} · {plugin.get('author')}" if plugin.get("author") else f"{plugin.get('id')} · v{plugin.get('version')}", halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["dim-hint", "dim-label"])
            vbox.append(sub)
            head.append(vbox)
            pill = status_pill("установлен" if installed else "не установлен", "ok" if installed else "idle")
            head.append(pill)
            # версия badge
            ver = Gtk.Label(label=f"v{plugin.get('version')}", css_classes=["pill", "pill-idle"])
            head.append(ver)
            card.append(head)

            # description
            desc = str(plugin.get("description") or "").strip()
            if desc:
                lbl = Gtk.Label(label=desc, halign=Gtk.Align.START, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, css_classes=["dim-hint"])
                lbl.set_max_width_chars(80)
                card.append(lbl)

            # meta line: filename + hooks + category
            meta = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["plugin-meta"])
            meta.set_margin_top(2)
            fname = str(plugin.get("filename") or "")
            meta.append(Gtk.Label(label=f"📄 {fname}", css_classes=["dim-hint", "pill", "pill-idle"]))
            hooks = plugin.get("hooks") or []
            if hooks:
                meta.append(Gtk.Label(label="hooks: " + ", ".join(hooks), css_classes=["dim-hint"]))
            cat = str(plugin.get("category") or "").strip()
            if cat:
                meta.append(Gtk.Label(label=f"#{cat}", css_classes=["dim-hint"]))
            # spacer
            meta.append(Gtk.Box(hexpand=True))
            # action button
            if installed:
                btn = Gtk.Button(label="Удалить", css_classes=["destructive-action"])
                btn.connect("clicked", lambda *_b, p=plugin: self._on_uninstall(p))
                btn.set_tooltip_text(f"Удалить {fname} из {PLUGINS_REL}/")
            else:
                btn = Gtk.Button(label="Установить", css_classes=["suggested-action"])
                btn.connect("clicked", lambda *_b, p=plugin: self._on_install(p))
                btn.set_tooltip_text(f"Установить {fname} → {PLUGINS_REL}/ (py_compile)")
            meta.append(btn)
            # view source button если есть source
            src = _resolve_source_file(plugin, get_registry_path(self.settings))
            if src is not None and src.is_file():
                view_btn = Gtk.Button(label="Код", css_classes=["mod-neutral", "btn-sm"])
                view_btn.set_tooltip_text(f"Показать исходник: {src.name}")
                view_btn.connect("clicked", lambda *_b, s=src: self._show_source(s))
                meta.append(view_btn)
            card.append(meta)

            row.set_child(card)
            return row

        # ── действия ────────────────────────────────────────────────────
        def _on_install(self, plugin: dict) -> None:
            ok, msg = install_plugin(self.settings, plugin, self.plugin_manager)
            if ok:
                self._toast(f"✓ {msg}")
                self._render_list()
            else:
                self._toast(f"✗ {msg}", error=True)
                # диалог ошибки
                self._show_error_dialog("Ошибка установки", msg)

        def _on_uninstall(self, plugin: dict) -> None:
            fname = plugin.get("filename") or plugin.get("id")
            dlg = Adw.MessageDialog.new(self.get_root(), f"Удалить плагин «{plugin.get('name', fname)}»?")
            dlg.add_response("cancel", "Отмена")
            dlg.add_response("delete", "Удалить")
            dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_default_response("cancel")
            dlg.set_close_response("cancel")

            def on_resp(d, resp: str) -> None:
                if resp == "delete":
                    ok, msg = uninstall_plugin(self.settings, plugin, self.plugin_manager)
                    if ok:
                        self._toast(f"✓ {msg}")
                        self._render_list()
                    else:
                        self._toast(f"✗ {msg}", error=True)
                        self._show_error_dialog("Ошибка удаления", msg)

            dlg.connect("response", on_resp)
            dlg.present()

        def _show_source(self, path: Path) -> None:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                self._toast(f"ошибка чтения: {e}", error=True)
                return
            dlg = Adw.Dialog(title=f"Исходник — {path.name}")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            box.set_margin_top(12)
            box.set_margin_bottom(12)
            box.set_margin_start(12)
            box.set_margin_end(12)
            header = Gtk.Label(label=f"{path.name} · {len(text.splitlines())} строк", halign=Gtk.Align.START, css_classes=["dim-hint"])
            box.append(header)
            buf = Gtk.TextBuffer(text=text)
            view = Gtk.TextView(buffer=buf, editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD, hexpand=True, vexpand=True)
            view.set_margin_top(6)
            sc = Gtk.ScrolledWindow(vexpand=True, hexpand=True, min_content_height=320, min_content_width=560)
            sc.set_child(view)
            sc.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            box.append(sc)
            btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
            close = Gtk.Button(label="Закрыть", css_classes=["suggested-action"])
            close.connect("clicked", lambda *_: dlg.close())
            btns.append(close)
            box.append(btns)
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            content.append(box)
            dlg.set_child(content)
            dlg.set_content_width(640)
            box.set_size_request(640, 420)
            dlg.present(self)

        def _show_error_dialog(self, title: str, msg: str) -> None:
            dlg = Adw.MessageDialog.new(self.get_root(), title)
            dlg.set_body(msg[:800])
            dlg.add_response("ok", "OK")
            dlg.set_default_response("ok")
            dlg.set_close_response("ok")
            dlg.present()

        def _open_plugins_folder(self) -> None:
            pdir = get_plugins_dir(self.settings)
            try:
                pdir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            try:
                # Gio
                Gio.AppInfo.launch_default_for_uri(f"file://{pdir}", None)
                self._toast(f"открыта папка {PLUGINS_REL}/")
                return
            except Exception:
                pass
            try:
                import subprocess

                subprocess.Popen(["xdg-open", str(pdir)])
                self._toast(f"открыта папка {pdir}")
            except Exception as e:
                self._toast(f"ошибка: {e}", error=True)

        def _toast(self, msg: str, error: bool = False) -> None:  # noqa: ARG002
            try:
                win = self.get_root()
                if win is not None and hasattr(win, "toast_overlay"):
                    t = Adw.Toast.new(msg)
                    t.set_timeout(3)
                    win.toast_overlay.add_toast(t)  # type: ignore
                    return
            except Exception:
                pass
            # fallback — обновить info
            try:
                old = self._info.get_text() if hasattr(self, "_info") else ""
                self._info.set_text(msg)
                GLib.timeout_add(3000, lambda: self._info.set_text(old) or False)
            except Exception:
                pass

        # для внешний refresh_all — alias
        def refresh(self) -> None:
            self.reload()

else:
    # Заглушка для headless/тестов — не требует GTK
    class PluginStoreView:  # type: ignore[no-redef]
        def __init__(self, *a, **kw) -> None:
            raise RuntimeError("GTK недоступен — PluginStoreView требует графической среды")


__all__ = [
    "PluginStoreView",
    "load_registry",
    "get_registry_path",
    "get_plugin_by_id",
    "is_installed",
    "install_plugin",
    "uninstall_plugin",
]
