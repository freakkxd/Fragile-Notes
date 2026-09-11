"""Настройки приложения (persisted в ~/.config/fragile-notes/settings.json)."""

from __future__ import annotations

import glob
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

APP_DIR: Path = Path(
    os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
) / "fragile-notes"
SETTINGS_FILE: Path = APP_DIR / "settings.json"

# Версионирование настроек: текущая версия схемы.
SETTINGS_VERSION: int = 2

# Рабочее пространство: порядок/состав иконок левого ribbon (как Obsidian:
# workspace ribbon настраивается), plus размер окна. t = вид элемента:
#   view — переключение вкладки (id = ключ вьюхи)
#   cmd  — действие приложения (sidebar/refresh/search/new/save)
#   open — открыть заметку vault (icon, tip, path относительный от vault_root)
#   sep  — разделитель
DEFAULT_WORKSPACE: dict[str, Any] = {
    "ribbon": [
        {"t": "cmd", "id": "sidebar"},
        {"t": "cmd", "id": "refresh"},
        {"t": "cmd", "id": "search"},
        {"t": "sep"},
        {"t": "view", "id": "home"},
        {"t": "view", "id": "runner"},
        {"t": "view", "id": "tasks"},
        {"t": "view", "id": "habits"},
        {"t": "view", "id": "pomodoro"},
        {"t": "sep"},
        {"t": "view", "id": "daily"},
        {"t": "view", "id": "calendar"},
        {"t": "view", "id": "review"},
        {"t": "view", "id": "srs"},
        {"t": "view", "id": "media"},
        {"t": "view", "id": "voice"},
        {"t": "view", "id": "video"},
        {"t": "view", "id": "files"},
        {"t": "view", "id": "templates"},
        {"t": "view", "id": "graph"},
        {"t": "view", "id": "canvas"},
        {"t": "view", "id": "whiteboard"},
        {"t": "view", "id": "kanban"},
        {"t": "view", "id": "database"},
        {"t": "view", "id": "slides"},
        {"t": "view", "id": "mindmap"},
        {"t": "view", "id": "mermaid_live"},
        {"t": "view", "id": "latex_live"},
        {"t": "sep"},
        {"t": "view", "id": "analytics"},
        {"t": "view", "id": "plugin_store"},
        {"t": "view", "id": "theme_editor"},
        {"t": "view", "id": "ai_chat"},
        {"t": "view", "id": "settings"},
    ],
    "window_width": None,
    "window_height": None,
    "sidebar_width": 276,
}


DEFAULT_SETTINGS: dict[str, Any] = {
    "version": SETTINGS_VERSION,
    # Vault — активный + список воркспейсов (несколько vault в одном окне)
    # Исправлено: не на рабочем столе, а в Documents/FragileNotes (Windows) / ~/FragileNotes (Linux)
    "vault_root": str(Path.home() / "Documents" / "FragileNotesVault") if (Path.home() / "Documents").exists() else str(Path.home() / "FragileNotesVault"),
    "vaults": [],  # [{path, name}]
    "workspaces": [],  # алиас vaults для совместимости
    # AO Engine (Node CLI)
    "node_path": "node",
    "engine_cli": "_System/ArchiveOrganism/ao-engine/dist/cli/cli.js",
    "engine_status_url": "http://127.0.0.1:17340/internal/enrich-status",
    # LLM servers
    "manage_llm_script": str(Path.home() / "LLM/scripts/manage-llm.sh"),
    "llm_day_port": 11435,
    "llm_archive_port": 11434,
    "llm_day_ctx": 12288,
    "llm_archive_ctx": 196608,
    # Fragilich Suite (Task Manager)
    "tm_tasks_folder": "_System/TaskManagerMain/_system/tasks",
    "tm_comments_folder": "_System/TaskManagerMain/_system/comments",
    "tm_templates_root": "_System/TaskManagerMain/Templates",
    "daily_folder": "02 Daily",
    "daily_template": "_System/TaskManagerMain/Templates/Daily note.md",
    "weekly_template": "_System/TaskManagerMain/Templates/Weekly Review.md",
    "weekly_review_folder": "Review",
    # Enrich pipeline
    "enrich_default_limit": 20,
    # Token warnings
    "tokens_soft_warning_percent": 90,
    # Масштабирование
    "ui_scale": 1.0,              # множитель масштаба интерфейса (0.75–2.0)
    "editor_zoom": 1.0,           # зум текста редактора/просмотра (0.5–2.0)
    "follow_system_scale": True,  # учитывать GNOME text-scaling-factor
    # Кастомизация рабочего пространства
    "workspace": DEFAULT_WORKSPACE,
    # Git auto-sync
    "git_auto_sync": True,
    "git_auto_push": True,
    "git_commit_template": "auto-sync: {now}",
    # Vim-режим
    "vim_mode": False,
    # Тема оформления
    "theme": "auto",  # auto | light | dark — Adw.StyleManager
    # Совместное редактирование (CRDT)
    "crdt_enabled": False,
    "crdt_kind": "lww",  # lww | rga
    "crdt_replica_id": "",
    # Крипто-папка авто-шифрования (vault/Secret/ -> .md.enc AES-GCM)
    "crypto_folder": "Secret",
    "auto_encrypt": True,
    # E2E шифрование всего vault (общий ключ PBKDF2 -> AES-GCM per-file)
    "e2e_enabled": False,
    "e2e_salt_file": ".e2e_salt",
}


