"""P2P sync (libp2p) для FragileNotes: децентрализованная синхронизация vault.

Заглушка libp2p + CRDT + WebSocket + discovery/sync. Только stdlib;
реальный ``libp2p`` / ``websockets`` подключаются опционально если доступны,
иначе используется in-memory stub совместимый по API.

Архитектура:

* **PeerInfo / P2PConfig** — идентичность пира: ``peer_id`` (libp2p PeerID),
  ``multiaddrs``, ``replica_id`` для CRDT tie-breaker.
* **VaultCRDT / VaultSyncState** — CRDT-состояние vault: map
  ``rel_path -> LWWRegister`` (целый файл как LWW) + vector clock.
  Merge — state-based, коммутативен. Поддерживает ``set_file``,
  ``delete_file``, ``merge``, ``diff``.
* **LibP2PHost (stub)** — заглушка libp2p Host: ``listen``, ``connect``,
  ``disconnect``, ``stream`` handler, ``get_peers``. При наличии
  ``libp2p`` (``py-libp2p``) делегирует туда, иначе in-memory.
* **P2PDiscovery** — discovery слой: mDNS-like локальный broadcast
  (in-memory глобальный реестр) + bootstrap peers + TTL pruning.
  API: ``announce()``, ``discover()``, ``find_peer()``,
  ``on_peer_discovered`` колбэк.
* **P2PWebSocketTransport / WebSocketSyncStub** — транспорт поверх
  WebSocket (заглушка). In-memory канал, ``send``/``receive``/``broadcast``,
  логирует операции. При наличии ``websockets`` может подключиться к
  ``ws://`` endpoint.
* **P2PSyncService (фасад)** — единая точка для vault sync:
  ``start()`` / ``stop()`` / ``announce()`` / ``discover_peers()``
  / ``connect_peer()`` / ``sync_file()`` / ``sync_vault()``
  / ``sync_with(peer_state)`` / ``broadcast_state()``.

Только stdlib; ``py_compile`` без зависимостей.

Пример::

    from fragilenotes.services.p2p import P2PSyncService, VaultCRDT

    svc = P2PSyncService({"vault_root": "/home/user/vault"}, replica_id="alice")
    svc.start()
    svc.announce()
    peers = svc.discover_peers()
    # локальное изменение файла
    svc.sync_file("Notes/hello.md", "# hello")
    # merge с удалённым peer
    other = VaultCRDT("vault", replica_id="bob")
    other.set_file("Notes/hello.md", "# world", timestamp=other._next_ts())
    svc.sync_with(other)
    assert svc.state.get_file("Notes/hello.md") in ("# hello", "# world")

Интеграция в app (опционально)::

    from fragilenotes.services.p2p import get_peer_id, P2PSyncService
    self.p2p = P2PSyncService(self.settings)
    self.p2p.start()
    self.p2p.announce()

Discovery колбэк::

    svc.discovery.on_peer_discovered(lambda peer: print("found", peer.peer_id))

"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Утилиты ────────────────────────────────────────────────────────────────

_DEFAULT_SETTINGS: dict[str, Any] = {}
try:
    from ..config import DEFAULT_SETTINGS as _IMPORTED_DEFAULT  # type: ignore

    _DEFAULT_SETTINGS = _IMPORTED_DEFAULT  # type: ignore[no-redef]
except Exception:
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_sec() -> float:
    return time.time()


def get_replica_id(settings: dict[str, Any] | None = None) -> str:
    """Стабильный replica_id для CRDT tie-breaker.

    Берётся из ``settings["crdt_replica_id"]`` / ``p2p_replica_id`` /
    ``replica_id``, иначе генерируется ``uuid4().hex[:8]`` и кэшируется.
    """
    if settings is not None:
        for key in ("p2p_replica_id", "crdt_replica_id", "replica_id"):
            rid = settings.get(key)
            if isinstance(rid, str) and rid.strip():
                return rid.strip()
    rid = uuid.uuid4().hex[:8]
    if settings is not None:
        try:
            settings["p2p_replica_id"] = rid
            if not settings.get("crdt_replica_id"):
                settings["crdt_replica_id"] = rid
        except Exception:
            pass
    return rid


def get_peer_id(settings: dict[str, Any] | None = None) -> str:
    """libp2p PeerID (заглушка): ``12D3KooW...``-like или ``peer-<hex>``.

    Берётся из ``settings["p2p_peer_id"]`` / ``peer_id``, иначе генерируется
    ``peer-<8hex>`` и кэшируется. Если доступен реальный libp2p — можно
    переопределить через ``libp2p.peer.id.ID``.
    """
    if settings is not None:
        for key in ("p2p_peer_id", "peer_id", "libp2p_peer_id"):
            pid = settings.get(key)
            if isinstance(pid, str) and pid.strip():
                return pid.strip()
    # пробуем сгенерировать детерминированно из replica_id
    rid = get_replica_id(settings) if settings is not None else uuid.uuid4().hex[:8]
    # libp2p peer id stub: peer-<hash>
    h = hashlib.sha256(rid.encode("utf-8")).hexdigest()[:12]
    pid = f"peer-{h}"
    # пробуем реальный libp2p если доступен (не обязателен)
    try:
        import libp2p.peer.id as _pid  # type: ignore

        _ = _pid  # noqa: F841
    except Exception:
        pass
    if settings is not None:
        try:
            settings["p2p_peer_id"] = pid
        except Exception:
            pass
    return pid


def _vault_root_from_settings(settings: dict[str, Any] | None) -> Path:
    if settings is None:
        return Path.home() / "desktop"
    # пробуем resolve_paths
    try:
        from ..paths import resolve_paths  # type: ignore

        return resolve_paths(settings).root
    except Exception:
        pass
    try:
        vr = str(settings.get("vault_root") or settings.get("vault_path") or "")
        if vr.strip():
            return Path(vr).expanduser()
    except Exception:
        pass
    return Path.home() / "desktop"


def _multiaddr_for_peer(peer_id: str, host: str = "127.0.0.1", port: int = 4001) -> str:
    """Сгенерировать multiaddr для пира (stub)."""
    # /ip4/127.0.0.1/tcp/4001/p2p/<peer_id>
    return f"/ip4/{host}/tcp/{port}/p2p/{peer_id}"


def _hash_content(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ── PeerInfo / P2PConfig ───────────────────────────────────────────────────

@dataclass
class PeerInfo:
    """Информация о пире в сети libp2p."""

    peer_id: str
    replica_id: str = ""
    addresses: list[str] = field(default_factory=list)
    agent: str = "fragilenotes/0.1.0"
    vault_id: str = ""  # hash vault_root или doc_id
    last_seen_ms: int = 0
    meta: dict[str, Any] = field(default_factory=dict)
    connected: bool = False

    def __post_init__(self) -> None:
        if not self.peer_id:
            self.peer_id = get_peer_id()
        if not self.replica_id:
            self.replica_id = self.peer_id[:8]
        if not self.last_seen_ms:
            self.last_seen_ms = _now_ms()
        if not self.addresses:
            self.addresses = [_multiaddr_for_peer(self.peer_id)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_id": self.peer_id,
            "replica_id": self.replica_id,
            "addresses": list(self.addresses),
            "agent": self.agent,
            "vault_id": self.vault_id,
            "last_seen_ms": int(self.last_seen_ms),
            "meta": dict(self.meta),
            "connected": bool(self.connected),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PeerInfo:
        return cls(
            peer_id=str(d.get("peer_id", "")),
            replica_id=str(d.get("replica_id", "")),
            addresses=list(d.get("addresses") or []),
            agent=str(d.get("agent", "fragilenotes/0.1.0")),
            vault_id=str(d.get("vault_id", "")),
            last_seen_ms=int(d.get("last_seen_ms", 0)),
            meta=dict(d.get("meta") or {}),
            connected=bool(d.get("connected", False)),
        )

    def touch(self) -> None:
        self.last_seen_ms = _now_ms()

    def is_expired(self, ttl_ms: int = 60000) -> bool:
        return (_now_ms() - int(self.last_seen_ms)) > int(ttl_ms)


@dataclass
class P2PConfig:
    """Конфигурация P2P узла."""

    enabled: bool = False
    peer_id: str = ""
    replica_id: str = ""
    listen_addrs: list[str] = field(default_factory=lambda: ["/ip4/0.0.0.0/tcp/4001", "/ip4/0.0.0.0/tcp/4002/ws"])
    bootstrap_peers: list[str] = field(default_factory=list)
    discovery_interval_ms: int = 5000
    sync_interval_ms: int = 10000
    mdns_enabled: bool = True
    relay_enabled: bool = False
    endpoint: str = "ws://localhost:8765/p2p"
    vault_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "peer_id": self.peer_id,
            "replica_id": self.replica_id,
            "listen_addrs": list(self.listen_addrs),
            "bootstrap_peers": list(self.bootstrap_peers),
            "discovery_interval_ms": int(self.discovery_interval_ms),
            "sync_interval_ms": int(self.sync_interval_ms),
            "mdns_enabled": bool(self.mdns_enabled),
            "relay_enabled": bool(self.relay_enabled),
            "endpoint": self.endpoint,
            "vault_id": self.vault_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> P2PConfig:
        return cls(
            enabled=bool(d.get("enabled", False)),
            peer_id=str(d.get("peer_id", "")),
            replica_id=str(d.get("replica_id", "")),
            listen_addrs=list(d.get("listen_addrs") or ["/ip4/0.0.0.0/tcp/4001"]),
            bootstrap_peers=list(d.get("bootstrap_peers") or []),
            discovery_interval_ms=int(d.get("discovery_interval_ms", 5000)),
            sync_interval_ms=int(d.get("sync_interval_ms", 10000)),
            mdns_enabled=bool(d.get("mdns_enabled", True)),
            relay_enabled=bool(d.get("relay_enabled", False)),
            endpoint=str(d.get("endpoint", "ws://localhost:8765/p2p")),
            vault_id=str(d.get("vault_id", "")),
        )

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None) -> P2PConfig:
        s = dict(settings) if settings is not None else {}
        return cls(
            enabled=bool(s.get("p2p_enabled", s.get("libp2p_enabled", False))),
            peer_id=str(s.get("p2p_peer_id") or get_peer_id(s)),
            replica_id=str(s.get("p2p_replica_id") or get_replica_id(s)),
            listen_addrs=list(s.get("p2p_listen_addrs") or ["/ip4/0.0.0.0/tcp/4001"]),
            bootstrap_peers=list(s.get("p2p_bootstrap_peers") or s.get("bootstrap_peers") or []),
            discovery_interval_ms=int(s.get("p2p_discovery_interval_ms", 5000)),
            sync_interval_ms=int(s.get("p2p_sync_interval_ms", 10000)),
            mdns_enabled=bool(s.get("p2p_mdns_enabled", True)),
            relay_enabled=bool(s.get("p2p_relay_enabled", False)),
            endpoint=str(s.get("p2p_endpoint") or s.get("ws_endpoint") or "ws://localhost:8765/p2p"),
            vault_id=str(s.get("p2p_vault_id") or _hash_content(str(s.get("vault_root", "")))),
        )


# ── CRDT для vault (per-file LWW) ────────────────────────────────────────

@dataclass
class FileEntry:
    """LWW-запись для одного файла vault."""

    rel_path: str
    content: str = ""
    content_hash: str = ""
    timestamp: int = 0
    replica_id: str = ""
    deleted: bool = False
    mtime_ms: int = 0
    size: int = 0

    def __post_init__(self) -> None:
        if not self.content_hash and self.content:
            self.content_hash = _hash_content(self.content)
        if not self.size:
            self.size = len(self.content.encode("utf-8"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "content": self.content,
            "content_hash": self.content_hash,
            "timestamp": int(self.timestamp),
            "replica_id": self.replica_id,
            "deleted": bool(self.deleted),
            "mtime_ms": int(self.mtime_ms),
            "size": int(self.size),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FileEntry:
        return cls(
            rel_path=str(d.get("rel_path", "")),
            content=str(d.get("content", "")),
            content_hash=str(d.get("content_hash", "")),
            timestamp=int(d.get("timestamp", 0)),
            replica_id=str(d.get("replica_id", "")),
            deleted=bool(d.get("deleted", False)),
            mtime_ms=int(d.get("mtime_ms", 0)),
            size=int(d.get("size", 0)),
        )


class VaultCRDT:
    """CRDT-состояние vault: map rel_path -> FileEntry (LWW).

    Каждый файл — отдельный LWW-регистр с tombstone для удаления.
    Merge — объединение словарей с LWW по (timestamp, replica_id).
    Поддерживает vector clock по replica_id.
    """

    def __init__(self, vault_id: str = "vault", replica_id: str | None = None) -> None:
        self.vault_id = str(vault_id)
        self.replica_id = replica_id or get_replica_id()
        self._entries: dict[str, FileEntry] = {}
        self._vector: dict[str, int] = {self.replica_id: 0}
        self._lock = threading.RLock()
        self._op_log: list[dict[str, Any]] = []

    # ── helpers ──
    def _next_ts(self) -> int:
        ts = _now_ms()
        cur = self._vector.get(self.replica_id, 0)
        if ts <= cur:
            ts = cur + 1
        self._vector[self.replica_id] = ts
        return ts

    def _log(self, op: str, rel_path: str, ts: int) -> None:
        self._op_log.append({"op": op, "rel_path": rel_path, "ts": ts, "replica": self.replica_id, "at": _now_ms()})
        if len(self._op_log) > 512:
            self._op_log = self._op_log[-512:]

    # ── public API ──
    def get_file(self, rel_path: str) -> str | None:
        with self._lock:
            e = self._entries.get(str(rel_path))
            if e is None or e.deleted:
                return None
            return e.content

    def has_file(self, rel_path: str) -> bool:
        with self._lock:
            e = self._entries.get(str(rel_path))
            return e is not None and not e.deleted

    def list_files(self, include_deleted: bool = False) -> list[str]:
        with self._lock:
            if include_deleted:
                return sorted(self._entries.keys())
            return sorted(k for k, v in self._entries.items() if not v.deleted)

    def get_entry(self, rel_path: str) -> FileEntry | None:
        with self._lock:
            e = self._entries.get(str(rel_path))
            return copy.deepcopy(e) if e is not None else None

    def set_file(self, rel_path: str, content: str, timestamp: int | None = None) -> bool:
        """Создать/обновить файл. Возвращает True если применено (LWW выиграл)."""
        rel = str(rel_path)
        ts = int(timestamp) if timestamp is not None else self._next_ts()
        h = _hash_content(content)
        with self._lock:
            existing = self._entries.get(rel)
            if existing is not None and (ts, self.replica_id) < (existing.timestamp, existing.replica_id):
                return False
            # если был tombstone — проверяем победу
            if existing is not None and existing.deleted and (ts, self.replica_id) <= (existing.timestamp, existing.replica_id):
                return False
            entry = FileEntry(
                rel_path=rel,
                content=content,
                content_hash=h,
                timestamp=ts,
                replica_id=self.replica_id,
                deleted=False,
                mtime_ms=ts,
                size=len(content.encode("utf-8")),
            )
            self._entries[rel] = entry
            self._log("set", rel, ts)
            return True

    def delete_file(self, rel_path: str, timestamp: int | None = None) -> bool:
        rel = str(rel_path)
        ts = int(timestamp) if timestamp is not None else self._next_ts()
        with self._lock:
            e = self._entries.get(rel)
            if e is None:
                self._entries[rel] = FileEntry(rel_path=rel, content="", timestamp=ts, replica_id=self.replica_id, deleted=True, mtime_ms=ts)
                self._log("delete", rel, ts)
                return True
            if (ts, self.replica_id) < (e.timestamp, e.replica_id):
                return False
            if e.deleted:
                if ts > e.timestamp:
                    e.timestamp = ts
                    e.replica_id = self.replica_id
                return False
            e.deleted = True
            e.timestamp = ts
            e.replica_id = self.replica_id
            e.mtime_ms = ts
            self._log("delete", rel, ts)
            return True

    def apply_op(self, op: dict[str, Any]) -> bool:
        """Применить операцию (idempotent): {op, rel_path, content, timestamp, replica_id}."""
        if not isinstance(op, dict):
            return False
        kind = str(op.get("op", "set")).lower()
        rel = str(op.get("rel_path") or op.get("path") or "")
        if not rel:
            return False
        ts = int(op.get("timestamp", _now_ms()))
        rid = str(op.get("replica_id", ""))
        with self._lock:
            if rid:
                self._vector[rid] = max(self._vector.get(rid, 0), ts)
            if kind in ("set", "put", "update", "add"):
                content = str(op.get("content", ""))
                existing = self._entries.get(rel)
                if existing is None or (ts, rid) > (existing.timestamp, existing.replica_id):
                    self._entries[rel] = FileEntry(
                        rel_path=rel, content=content, content_hash=_hash_content(content),
                        timestamp=ts, replica_id=rid or self.replica_id, deleted=False, mtime_ms=ts,
                        size=len(content.encode("utf-8")),
                    )
                    return True
            elif kind in ("delete", "remove", "rm"):
                e = self._entries.get(rel)
                if e is None:
                    self._entries[rel] = FileEntry(rel_path=rel, timestamp=ts, replica_id=rid or self.replica_id, deleted=True, mtime_ms=ts)
                    return True
                if (ts, rid) > (e.timestamp, e.replica_id):
                    e.deleted = True
                    e.timestamp = ts
                    e.replica_id = rid or e.replica_id
                    e.mtime_ms = ts
                    return True
        return False

    def merge(self, other: VaultCRDT) -> bool:
        """State-based merge с другим VaultCRDT. Возвращает True если локально изменилось."""
        if not isinstance(other, VaultCRDT):
            return False
        changed = False
        with self._lock:
            for rid, ts in other._vector.items():
                if int(ts) > int(self._vector.get(rid, 0)):
                    self._vector[rid] = int(ts)
                    changed = True
            for rel, oentry in other._entries.items():
                sentry = self._entries.get(rel)
                if sentry is None:
                    self._entries[rel] = copy.deepcopy(oentry)
                    changed = True
                else:
                    if (oentry.timestamp, oentry.replica_id) > (sentry.timestamp, sentry.replica_id):
                        self._entries[rel] = copy.deepcopy(oentry)
                        changed = True
            # append op logs (для отладки)
            for op in other._op_log[-32:]:
                if op not in self._op_log:
                    self._op_log.append(dict(op))
            if len(self._op_log) > 512:
                self._op_log = self._op_log[-512:]
        return changed

    def diff(self, other: VaultCRDT) -> dict[str, list[str]]:
        """Сравнить с другим состоянием: {added, updated, deleted, conflict}."""
        with self._lock:
            a_keys = set(k for k, v in self._entries.items() if not v.deleted)
            b_keys = set(k for k, v in other._entries.items() if not v.deleted)
            added = sorted(b_keys - a_keys)
            deleted = sorted(a_keys - b_keys)
            updated: list[str] = []
            conflict: list[str] = []
            for k in a_keys & b_keys:
                ae = self._entries[k]
                be = other._entries[k]
                if ae.content_hash != be.content_hash:
                    # кто новее — тот updated, иначе conflict (concurrent)
                    if (be.timestamp, be.replica_id) > (ae.timestamp, ae.replica_id):
                        updated.append(k)
                    elif (ae.timestamp, ae.replica_id) > (be.timestamp, be.replica_id):
                        pass
                    else:
                        conflict.append(k)
                        updated.append(k)
            return {"added": added, "updated": sorted(updated), "deleted": deleted, "conflict": sorted(conflict)}

    # ── сериализация ──
    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "type": "vault-crdt",
                "vault_id": self.vault_id,
                "replica_id": self.replica_id,
                "vector": dict(self._vector),
                "entries": {k: v.to_dict() for k, v in self._entries.items()},
                "version": 1,
            }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VaultCRDT:
        vault_id = str(d.get("vault_id", "vault"))
        replica = str(d.get("replica_id", "")) or None
        obj = cls(vault_id, replica_id=replica)
        vec = d.get("vector")
        if isinstance(vec, dict):
            obj._vector = {str(k): int(v) for k, v in vec.items()}
        entries = d.get("entries")
        if isinstance(entries, dict):
            for k, v in entries.items():
                if isinstance(v, dict):
                    try:
                        obj._entries[str(k)] = FileEntry.from_dict(v)
                    except Exception:
                        continue
        return obj

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, s: str) -> VaultCRDT:
        return cls.from_dict(json.loads(s))

    def get_vector(self) -> dict[str, int]:
        with self._lock:
            return dict(self._vector)

    def op_log(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._op_log)

    # совместимость с core.crdt.LWWDocument API: text -> json dump для теста
    @property
    def text(self) -> str:
        return self.to_json()


# алиасы
VaultSyncState = VaultCRDT
P2PDocument = VaultCRDT
VaultState = VaultCRDT


# ── FileSync для vault (sidecar) ─────────────────────────────────────────

def _vault_sidecar_path(vault_root: Path) -> Path:
    return Path(vault_root) / ".p2p" / "vault.crdt.json"


class VaultFileSync:
    """Синхронизация VaultCRDT через sidecar файл ``.p2p/vault.crdt.json``."""

    def __init__(self, state: VaultCRDT, vault_root: Path | str) -> None:
        self.state = state
        self.vault_root = Path(vault_root)
        self.sidecar = _vault_sidecar_path(self.vault_root)

    def save(self) -> Path:
        data = self.state.to_dict()
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

    def load_state(self) -> VaultCRDT | None:
        d = self.load()
        if not isinstance(d, dict):
            return None
        try:
            return VaultCRDT.from_dict(d)
        except Exception:
            return None

    def sync(self) -> bool:
        other = self.load_state()
        if other is None:
            try:
                self.save()
            except Exception:
                pass
            return False
        changed = self.state.merge(other)
        if changed:
            try:
                self.save()
            except Exception:
                pass
            return True
        # если sidecar устарел — перезаписать
        try:
            cur = self.state.to_dict()
            if cur.get("vector") != other.to_dict().get("vector"):
                self.save()
        except Exception:
            pass
        return False


# ── LibP2P Host Stub ───────────────────────────────────────────────────────

# Глобальный in-memory реестр для stub-сети (имитация DHT)
_GLOBAL_HOST_REGISTRY: dict[str, LibP2PHost] = {}  # type: ignore[name-defined]
_GLOBAL_HOST_LOCK = threading.RLock()


class LibP2PHost:
    """Заглушка libp2p Host.

    In-memory сеть без реальных сокетов; при наличии ``libp2p`` может
    использовать реальный host. API совместим с ``py-libp2p`` Host:
    ``start()``, ``stop()``, ``connect()``, ``disconnect()``, ``get_peers()``.
    """

    def __init__(self, peer_id: str | None = None, listen_addrs: list[str] | None = None, replica_id: str | None = None) -> None:
        self.peer_id = peer_id or get_peer_id()
        self.replica_id = replica_id or get_replica_id()
        self.listen_addrs = list(listen_addrs or [_multiaddr_for_peer(self.peer_id)])
        # если peer_id не в multiaddr — добавить
        if not any(self.peer_id in a for a in self.listen_addrs):
            self.listen_addrs.append(_multiaddr_for_peer(self.peer_id))
        self._connected: dict[str, PeerInfo] = {}
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self._started = False
        self._lock = threading.RLock()
        self._log: list[dict[str, Any]] = []

    def start(self) -> bool:
        with self._lock:
            if self._started:
                return True
            self._started = True
            try:
                # пробуем реальный libp2p если доступен
                import libp2p  # type: ignore  # noqa: F401

                self._log.append({"op": "start", "at": _now_ms(), "peer_id": self.peer_id, "real_libp2p": True})
            except Exception:
                self._log.append({"op": "start", "at": _now_ms(), "peer_id": self.peer_id, "real_libp2p": False})
            with _GLOBAL_HOST_LOCK:
                _GLOBAL_HOST_REGISTRY[self.peer_id] = self
            return True

    def stop(self) -> None:
        with self._lock:
            self._started = False
            self._log.append({"op": "stop", "at": _now_ms(), "peer_id": self.peer_id})
        with _GLOBAL_HOST_LOCK:
            _GLOBAL_HOST_REGISTRY.pop(self.peer_id, None)

    @property
    def is_started(self) -> bool:
        with self._lock:
            return self._started

    def listen(self) -> list[str]:
        return list(self.listen_addrs)

    def get_peers(self) -> list[PeerInfo]:
        with self._lock:
            return [copy.deepcopy(p) for p in self._connected.values()]

    def is_connected(self, peer_id: str) -> bool:
        with self._lock:
            return str(peer_id) in self._connected

    def connect(self, peer: PeerInfo | str) -> bool:
        pid = peer.peer_id if isinstance(peer, PeerInfo) else str(peer)
        if not pid or pid == self.peer_id:
            return False
        with self._lock:
            if pid in self._connected:
                return True
        # найти в глобальном реестре
        with _GLOBAL_HOST_LOCK:
            target = _GLOBAL_HOST_REGISTRY.get(pid)
        if target is not None:
            # взаимное соединение
            me_info = PeerInfo(peer_id=self.peer_id, replica_id=self.replica_id, addresses=list(self.listen_addrs), last_seen_ms=_now_ms(), connected=True)
            peer_info = PeerInfo(peer_id=pid, replica_id=target.replica_id, addresses=list(target.listen_addrs), last_seen_ms=_now_ms(), connected=True)
            with self._lock:
                self._connected[pid] = peer_info
                self._log.append({"op": "connect", "at": _now_ms(), "peer_id": pid, "in_memory": True})
            with target._lock:
                target._connected[self.peer_id] = me_info
                target._log.append({"op": "connect", "at": _now_ms(), "peer_id": self.peer_id, "in_memory": True})
            return True
        # peer не в локальной сети — считаем что bootstrap peer удалённый, создаём запись
        with self._lock:
            self._connected[pid] = PeerInfo(peer_id=pid, replica_id=pid[:8], addresses=[_multiaddr_for_peer(pid)], last_seen_ms=_now_ms(), connected=True)
            self._log.append({"op": "connect", "at": _now_ms(), "peer_id": pid, "bootstrap": True})
        return True

    def disconnect(self, peer_id: str) -> None:
        pid = str(peer_id)
        with self._lock:
            self._connected.pop(pid, None)
            self._log.append({"op": "disconnect", "at": _now_ms(), "peer_id": pid})
        with _GLOBAL_HOST_LOCK:
            target = _GLOBAL_HOST_REGISTRY.get(pid)
            if target is not None:
                with target._lock:
                    target._connected.pop(self.peer_id, None)

    def set_stream_handler(self, protocol: str, handler: Callable[[dict[str, Any]], Any]) -> None:
        with self._lock:
            self._handlers[str(protocol)] = handler

    def send(self, peer_id: str, protocol: str, payload: dict[str, Any]) -> bool:
        """Отправить payload пиру через protocol handler (stub)."""
        pid = str(peer_id)
        with _GLOBAL_HOST_LOCK:
            target = _GLOBAL_HOST_REGISTRY.get(pid)
        if target is None:
            with self._lock:
                self._log.append({"op": "send_failed", "at": _now_ms(), "peer_id": pid, "protocol": protocol})
            return False
        handler = None
        with target._lock:
            handler = target._handlers.get(str(protocol))
        if handler is None:
            # fallback: если нет handler — считаем доставлено в лог
            with target._lock:
                target._log.append({"op": "recv_no_handler", "at": _now_ms(), "from": self.peer_id, "protocol": protocol})
            with self._lock:
                self._log.append({"op": "send", "at": _now_ms(), "peer_id": pid, "protocol": protocol, "no_handler": True})
            return True
        try:
            handler({"from": self.peer_id, "protocol": protocol, "payload": copy.deepcopy(payload), "at": _now_ms()})
            with self._lock:
                self._log.append({"op": "send", "at": _now_ms(), "peer_id": pid, "protocol": protocol})
            return True
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._log.append({"op": "send_error", "at": _now_ms(), "peer_id": pid, "error": str(exc)[:120]})
            return False

    def broadcast(self, protocol: str, payload: dict[str, Any]) -> int:
        """Broadcast всем подключенным пирам. Возвращает количество доставок."""
        peers = list(self._connected.keys())
        sent = 0
        for pid in peers:
            if self.send(pid, protocol, payload):
                sent += 1
        return sent

    def get_log(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._log)

    def get_multiaddrs(self) -> list[str]:
        return list(self.listen_addrs)


# алиасы
P2PHost = LibP2PHost
Host = LibP2PHost


# ── Discovery ──────────────────────────────────────────────────────────────

# Глобальный in-memory реестр discovery (имитация mDNS + DHT)
_GLOBAL_DISCOVERY_REGISTRY: dict[str, PeerInfo] = {}
_GLOBAL_DISCOVERY_LOCK = threading.RLock()
_GLOBAL_DISCOVERY_CBS: list[Callable[[PeerInfo], None]] = []


class P2PDiscovery:
    """Discovery слой: mDNS-like + bootstrap + глобальный in-memory реестр.

    TTL 60с, автоматическая очистка expired.
    """

    TTL_MS: int = 60000
    PRUNE_INTERVAL_MS: int = 15000

    def __init__(
        self,
        peer_id: str | None = None,
        replica_id: str | None = None,
        bootstrap_peers: list[str] | None = None,
        mdns_enabled: bool = True,
        vault_id: str = "",
        listen_addrs: list[str] | None = None,
    ) -> None:
        self.peer_id = peer_id or get_peer_id()
        self.replica_id = replica_id or get_replica_id()
        self.bootstrap_peers = list(bootstrap_peers or [])
        self.mdns_enabled = bool(mdns_enabled)
        self.vault_id = str(vault_id)
        self.listen_addrs = list(listen_addrs or [_multiaddr_for_peer(self.peer_id)])
        self._local_peers: dict[str, PeerInfo] = {}
        self._lock = threading.RLock()
        self._on_discovered: Callable[[PeerInfo], None] | None = None
        self._started = False
        self._prune_timer: threading.Timer | None = None

    # ── lifecycle ──
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        self.announce()
        self._schedule_prune()

    def stop(self) -> None:
        with self._lock:
            self._started = False
            if self._prune_timer is not None:
                try:
                    self._prune_timer.cancel()
                except Exception:
                    pass
                self._prune_timer = None
        # удалить себя из глобального реестра
        with _GLOBAL_DISCOVERY_LOCK:
            _GLOBAL_DISCOVERY_REGISTRY.pop(self.peer_id, None)

    def _schedule_prune(self) -> None:
        with self._lock:
            if not self._started:
                return
            if self._prune_timer is not None:
                try:
                    self._prune_timer.cancel()
                except Exception:
                    pass
            t = threading.Timer(self.PRUNE_INTERVAL_MS / 1000.0, self._prune)
            t.daemon = True
            self._prune_timer = t
            t.start()

    def _prune(self) -> None:
        try:
            self.prune_expired()
        except Exception:
            pass
        self._schedule_prune()

    def prune_expired(self) -> int:
        removed = 0
        with _GLOBAL_DISCOVERY_LOCK:
            dead = [pid for pid, info in _GLOBAL_DISCOVERY_REGISTRY.items() if info.is_expired(self.TTL_MS)]
            for pid in dead:
                _GLOBAL_DISCOVERY_REGISTRY.pop(pid, None)
                removed += 1
        with self._lock:
            dead_local = [pid for pid, info in self._local_peers.items() if info.is_expired(self.TTL_MS)]
            for pid in dead_local:
                self._local_peers.pop(pid, None)
        return removed

    # ── announce / discover ──
    def announce(self, peer_info: PeerInfo | None = None) -> PeerInfo:
        """Анонсировать себя в сети."""
        if peer_info is None:
            peer_info = PeerInfo(
                peer_id=self.peer_id,
                replica_id=self.replica_id,
                addresses=list(self.listen_addrs),
                vault_id=self.vault_id,
                last_seen_ms=_now_ms(),
                connected=False,
                meta={"agent": "fragilenotes/0.1.0"},
            )
        else:
            peer_info.touch()
        with _GLOBAL_DISCOVERY_LOCK:
            _GLOBAL_DISCOVERY_REGISTRY[peer_info.peer_id] = copy.deepcopy(peer_info)
            # уведомить глобальные колбэки
            for cb in list(_GLOBAL_DISCOVERY_CBS):
                try:
                    cb(copy.deepcopy(peer_info))
                except Exception:
                    pass
        with self._lock:
            # не храним себя в local peers
            pass
        # уведомить bootstrap peers (stub)
        for b in self.bootstrap_peers:
            try:
                pid = str(b).split("/")[-1] if "/" in str(b) else str(b)
                if pid and pid != self.peer_id:
                    with self._lock:
                        if pid not in self._local_peers:
                            self._local_peers[pid] = PeerInfo(peer_id=pid, addresses=[str(b)], last_seen_ms=_now_ms())
            except Exception:
                continue
        return peer_info

    def discover(self, vault_id: str | None = None) -> list[PeerInfo]:
        """Найти пиры (фильтр по vault_id если указан)."""
        self.prune_expired()
        out: list[PeerInfo] = []
        with _GLOBAL_DISCOVERY_LOCK:
            for info in _GLOBAL_DISCOVERY_REGISTRY.values():
                if info.peer_id == self.peer_id:
                    continue
                if vault_id is not None and vault_id and info.vault_id and info.vault_id != vault_id:
                    continue
                out.append(copy.deepcopy(info))
        # добавить bootstrap которых нет в реестре
        with self._lock:
            for pid, info in self._local_peers.items():
                if not any(p.peer_id == pid for p in out) and pid != self.peer_id:
                    out.append(copy.deepcopy(info))
            # также bootstrap_peers как потенциальные
            for b in self.bootstrap_peers:
                pid = str(b).split("/")[-1] if "/" in str(b) else str(b)
                if pid and pid != self.peer_id and not any(p.peer_id == pid for p in out):
                    out.append(PeerInfo(peer_id=pid, addresses=[str(b)], last_seen_ms=_now_ms()))
        # вызвать колбэк для каждого нового
        if self._on_discovered is not None:
            for p in out:
                try:
                    self._on_discovered(copy.deepcopy(p))
                except Exception:
                    pass
        return out

    def find_peer(self, peer_id: str) -> PeerInfo | None:
        pid = str(peer_id)
        with _GLOBAL_DISCOVERY_LOCK:
            info = _GLOBAL_DISCOVERY_REGISTRY.get(pid)
            if info is not None:
                return copy.deepcopy(info)
        with self._lock:
            info = self._local_peers.get(pid)
            if info is not None:
                return copy.deepcopy(info)
        return None

    def add_peer(self, info: PeerInfo) -> None:
        if not isinstance(info, PeerInfo) or not info.peer_id or info.peer_id == self.peer_id:
            return
        with self._lock:
            existing = self._local_peers.get(info.peer_id)
            if existing is not None and info.last_seen_ms <= existing.last_seen_ms:
                return
            self._local_peers[info.peer_id] = copy.deepcopy(info)
        with _GLOBAL_DISCOVERY_LOCK:
            _GLOBAL_DISCOVERY_REGISTRY[info.peer_id] = copy.deepcopy(info)
        if self._on_discovered is not None:
            try:
                self._on_discovered(copy.deepcopy(info))
            except Exception:
                pass

    def remove_peer(self, peer_id: str) -> None:
        pid = str(peer_id)
        with self._lock:
            self._local_peers.pop(pid, None)

    def on_peer_discovered(self, cb: Callable[[PeerInfo], None] | None) -> None:
        self._on_discovered = cb

    def get_peers(self) -> list[PeerInfo]:
        return self.discover()

    def peer_count(self) -> int:
        return len(self.discover())

    @classmethod
    def global_registry_snapshot(cls) -> dict[str, PeerInfo]:
        with _GLOBAL_DISCOVERY_LOCK:
            return {k: copy.deepcopy(v) for k, v in _GLOBAL_DISCOVERY_REGISTRY.items()}

    @classmethod
    def clear_global_registry(cls) -> None:
        with _GLOBAL_DISCOVERY_LOCK:
            _GLOBAL_DISCOVERY_REGISTRY.clear()


# алиасы
PeerDiscovery = P2PDiscovery
DiscoveryService = P2PDiscovery
Discovery = P2PDiscovery


# ── WebSocket Transport Stub ───────────────────────────────────────────────

class P2PWebSocketTransport:
    """WebSocket транспорт для P2P sync (заглушка).

    In-memory канал + лог. При наличии ``websockets`` пытается
    использовать реальный ``websockets.connect``.
    """

    def __init__(self, endpoint: str | None = None, peer_id: str | None = None) -> None:
        self.endpoint = endpoint or "ws://localhost:8765/p2p"
        self.peer_id = peer_id or get_peer_id()
        self._connected = False
        self._peers: list[str] = []  # peer_ids
        self._peer_transports: list[P2PWebSocketTransport] = []
        self._log: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self._on_message: Callable[[dict[str, Any]], None] | None = None
        self._loop: Any | None = None
        self._ws: Any | None = None

    def connect(self) -> bool:
        with self._lock:
            if self._connected:
                return True
            self._connected = True
            self._log.append({"op": "ws_connect", "at": _now_ms(), "endpoint": self.endpoint, "peer_id": self.peer_id})
        # пробуем реальный websockets если доступен
        try:
            import websockets  # type: ignore  # noqa: F401

            self._log[-1]["real_ws"] = True
        except Exception:
            self._log[-1]["real_ws"] = False
        return True

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            self._log.append({"op": "ws_disconnect", "at": _now_ms(), "endpoint": self.endpoint})
        # закрыть реальный ws если есть
        if self._ws is not None:
            try:
                import asyncio

                asyncio.run(self._ws.close())  # type: ignore
            except Exception:
                pass
            self._ws = None

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def set_on_message(self, cb: Callable[[dict[str, Any]], None] | None) -> None:
        self._on_message = cb

    def send(self, payload: dict[str, Any], peer_id: str | None = None) -> dict[str, Any]:
        """Отправить payload. Broadcast если peer_id None."""
        envelope = {
            "type": str(payload.get("type", "p2p-msg")),
            "from": self.peer_id,
            "payload": copy.deepcopy(payload),
            "_sent_at": _now_ms(),
            "_endpoint": self.endpoint,
            "to": peer_id,
        }
        with self._lock:
            self._log.append({"op": "ws_send", "at": _now_ms(), "type": envelope["type"], "to": peer_id or "broadcast"})
            # in-memory broadcast peers
            for t in list(self._peer_transports):
                try:
                    t.receive(envelope)
                except Exception:
                    pass
        # реальный ws send если есть
        if self._ws is not None:
            try:
                import asyncio
                import json as _json

                asyncio.run(self._ws.send(_json.dumps(envelope)))  # type: ignore
            except Exception:
                pass
        return envelope

    def send_to(self, peer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.send(payload, peer_id=peer_id)

    def broadcast(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.send(payload, peer_id=None)

    def receive(self, envelope: dict[str, Any] | None) -> bool:
        """Принять envelope и вызвать on_message."""
        if not isinstance(envelope, dict):
            with self._lock:
                self._log.append({"op": "ws_recv", "at": _now_ms(), "noop": True})
            return False
        with self._lock:
            self._log.append({"op": "ws_recv", "at": _now_ms(), "type": str(envelope.get("type", ""))})
        cb = self._on_message
        if cb is not None:
            try:
                cb(copy.deepcopy(envelope))
            except Exception:
                pass
        return True

    # alias compat
    on_message = receive
    push = send
    pull = receive

    def connect_peer(self, other: P2PWebSocketTransport) -> None:
        if other is self:
            return
        with self._lock:
            if other not in self._peer_transports:
                self._peer_transports.append(other)
        with other._lock:
            if self not in other._peer_transports:
                other._peer_transports.append(self)

    def add_peer(self, peer_id: str) -> None:
        with self._lock:
            if peer_id not in self._peers and peer_id != self.peer_id:
                self._peers.append(peer_id)

    def get_log(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._log)

    def get_endpoint(self) -> str:
        return self.endpoint


# алиасы
WebSocketSyncStub = P2PWebSocketTransport
P2PWebSocketSync = P2PWebSocketTransport
WebSocketTransport = P2PWebSocketTransport


# ── P2P Sync Service (фасад vault-sync) ───────────────────────────────────

class P2PSyncService:
    """Фасад P2P синхронизации vault: libp2p host + discovery + CRDT + WebSocket.

    Децентрализованный sync без центрального сервера; заглушка полностью
    in-memory с теми же API что и будущий реальный libp2p транспорт.

    Пример::

        svc = P2PSyncService({"vault_root": "/tmp/vault", "p2p_enabled": True})
        svc.start()
        svc.announce()
        svc.sync_file("a.md", "hello")
        svc.broadcast_state()
        peers = svc.discover_peers()
        # sync с другим узлом
        other = P2PSyncService({"vault_root": "/tmp/vault2"}, replica_id="bob")
        other.start()
        svc.connect_peer(other.host.peer_id)
        svc.sync_with(other.state)
    """

    def __init__(
        self,
        settings: dict[str, Any] | None = None,
        vault_root: Path | str | None = None,
        replica_id: str | None = None,
        peer_id: str | None = None,
        config: P2PConfig | None = None,
        vault_id: str | None = None,
        endpoint: str | None = None,
    ) -> None:
        self.settings = dict(settings) if settings is not None else {}
        self.replica_id = replica_id or get_replica_id(self.settings)
        self.peer_id = peer_id or get_peer_id(self.settings)
        # vault_root
        if vault_root is not None:
            self.vault_root = Path(vault_root)
        else:
            self.vault_root = _vault_root_from_settings(self.settings)
        # config
        if config is not None:
            self.config = config
        else:
            self.config = P2PConfig.from_settings(self.settings)
            if replica_id is not None:
                self.config.replica_id = self.replica_id
            if peer_id is not None:
                self.config.peer_id = self.peer_id
            if endpoint is not None:
                self.config.endpoint = endpoint
            if vault_id is not None:
                self.config.vault_id = vault_id
        # синхронизируем peer_id/replica_id с конфигом
        if not self.config.peer_id:
            self.config.peer_id = self.peer_id
        else:
            self.peer_id = self.config.peer_id
        if not self.config.replica_id:
            self.config.replica_id = self.replica_id
        else:
            self.replica_id = self.config.replica_id
        if not self.config.vault_id:
            self.config.vault_id = _hash_content(str(self.vault_root))
        self.vault_id = self.config.vault_id

        # CRDT состояние vault
        self.state = VaultCRDT(vault_id=self.vault_id, replica_id=self.replica_id)
        self._file_sync = VaultFileSync(self.state, self.vault_root)

        # libp2p host
        self.host = LibP2PHost(peer_id=self.peer_id, listen_addrs=list(self.config.listen_addrs), replica_id=self.replica_id)
        # discovery
        self.discovery = P2PDiscovery(
            peer_id=self.peer_id,
            replica_id=self.replica_id,
            bootstrap_peers=list(self.config.bootstrap_peers),
            mdns_enabled=self.config.mdns_enabled,
            vault_id=self.vault_id,
            listen_addrs=list(self.config.listen_addrs),
        )
        # websocket transport
        self.transport = P2PWebSocketTransport(endpoint=self.config.endpoint, peer_id=self.peer_id)

        self._enabled = bool(self.config.enabled or self.settings.get("p2p_enabled", False))
        self._started = False
        self._lock = threading.RLock()
        self._on_sync: Callable[[VaultCRDT], None] | None = None
        self._on_peer: Callable[[PeerInfo], None] | None = None
        self._log: list[dict[str, Any]] = []
        self._sync_timer: threading.Timer | None = None

        # wired handlers
        try:
            self.host.set_stream_handler("/fragilenotes/sync/1.0.0", self._handle_sync_stream)
            self.host.set_stream_handler("/fragilenotes/discovery/1.0.0", self._handle_discovery_stream)
        except Exception:
            pass
        try:
            self.transport.set_on_message(self._handle_ws_message)
            self.discovery.on_peer_discovered(self._handle_peer_discovered)
        except Exception:
            pass

        # попытаться загрузить sidecar
        try:
            loaded = self._file_sync.load_state()
            if loaded is not None:
                self.state.merge(loaded)
        except Exception:
            pass

    # ── internal handlers ──
    def _handle_sync_stream(self, msg: dict[str, Any]) -> None:
        payload = msg.get("payload") if isinstance(msg, dict) and "payload" in msg else msg
        if isinstance(payload, dict):
            self._handle_sync_payload(payload)

    def _handle_discovery_stream(self, msg: dict[str, Any]) -> None:
        payload = msg.get("payload") if isinstance(msg, dict) and "payload" in msg else msg
        if isinstance(payload, dict) and isinstance(payload.get("peer_id"), str):
            try:
                info = PeerInfo.from_dict(payload)
                self.discovery.add_peer(info)
            except Exception:
                pass

    def _handle_ws_message(self, envelope: dict[str, Any]) -> None:
        payload = envelope.get("payload") if isinstance(envelope, dict) and "payload" in envelope else envelope
        if isinstance(payload, dict):
            ptype = str(payload.get("type", "")).lower()
            if ptype in ("vault-sync", "vault_crdt", "p2p-sync", "sync", "vault-crdt"):
                self._handle_sync_payload(payload)
            elif ptype in ("discovery", "peer-announce", "p2p-discovery"):
                try:
                    info = PeerInfo.from_dict(payload)
                    self.discovery.add_peer(info)
                except Exception:
                    pass

    def _handle_peer_discovered(self, info: PeerInfo) -> None:
        cb = self._on_peer
        if cb is not None:
            try:
                cb(copy.deepcopy(info))
            except Exception:
                pass
        with self._lock:
            self._log.append({"op": "peer_discovered", "at": _now_ms(), "peer_id": info.peer_id})

    def _handle_sync_payload(self, payload: dict[str, Any]) -> bool:
        """Обработать sync payload: мердж VaultCRDT."""
        # payload может быть либо полный state, либо entries
        if not isinstance(payload, dict):
            return False
        # если есть entries -> VaultCRDT
        if isinstance(payload.get("entries"), dict) or payload.get("type") in ("vault-crdt", "vault_crdt", "p2p-sync", "vault-sync"):
            try:
                other = VaultCRDT.from_dict(payload)
                changed = self.state.merge(other)
                if changed:
                    try:
                        self._file_sync.save()
                    except Exception:
                        pass
                    cb = self._on_sync
                    if cb is not None:
                        try:
                            cb(self.state)
                        except Exception:
                            pass
                with self._lock:
                    self._log.append({"op": "sync_recv", "at": _now_ms(), "changed": bool(changed), "from": str(payload.get("replica_id", ""))[:8]})
                return bool(changed)
            except Exception:
                pass
        # fallback: single file op
        if isinstance(payload.get("rel_path"), str) or isinstance(payload.get("path"), str):
            changed = self.state.apply_op(payload)
            if changed:
                with self._lock:
                    self._log.append({"op": "op_apply", "at": _now_ms(), "rel_path": str(payload.get("rel_path") or payload.get("path"))})
            return bool(changed)
        return False

    # ── lifecycle ──
    def start(self) -> bool:
        with self._lock:
            if self._started:
                return True
            self._started = True
        ok_host = self.host.start()
        self.discovery.start()
        self.transport.connect()
        # загрузить sidecar повторно
        try:
            self._file_sync.sync()
        except Exception:
            pass
        with self._lock:
            self._log.append({"op": "start", "at": _now_ms(), "peer_id": self.peer_id, "host_ok": bool(ok_host)})
        # периодический sync если enabled
        if self.config.sync_interval_ms > 0 and self._enabled:
            self._schedule_sync()
        return bool(ok_host)

    def stop(self) -> None:
        with self._lock:
            self._started = False
            if self._sync_timer is not None:
                try:
                    self._sync_timer.cancel()
                except Exception:
                    pass
                self._sync_timer = None
            self._log.append({"op": "stop", "at": _now_ms(), "peer_id": self.peer_id})
        try:
            self.transport.disconnect()
        except Exception:
            pass
        try:
            self.discovery.stop()
        except Exception:
            pass
        try:
            self.host.stop()
        except Exception:
            pass

    def shutdown(self) -> None:
        self.stop()

    def _schedule_sync(self) -> None:
        with self._lock:
            if not self._started or not self._enabled:
                return
            if self._sync_timer is not None:
                try:
                    self._sync_timer.cancel()
                except Exception:
                    pass
            interval = max(1.0, self.config.sync_interval_ms / 1000.0)
            t = threading.Timer(interval, self._periodic_sync)
            t.daemon = True
            self._sync_timer = t
            t.start()

    def _periodic_sync(self) -> None:
        try:
            self.broadcast_state()
        except Exception:
            pass
        self._schedule_sync()

    @property
    def is_started(self) -> bool:
        with self._lock:
            return self._started

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        self._enabled = True
        try:
            self.settings["p2p_enabled"] = True
            self.config.enabled = True
        except Exception:
            pass
        if not self.is_started:
            self.start()
        else:
            self._schedule_sync()

    def disable(self) -> None:
        self._enabled = False
        try:
            self.settings["p2p_enabled"] = False
            self.config.enabled = False
        except Exception:
            pass
        with self._lock:
            if self._sync_timer is not None:
                try:
                    self._sync_timer.cancel()
                except Exception:
                    pass
                self._sync_timer = None

    # ── discovery ──
    def announce(self) -> PeerInfo:
        info = self.discovery.announce(
            PeerInfo(
                peer_id=self.peer_id,
                replica_id=self.replica_id,
                addresses=list(self.host.get_multiaddrs()),
                vault_id=self.vault_id,
                last_seen_ms=_now_ms(),
                meta={"agent": "fragilenotes/0.1.0", "vault_root": str(self.vault_root)},
            )
        )
        # также broadcast via host + ws
        try:
            self.host.broadcast("/fragilenotes/discovery/1.0.0", info.to_dict())
        except Exception:
            pass
        try:
            self.transport.broadcast({"type": "p2p-discovery", **info.to_dict()})
        except Exception:
            pass
        with self._lock:
            self._log.append({"op": "announce", "at": _now_ms(), "peer_id": self.peer_id})
        return info

    def discover_peers(self, vault_id: str | None = None) -> list[PeerInfo]:
        peers = self.discovery.discover(vault_id=vault_id or self.vault_id)
        # также из host
        try:
            host_peers = self.host.get_peers()
            for hp in host_peers:
                if not any(p.peer_id == hp.peer_id for p in peers):
                    peers.append(hp)
        except Exception:
            pass
        return peers

    def find_peer(self, peer_id: str) -> PeerInfo | None:
        return self.discovery.find_peer(peer_id) or next((p for p in self.host.get_peers() if p.peer_id == peer_id), None)

    def connect_peer(self, peer_id: str | PeerInfo) -> bool:
        pid = peer_id.peer_id if isinstance(peer_id, PeerInfo) else str(peer_id)
        if not pid or pid == self.peer_id:
            return False
        ok = self.host.connect(pid)
        if ok:
            try:
                self.transport.add_peer(pid)
            except Exception:
                pass
            with self._lock:
                self._log.append({"op": "connect_peer", "at": _now_ms(), "peer_id": pid, "ok": True})
            # сразу попробовать sync
            try:
                self.send_state(pid)
            except Exception:
                pass
        return bool(ok)

    def disconnect_peer(self, peer_id: str) -> None:
        pid = str(peer_id)
        try:
            self.host.disconnect(pid)
        except Exception:
            pass
        try:
            self.discovery.remove_peer(pid)
        except Exception:
            pass
        with self._lock:
            self._log.append({"op": "disconnect_peer", "at": _now_ms(), "peer_id": pid})

    # ── sync ──
    def sync_file(self, rel_path: str, content: str | None = None) -> bool:
        """Синхронизировать один файл: обновить CRDT + sidecar + broadcast."""
        rel = str(rel_path)
        # если content None — прочитать с диска
        if content is None:
            # попытаться прочитать из vault_root / rel
            p = self.vault_root / rel
            try:
                if p.is_file():
                    content = p.read_text(encoding="utf-8", errors="replace")
                else:
                    # файл удалён -> delete
                    return bool(self.state.delete_file(rel))
            except OSError:
                return False
        # sanitize content
        content = str(content)
        ok = self.state.set_file(rel, content)
        if ok:
            try:
                self._file_sync.save()
            except Exception:
                pass
            # broadcast op
            _entry = self.state.get_entry(rel)
            _ts = _entry.timestamp if _entry is not None else _now_ms()
            op = {"op": "set", "rel_path": rel, "content": content, "timestamp": _ts, "replica_id": self.replica_id}
            try:
                self.broadcast_op(op)
            except Exception:
                pass
        return bool(ok)

    def delete_file(self, rel_path: str) -> bool:
        ok = self.state.delete_file(str(rel_path))
        if ok:
            try:
                self._file_sync.save()
            except Exception:
                pass
            op = {"op": "delete", "rel_path": str(rel_path), "timestamp": _now_ms(), "replica_id": self.replica_id}
            try:
                self.broadcast_op(op)
            except Exception:
                pass
        return bool(ok)

    def sync_vault(self, scan_disk: bool = True) -> int:
        """Сканировать vault и слить дисковые файлы в CRDT; возвращает количество изменений."""
        changed = 0
        if scan_disk:
            try:
                from ..vault import ALLOWED_EXTS, HEAVY_DIRS  # type: ignore
            except Exception:
                ALLOWED_EXTS = {".md"}
                HEAVY_DIRS = {".git", ".obsidian"}
            # простой обход через pathlib
            vault_files: list[Path] = []
            try:
                for p in self.vault_root.rglob("*.md"):
                    # пропустить тяжёлые
                    try:
                        rel = p.relative_to(self.vault_root)
                        if any(part in HEAVY_DIRS or part.startswith(".") for part in rel.parts):
                            continue
                    except Exception:
                        continue
                    if p.is_file():
                        vault_files.append(p)
                # также txt/json если разрешены
                for ext in (ALLOWED_EXTS - {".md"}):
                    try:
                        for p in self.vault_root.rglob(f"*{ext}"):
                            try:
                                rel = p.relative_to(self.vault_root)
                                if any(part in HEAVY_DIRS or part.startswith(".") for part in rel.parts):
                                    continue
                            except Exception:
                                continue
                            if p.is_file() and p not in vault_files:
                                vault_files.append(p)
                    except Exception:
                        continue
            except Exception:
                vault_files = []
            # set_file для каждого
            for p in vault_files:
                try:
                    rel_str = str(p.relative_to(self.vault_root))
                    text = p.read_text(encoding="utf-8", errors="replace")
                    before = self.state.get_file(rel_str)
                    if before is None or before != text:
                        if self.state.set_file(rel_str, text):
                            changed += 1
                except Exception:
                    continue
        # сохранить sidecar если изменилось
        if changed:
            try:
                self._file_sync.save()
            except Exception:
                pass
            try:
                self.broadcast_state()
            except Exception:
                pass
        else:
            # всё равно проверить merge sidecar
            try:
                if self._file_sync.sync():
                    changed = 1
            except Exception:
                pass
        with self._lock:
            self._log.append({"op": "sync_vault", "at": _now_ms(), "changed": int(changed)})
        return int(changed)

    def get_state_payload(self) -> dict[str, Any]:
        d = self.state.to_dict()
        d["_sent_at"] = _now_ms()
        d["_peer_id"] = self.peer_id
        d["_vault_id"] = self.vault_id
        return d

    def send_state(self, peer_id: str) -> bool:
        payload = self.get_state_payload()
        payload["type"] = "vault-crdt"
        ok = False
        try:
            ok = bool(self.host.send(peer_id, "/fragilenotes/sync/1.0.0", payload))
        except Exception:
            ok = False
        try:
            self.transport.send_to(peer_id, payload)
            ok = True
        except Exception:
            pass
        with self._lock:
            self._log.append({"op": "send_state", "at": _now_ms(), "peer_id": str(peer_id), "ok": bool(ok)})
        return bool(ok)

    def broadcast_state(self) -> int:
        payload = self.get_state_payload()
        payload["type"] = "vault-crdt"
        n = 0
        try:
            n += self.host.broadcast("/fragilenotes/sync/1.0.0", payload)
        except Exception:
            pass
        try:
            self.transport.broadcast(payload)
            # считаем как broadcast к ws peers (не точное число)
            n = max(n, len(self.transport._peer_transports) or 1)
        except Exception:
            pass
        with self._lock:
            self._log.append({"op": "broadcast_state", "at": _now_ms(), "n": int(n)})
        return int(n)

    def broadcast_op(self, op: dict[str, Any]) -> dict[str, Any]:
        """Broadcast одной операции (set/delete)."""
        payload = dict(op)
        payload.setdefault("type", "p2p-op")
        payload["_sent_at"] = _now_ms()
        payload["_peer_id"] = self.peer_id
        try:
            self.host.broadcast("/fragilenotes/sync/1.0.0", payload)
        except Exception:
            pass
        try:
            self.transport.broadcast(payload)
        except Exception:
            pass
        with self._lock:
            self._log.append({"op": "broadcast_op", "at": _now_ms(), "rel_path": str(payload.get("rel_path", ""))[:60]})
        return payload

    def sync_with(self, other: VaultCRDT | P2PSyncService | dict[str, Any]) -> bool:
        """Двусторонний sync с другим состоянием/сервисом.

        Возвращает True если локальное состояние изменилось.
        """
        if isinstance(other, P2PSyncService):
            # двусторонний merge
            a_changed = self.state.merge(other.state)
            b_changed = other.state.merge(self.state)
            try:
                if a_changed:
                    self._file_sync.save()
                if b_changed:
                    other._file_sync.save()
            except Exception:
                pass
            with self._lock:
                self._log.append({"op": "sync_with_service", "at": _now_ms(), "peer_id": other.peer_id, "a_changed": bool(a_changed), "b_changed": bool(b_changed)})
            # уведомить колбэки
            if a_changed and self._on_sync is not None:
                try:
                    self._on_sync(self.state)
                except Exception:
                    pass
            if b_changed and other._on_sync is not None:
                try:
                    other._on_sync(other.state)
                except Exception:
                    pass
            return bool(a_changed or b_changed)
        if isinstance(other, VaultCRDT):
            before = self.state.to_dict()
            changed = self.state.merge(other)
            other.merge(VaultCRDT.from_dict(before))
            if changed:
                try:
                    self._file_sync.save()
                except Exception:
                    pass
                if self._on_sync is not None:
                    try:
                        self._on_sync(self.state)
                    except Exception:
                        pass
            with self._lock:
                self._log.append({"op": "sync_with_state", "at": _now_ms(), "changed": bool(changed)})
            return bool(changed)
        if isinstance(other, dict):
            return bool(self._handle_sync_payload(other))
        return False

    # alias для совместимости с NetworkSyncStub / canvas_collab
    def push(self) -> dict[str, Any]:
        self._file_sync.save()
        return self.broadcast_state()  # type: ignore[return-value]

    def pull(self, payload: dict[str, Any] | None = None) -> bool:
        if payload is None:
            return bool(self._file_sync.sync())
        return bool(self._handle_sync_payload(payload))

    def receive(self, payload: dict[str, Any]) -> bool:
        return bool(self._handle_sync_payload(payload))

    def sync(self, other: VaultCRDT | P2PSyncService | dict[str, Any] | None = None) -> bool:
        if other is not None:
            return self.sync_with(other)
        # sync со всеми подключенными peers (in-memory)
        any_changed = False
        for pid in list(self.host._connected.keys()):
            with _GLOBAL_HOST_LOCK:
                target = _GLOBAL_HOST_REGISTRY.get(pid)
            if target is not None and hasattr(target, "_p2p_service_ref"):
                try:
                    svc = target._p2p_service_ref  # type: ignore
                    if isinstance(svc, P2PSyncService):
                        any_changed = self.sync_with(svc) or any_changed
                except Exception:
                    continue
        # также sidecar
        try:
            if self._file_sync.sync():
                any_changed = True
        except Exception:
            pass
        return bool(any_changed)

    def connect_peer_transport(self, other: P2PSyncService) -> None:
        """Соединить два сервиса in-memory (host + transport + discovery)."""
        if other is self:
            return
        # host connect взаимно
        self.host.connect(other.peer_id)
        other.host.connect(self.peer_id)
        # transport peer
        self.transport.connect_peer(other.transport)
        other.transport.connect_peer(self.transport)
        # discovery add
        self.discovery.add_peer(PeerInfo(peer_id=other.peer_id, replica_id=other.replica_id, addresses=list(other.host.get_multiaddrs()), vault_id=other.vault_id, last_seen_ms=_now_ms()))
        other.discovery.add_peer(PeerInfo(peer_id=self.peer_id, replica_id=self.replica_id, addresses=list(self.host.get_multiaddrs()), vault_id=self.vault_id, last_seen_ms=_now_ms()))

    # ── callbacks ──
    def set_on_sync(self, cb: Callable[[VaultCRDT], None] | None) -> None:
        self._on_sync = cb

    def set_on_peer(self, cb: Callable[[PeerInfo], None] | None) -> None:
        self._on_peer = cb

    def on_peer_discovered(self, cb: Callable[[PeerInfo], None] | None) -> None:
        self.set_on_peer(cb)
        self.discovery.on_peer_discovered(cb)

    def on_sync(self, cb: Callable[[VaultCRDT], None] | None) -> None:
        self.set_on_sync(cb)

    # ── status ──
    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "started": self._started,
                "peer_id": self.peer_id,
                "replica_id": self.replica_id,
                "vault_id": self.vault_id,
                "vault_root": str(self.vault_root),
                "endpoint": self.config.endpoint,
                "listen_addrs": list(self.host.get_multiaddrs()),
                "peers": len(self.discovery.discover()),
                "connected": len(self.host.get_peers()),
                "files": len(self.state.list_files()),
                "vector": self.state.get_vector(),
                "log": len(self._log),
            }

    def get_log(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._log)

    def get_files(self) -> list[str]:
        return self.state.list_files()

    def get_file(self, rel_path: str) -> str | None:
        return self.state.get_file(rel_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_id": self.peer_id,
            "replica_id": self.replica_id,
            "vault_id": self.vault_id,
            "config": self.config.to_dict(),
            "state": self.state.to_dict(),
            "status": self.status(),
        }


# ── Привязка сервиса к host для sync() перебора ───────────────────────────
# (костыль для in-memory: host хранит ref на сервис если нужно)
_orig_host_start = LibP2PHost.start


def _patched_host_start(self: LibP2PHost) -> bool:  # type: ignore
    res = _orig_host_start(self)
    return res


# ── Helpers для files_view / app ───────────────────────────────────────────

def create_p2p_service(settings: dict[str, Any] | None = None, vault_root: Path | str | None = None) -> P2PSyncService:
    """Создать P2P сервис из settings."""
    return P2PSyncService(settings=settings, vault_root=vault_root)


def create_vault_crdt(vault_id: str = "vault", replica_id: str | None = None) -> VaultCRDT:
    return VaultCRDT(vault_id=vault_id, replica_id=replica_id or get_replica_id())


# алиасы фасада
P2PService = P2PSyncService
VaultP2PService = P2PSyncService
LibP2PSyncService = P2PSyncService
P2PSync = P2PSyncService
SyncService = P2PSyncService

# WebSocket алиасы уже выше
# Discovery алиасы уже выше

__all__ = [
    "get_replica_id",
    "get_peer_id",
    "PeerInfo",
    "P2PConfig",
    "FileEntry",
    "VaultCRDT",
    "VaultSyncState",
    "P2PDocument",
    "VaultState",
    "VaultFileSync",
    "LibP2PHost",
    "P2PHost",
    "Host",
    "P2PDiscovery",
    "PeerDiscovery",
    "DiscoveryService",
    "Discovery",
    "P2PWebSocketTransport",
    "WebSocketSyncStub",
    "P2PWebSocketSync",
    "WebSocketTransport",
    "P2PSyncService",
    "P2PService",
    "VaultP2PService",
    "LibP2PSyncService",
    "P2PSync",
    "SyncService",
    "create_p2p_service",
    "create_vault_crdt",
]
