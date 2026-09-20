"""Плагин-система FragileNotes: загрузка Python хуков из vault/_System/Plugins/*.py.

Хуки: on_open, on_save, on_new
API для плагинов: register_hook(name, func)

Пример плагина vault/_System/Plugins/example.py::

    def my_open(path):
        print(f"opened {path}")

    register_hook("on_open", my_open)

Хук ``register_hook`` инъектируется в globals каждого плагина перед exec.
Также доступен импорт::

    from fragilenotes.core.plugins import register_hook

Выполнение изолировано: ошибки одного плагина не ломают другие.
Перед exec выполняется проверка py_compile.
"""

from __future__ import annotations

import inspect
import py_compile
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

PLUGINS_REL = "_System/Plugins"
VALID_HOOKS: tuple[str, ...] = ("on_open", "on_save", "on_new")
HOOKS = VALID_HOOKS  # alias для совместимости

# ── утилы путей ───────────────────────────────────────────────────


def get_plugins_dir(settings: dict) -> Path:
    """Путь к vault/_System/Plugins по настройкам."""
    root = settings.get("vault_root") if isinstance(settings, dict) else None
    if not root:
        root = str(Path.home() / "desktop")
    return Path(str(root)) / PLUGINS_REL


def ensure_plugins_dir(settings: dict) -> Path:
    """Создать директорию плагинов если отсутствует, вернуть путь."""
    p = get_plugins_dir(settings)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


# ── глобальное делегирование для ``from ... import register_hook`` ──

_current_manager: Any | None = None
_fallback_manager: Any | None = None


def _get_fallback() -> Any:
    global _fallback_manager
    if _fallback_manager is None:
        _fallback_manager = PluginManager()
    return _fallback_manager


def register_hook(hook: str, func: Callable[..., Any]) -> None:
    """Глобальный API для плагинов: делегирует в активный PluginManager.

    Во время ``PluginManager.load_plugins()`` ``_current_manager`` установлен,
    поэтому регистрация уходит в загружающий менеджер. Вне загрузки — в
    fallback-синглтон (или последний загруженный менеджер).
    """
    if _current_manager is not None:
        _current_manager.register_hook(hook, func)
        return
    _get_fallback().register_hook(hook, func)


def trigger(hook: str, *args: Any, **kwargs: Any) -> None:
    """Глобальный триггер хука (делегирует в fallback)."""
    if _fallback_manager is not None:
        try:
            _fallback_manager.trigger(hook, *args, **kwargs)
        except Exception:
            pass
        return
    # если fallback ещё не создан — нечего триггерить
    return


# alias
trigger_hook = trigger


def load_plugins(settings: dict | None = None) -> Any:
    """Удобный хелпер: создать менеджер и загрузить плагины."""
    mgr = PluginManager(settings)
    mgr.load_plugins()
    return mgr


def get_plugin_manager(settings: dict | None = None) -> Any:
    """Вернуть fallback-менеджер (создаст если нет)."""
    if _fallback_manager is not None:
        if settings is not None:
            try:
                _fallback_manager.settings = dict(settings)
            except Exception:
                pass
        return _fallback_manager
    return _get_fallback()


# ── PluginManager ─────────────────────────────────────────────────


