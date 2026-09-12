from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VAULT_STATE_DIR = ".fragilenotes"
VAULT_STATE_FILE = "ui_state.json"
LEGACY_DIR = "_System/Config"

DEFAULT_UI_STATE: dict[str, Any] = {
    "version": 1,
    "window": {"width": 1280, "height": 820, "maximized": False},
    "panels": {"left_width": 276, "right_width": 220, "left_pinned": True, "right_visible": False},
    "active_view": "files",
    "editor_mode": "split",
    "preset": "obsidian",
    "open_tabs": [],
    "current_tab": None,
    "file_state": {},
}

PRESETS: dict[str, dict[str, Any]] = {
    "obsidian": {"panels": {"left_width": 276, "right_width": 220, "left_pinned": True, "right_visible": False}, "editor_mode": "split"},
    "focus": {"panels": {"left_width": 232, "right_width": 0, "left_pinned": False, "right_visible": False}, "editor_mode": "preview"},
    "split": {"panels": {"left_width": 276, "right_width": 320, "left_pinned": True, "right_visible": True}, "editor_mode": "split"},
    "minimal": {"panels": {"left_width": 232, "right_width": 0, "left_pinned": False, "right_visible": False}, "editor_mode": "edit"},
}

def vault_state_path(vault_root: str | Path) -> Path:
    p = Path(str(vault_root)).expanduser()
    return p / VAULT_STATE_DIR / VAULT_STATE_FILE

def legacy_state_path(vault_root: str | Path) -> Path:
    return Path(str(vault_root)).expanduser() / LEGACY_DIR / VAULT_STATE_FILE

def load_vault_state(vault_root: str | Path) -> dict[str, Any]:
    for cand in (vault_state_path(vault_root), legacy_state_path(vault_root)):
        if cand.is_file():
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return _migrate(data)
            except Exception:
                continue
    return dict(DEFAULT_UI_STATE)

def save_vault_state(vault_root: str | Path, state: dict[str, Any]) -> None:
    path = vault_state_path(vault_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        state = _migrate(dict(state))
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def _migrate(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(DEFAULT_UI_STATE)
    out.update({k: v for k, v in data.items() if k in DEFAULT_UI_STATE})
    if isinstance(data.get("window"), dict):
        w = dict(out["window"])
        w.update({k: v for k, v in data["window"].items() if k in w})
        out["window"] = w
    if isinstance(data.get("panels"), dict):
        p = dict(out["panels"])
        p.update({k: v for k, v in data["panels"].items() if k in p})
        try:
            p["left_width"] = max(232, min(400, int(p["left_width"])))
        except Exception:
            p["left_width"] = 276
        try:
            p["right_width"] = max(0, min(400, int(p["right_width"])))
        except Exception:
            p["right_width"] = 220
        out["panels"] = p
    if out.get("preset") not in PRESETS:
        out["preset"] = "obsidian"
    if not isinstance(out.get("open_tabs"), list):
        out["open_tabs"] = []
    if not isinstance(out.get("file_state"), dict):
        out["file_state"] = {}
    out["version"] = 1
    return out

def apply_preset(state: dict[str, Any], name: str) -> dict[str, Any]:
    preset = PRESETS.get(name)
    if not preset:
        return state
    out = dict(state)
    out["preset"] = name
    p = dict(out.get("panels", {}))
    p.update(preset["panels"])
    out["panels"] = p
    out["editor_mode"] = preset["editor_mode"]
    return out

def update_window(state: dict[str, Any], width: int, height: int, maximized: bool) -> dict[str, Any]:
    out = dict(state)
    out["window"] = {"width": int(width), "height": int(height), "maximized": bool(maximized)}
    return out

def update_panels(state: dict[str, Any], left_width: int | None = None, right_width: int | None = None, left_pinned: bool | None = None, right_visible: bool | None = None) -> dict[str, Any]:
    out = dict(state)
    p = dict(out.get("panels", {}))
    if left_width is not None:
        p["left_width"] = max(232, min(400, int(left_width)))
    if right_width is not None:
        p["right_width"] = max(0, min(400, int(right_width)))
    if left_pinned is not None:
        p["left_pinned"] = bool(left_pinned)
    if right_visible is not None:
        p["right_visible"] = bool(right_visible)
    out["panels"] = p
    return out
