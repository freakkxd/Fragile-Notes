"""Workspaces — несколько vault в одном окне.

Хранение списка vault (path + name) в settings.json.
Поддержка быстрого переключения Ctrl+Shift+O и UI в sidebar.

Модель: чистые функции без GTK, работают с dict settings.

Схема в settings.json::

    {
      "vault_root": "/home/user/notes",
      "vaults": [
        {"path": "/home/user/notes", "name": "notes"},
        {"path": "/home/user/work", "name": "work"}
      ]
    }

Где ``vault_root`` — активный vault, ``vaults`` — сохранённый список.
Ключ ``workspaces`` поддерживается как алиас ``vaults`` для совместимости.

Функции мутируют переданный ``settings`` in-place и возвращают
нормализованный список (удобно для цепочек). Сохранение на диск —
ответственность вызывающего через :func:`fragilenotes.config.save_settings`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

SETTINGS_KEY = "vaults"
ALT_KEY = "workspaces"


def _display_name(path: str | Path, name: str | None = None) -> str:
    """Имя по умолчанию:basename пути или полный путь если пусто."""
    if name is not None:
        n = str(name).strip()
        if n:
            return n
    try:
        p = Path(str(path)).expanduser()
        base = p.name.strip()
        if base:
            return base
        # корневой путь вроде "/" или "C:\\"
        if str(p).strip():
            return str(p).strip()
    except Exception:
        pass
    return str(path).strip() or "vault"


def _normalize_path(raw: str | Path) -> str:
    """Нормализовать путь к абсолютному строковому виду без резолва несуществующих.

    - ``~`` раскрывается
    - относительные → абсолютные через ``Path.absolute()``
    - убираем завершающий слеш (кроме корня)
    """
    s = str(raw).strip()
    if not s:
        return s
    try:
        p = Path(s).expanduser()
        # absolute без resolve (чтобы не падать на несуществующих)
        if not p.is_absolute():
            p = (Path.cwd() / p)
        # нормализуем ``.`` и ``..`` без обязательного существования
        try:
            # resolve без strict — уберёт ``.``/``..``, по возможности симлинки
            p = p.resolve(strict=False)
        except Exception:
            p = p.absolute()
        out = str(p)
        # убрать trailing slash кроме "/"
        if len(out) > 1 and out.endswith("/"):
            out = out.rstrip("/")
        return out
    except Exception:
        return s


def _get_raw_list(settings: dict) -> list[Any]:
    """Достать сырой список из settings (vaults | workspaces)."""
    if not isinstance(settings, dict):
        return []
    v = settings.get(SETTINGS_KEY)
    if isinstance(v, list):
        return v
    # fallback alias
    w = settings.get(ALT_KEY)
    if isinstance(w, list):
        return w
    return []


def _dedupe_entries(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    """Удалить дубликаты по нормализованному path, сохраняем первый."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for e in entries:
        p = _normalize_path(e.get("path", ""))
        if not p:
            continue
        key = p.lower() if _is_windows_path(p) else p
        if key in seen:
            continue
        seen.add(key)
        out.append({"path": p, "name": e.get("name") or _display_name(p, e.get("name"))})
    return out


def _is_windows_path(p: str) -> bool:
    return len(p) > 1 and p[1] == ":"


def normalize_vaults(settings: dict[str, Any]) -> list[dict[str, str]]:
    """Нормализовать список vaults из settings.

    - Читает ``vaults`` (или ``workspaces``).
    - Фильтрует битые записи (без path).
    - Нормализует пути, подставляет имя по умолчанию.
    - Дедуплицирует по пути.
    - Гарантирует что ``vault_root`` входит в список (если указан).
    """
    raw = _get_raw_list(settings)
    entries: list[dict[str, str]] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        path = str(it.get("path") or it.get("vault_root") or "").strip()
        if not path:
            continue
        name = it.get("name") or it.get("title") or ""
        entries.append({"path": _normalize_path(path), "name": _display_name(path, str(name) if name else None)})
    # гарантируем vault_root в списке
    vault_root = str(settings.get("vault_root") or "").strip()
    if vault_root:
        norm_root = _normalize_path(vault_root)
        has_root = any(_normalize_path(e["path"]) == norm_root or e["path"] == norm_root for e in entries)
        if not has_root:
            entries.insert(0, {"path": norm_root, "name": _display_name(norm_root)})
    # дедуп
    entries = _dedupe_entries(entries)
    return entries


def get_vaults(settings: dict[str, Any]) -> list[dict[str, str]]:
    """Публичный геттер — алиас normalize_vaults."""
    return normalize_vaults(settings)


def current_vault(settings: dict[str, Any]) -> dict[str, str] | None:
    """Активный vault как ``{path, name}`` или ``None`` если vault_root пуст."""
    root = str(settings.get("vault_root") or "").strip()
    if not root:
        return None
    norm = _normalize_path(root)
    # имя — из списка если есть, иначе basename
    for v in normalize_vaults(settings):
        if _normalize_path(v["path"]) == norm:
            return {"path": norm, "name": v["name"]}
    return {"path": norm, "name": _display_name(norm)}