class PluginManager:
    """Менеджер хуков FragileNotes.

    Хранит ``settings`` (vault_root) и список зарегистрированных хуков.
    Загрузка: ``vault/_System/Plugins/*.py`` → py_compile → exec с
    иньекцией ``register_hook``.
    """

    PLUGINS_REL = PLUGINS_REL
    VALID_HOOKS = VALID_HOOKS

    def __init__(self, settings: dict | None = None) -> None:
        self.settings: dict[str, Any] = dict(settings) if isinstance(settings, dict) and settings is not None else {}
        self._hooks: dict[str, list[Callable[..., Any]]] = {h: [] for h in VALID_HOOKS}
        self._modules: dict[str, dict[str, Any]] = {}
        self._loaded: list[str] = []
        self._errors: list[tuple[str, str]] = []

    # ── settings ──────────────────────────────────────────────
    def update_settings(self, settings: dict) -> None:
        """Синхронизация настроек (вызывается из app.py при сохранении)."""
        self.settings = dict(settings) if isinstance(settings, dict) and settings is not None else {}

    def get_plugins_dir(self) -> Path:
        return get_plugins_dir(self.settings)

    def ensure_plugins_dir(self) -> Path:
        return ensure_plugins_dir(self.settings)

    # ── хуки ──────────────────────────────────────────────────
    def register_hook(self, hook: str, func: Callable[..., Any]) -> None:
        """Зарегистрировать callback для хука.

        Args:
            hook: имя хука (on_open, on_save, on_new)
            func: callable

        Raises:
            ValueError: если хук неизвестен
            TypeError: если func не callable
        """
        if hook not in VALID_HOOKS:
            raise ValueError(f"unknown hook {hook!r}, valid: {VALID_HOOKS}")
        if not callable(func):
            raise TypeError(f"hook func must be callable, got {type(func)}")
        self._hooks.setdefault(hook, []).append(func)

    def unregister_hook(self, hook: str, func: Callable[..., Any]) -> None:
        """Удалить конкретный callback (если есть)."""
        lst = self._hooks.get(hook)
        if lst is None:
            return
        try:
            lst.remove(func)
        except ValueError:
            pass

    def clear(self) -> None:
        """Очистить все хуки и кэш загруженных модулей."""
        for k in self._hooks:
            self._hooks[k] = []
        self._modules.clear()
        self._loaded = []
        self._errors = []

    def hooks(self, name: str | None = None) -> dict[str, list[Callable[..., Any]]] | list[Callable[..., Any]]:
        """Вернуть хуки: все или по имени."""
        if name is not None:
            return list(self._hooks.get(name, []))
        return {k: list(v) for k, v in self._hooks.items()}

    def list_hooks(self) -> dict[str, list[Callable[..., Any]]]:
        return self.hooks()  # type: ignore[return-value]

    @property
    def loaded_plugins(self) -> list[str]:
        return list(self._loaded)

    @property
    def errors(self) -> list[tuple[str, str]]:
        return list(self._errors)

    @property
    def loaded(self) -> list[str]:
        return self.loaded_plugins

    # ── триггер ───────────────────────────────────────────────
    def trigger(self, hook: str, *args: Any, **kwargs: Any) -> None:
        """Вызвать все callbacks хука. Ошибки изолированы.

        Для ``on_save`` поддерживается сигнатура как ``(path)`` так и
        ``(path, content)``: если hook ожидает 1 аргумент а передано 2,
        делается fallback к первому.
        """
        if hook not in VALID_HOOKS:
            return
        # копия списка — хуки могут регистрировать другие хуки во время итерации
        for func in list(self._hooks.get(hook, [])):
            try:
                func(*args, **kwargs)
            except TypeError:
                # fallback для несовпадения сигнатуры (например on_save(path, content) vs on_save(path))
                try:
                    # пробуем с одним первым аргументом
                    if args:
                        # inspect для точного fallback
                        try:
                            sig = inspect.signature(func)
                            params = [p for p in sig.parameters.values() if p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)]
                            # считаем обязательные
                            if len(params) == 1 and len(args) >= 1:
                                func(args[0])
                                continue
                            if len(params) == 0:
                                func()
                                continue
                        except (ValueError, TypeError):
                            pass
                        # простой fallback: один аргумент
                        func(args[0])
                    else:
                        func()
                except Exception:
                    # изолируем даже fallback ошибки
                    print(f"[plugins] hook {hook} error in {getattr(func, '__name__', repr(func))}: {traceback.format_exc()}")
            except Exception:
                print(f"[plugins] hook {hook} error in {getattr(func, '__name__', repr(func))}: {traceback.format_exc()}")

    # alias
    trigger_hook = trigger
    emit = trigger

    def has_hook(self, hook: str) -> bool:
        return bool(self._hooks.get(hook))

    # ── загрузка ──────────────────────────────────────────────
    def load_plugins(self, settings: dict | None = None) -> list[str]:
        """Загрузить/перезагрузить плагины из vault/_System/Plugins/*.py.

        Выполняет py_compile проверку перед exec. Ошибки одного файла не
        прерывают загрузку остальных. Перед перезагрузкой хуки очищаются.

        Returns:
            список имён загруженных файлов (например ["example.py"])
        """
        global _current_manager, _fallback_manager
        if settings is not None:
            self.settings = dict(settings)
        # очистить предыдущие хуки (перезагрузка)
        self.clear()
        plugins_dir = get_plugins_dir(self.settings)
        try:
            plugins_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if not plugins_dir.is_dir():
            self._loaded = []
            self._errors = []
            _fallback_manager = self
            return []

        files = sorted(plugins_dir.glob("*.py"))
        loaded: list[str] = []
        errors: list[tuple[str, str]] = []

        for f in files:
            # пропускаем скрытые/служебные
            if f.name.startswith("_") or f.name.startswith("."):
                continue
            if f.name == "__init__.py":
                continue
            # ── py_compile валидация ──
            try:
                py_compile.compile(str(f), cfile=None, doraise=True)
            except py_compile.PyCompileError as e:
                errors.append((str(f), f"py_compile: {e}"))
                continue
            except OSError as e:
                errors.append((str(f), f"OSError: {e}"))
                continue
            except Exception as e:
                errors.append((str(f), f"compile error: {e}"))
                continue

            # ── чтение исходника ──
            try:
                source = f.read_text(encoding="utf-8")
            except OSError as e:
                errors.append((str(f), f"read error: {e}"))
                continue

            # ── compile для SyntaxError до exec ──
            try:
                code = compile(source, str(f), "exec")
            except SyntaxError as e:
                errors.append((str(f), f"SyntaxError: {e}"))
                continue
            except Exception as e:
                errors.append((str(f), f"compile error: {e}"))
                continue

            # ── namespace с инъекцией register_hook ──
            ns: dict[str, Any] = {
                "__file__": str(f),
                "__name__": f"fragilenotes.plugins.{f.stem}",
                "__package__": "fragilenotes.plugins",
                "__plugin_name__": f.stem,
                "register_hook": self.register_hook,
                # удобный alias: plugin_api / plugins
                "PLUGINS_DIR": str(plugins_dir),
            }

            # установить текущий менеджер для глобального register_hook
            prev = _current_manager
            _current_manager = self
            try:
                exec(code, ns)
                # авто-регистрация если плагин определил функции с именами хуков
                for hook_name in VALID_HOOKS:
                    if hook_name in ns and callable(ns[hook_name]):
                        # не дублировать если уже зарегистрирован тот же объект
                        fn = ns[hook_name]
                        if fn not in self._hooks.get(hook_name, []):
                            try:
                                self.register_hook(hook_name, fn)
                            except Exception:
                                pass
                loaded.append(f.name)
                self._modules[f.stem] = ns
            except Exception:
                errors.append((str(f), traceback.format_exc()))
            finally:
                _current_manager = prev

        self._loaded = loaded
        self._errors = errors
        # сделать этот менеджер fallback для глобальных вызовов trigger/register_hook
        _fallback_manager = self
        return loaded

    def reload(self, settings: dict | None = None) -> list[str]:
        """Алиас для load_plugins (перезагрузка)."""
        return self.load_plugins(settings=settings)


__all__ = [
    "PLUGINS_REL",
    "VALID_HOOKS",
    "HOOKS",
    "PluginManager",
    "get_plugins_dir",
    "ensure_plugins_dir",
    "register_hook",
    "trigger",
    "trigger_hook",
    "load_plugins",
    "get_plugin_manager",
]