# ── Миграция ribbon ───────────────────────────────────────────

def _is_ribbon_outdated(ribbon: list[Any]) -> bool:
    """Старый ribbon: без graph/voice/ai_chat/canvas/whiteboard/kanban/calendar/database/templates/review или без сепараторов ( <3 ).

    VAULT/СИСТЕМА — группы отделены sep: cmds | main | vault | system.
    Старые конфиги имели 0-1 sep и не содержали graph/voice/ai_chat/canvas/kanban/calendar/database/templates/review.
    Короткие фрагменты (<7 элементов, как в тестах) не считаем устаревшими.
    """
    if not isinstance(ribbon, list) or not ribbon:
        return False  # пустой обработает normalize -> default
    # Не трогаем короткие тестовые/пользовательские фрагменты — только полный старый ribbon
    if len(ribbon) < 7:
        return False
    has_graph = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "graph"
        for it in ribbon
    )
    if not has_graph:
        return True
    has_voice = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "voice"
        for it in ribbon
    )
    if not has_voice:
        return True
    has_video = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "video"
        for it in ribbon
    )
    if not has_video:
        return True
    has_ai = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "ai_chat"
        for it in ribbon
    )
    if not has_ai:
        return True
    has_canvas = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "canvas"
        for it in ribbon
    )
    if not has_canvas:
        return True
    has_kanban = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "kanban"
        for it in ribbon
    )
    if not has_kanban:
        return True
    has_calendar = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "calendar"
        for it in ribbon
    )
    if not has_calendar:
        return True
    has_database = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "database"
        for it in ribbon
    )
    if not has_database:
        return True
    has_templates = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "templates"
        for it in ribbon
    )
    if not has_templates:
        return True
    has_slides = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "slides"
        for it in ribbon
    )
    if not has_slides:
        return True
    has_review = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "review"
        for it in ribbon
    )
    if not has_review:
        return True
    has_pomodoro = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "pomodoro"
        for it in ribbon
    )
    if not has_pomodoro:
        return True
    has_habits = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "habits"
        for it in ribbon
    )
    if not has_habits:
        return True
    has_srs = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "srs"
        for it in ribbon
    )
    if not has_srs:
        return True
    has_mindmap = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "mindmap"
        for it in ribbon
    )
    if not has_mindmap:
        return True
    has_whiteboard = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "whiteboard"
        for it in ribbon
    )
    if not has_whiteboard:
        return True
    has_analytics = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "analytics"
        for it in ribbon
    )
    if not has_analytics:
        return True
    has_mermaid_live = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "mermaid_live"
        for it in ribbon
    )
    if not has_mermaid_live:
        return True
    has_latex_live = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "latex_live"
        for it in ribbon
    )
    if not has_latex_live:
        return True
    has_theme_editor = any(
        isinstance(it, dict) and it.get("t") == "view" and it.get("id") == "theme_editor"
        for it in ribbon
    )
    if not has_theme_editor:
        return True
    sep_count = sum(1 for it in ribbon if isinstance(it, dict) and it.get("t") == "sep")
    if sep_count < 3:
        return True
    return False