def is_known_vault(settings: dict[str, Any], path: str | Path) -> bool:
    norm = _normalize_path(str(path))
    for v in normalize_vaults(settings):
        if _normalize_path(v["path"]) == norm:
            return True
    return False


def ensure_vaults(settings: dict[str, Any]) -> list[dict[str, str]]:
    """Убедиться что ``settings['vaults']`` нормализован и включает ``vault_root``.

    Мутирует ``settings`` и возвращает нормализованный список.
    """
    norm = normalize_vaults(settings)
    # записать обратно под основным ключом
    settings[SETTINGS_KEY] = [dict(e) for e in norm]
    # если использовался ALT_KEY — синхронизировать для совместимости
    if ALT_KEY in settings and isinstance(settings.get(ALT_KEY), list):
        settings[ALT_KEY] = [dict(e) for e in norm]
    return norm


def add_vault(settings: dict[str, Any], path: str | Path, name: str | None = None) -> list[dict[str, str]]:
    """Добавить vault в список (дедуп по пути). Мутирует settings.

    Если vault уже есть — обновит имя если передано новое непустое.
    """
    p = _normalize_path(str(path).strip())
    if not p:
        return normalize_vaults(settings)
    n = _display_name(p, name)
    # собрать текущие
    vaults = normalize_vaults(settings)
    for v in vaults:
        if _normalize_path(v["path"]) == p:
            if name is not None and str(name).strip():
                v["name"] = _display_name(p, name)
            settings[SETTINGS_KEY] = [dict(e) for e in vaults]
            return vaults
    vaults.append({"path": p, "name": n})
    vaults = _dedupe_entries(vaults)
    settings[SETTINGS_KEY] = [dict(e) for e in vaults]
    return vaults


def remove_vault(settings: dict[str, Any], path: str | Path) -> list[dict[str, str]]:
    """Удалить vault по пути. Мутирует settings.

    Не удаляет последний элемент если это активный ``vault_root``? Удаляет,
    но ``vault_root`` при этом остаётся (пользователь должен переключить).
    """
    p = _normalize_path(str(path).strip())
    if not p:
        return normalize_vaults(settings)
    vaults = normalize_vaults(settings)
    vaults = [v for v in vaults if _normalize_path(v["path"]) != p]
    settings[SETTINGS_KEY] = [dict(e) for e in vaults]
    return vaults


def rename_vault(settings: dict[str, Any], path: str | Path, new_name: str) -> list[dict[str, str]]:
    """Переименовать vault по пути."""
    p = _normalize_path(str(path).strip())
    n = str(new_name).strip()
    if not p or not n:
        return normalize_vaults(settings)
    vaults = normalize_vaults(settings)
    for v in vaults:
        if _normalize_path(v["path"]) == p:
            v["name"] = n
            break
    settings[SETTINGS_KEY] = [dict(e) for e in vaults]
    return vaults


def update_vault(
    settings: dict[str, Any],
    old_path: str | Path,
    new_path: str | Path | None = None,
    new_name: str | None = None,
) -> list[dict[str, str]]:
    """Обновить путь и/или имя vault."""
    op = _normalize_path(str(old_path).strip())
    if not op:
        return normalize_vaults(settings)
    vaults = normalize_vaults(settings)
    for v in vaults:
        if _normalize_path(v["path"]) == op:
            if new_path is not None and str(new_path).strip():
                np = _normalize_path(str(new_path).strip())
                v["path"] = np
            if new_name is not None and str(new_name).strip():
                v["name"] = str(new_name).strip()
            elif new_path is not None and str(new_path).strip():
                # если имя не передано, но путь сменился — обновить имя на basename если старое совпадало с базисным
                v["name"] = _display_name(v["path"], v.get("name"))
            break
    vaults = _dedupe_entries(vaults)
    settings[SETTINGS_KEY] = [dict(e) for e in vaults]
    return vaults


def switch_vault(settings: dict[str, Any], path: str | Path) -> dict[str, Any]:
    """Переключить активный vault.

    - Нормализует путь
    - Добавляет его в список если отсутствует
    - Устанавливает ``settings['vault_root']`` на новый путь
    - Синхронизирует ``settings['vaults']``

    Возвращает мутированный ``settings``.
    """
    p = _normalize_path(str(path).strip())
    if not p:
        return settings
    # добавить если нет
    add_vault(settings, p)
    settings["vault_root"] = p
    # убедиться что список актуален
    ensure_vaults(settings)
    return settings


__all__ = [
    "SETTINGS_KEY",
    "ALT_KEY",
    "normalize_vaults",
    "get_vaults",
    "current_vault",
    "is_known_vault",
    "ensure_vaults",
    "add_vault",
    "remove_vault",
    "rename_vault",
    "update_vault",
    "switch_vault",
]
