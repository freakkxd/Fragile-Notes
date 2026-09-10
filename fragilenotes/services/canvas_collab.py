"""Коллаборация для Canvas/Excalidraw в FragileNotes.

WebSocket sync (заглушка) + CRDT для фигур + shared cursors.

Архитектура:

* **CanvasCRDT** — LWW-реф  CRDT для списка фигур. Каждая фигура
  хранится как ``{shape, _id, _ts, _replica, _deleted}``; конфликт
  решается по ``(timestamp, replica_id)``. Операции ``add/update/delete``
  коммутативны и идемпотентны. Поддерживает tombstones и вектор часов.

* **CanvasFileSync** — sidecar ``<canvas>.crdt.json`` рядом с
  ``*.canvas.json`` (опционально). Локальный merge при загрузке/сохранении.

* **SharedCursors / CursorManager** — shared cursors участников.
  Хранит ``CursorState(replica, x, y, color, label, ts)`` с TTL
  и колбэками для отрисовки в ``canvas_view``.

* **CanvasSyncStub / CanvasWebSocketSync** — заглушка WebSocket.
  In-memory канал + лог операций, API совместим с будущим
  реальным ``websockets`` клиентом: ``connect()``, ``disconnect()``,
  ``send_operation()``, ``send_cursor()``, ``receive()``, ``sync(peer)``.

* **CanvasCollabService** — фасад: объединяет CRDT + SyncStub + Cursors,
  предоставляет простые хуки для ``CanvasView``: ``enable()``,
  ``broadcast_shapes()``, ``broadcast_cursor()``, ``on_remote_shapes``.

Только stdlib; ``py_compile`` без зависимостей. Реальный WebSocket
транспакт — опционально через ``websockets`` если доступен, иначе stub.

Пример::

    from fragilenotes.services.canvas_collab import CanvasCRDT, CanvasCollabService

    crdt = CanvasCRDT("note-foo", replica_id="alice")
    crdt.add_shape({"type":"rect","x":10,"y":10,"w":100,"h":60,"color":"#8ab4ff"})
    svc = CanvasCollabService(crdt, endpoint="ws://localhost:8765/canvas")
    svc.connect()
    svc.broadcast_shapes()

    # cursor
    svc.update_local_cursor(x=120, y=80, color="#8ab4ff", label="Alice")

    # merge с удалённым peer
    other = CanvasCRDT("note-foo", replica_id="bob")
    other.add_shape({"type":"ellipse","x":0,"y":0,"w":50,"h":50})
    crdt.merge(other)

Интеграция в ``fragilenotes/ui/canvas_view.py``::

    from fragilenotes.services.canvas_collab import (
        CanvasCRDT, CanvasCollabService, CursorManager, get_canvas_replica_id
    )
    # в CanvasView.__init__:
    self._collab = CanvasCollabService(CanvasCRDT(doc_id, replica_id=...))
    self._collab.connect()
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Утилиты ────────────────────────────────────────────────────────────────

def get_canvas_replica_id(settings: dict[str, Any] | None = None) -> str:
    """Стабильный replica_id для canvas-коллаба.

    Берётся из ``settings["canvas_replica_id"]`` или ``crdt_replica_id``,
    иначе генерируется ``uuid4().hex[:8]`` и кэшируется в settings.
    """
    if settings is not None:
        for key in ("canvas_replica_id", "crdt_replica_id", "replica_id"):
            rid = settings.get(key)
            if isinstance(rid, str) and rid.strip():
                return rid.strip()
    rid = uuid.uuid4().hex[:8]
    if settings is not None:
        try:
            settings["canvas_replica_id"] = rid
        except Exception:
            pass
    return rid


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_shape_id() -> str:
    return uuid.uuid4().hex[:10]


def _canvas_sidecar_path(canvas_path: Path) -> Path:
    """Sidecar для CRDT: ``foo.canvas.json`` -> ``foo.canvas.json.crdt.json``."""
    return Path(str(canvas_path) + ".crdt.json")


def _norm_color(c: str) -> str:
    c = str(c or "#8ab4ff").strip()
    if not c.startswith("#"):
        c = "#" + c
    return c


# ── Операции ───────────────────────────────────────────────────────────────

@dataclass
class CanvasOperation:
    """Одна операция над canvas.

    ``op``: add | update | delete | move | clear
    """

    op: str  # add/update/delete/move/clear/sync
    shape_id: str = ""
    shape: dict[str, Any] | None = None
    timestamp: int = 0
    replica_id: str = ""
    # для move: dx/dy
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "shape_id": self.shape_id,
            "shape": copy.deepcopy(self.shape) if self.shape is not None else None,
            "timestamp": int(self.timestamp),
            "replica_id": self.replica_id,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CanvasOperation:
        return cls(
            op=str(d.get("op", "sync")),
            shape_id=str(d.get("shape_id", "")),
            shape=copy.deepcopy(d.get("shape")) if isinstance(d.get("shape"), dict) else None,
            timestamp=int(d.get("timestamp", 0)),
            replica_id=str(d.get("replica_id", "")),
            meta=dict(d.get("meta") or {}),
        )


# ── CRDT для canvas фигур ─────────────────────────────────────────────────

@dataclass
class _ShapeEntry:
    """Внутреннее хранение фигуры + LWW-метаданные."""
    shape: dict[str, Any]
    shape_id: str
    timestamp: int
    replica_id: str
    deleted: bool = False
    # для отладки/вектора
    vector: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shape_id": self.shape_id,
            "shape": copy.deepcopy(self.shape),
            "timestamp": int(self.timestamp),
            "replica_id": self.replica_id,
            "deleted": bool(self.deleted),
            "vector": dict(self.vector),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> _ShapeEntry:
        return cls(
            shape=dict(d.get("shape") or {}),
            shape_id=str(d.get("shape_id", "")),
            timestamp=int(d.get("timestamp", 0)),
            replica_id=str(d.get("replica_id", "")),
            deleted=bool(d.get("deleted", False)),
            vector=dict(d.get("vector") or {}),
        )


class CanvasCRDT:
    """LWW CRDT для canvas-фигур.

    Каждая фигура — отдельный LWW-регистр с ключом ``shape_id``.
    Удаление — tombstone (``deleted=True``), побеждает больший
    ``(timestamp, replica_id)``. Merge — объединение словарей.

    Поддерживает операции ``add_shape``, ``update_shape``, ``delete_shape``,
    ``move_shape``, ``clear`` и сериализацию ``to_dict``/``from_dict``.
    """

    def __init__(self, doc_id: str, replica_id: str | None = None) -> None:
        self.doc_id = str(doc_id)
        self.replica_id = replica_id or get_canvas_replica_id()
        self._entries: dict[str, _ShapeEntry] = {}
        self._vector: dict[str, int] = {self.replica_id: 0}
        self._op_log: list[CanvasOperation] = []
        self._lock = threading.RLock()

    # ── helpers ──
    def _next_ts(self) -> int:
        ts = _now_ms()
        # монотонность в рамках реплики
        cur = self._vector.get(self.replica_id, 0)
        if ts <= cur:
            ts = cur + 1
        self._vector[self.replica_id] = ts
        return ts

    def _ensure_id(self, shape: dict[str, Any]) -> str:
        sid = str(shape.get("id") or shape.get("shape_id") or "").strip()
        if not sid:
            sid = _new_shape_id()
            shape["id"] = sid
        return sid

    # ── public: shapes ──
    def get_shapes(self, include_deleted: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            out: list[dict[str, Any]] = []
            for e in self._entries.values():
                if e.deleted and not include_deleted:
                    continue
                out.append(copy.deepcopy(e.shape))
            # стабильный порядок по (timestamp, shape_id)
            out.sort(key=lambda s: (str(s.get("id") or s.get("shape_id") or "")))
            return out

    def get_shape(self, shape_id: str) -> dict[str, Any] | None:
        with self._lock:
            e = self._entries.get(str(shape_id))
            if e is None or e.deleted:
                return None
            return copy.deepcopy(e.shape)

    def add_shape(self, shape: dict[str, Any], timestamp: int | None = None) -> str:
        """Добавить фигуру. Возвращает shape_id."""
        with self._lock:
            sid = self._ensure_id(shape)
            ts = int(timestamp) if timestamp is not None else self._next_ts()
            # копия с id
            sc = copy.deepcopy(shape)
            sc["id"] = sid
            entry = _ShapeEntry(shape=sc, shape_id=sid, timestamp=ts, replica_id=self.replica_id, deleted=False, vector=dict(self._vector))
            # LWW: если уже есть — побеждает более свежий
            existing = self._entries.get(sid)
            if existing is not None:
                if (ts, self.replica_id) < (existing.timestamp, existing.replica_id):
                    return sid
                # если уже удалён tombstone с большим ts — не воскрешаем старой записью
                if existing.deleted and (ts, self.replica_id) <= (existing.timestamp, existing.replica_id):
                    return sid
            self._entries[sid] = entry
            self._op_log.append(CanvasOperation(op="add", shape_id=sid, shape=copy.deepcopy(sc), timestamp=ts, replica_id=self.replica_id))
            if len(self._op_log) > 512:
                self._op_log = self._op_log[-512:]
            return sid

    def update_shape(self, shape_id: str, patch: dict[str, Any] | None = None, shape: dict[str, Any] | None = None, timestamp: int | None = None) -> bool:
        """Обновить фигуру. Принимает либо ``shape`` целиком, либо ``patch``."""
        with self._lock:
            sid = str(shape_id)
            e = self._entries.get(sid)
            if e is None or e.deleted:
                # если нет — трактуем как add если передан shape
                if shape is not None:
                    return bool(self.add_shape(shape, timestamp=timestamp))
                return False
            ts = int(timestamp) if timestamp is not None else self._next_ts()
            if (ts, self.replica_id) < (e.timestamp, e.replica_id):
                return False
            if shape is not None:
                sc = copy.deepcopy(shape)
                sc["id"] = sid
                e.shape = sc
            elif patch is not None:
                # merge patch
                for k, v in patch.items():
                    if k == "id":
                        continue
                    e.shape[k] = copy.deepcopy(v)
            else:
                return False
            e.timestamp = ts
            e.replica_id = self.replica_id
            e.vector = dict(self._vector)
            self._op_log.append(CanvasOperation(op="update", shape_id=sid, shape=copy.deepcopy(e.shape), timestamp=ts, replica_id=self.replica_id, meta=dict(patch or {})))
            if len(self._op_log) > 512:
                self._op_log = self._op_log[-512:]
            return True

    def delete_shape(self, shape_id: str, timestamp: int | None = None) -> bool:
        with self._lock:
            sid = str(shape_id)
            e = self._entries.get(sid)
            ts = int(timestamp) if timestamp is not None else self._next_ts()
            if e is None:
                # создаём tombstone
                self._entries[sid] = _ShapeEntry(shape={"id": sid, "type": "deleted"}, shape_id=sid, timestamp=ts, replica_id=self.replica_id, deleted=True, vector=dict(self._vector))
                self._op_log.append(CanvasOperation(op="delete", shape_id=sid, timestamp=ts, replica_id=self.replica_id))
                return True
            if (ts, self.replica_id) < (e.timestamp, e.replica_id):
                return False
            if e.deleted:
                # уже удалён — обновить ts если новее
                if ts > e.timestamp:
                    e.timestamp = ts
                    e.replica_id = self.replica_id
                return False
            e.deleted = True
            e.timestamp = ts
            e.replica_id = self.replica_id
            e.vector = dict(self._vector)
            self._op_log.append(CanvasOperation(op="delete", shape_id=sid, timestamp=ts, replica_id=self.replica_id))
            if len(self._op_log) > 512:
                self._op_log = self._op_log[-512:]
            return True

    def move_shape(self, shape_id: str, dx: float, dy: float, timestamp: int | None = None) -> bool:
        with self._lock:
            sid = str(shape_id)
            e = self._entries.get(sid)
            if e is None or e.deleted:
                return False
            ts = int(timestamp) if timestamp is not None else self._next_ts()
            if (ts, self.replica_id) < (e.timestamp, e.replica_id):
                return False
            sh = e.shape
            t = sh.get("type")
            if t in ("rect", "ellipse", "text"):
                sh["x"] = float(sh.get("x", 0)) + float(dx)
                sh["y"] = float(sh.get("y", 0)) + float(dy)
            elif t == "line":
                sh["x1"] = float(sh.get("x1", sh.get("x", 0))) + float(dx)
                sh["y1"] = float(sh.get("y1", sh.get("y", 0))) + float(dy)
                sh["x2"] = float(sh.get("x2", sh.get("x", 0))) + float(dx)
                sh["y2"] = float(sh.get("y2", sh.get("y", 0))) + float(dy)
            elif t == "pen":
                pts = sh.get("points") or []
                sh["points"] = [[float(p[0]) + float(dx), float(p[1]) + float(dy)] for p in pts]
            else:
                # generic: двигаем x/y если есть
                if "x" in sh:
                    sh["x"] = float(sh.get("x", 0)) + float(dx)
                if "y" in sh:
                    sh["y"] = float(sh.get("y", 0)) + float(dy)
            e.timestamp = ts
            e.replica_id = self.replica_id
            e.vector = dict(self._vector)
            self._op_log.append(CanvasOperation(op="move", shape_id=sid, shape=copy.deepcopy(sh), timestamp=ts, replica_id=self.replica_id, meta={"dx": float(dx), "dy": float(dy)}))
            return True

    def clear(self, timestamp: int | None = None) -> None:
        with self._lock:
            ts = int(timestamp) if timestamp is not None else self._next_ts()
            for e in self._entries.values():
                if not e.deleted:
                    e.deleted = True
                    e.timestamp = ts
                    e.replica_id = self.replica_id
            self._op_log.append(CanvasOperation(op="clear", timestamp=ts, replica_id=self.replica_id))

    def set_shapes(self, shapes: list[dict[str, Any]]) -> None:
        """Полная перезапись из списка фигур (LWW, через add/update/delete)."""
        with self._lock:
            # собрать id из нового списка
            new_ids = set()
            for s in shapes:
                if not isinstance(s, dict):
                    continue
                sid = str(s.get("id") or s.get("shape_id") or "").strip()
                if not sid:
                    sid = _new_shape_id()
                    s = dict(s)
                    s["id"] = sid
                else:
                    s = dict(s)
                new_ids.add(sid)
                # add или update
                existing = self._entries.get(sid)
                if existing is None or existing.deleted:
                    self.add_shape(s)
                else:
                    # обновить если новее — форсируем ts
                    self.update_shape(sid, shape=s)
            # удалить отсутствующие
            for sid in list(self._entries.keys()):
                if sid not in new_ids and not self._entries[sid].deleted:
                    self.delete_shape(sid)

    # ── merge ──
    def merge(self, other: CanvasCRDT) -> bool:
        """State-based merge с другим CRDT. Возвращает True если локально изменилось."""
        if not isinstance(other, CanvasCRDT):
            return False
        changed = False
        with self._lock:
            # merge vector
            for rid, ts in other._vector.items():
                if int(ts) > int(self._vector.get(rid, 0)):
                    self._vector[rid] = int(ts)
                    changed = True
            # merge shapes LWW
            for sid, oentry in other._entries.items():
                sentry = self._entries.get(sid)
                if sentry is None:
                    self._entries[sid] = _ShapeEntry(
                        shape=copy.deepcopy(oentry.shape),
                        shape_id=oentry.shape_id,
                        timestamp=oentry.timestamp,
                        replica_id=oentry.replica_id,
                        deleted=oentry.deleted,
                        vector=dict(oentry.vector),
                    )
                    changed = True
                else:
                    if (oentry.timestamp, oentry.replica_id) > (sentry.timestamp, sentry.replica_id):
                        sentry.shape = copy.deepcopy(oentry.shape)
                        sentry.timestamp = oentry.timestamp
                        sentry.replica_id = oentry.replica_id
                        sentry.deleted = oentry.deleted
                        sentry.vector = dict(oentry.vector)
                        changed = True
            # counter монотонность
            my_ts = self._vector.get(self.replica_id, 0)
            if my_ts < _now_ms():
                # не трогаем, вектор уже обновлён выше
                pass
        return changed

    def apply_operation(self, op: CanvasOperation) -> bool:
        """Применить одну операцию (idempotent)."""
        if not isinstance(op, CanvasOperation):
            return False
        with self._lock:
            # обновить вектор часов для реплики операции
            if op.replica_id:
                self._vector[op.replica_id] = max(self._vector.get(op.replica_id, 0), int(op.timestamp))
            if op.op == "add" and op.shape is not None:
                # LWW add
                sid = str(op.shape_id or op.shape.get("id") or "")
                if not sid:
                    sid = self._ensure_id(op.shape)
                existing = self._entries.get(sid)
                if existing is None or (op.timestamp, op.replica_id) > (existing.timestamp, existing.replica_id):
                    self._entries[sid] = _ShapeEntry(shape=copy.deepcopy(op.shape), shape_id=sid, timestamp=op.timestamp, replica_id=op.replica_id, deleted=False, vector={op.replica_id: op.timestamp})
                    return True
            elif op.op == "update" and op.shape is not None:
                sid = str(op.shape_id)
                e = self._entries.get(sid)
                if e is None or (op.timestamp, op.replica_id) >= (e.timestamp, e.replica_id):
                    self._entries[sid] = _ShapeEntry(shape=copy.deepcopy(op.shape), shape_id=sid, timestamp=op.timestamp, replica_id=op.replica_id, deleted=False, vector={op.replica_id: op.timestamp})
                    return True
            elif op.op == "delete":
                sid = str(op.shape_id)
                e = self._entries.get(sid)
                if e is None:
                    self._entries[sid] = _ShapeEntry(shape={"id": sid}, shape_id=sid, timestamp=op.timestamp, replica_id=op.replica_id, deleted=True, vector={op.replica_id: op.timestamp})
                    return True
                if (op.timestamp, op.replica_id) > (e.timestamp, e.replica_id):
                    e.deleted = True
                    e.timestamp = op.timestamp
                    e.replica_id = op.replica_id
                    return True
            elif op.op == "move" and op.shape is not None:
                sid = str(op.shape_id)
                e = self._entries.get(sid)
                if e is None or (op.timestamp, op.replica_id) >= (e.timestamp, e.replica_id):
                    self._entries[sid] = _ShapeEntry(shape=copy.deepcopy(op.shape), shape_id=sid, timestamp=op.timestamp, replica_id=op.replica_id, deleted=False, vector={op.replica_id: op.timestamp})
                    return True
            elif op.op == "clear":
                # clear — tombstone все
                for e in self._entries.values():
                    if not e.deleted and (op.timestamp, op.replica_id) > (e.timestamp, e.replica_id):
                        e.deleted = True
                        e.timestamp = op.timestamp
                        e.replica_id = op.replica_id
                return True
        return False

    # ── сериализация ──
    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "type": "canvas-crdt",
                "doc_id": self.doc_id,
                "replica_id": self.replica_id,
                "vector": dict(self._vector),
                "entries": {sid: e.to_dict() for sid, e in self._entries.items()},
                "version": 1,
            }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CanvasCRDT:
        doc_id = str(d.get("doc_id", ""))
        replica = str(d.get("replica_id", "")) or None
        obj = cls(doc_id, replica_id=replica)
        vec = d.get("vector")
        if isinstance(vec, dict):
            obj._vector = {str(k): int(v) for k, v in vec.items()}
        entries = d.get("entries")
        if isinstance(entries, dict):
            for sid, ed in entries.items():
                if isinstance(ed, dict):
                    try:
                        obj._entries[str(sid)] = _ShapeEntry.from_dict(ed)
                    except Exception:
                        continue
        # legacy: shapes list without CRDT metadata
        if not obj._entries and isinstance(d.get("shapes"), list):
            for s in d["shapes"]:
                if isinstance(s, dict):
                    obj.add_shape(s, timestamp=0)
        return obj

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, s: str) -> CanvasCRDT:
        return cls.from_dict(json.loads(s))

    def get_vector(self) -> dict[str, int]:
        with self._lock:
            return dict(self._vector)

    def op_log(self) -> list[CanvasOperation]:
        with self._lock:
            return list(self._op_log)


# алиасы для удобства
CanvasDocument = CanvasCRDT
CanvasLWW = CanvasCRDT


# ── Shared Cursors ─────────────────────────────────────────────────────────

@dataclass
class CursorState:
    """Состояние курсора одного участника."""
    replica_id: str
    x: float
    y: float
    color: str = "#8ab4ff"
    label: str = ""
    timestamp: int = 0
    # дополнительные поля: selected ids, tool
    selected: list[str] = field(default_factory=list)
    tool: str = "select"
    visible: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "replica_id": self.replica_id,
            "x": float(self.x),
            "y": float(self.y),
            "color": _norm_color(self.color),
            "label": str(self.label),
            "timestamp": int(self.timestamp),
            "selected": list(self.selected),
            "tool": str(self.tool),
            "visible": bool(self.visible),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CursorState:
        return cls(
            replica_id=str(d.get("replica_id", "")),
            x=float(d.get("x", 0)),
            y=float(d.get("y", 0)),
            color=_norm_color(str(d.get("color", "#8ab4ff"))),
            label=str(d.get("label", "")),
            timestamp=int(d.get("timestamp", 0)),
            selected=list(d.get("selected") or []),
            tool=str(d.get("tool", "select")),
            visible=bool(d.get("visible", True)),
        )


class CursorManager:
    """Управление shared cursors: TTL, локальный курсор, broadcast stub."""

    CURSOR_TTL_MS: int = 30000  # курсор скрывается через 30с без обновления
    THROTTLE_MS: int = 50  # throttle локального курсора

    def __init__(self, replica_id: str | None = None) -> None:
        self.replica_id = replica_id or get_canvas_replica_id()
        self._cursors: dict[str, CursorState] = {}
        self._lock = threading.RLock()
        self._last_emit: int = 0
        self._on_changed: Callable[[dict[str, CursorState]], None] | None = None

    def set_callback(self, cb: Callable[[dict[str, CursorState]], None] | None) -> None:
        self._on_changed = cb

    def update_local(self, x: float, y: float, color: str | None = None, label: str | None = None, tool: str | None = None, selected: list[str] | None = None) -> CursorState | None:
        """Обновить локальный курсор (throttled). Возвращает state если отправлено."""
        now = _now_ms()
        with self._lock:
            if now - self._last_emit < self.THROTTLE_MS:
                # throttle — всё равно обновляем внутри, но не триггерим колбэк
                cur = self._cursors.get(self.replica_id)
                if cur is not None:
                    cur.x = float(x); cur.y = float(y); cur.timestamp = now
                    if color is not None:
                        cur.color = _norm_color(color)
                    if label is not None:
                        cur.label = str(label)
                    if tool is not None:
                        cur.tool = str(tool)
                    if selected is not None:
                        cur.selected = list(selected)
                else:
                    self._cursors[self.replica_id] = CursorState(replica_id=self.replica_id, x=float(x), y=float(y), color=_norm_color(color or "#8ab4ff"), label=str(label or self.replica_id), timestamp=now, tool=str(tool or "select"), selected=list(selected or []))
                return None
            self._last_emit = now
            state = CursorState(
                replica_id=self.replica_id,
                x=float(x), y=float(y),
                color=_norm_color(color or self._cursors.get(self.replica_id, CursorState(self.replica_id, 0, 0)).color),
                label=str(label if label is not None else (self._cursors.get(self.replica_id).label if self._cursors.get(self.replica_id) else self.replica_id)),
                timestamp=now,
                tool=str(tool or "select"),
                selected=list(selected or []),
                visible=True,
            )
            self._cursors[self.replica_id] = state
            cb = self._on_changed
        if cb is not None:
            try:
                cb(self.get_all())
            except Exception:
                pass
        return state

    def update_remote(self, state: CursorState | dict[str, Any]) -> None:
        """Применить удалённый курсор."""
        if isinstance(state, dict):
            try:
                state = CursorState.from_dict(state)
            except Exception:
                return
        if not isinstance(state, CursorState) or not state.replica_id or state.replica_id == self.replica_id:
            return
        with self._lock:
            # LWW по timestamp
            existing = self._cursors.get(state.replica_id)
            if existing is not None and int(state.timestamp) <= int(existing.timestamp):
                return
            self._cursors[state.replica_id] = state
            cb = self._on_changed
        if cb is not None:
            try:
                cb(self.get_all())
            except Exception:
                pass

    def remove(self, replica_id: str) -> None:
        with self._lock:
            self._cursors.pop(str(replica_id), None)

    def get_all(self) -> dict[str, CursorState]:
        with self._lock:
            self.prune_locked()
            return {k: copy.deepcopy(v) for k, v in self._cursors.items()}

    def get_visible(self) -> list[CursorState]:
        with self._lock:
            self.prune_locked()
            now = _now_ms()
            out: list[CursorState] = []
            for c in self._cursors.values():
                if c.replica_id == self.replica_id:
                    continue
                if now - c.timestamp > self.CURSOR_TTL_MS:
                    continue
                if not c.visible:
                    continue
                out.append(copy.deepcopy(c))
            return out

    def prune_locked(self) -> None:
        now = _now_ms()
        dead = [rid for rid, c in self._cursors.items() if now - c.timestamp > self.CURSOR_TTL_MS * 2 and rid != self.replica_id]
        for rid in dead:
            self._cursors.pop(rid, None)

    def prune(self) -> None:
        with self._lock:
            self.prune_locked()

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {"replica_id": self.replica_id, "cursors": {k: v.to_dict() for k, v in self._cursors.items()}}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CursorManager:
        rid = str(d.get("replica_id", "")) or get_canvas_replica_id()
        mgr = cls(replica_id=rid)
        curs = d.get("cursors")
        if isinstance(curs, dict):
            for k, v in curs.items():
                if isinstance(v, dict):
                    try:
                        mgr._cursors[str(k)] = CursorState.from_dict(v)
                    except Exception:
                        continue
        return mgr


# alias
SharedCursors = CursorManager
SharedCursorManager = CursorManager


# ── File Sync для canvas ──────────────────────────────────────────────────

class CanvasFileSync:
    """Синхронизация CRDT через sidecar-файл.

    Sidecar — ``<canvas>.canvas.json.crdt.json`` с сериализованным состоянием
    ``CanvasCRDT``. Поддерживает ``save()``, ``load()``, ``sync()``.
    """

    def __init__(self, doc: CanvasCRDT, canvas_path: Path | str) -> None:
        self.doc = doc
        self.canvas_path = Path(canvas_path)
        self.sidecar = _canvas_sidecar_path(self.canvas_path)

    def save(self) -> Path:
        data = self.doc.to_dict()
        data["_saved_at"] = _now_ms()
        tmp = self.sidecar.with_name(self.sidecar.name + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.sidecar)
        return self.sidecar

    def load(self) -> dict[str, Any] | None:
        if not self.sidecar.is_file():
            return None
        try:
            return json.loads(self.sidecar.read_text(encoding="utf-8"))
        except Exception:
            return None

    def load_doc(self) -> CanvasCRDT | None:
        d = self.load()
        if not isinstance(d, dict):
            return None
        try:
            return CanvasCRDT.from_dict(d)
        except Exception:
            return None

    def sync(self) -> bool:
        """Загрузить sidecar, merge с локальным doc, сохранить если изменилось."""
        other = self.load_doc()
        if other is None:
            try:
                self.save()
            except Exception:
                pass
            return False
        before = len(self.doc.get_shapes())
        changed = self.doc.merge(other)
        if changed:
            try:
                self.save()
            except Exception:
                pass
            return True
        # если sidecar устарел — перезаписать текущим
        try:
            cur = self.doc.to_dict()
            if cur.get("vector") != other.to_dict().get("vector"):
                self.save()
        except Exception:
            pass
        return False


# ── WebSocket Sync Stub ────────────────────────────────────────────────────

class CanvasSyncStub:
    """Заглушка WebSocket-синхронизации для canvas.

    In-memory канал, лог операций. API совместим с будущим реальным
    WebSocket-клиентом: ``connect()``, ``disconnect()``, ``send_operation()``,
    ``send_cursor()``, ``receive()``, ``sync(other)``.

    Для локального теста можно связать две заглушки через ``sync`` или
    ``add_peer``. В проде заменить на ``websockets.connect(...)``.
    """

    def __init__(self, doc: CanvasCRDT, endpoint: str | None = None, cursor_manager: CursorManager | None = None) -> None:
        self.doc = doc
        self.endpoint = endpoint or "stub://local/canvas"
        self.cursor_manager = cursor_manager
        self._connected: bool = False
        self._peers: list[CanvasCRDT] = []
        self._peer_stubs: list[CanvasSyncStub] = []
        self._log: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self._on_shapes: Callable[[list[dict[str, Any]]], None] | None = None
        self._on_cursor: Callable[[CursorState], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ── lifecycle ──
    def connect(self) -> bool:
        with self._lock:
            if self._connected:
                return True
            self._connected = True
            self._log.append({"op": "connect", "at": _now_ms(), "endpoint": self.endpoint, "doc_id": self.doc.doc_id})
        return True

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            self._log.append({"op": "disconnect", "at": _now_ms(), "endpoint": self.endpoint})

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def set_on_shapes(self, cb: Callable[[list[dict[str, Any]]], None] | None) -> None:
        self._on_shapes = cb

    def set_on_cursor(self, cb: Callable[[CursorState], None] | None) -> None:
        self._on_cursor = cb

    # ── send ──
    def send_operation(self, op: CanvasOperation) -> dict[str, Any]:
        """Сериализовать и 'отправить' операцию. Возвращает payload."""
        payload = {
            "type": "canvas-op",
            "doc_id": self.doc.doc_id,
            "op": op.to_dict(),
            "vector": self.doc.get_vector(),
            "_sent_at": _now_ms(),
            "_endpoint": self.endpoint,
        }
        with self._lock:
            self._log.append({"op": "send_operation", "at": _now_ms(), "payload_op": op.op, "shape_id": op.shape_id})
            # broadcast peers in-memory
            for stub in list(self._peer_stubs):
                try:
                    stub.receive(payload)
                except Exception:
                    pass
        return payload

    def send_shapes(self, shapes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Отправить полный снепшот фигур (sync)."""
        snap = shapes if shapes is not None else self.doc.get_shapes()
        payload = {
            "type": "canvas-sync",
            "doc_id": self.doc.doc_id,
            "shapes": copy.deepcopy(snap),
            "crdt": self.doc.to_dict(),
            "vector": self.doc.get_vector(),
            "_sent_at": _now_ms(),
            "_endpoint": self.endpoint,
        }
        with self._lock:
            self._log.append({"op": "send_shapes", "at": _now_ms(), "count": len(snap)})
            for stub in list(self._peer_stubs):
                try:
                    stub.receive(payload)
                except Exception:
                    pass
        return payload

    def send_cursor(self, cursor: CursorState | dict[str, Any]) -> dict[str, Any]:
        """Отправить курсор."""
        if isinstance(cursor, CursorState):
            cur = cursor.to_dict()
        elif isinstance(cursor, dict):
            cur = dict(cursor)
        else:
            cur = {}
        payload = {
            "type": "canvas-cursor",
            "doc_id": self.doc.doc_id,
            "cursor": cur,
            "_sent_at": _now_ms(),
            "_endpoint": self.endpoint,
        }
        with self._lock:
            self._log.append({"op": "send_cursor", "at": _now_ms(), "replica": cur.get("replica_id")})
            for stub in list(self._peer_stubs):
                try:
                    stub.receive(payload)
                except Exception:
                    pass
        # локально тоже обновить менеджер если есть
        if self.cursor_manager is not None and isinstance(cursor, CursorState):
            # не триггерим echo — remote update уже через receive
            pass
        return payload

    # ── receive ──
    def receive(self, payload: dict[str, Any] | None) -> bool:
        """Принять payload и применить. Возвращает True если состояние изменилось."""
        if not isinstance(payload, dict):
            with self._lock:
                self._log.append({"op": "receive", "at": _now_ms(), "noop": True})
            return False
        ptype = str(payload.get("type", "")).lower()
        changed = False
        if ptype in ("canvas-op", "canvas_op"):
            opd = payload.get("op")
            if isinstance(opd, dict):
                try:
                    op = CanvasOperation.from_dict(opd)
                    changed = self.doc.apply_operation(op)
                except Exception:
                    pass
        elif ptype in ("canvas-sync", "canvas_sync", "sync"):
            # полный CRDT state
            crdt_data = payload.get("crdt")
            if isinstance(crdt_data, dict):
                try:
                    other = CanvasCRDT.from_dict(crdt_data)
                    changed = self.doc.merge(other)
                except Exception:
                    pass
            # fallback: shapes list
            if not changed and isinstance(payload.get("shapes"), list):
                try:
                    other = CanvasCRDT(self.doc.doc_id, replica_id=payload.get("_endpoint") or "remote")
                    for s in payload["shapes"]:
                        if isinstance(s, dict):
                            other.add_shape(s, timestamp=0)
                    changed = self.doc.merge(other)
                except Exception:
                    pass
        elif ptype in ("canvas-cursor", "cursor", "canvas_cursor"):
            cur = payload.get("cursor")
            if isinstance(cur, dict):
                try:
                    state = CursorState.from_dict(cur)
                    if self.cursor_manager is not None:
                        self.cursor_manager.update_remote(state)
                    if self._on_cursor is not None:
                        try:
                            self._on_cursor(state)
                        except Exception:
                            pass
                    self._log.append({"op": "cursor", "at": _now_ms(), "replica": state.replica_id})
                    return False
                except Exception:
                    pass
            return False
        else:
            # unknown — try generic crdt
            if isinstance(payload.get("entries"), dict):
                try:
                    other = CanvasCRDT.from_dict(payload)
                    changed = self.doc.merge(other)
                except Exception:
                    pass
        with self._lock:
            self._log.append({"op": "receive", "at": _now_ms(), "type": ptype, "changed": bool(changed)})
        if changed and self._on_shapes is not None:
            try:
                self._on_shapes(self.doc.get_shapes())
            except Exception:
                pass
        return bool(changed)

    # alias compat
    pull = receive
    push = send_shapes
    on_message = receive

    def sync(self, other_doc: CanvasCRDT | None = None, other_stub: CanvasSyncStub | None = None) -> bool:
        """Двусторонний sync с другим документом/стабом."""
        if other_stub is not None:
            # sync stubs взаимно
            a_changed = self.doc.merge(other_stub.doc)
            b_changed = other_stub.doc.merge(self.doc)
            # обменяться курсорами если есть
            if self.cursor_manager is not None and other_stub.cursor_manager is not None:
                for c in self.cursor_manager.get_all().values():
                    other_stub.cursor_manager.update_remote(c)
                for c in other_stub.cursor_manager.get_all().values():
                    self.cursor_manager.update_remote(c)
            with self._lock:
                self._log.append({"op": "sync_stub", "at": _now_ms(), "a_changed": bool(a_changed), "b_changed": bool(b_changed)})
            return bool(a_changed or b_changed)
        if other_doc is not None:
            a_changed = self.doc.merge(other_doc)
            b_changed = other_doc.merge(self.doc)
            with self._lock:
                self._log.append({"op": "sync_doc", "at": _now_ms(), "a_changed": bool(a_changed), "b_changed": bool(b_changed)})
            if a_changed and self._on_shapes is not None:
                try:
                    self._on_shapes(self.doc.get_shapes())
                except Exception:
                    pass
            return bool(a_changed or b_changed)
        # sync со всеми peers
        any_changed = False
        for peer in list(self._peers):
            c1 = self.doc.merge(peer)
            c2 = peer.merge(self.doc)
            any_changed = any_changed or bool(c1 or c2)
        if any_changed and self._on_shapes is not None:
            try:
                self._on_shapes(self.doc.get_shapes())
            except Exception:
                pass
        return any_changed

    def add_peer(self, peer: CanvasCRDT) -> None:
        with self._lock:
            if peer is not self.doc and peer not in self._peers:
                self._peers.append(peer)

    def connect_peer_stub(self, peer_stub: CanvasSyncStub) -> None:
        """Связать два стаба для in-memory broadcast."""
        if peer_stub is self:
            return
        with self._lock:
            if peer_stub not in self._peer_stubs:
                self._peer_stubs.append(peer_stub)
        with peer_stub._lock:
            if self not in peer_stub._peer_stubs:
                peer_stub._peer_stubs.append(self)

    def get_log(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._log)

    def get_endpoint(self) -> str:
        return self.endpoint