def _migrate_ribbon(old_ribbon: list[Any]) -> list[dict[str, Any]]:
    """Обновить старый ribbon до DEFAULT_WORKSPACE, сохраняя open-элементы.

    Сохраняет пользовательские open-элементы (заметки) и их порядок,
    остальное — сбрасывает на дефолт.
    """
    open_items: list[dict[str, Any]] = []
    for it in old_ribbon:
        if not isinstance(it, dict) or it.get("t") != "open":
            continue
        path = str(it.get("path") or "").strip()
        if not path:
            continue
        open_items.append({
            "t": "open",
            "id": it.get("id") or f"open-{len(open_items)}",
            "icon": str(it.get("icon") or "📄")[:2],
            "tip": str(it.get("tip") or path).strip() or path,
            "path": path,
        })
    base: list[dict[str, Any]] = [dict(it) for it in DEFAULT_WORKSPACE["ribbon"]]
    if not open_items:
        return base
    # Вставляем open-элементы перед последним sep (перед СИСТЕМА/settings)
    last_sep = -1
    for idx, it in enumerate(base):
        if it.get("t") == "sep":
            last_sep = idx
    insert_at = last_sep if last_sep != -1 else len(base)
    for offset, oi in enumerate(open_items):
        base.insert(insert_at + offset, oi)
    return base


VALID_THEMES = {"auto", "light", "dark"}


def _normalize_theme(value: Any) -> str:
    try:
        v = str(value).strip().lower()
    except Exception:
        return "auto"
    return v if v in VALID_THEMES else "auto"


def _migrate_settings(stored: dict[str, Any]) -> dict[str, Any]:
    """Мигрировать старые версии настроек к текущей схеме."""
    # Определяем версию файла
    raw_v = stored.get("version")
    try:
        v = int(raw_v) if raw_v is not None else 0
    except (TypeError, ValueError):
        v = 0

    if v < SETTINGS_VERSION:
        # v==0 → файл без поля version (v1). Миграция в v2.
        ws = stored.get("workspace")
        if not isinstance(ws, dict):
            stored["workspace"] = {
                "ribbon": [dict(it) for it in DEFAULT_WORKSPACE["ribbon"]],
                "window_width": DEFAULT_WORKSPACE["window_width"],
                "window_height": DEFAULT_WORKSPACE["window_height"],
                "sidebar_width": DEFAULT_WORKSPACE["sidebar_width"],
            }
        else:
            # sidebar_width (новое поле v2)
            if "sidebar_width" not in ws or ws["sidebar_width"] is None:
                ws["sidebar_width"] = DEFAULT_WORKSPACE["sidebar_width"]
            else:
                try:
                    sw = int(ws["sidebar_width"])
                except (TypeError, ValueError):
                    ws["sidebar_width"] = DEFAULT_WORKSPACE["sidebar_width"]
                else:
                    # кламп как в workspace._clamp_sidebar_width
                    ws["sidebar_width"] = max(232, min(400, sw))
            # window_* — гарантируем наличие ключей
            for k in ("window_width", "window_height"):
                if k not in ws:
                    ws[k] = DEFAULT_WORKSPACE[k]
            # ribbon миграция
            raw_ribbon = ws.get("ribbon")
            if isinstance(raw_ribbon, list):
                if _is_ribbon_outdated(raw_ribbon):
                    ws["ribbon"] = _migrate_ribbon(raw_ribbon)
            else:
                ws["ribbon"] = [dict(it) for it in DEFAULT_WORKSPACE["ribbon"]]
        stored["version"] = SETTINGS_VERSION
    # Гарантируем наличие version даже если файл уже был v2 но поле отсутствовало
    if "version" not in stored:
        stored["version"] = SETTINGS_VERSION
    # Тема: нормализация
    if "theme" in stored:
        stored["theme"] = _normalize_theme(stored.get("theme"))
    else:
        stored["theme"] = "auto"
    return stored


# ── Бэкап ─────────────────────────────────────────────────────

def _prune_backups() -> None:
    """Оставить последние 3 бэкапа с таймстампом."""
    try:
        # Матчим только timestamp-бэкапы: settings.json.bak.YYYY...
        pattern = str(SETTINGS_FILE) + ".bak.*"
        files = sorted(glob.glob(pattern))
        # Фильтруем только timestamp файлы (не .bak без суффикса — уже отдельный)
        # Но glob для .bak.* не включает plain .bak, так что все — timestamp
        if len(files) > 3:
            for old in files[:-3]:
                try:
                    Path(old).unlink()
                except OSError:
                    pass
    except OSError:
        pass


def _backup_settings() -> None:
    """Создать копию предыдущего settings.json.

    Делает две копии: settings.json.bak (последний) и
    settings.json.bak.YYYYmmdd-HHMMSS (история). Хранит последние 3 истории.
    """
    if not SETTINGS_FILE.exists():
        return
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        # Основной .bak (перезаписывается)
        bak = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".bak")
        shutil.copy2(SETTINGS_FILE, bak)
        # Таймстамп-копия
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        ts_bak = SETTINGS_FILE.with_name(f"{SETTINGS_FILE.name}.bak.{ts}")
        # На случай коллизии в одну секунду — добавляем микросекунды
        if ts_bak.exists():
            ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            ts_bak = SETTINGS_FILE.with_name(f"{SETTINGS_FILE.name}.bak.{ts}")
        shutil.copy2(SETTINGS_FILE, ts_bak)
        _prune_backups()
    except OSError:
        pass


def load_settings() -> dict[str, Any]:
    # Делаем глубокую копию дефолта (особенно workspace.ribbon)
    merged: dict[str, Any] = dict(DEFAULT_SETTINGS)
    merged["workspace"] = dict(DEFAULT_WORKSPACE)
    merged["workspace"]["ribbon"] = [dict(it) for it in DEFAULT_WORKSPACE["ribbon"]]
    if SETTINGS_FILE.exists():
        try:
            stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                stored = _migrate_settings(stored)
                merged.update(stored)
                # workspace после update уже содержит мигрированные данные,
                # но на случай частичной порчи — добиваем недостающие ключи
                ws = merged.get("workspace")
                if not isinstance(ws, dict):
                    merged["workspace"] = dict(DEFAULT_WORKSPACE)
                    merged["workspace"]["ribbon"] = [dict(it) for it in DEFAULT_WORKSPACE["ribbon"]]
                else:
                    if "sidebar_width" not in ws or ws["sidebar_width"] is None:
                        ws["sidebar_width"] = DEFAULT_WORKSPACE["sidebar_width"]
                    raw_ribbon = ws.get("ribbon")
                    if isinstance(raw_ribbon, list) and _is_ribbon_outdated(raw_ribbon):
                        ws["ribbon"] = _migrate_ribbon(raw_ribbon)
        except (json.JSONDecodeError, OSError):
            pass
    # Финальная гарантия версии
    if merged.get("version") != SETTINGS_VERSION:
        merged["version"] = SETTINGS_VERSION
    # Тема — гарантируем наличие и валидность
    merged["theme"] = _normalize_theme(merged.get("theme", "auto"))
    # Workspaces: гарантируем список vaults + синхронизируем алиас workspaces
    try:
        from .core.workspaces import ensure_vaults as _ensure_v

        _ensure_v(merged)
    except Exception:
        # fallback без импорта: гарантируем ключи и включим vault_root
        if not isinstance(merged.get("vaults"), list):
            # мигрируем из workspaces если есть
            alt = merged.get("workspaces")
            merged["vaults"] = list(alt) if isinstance(alt, list) else []
        if not isinstance(merged.get("workspaces"), list):
            merged["workspaces"] = list(merged.get("vaults") or [])
        vr = str(merged.get("vault_root") or "").strip()
        if vr:
            try:
                from pathlib import Path as _P

                norm = str(_P(vr).expanduser().resolve(strict=False))
            except Exception:
                norm = vr
            has = any(isinstance(e, dict) and str(e.get("path") or "").strip() == norm for e in merged["vaults"])
            if not has and norm:
                base = _P(norm).name or norm
                merged["vaults"].insert(0, {"path": norm, "name": base})
                merged["workspaces"] = list(merged["vaults"])
    return merged


def save_settings(settings: dict[str, Any]) -> None:
    # Гарантируем версию и поле sidebar_width перед записью
    if settings.get("version") != SETTINGS_VERSION:
        settings["version"] = SETTINGS_VERSION
    # Тема — нормализация перед записью
    if "theme" in settings:
        settings["theme"] = _normalize_theme(settings.get("theme"))
    else:
        settings["theme"] = "auto"
    ws = settings.get("workspace")
    if isinstance(ws, dict) and "sidebar_width" not in ws:
        ws["sidebar_width"] = DEFAULT_WORKSPACE["sidebar_width"]
    APP_DIR.mkdir(parents=True, exist_ok=True)
    _backup_settings()
    SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