# алиасы
CanvasWebSocketSync = CanvasSyncStub
CanvasNetworkSync = CanvasSyncStub
WebSocketSyncStub = CanvasSyncStub


# ── Фасад ──────────────────────────────────────────────────────────────────

class CanvasCollabService:
    """Фасад коллаборации: CRDT + WebSocket stub + cursors.

    Единая точка входа для ``CanvasView``.

    Пример::

        svc = CanvasCollabService(doc_id="my-canvas", settings=settings)
        svc.connect()
        svc.broadcast_shapes(view_shapes)
        svc.update_cursor(x=100, y=200)
        svc.set_on_remote_shapes(lambda shapes: view.set_shapes(shapes))
    """

    def __init__(
        self,
        doc: CanvasCRDT | str | None = None,
        settings: dict[str, Any] | None = None,
        endpoint: str | None = None,
        replica_id: str | None = None,
        cursor_manager: CursorManager | None = None,
    ) -> None:
        self.settings = dict(settings) if settings is not None else {}
        self.replica_id = replica_id or get_canvas_replica_id(self.settings)
        # doc
        if isinstance(doc, CanvasCRDT):
            self.doc = doc
        elif isinstance(doc, str):
            self.doc = CanvasCRDT(doc, replica_id=self.replica_id)
        elif doc is None:
            doc_id = str(self.settings.get("canvas_doc_id") or "canvas-default")
            self.doc = CanvasCRDT(doc_id, replica_id=self.replica_id)
        else:
            raise TypeError("doc must be CanvasCRDT or str doc_id")
        # endpoint
        self.endpoint = endpoint or str(self.settings.get("canvas_collab_endpoint") or self.settings.get("collab_endpoint") or "ws://localhost:8765/canvas")
        # cursors
        self.cursors = cursor_manager if cursor_manager is not None else CursorManager(replica_id=self.replica_id)
        # sync stub
        self.sync = CanvasSyncStub(self.doc, endpoint=self.endpoint, cursor_manager=self.cursors)
        self._enabled: bool = bool(self.settings.get("canvas_collab_enabled", False))
        self._on_remote_shapes: Callable[[list[dict[str, Any]]], None] | None = None
        self._on_remote_cursor: Callable[[CursorState], None] | None = None
        # пробросить колбэки
        self.sync.set_on_shapes(self._handle_remote_shapes)
        self.sync.set_on_cursor(self._handle_remote_cursor)
        self.cursors.set_callback(self._handle_cursor_changed)

    def _handle_remote_shapes(self, shapes: list[dict[str, Any]]) -> None:
        cb = self._on_remote_shapes
        if cb is not None:
            try:
                cb(shapes)
            except Exception:
                pass

    def _handle_remote_cursor(self, state: CursorState) -> None:
        cb = self._on_remote_cursor
        if cb is not None:
            try:
                cb(state)
            except Exception:
                pass

    def _handle_cursor_changed(self, all_cursors: dict[str, CursorState]) -> None:
        # для будущего: можно автоматически broadcast local cursor
        pass

    # ── public API ──
    def is_enabled(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        self._enabled = True
        try:
            self.settings["canvas_collab_enabled"] = True
        except Exception:
            pass
        self.connect()

    def disable(self) -> None:
        self._enabled = False
        try:
            self.settings["canvas_collab_enabled"] = False
        except Exception:
            pass
        self.disconnect()

    def connect(self) -> bool:
        return self.sync.connect()

    def disconnect(self) -> None:
        self.sync.disconnect()

    @property
    def is_connected(self) -> bool:
        return self.sync.is_connected

    def set_on_remote_shapes(self, cb: Callable[[list[dict[str, Any]]], None] | None) -> None:
        self._on_remote_shapes = cb

    def set_on_remote_cursor(self, cb: Callable[[CursorState], None] | None) -> None:
        self._on_remote_cursor = cb

    def broadcast_shapes(self, shapes: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        if not self._enabled and not self.sync.is_connected:
            # даже без enabled — обновить CRDT локально
            if shapes is not None:
                self.doc.set_shapes(shapes)
            return None
        if shapes is not None:
            self.doc.set_shapes(shapes)
        return self.sync.send_shapes(self.doc.get_shapes())

    def broadcast_operation(self, op: CanvasOperation) -> dict[str, Any] | None:
        if not self.sync.is_connected:
            # локально применить всё равно
            self.doc.apply_operation(op)
            return None
        self.doc.apply_operation(op)
        return self.sync.send_operation(op)

    def push_shape(self, shape: dict[str, Any]) -> dict[str, Any] | None:
        op = CanvasOperation(op="add", shape_id=str(shape.get("id") or ""), shape=copy.deepcopy(shape), timestamp=_now_ms(), replica_id=self.replica_id)
        return self.broadcast_operation(op)

    def push_update(self, shape_id: str, shape: dict[str, Any]) -> dict[str, Any] | None:
        op = CanvasOperation(op="update", shape_id=str(shape_id), shape=copy.deepcopy(shape), timestamp=_now_ms(), replica_id=self.replica_id)
        return self.broadcast_operation(op)

    def push_delete(self, shape_id: str) -> dict[str, Any] | None:
        op = CanvasOperation(op="delete", shape_id=str(shape_id), timestamp=_now_ms(), replica_id=self.replica_id)
        return self.broadcast_operation(op)

    def update_cursor(self, x: float, y: float, color: str | None = None, label: str | None = None, tool: str | None = None, selected: list[str] | None = None) -> None:
        state = self.cursors.update_local(x=float(x), y=float(y), color=color, label=label, tool=tool, selected=selected)
        if state is not None and self.sync.is_connected:
            try:
                self.sync.send_cursor(state)
            except Exception:
                pass

    def update_local_cursor(self, *a, **kw) -> None:
        self.update_cursor(*a, **kw)

    def get_remote_cursors(self) -> list[CursorState]:
        return self.cursors.get_visible()

    def get_all_cursors(self) -> dict[str, CursorState]:
        return self.cursors.get_all()

    def receive(self, payload: dict[str, Any]) -> bool:
        return self.sync.receive(payload)

    def sync_with(self, other: CanvasCRDT | CanvasSyncStub | CanvasCollabService) -> bool:
        if isinstance(other, CanvasCollabService):
            return self.sync.sync(other_stub=other.sync)
        if isinstance(other, CanvasSyncStub):
            return self.sync.sync(other_stub=other)
        if isinstance(other, CanvasCRDT):
            return self.sync.sync(other_doc=other)
        return False

    def get_shapes(self) -> list[dict[str, Any]]:
        return self.doc.get_shapes()

    def set_shapes(self, shapes: list[dict[str, Any]]) -> None:
        self.doc.set_shapes(shapes)

    def merge_payload(self, payload: dict[str, Any]) -> bool:
        return self.sync.receive(payload)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "connected": self.sync.is_connected,
            "endpoint": self.endpoint,
            "replica_id": self.replica_id,
            "doc_id": self.doc.doc_id,
            "shapes": len(self.doc.get_shapes()),
            "peers": len(self.sync.get_log()),
            "cursors": len(self.cursors.get_visible()),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc.doc_id,
            "replica_id": self.replica_id,
            "endpoint": self.endpoint,
            "enabled": self._enabled,
            "crdt": self.doc.to_dict(),
            "cursors": self.cursors.to_dict(),
        }


# алиасы фасада
CanvasCollab = CanvasCollabService
CanvasCollaborationService = CanvasCollabService


# ── Helpers для canvas_view ────────────────────────────────────────────────

def create_canvas_crdt_for_path(canvas_path: Path | str, settings: dict[str, Any] | None = None) -> CanvasCRDT:
    """Создать CRDT для пути canvas-файла, попытаться подтянуть sidecar."""
    p = Path(canvas_path)
    doc_id = p.stem.replace(".canvas", "").replace(".whiteboard", "") or p.name
    crdt = CanvasCRDT(doc_id, replica_id=get_canvas_replica_id(settings))
    # попробовать загрузить sidecar
    try:
        sync = CanvasFileSync(crdt, p)
        other = sync.load_doc()
        if other is not None:
            crdt.merge(other)
    except Exception:
        pass
    # если есть существующий canvas.json — импортировать фигуры
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            shapes: list[dict[str, Any]] = []
            if isinstance(data, dict) and isinstance(data.get("shapes"), list):
                shapes = [s for s in data["shapes"] if isinstance(s, dict)]
            elif isinstance(data, list):
                shapes = [s for s in data if isinstance(s, dict) and "type" in s]
            for s in shapes:
                crdt.add_shape(s, timestamp=0)
        except Exception:
            pass
    return crdt


def shapes_to_crdt(shapes: list[dict[str, Any]], doc_id: str = "canvas", replica_id: str | None = None) -> CanvasCRDT:
    crdt = CanvasCRDT(doc_id, replica_id=replica_id or get_canvas_replica_id())
    crdt.set_shapes(shapes)
    return crdt


def crdt_to_shapes(crdt: CanvasCRDT) -> list[dict[str, Any]]:
    return crdt.get_shapes()


__all__ = [
    "get_canvas_replica_id",
    "CanvasOperation",
    "CanvasCRDT",
    "CanvasDocument",
    "CanvasLWW",
    "CursorState",
    "CursorManager",
    "SharedCursors",
    "SharedCursorManager",
    "CanvasFileSync",
    "CanvasSyncStub",
    "CanvasWebSocketSync",
    "CanvasNetworkSync",
    "WebSocketSyncStub",
    "CanvasCollabService",
    "CanvasCollab",
    "CanvasCollaborationService",
    "create_canvas_crdt_for_path",
    "shapes_to_crdt",
    "crdt_to_shapes",
]
