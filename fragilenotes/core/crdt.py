"""CRDT для совместного редактирования текстовых заметок FragileNotes.

Простая CRDT (Conflict-free Replicated Data Type) — два уровня:

* **LWWDocument** — Last-Write-Wins регистр для целого текста заметки.
  Конфликт решается по (timestamp, replica_id). Достаточно для
  простых заметок; операции коммутативны и идемпотентны.

* **RGAText** (Sequence CRDT, Yjs/RGA-like) — посимвольная CRDT с
  tombstones. Каждый символ получает уникальный ``CharId(counter,
  replica)`` и позицию ``key`` (fractional index). Вставка между
  двумя соседями берёт midpoint ключа, удаление — tombstone.
  Merge — объединение множеств с сортировкой по ключу. Поддерживает
  concurrent вставки в одну позицию без потери данных.

Синхронизация:

* **FileSync** — sidecar файл ``<note>.crdt.json`` рядом с заметкой
  (или ``<note>.crdt``). Сохраняет сериализованное состояние CRDT.
  При открытии — merge локального текста с файлом-сигналом.
  При сохранении — запись sidecar + основной ``.md``.

* **NetworkSyncStub** — заглушка сетевой синхронизации (WebSocket /
  HTTP в будущем). Сейчас — in-memory канал + лог операций, API
  совместим с FileSync: ``push()``, ``pull()``, ``sync(other)``.

Интеграция с ``files_view`` — опциональна, включается флагом
``settings["crdt_enabled"]``. См. ``FilesView._crdt_*``.

Требования: только stdlib, py_compile без зависимостей.

Пример::

    from fragilenotes.core.crdt import LWWDocument, RGAText, FileSync

    doc = LWWDocument("note-1", replica_id="alice")
    doc.set_text("hello")
    other = LWWDocument("note-1", replica_id="bob")
    other.set_text("world")
    doc.merge(other)
    assert doc.text in ("hello", "world")  # LWW — побеждает большее время

    rga = RGAText("note-1", replica_id="alice")
    rga.local_insert(0, "Hi")
    rga2 = RGAText("note-1", replica_id="bob")
    rga2.merge(rga)
    rga2.local_insert(2, "!")
    rga.merge(rga2)
    assert rga.to_text() == "Hi!"
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── Утилиты ──────────────────────────────────────────────────────────

def get_replica_id(settings: dict[str, Any] | None = None) -> str:
    """Стабильный replica_id: из settings или генерируется."""
    if settings is not None:
        rid = settings.get("crdt_replica_id")
        if isinstance(rid, str) and rid.strip():
            return rid.strip()
    # генерируем и кэшируем в settings если переданы
    rid = uuid.uuid4().hex[:8]
    if settings is not None:
        try:
            settings["crdt_replica_id"] = rid
        except Exception:
            pass
    return rid


def _now_ms() -> int:
    return int(time.time() * 1000)


def _sidecar_path(note_path: Path) -> Path:
    """Sidecar для CRDT: ``note.md`` -> ``note.md.crdt.json``."""
    return Path(str(note_path) + ".crdt.json")


# ── LWWRegister ──────────────────────────────────────────────────────

@dataclass
class LWWRegister:
    """Last-Write-Wins регистр.

    Сравнение по (timestamp, replica_id) — лексикографически,
    replica_id — tie-breaker для детерминизма.
    """

    value: str = ""
    timestamp: int = 0
    replica_id: str = ""

    def set(self, value: str, timestamp: int | None = None, replica_id: str | None = None) -> None:
        ts = int(timestamp) if timestamp is not None else _now_ms()
        rid = replica_id if replica_id is not None else self.replica_id
        # LWW: побеждает большее время, при равенстве — больший replica_id
        if (ts, rid) >= (self.timestamp, self.replica_id):
            self.value = value
            self.timestamp = ts
            self.replica_id = rid

    def merge(self, other: LWWRegister) -> bool:
        """Слить other. Возвращает True если состояние изменилось."""
        if (other.timestamp, other.replica_id) > (self.timestamp, self.replica_id):
            self.value = other.value
            self.timestamp = other.timestamp
            self.replica_id = other.replica_id
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "timestamp": self.timestamp, "replica_id": self.replica_id}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LWWRegister:
        return cls(
            value=str(d.get("value", "")),
            timestamp=int(d.get("timestamp", 0)),
            replica_id=str(d.get("replica_id", "")),
        )


# ── LWWDocument ──────────────────────────────────────────────────────

class LWWDocument:
    """Документ заметки как один LWW-регистр (целый текст)."""

    def __init__(self, doc_id: str, replica_id: str | None = None) -> None:
        self.doc_id = str(doc_id)
        self.replica_id = replica_id or get_replica_id()
        self.register = LWWRegister(value="", timestamp=0, replica_id=self.replica_id)
        self._vector: dict[str, int] = {self.replica_id: 0}

    @property
    def text(self) -> str:
        return self.register.value

    def set_text(self, text: str, timestamp: int | None = None) -> None:
        ts = int(timestamp) if timestamp is not None else _now_ms()
        self.register.set(text, timestamp=ts, replica_id=self.replica_id)
        self._vector[self.replica_id] = max(self._vector.get(self.replica_id, 0), ts)

    def merge(self, other: LWWDocument) -> bool:
        """Коммутативный merge. Возвращает True если локальный текст изменился."""
        changed = self.register.merge(other.register)
        # vector clock — max
        for rid, ts in other._vector.items():
            self._vector[rid] = max(self._vector.get(rid, 0), int(ts))
        self._vector[self.replica_id] = max(self._vector.get(self.replica_id, 0), self.register.timestamp)
        return changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "lww",
            "doc_id": self.doc_id,
            "replica_id": self.replica_id,
            "register": self.register.to_dict(),
            "vector": dict(self._vector),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LWWDocument:
        doc = cls(str(d.get("doc_id", "")), replica_id=str(d.get("replica_id", "")) or None)
        reg = d.get("register")
        if isinstance(reg, dict):
            doc.register = LWWRegister.from_dict(reg)
        vec = d.get("vector")
        if isinstance(vec, dict):
            doc._vector = {str(k): int(v) for k, v in vec.items()}
        return doc

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, s: str) -> LWWDocument:
        return cls.from_dict(json.loads(s))


# ── RGAText (Sequence CRDT, Yjs-like) ────────────────────────────────

@dataclass(frozen=True, order=True)
class CharId:
    counter: int
    replica: str


@dataclass
class CharItem:
    id: CharId
    char: str  # один символ (может быть "" для head sentinel)
    deleted: bool = False
    timestamp: int = 0
    # fractional key для упорядочивания; чем меньше — тем левее
    key: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "counter": self.id.counter,
            "replica": self.id.replica,
            "char": self.char,
            "deleted": bool(self.deleted),
            "timestamp": int(self.timestamp),
            "key": float(self.key),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CharItem:
        return cls(
            id=CharId(int(d.get("counter", 0)), str(d.get("replica", ""))),
            char=str(d.get("char", "")),
            deleted=bool(d.get("deleted", False)),
            timestamp=int(d.get("timestamp", 0)),
            key=float(d.get("key", 0.0)),
        )


class RGAText:
    """Посимвольная Sequence CRDT (RGA-like).

    Хранит ``items`` — отсортированный по ``key`` список символов с
    tombstones. ``key`` — fractional index: при вставке между
    ``left_key`` и ``right_key`` берётся midpoint. Merge — объединение
    множеств, сортировка по (key, replica, counter) обеспечивает
    сходимость без координации.
    """

    def __init__(self, doc_id: str, replica_id: str | None = None) -> None:
        self.doc_id = str(doc_id)
        self.replica_id = replica_id or get_replica_id()
        self._counter = 0
        self._items: list[CharItem] = []
        self._vector: dict[str, int] = {self.replica_id: 0}

    # ── helpers ──
    def _next_id(self) -> CharId:
        self._counter += 1
        return CharId(self._counter, self.replica_id)

    def _sorted_items(self) -> list[CharItem]:
        return sorted(self._items, key=lambda it: (it.key, it.id.replica, it.id.counter))

    def _visible_items(self) -> list[CharItem]:
        return [it for it in self._sorted_items() if not it.deleted and it.char != ""]

    # ── rebalance: защита от float key коллизий ─────────────────────
    _REBALANCE_EPS: float = 1e-9

    def _needs_rebalance(self) -> bool:
        """Проверить, что fractional keys слишком близко (коллизия float)."""
        sorted_items = self._sorted_items()
        for a, b in zip(sorted_items, sorted_items[1:]):
            if abs(b.key - a.key) < self._REBALANCE_EPS:
                return True
        return False

    def _rebalance(self) -> None:
        """Перераспределить ключи равномерно с шагом 1.0, сохраняя порядок."""
        sorted_items = self._sorted_items()
        for idx, it in enumerate(sorted_items):
            it.key = float(idx + 1)

    def rebalance(self) -> None:
        """Публичный ребаланс (вызывается извне или при коллизии)."""
        self._rebalance()

    def _maybe_rebalance(self) -> bool:
        """Ребаланс если нужна, возвращает True если был выполнен."""
        if self._needs_rebalance():
            self._rebalance()
            return True
        return False

    # ── public API ──
    def to_text(self) -> str:
        return "".join(it.char for it in self._visible_items())

    # alias ожидаемый в files_view / тестах
    @property
    def text(self) -> str:  # pragma: no cover - alias
        return self.to_text()

    def get_text(self) -> str:
        return self.to_text()

    def set_text(self, text: str) -> None:
        """Полная перезапись через LWW-логику: очищает и вставляет заново.

        Для простоты — удаляет все видимые символы (tombstone) и
        вставляет новый текст последовательно.
        """
        # tombstone все видимые
        ts = _now_ms()
        for it in self._items:
            if not it.deleted and it.char != "":
                it.deleted = True
                it.timestamp = max(it.timestamp, ts)
        # вставляем новый текст
        self._items = [it for it in self._items if it.deleted]  # оставляем tombstones
        # сбрасываем ключи — вставляем с шагом 1.0
        self._items.sort(key=lambda it: (it.key, it.id.replica, it.id.counter))
        # новые ключи после максимального
        base = max((it.key for it in self._items), default=0.0)
        for i, ch in enumerate(text):
            cid = self._next_id()
            key = base + 1.0 + float(i)
            self._items.append(CharItem(id=cid, char=ch, deleted=False, timestamp=ts, key=key))
        self._vector[self.replica_id] = max(self._vector.get(self.replica_id, 0), ts)
        # пересортировать не нужно — ключи уже упорядочены

    def local_insert(self, offset: int, text: str) -> None:
        """Вставить text в позицию offset (по видимым символам)."""
        if not text:
            return
        visible = self._visible_items()
        # clamp offset
        offset = max(0, min(int(offset), len(visible)))
        # определить ключи соседей
        if not visible:
            left_key = 0.0
            right_key = 1.0  # будет пересчитано на каждый символ
            # для пустого — последовательные ключи 0.5, 1.5, ...
            # найдём базу от tombstones
            base = max((it.key for it in self._items), default=0.0)
            left_key = base
            right_key = base + float(len(text)) + 1.0
        else:
            if offset == 0:
                # перед первым видимым
                first = visible[0]
                left_key = first.key - 1.0
                # найти предыдущий ключ (включая удалённые) меньше first.key
                # упростим: left = first.key - 1
                right_key = first.key
            elif offset >= len(visible):
                last = visible[-1]
                left_key = last.key
                right_key = last.key + 1.0
                # расширить если есть элементы после last (удалённые с большим key)
                max_key = max((it.key for it in self._items), default=last.key)
                if max_key > last.key:
                    right_key = max_key + 1.0
            else:
                left_item = visible[offset - 1]
                right_item = visible[offset]
                left_key = left_item.key
                right_key = right_item.key
        ts = _now_ms()
        n = len(text)
        for i, ch in enumerate(text):
            # fractional midpoint разделенный на n частей
            if n == 1:
                key = (left_key + right_key) / 2.0
            else:
                # равномерно распределить между соседями
                frac = (i + 1) / (n + 1)
                key = left_key + (right_key - left_key) * frac
                # tie-breaker микродобавка чтобы ключи не совпали при float совпадении
                key += (i * 1e-9)
            cid = self._next_id()
            self._items.append(CharItem(id=cid, char=ch, deleted=False, timestamp=ts, key=key))
        self._vector[self.replica_id] = max(self._vector.get(self.replica_id, 0), ts)
        # защита от float-коллизий: если ключи слишком близко — ребаланс
        self._maybe_rebalance()

    def local_delete(self, offset: int, length: int = 1) -> None:
        """Удалить length символов начиная с offset (visible)."""
        if length <= 0:
            return
        visible = self._visible_items()
        offset = max(0, min(int(offset), len(visible)))
        end = min(offset + int(length), len(visible))
        if offset >= end:
            return
        to_delete = visible[offset:end]
        ts = _now_ms()
        # отметить tombstone по id
        id_set = {it.id for it in to_delete}
        for it in self._items:
            if it.id in id_set and not it.deleted:
                # LWW для удаления — побеждает большее время
                if ts >= it.timestamp:
                    it.deleted = True
                    it.timestamp = ts
        self._vector[self.replica_id] = max(self._vector.get(self.replica_id, 0), ts)

    def merge(self, other: RGAText) -> bool:
        """Merge state-based: union items, LWW для deleted."""
        if other.doc_id != self.doc_id:
            # разные документы — игнорируем doc_id, но мерджим как если бы один
            pass
        # индекс по CharId
        index: dict[CharId, CharItem] = {it.id: it for it in self._items}
        changed = False
        for oit in other._items:
            sit = index.get(oit.id)
            if sit is None:
                # новый символ — копируем
                self._items.append(CharItem(
                    id=oit.id, char=oit.char, deleted=oit.deleted,
                    timestamp=oit.timestamp, key=oit.key,
                ))
                changed = True
            else:
                # конфликт deleted — LWW по timestamp, tie-breaker replica
                if oit.deleted != sit.deleted:
                    # кто новее — тот и побеждает
                    if (oit.timestamp, oit.id.replica) > (sit.timestamp, sit.id.replica):
                        sit.deleted = oit.deleted
                        sit.timestamp = oit.timestamp
                        changed = True
                elif oit.deleted and sit.deleted:
                    # оба удалены — max timestamp
                    if oit.timestamp > sit.timestamp:
                        sit.timestamp = oit.timestamp
                        changed = True
                # key/char не меняем — они иммутабельны
        # обновить counter — максимум по реплике
        # для каждой реплики найдём max counter
        counters: dict[str, int] = {}
        for it in self._items:
            counters[it.id.replica] = max(counters.get(it.id.replica, 0), it.id.counter)
        # локальный counter должен быть >= максимума своей реплики
        my_max = counters.get(self.replica_id, 0)
        if my_max > self._counter:
            self._counter = my_max
        # vector clock max
        for rid, ts in other._vector.items():
            if ts > self._vector.get(rid, 0):
                self._vector[rid] = int(ts)
                changed = True
        # защита от коллизий после merge — ребаланс если нужно
        if self._maybe_rebalance():
            changed = True
        # также обновить вектор своим последним ts
        # сортировка не хранится — вычисляется лениво
        return changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "rga",
            "doc_id": self.doc_id,
            "replica_id": self.replica_id,
            "counter": int(self._counter),
            "vector": dict(self._vector),
            "items": [it.to_dict() for it in self._items],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RGAText:
        doc = cls(str(d.get("doc_id", "")), replica_id=str(d.get("replica_id", "")) or None)
        doc._counter = int(d.get("counter", 0))
        vec = d.get("vector")
        if isinstance(vec, dict):
            doc._vector = {str(k): int(v) for k, v in vec.items()}
        items = d.get("items")
        if isinstance(items, list):
            doc._items = [CharItem.from_dict(x) for x in items if isinstance(x, dict)]
        return doc

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, s: str) -> RGAText:
        return cls.from_dict(json.loads(s))


# ── Алиасы для удобства ─────────────────────────────────────────────

# CRDTDocument — по умолчанию LWW (простой), но можно выбрать RGA через параметр
CRDTDocument = LWWDocument
SequenceCRDT = RGAText
TextCRDT = RGAText


def create_document(doc_id: str, replica_id: str | None = None, kind: str = "lww") -> LWWDocument | RGAText:
    """Фабрика документов.

    kind: "lww" -> LWWDocument, "rga"/"yjs"/"seq" -> RGAText
    """
    k = str(kind).lower()
    if k in ("rga", "yjs", "seq", "sequence", "text"):
        return RGAText(doc_id, replica_id=replica_id)
    return LWWDocument(doc_id, replica_id=replica_id)


# ── Синхронизация через файл ────────────────────────────────────────

class FileSync:
    """Синхронизация CRDT через sidecar-файл.

    Sidecar — ``<note>.crdt.json`` с сериализованным состоянием.
    Операции: ``save()``, ``load()``, ``sync()`` (load+merge+save).

    Поддерживает оба типа документов (lww/rga) — тип определяется полем
    ``type`` в JSON.
    """

    def __init__(self, doc: LWWDocument | RGAText, note_path: Path | str) -> None:
        self.doc = doc
        self.note_path = Path(note_path)
        self.sidecar = _sidecar_path(self.note_path)

    def save(self) -> Path:
        """Сохранить состояние doc в sidecar (атомарно)."""
        data = self.doc.to_dict()
        # добавить mtime для отладки
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

    def load_doc(self) -> LWWDocument | RGAText | None:
        d = self.load()
        if not isinstance(d, dict):
            return None
        t = str(d.get("type", "lww")).lower()
        try:
            if t in ("rga", "yjs", "seq"):
                return RGAText.from_dict(d)
            return LWWDocument.from_dict(d)
        except Exception:
            return None

    def sync(self) -> bool:
        """Загрузить sidecar, слить с локальным doc, сохранить если изменилось.

        Возвращает True если локальный doc изменился (был merge).
        """
        other = self.load_doc()
        if other is None:
            # sidecar нет — просто сохранить локальное
            try:
                self.save()
            except Exception:
                pass
            return False
        # типы должны совпадать — если нет, приводим через текст
        if type(other) is not type(self.doc):
            # перенос текста через LWW логику
            try:
                other_text = other.text if hasattr(other, "text") else str(other.to_dict().get("register", {}).get("value", ""))
                self_text = self.doc.text if hasattr(self.doc, "text") else ""
                if other_text != self_text:
                    # выбираем LWW — более свежий
                    other_ts = getattr(getattr(other, "register", None), "timestamp", 0) if hasattr(other, "register") else 0
                    self_ts = getattr(getattr(self.doc, "register", None), "timestamp", 0) if hasattr(self.doc, "register") else 0
                    if other_ts > self_ts:
                        if hasattr(self.doc, "set_text"):
                            self.doc.set_text(other_text)
                        self.save()
                        return True
                    else:
                        self.save()
            except Exception:
                pass
            return False
        # один тип — обычный merge
        before = self.doc.text if hasattr(self.doc, "text") else ""
        changed = self.doc.merge(other)  # type: ignore[arg-type]
        if changed:
            try:
                self.save()
            except Exception:
                pass
            return True
        # даже без изменений — если sidecar устарел (вектор меньше) — перезаписать
        try:
            # сравнить сериализацию — если отличается, сохранить
            cur = self.doc.to_dict()
            if cur != other.to_dict():
                self.save()
        except Exception:
            pass
        return False

    # alias
    sync_from_file = sync
    save_to_file = save


# alias для обратной совместимости
FileCRDTSync = FileSync


# ── Заглушка сетевой синхронизации ──────────────────────────────────

class NetworkSyncStub:
    """Заглушка сети: in-memory канал, лог операций.

    В будущем заменить на WebSocket/HTTP. Сейчас хранит
    ``peers`` и умеет ``push``/``pull``/``sync`` между документами.
    """

    def __init__(self, doc: LWWDocument | RGAText, endpoint: str | None = None) -> None:
        self.doc = doc
        self.endpoint = endpoint or "stub://local"
        self._peers: list[LWWDocument | RGAText] = []
        self._log: list[dict[str, Any]] = []

    def push(self) -> dict[str, Any]:
        """Сериализовать и 'отправить' — возвращает payload."""
        payload = self.doc.to_dict()
        payload["_pushed_at"] = _now_ms()
        payload["_endpoint"] = self.endpoint
        self._log.append({"op": "push", "at": _now_ms(), "doc_id": self.doc.doc_id})
        # в реале: http post / ws send
        return payload

    def pull(self, payload: dict[str, Any] | None = None) -> bool:
        """'Получить' payload и слить. Если payload None — no-op."""
        if not isinstance(payload, dict):
            self._log.append({"op": "pull", "at": _now_ms(), "doc_id": self.doc.doc_id, "noop": True})
            return False
        t = str(payload.get("type", "lww")).lower()
        try:
            if t in ("rga", "yjs", "seq"):
                other = RGAText.from_dict(payload)
            else:
                other = LWWDocument.from_dict(payload)
        except Exception:
            return False
        if type(other) is not type(self.doc):
            return False
        changed = self.doc.merge(other)  # type: ignore[arg-type]
        self._log.append({"op": "pull", "at": _now_ms(), "doc_id": self.doc.doc_id, "changed": bool(changed)})
        return bool(changed)

    def sync(self, other_doc: LWWDocument | RGAText | None = None) -> bool:
        """Двусторонний sync с другим документом (или peers)."""
        if other_doc is not None:
            a_changed = self.doc.merge(other_doc)  # type: ignore[arg-type]
            b_changed = other_doc.merge(self.doc)  # type: ignore[arg-type]
            self._log.append({"op": "sync", "at": _now_ms(), "a_changed": bool(a_changed), "b_changed": bool(b_changed)})
            return bool(a_changed or b_changed)
        # sync со всеми peers
        any_changed = False
        for peer in list(self._peers):
            if type(peer) is not type(self.doc):
                continue
            c1 = self.doc.merge(peer)  # type: ignore[arg-type]
            c2 = peer.merge(self.doc)  # type: ignore[arg-type]
            any_changed = any_changed or bool(c1 or c2)
        if any_changed:
            self._log.append({"op": "sync_peers", "at": _now_ms(), "changed": True})
        return any_changed

    def add_peer(self, peer: LWWDocument | RGAText) -> None:
        if peer is not self.doc and peer not in self._peers:
            self._peers.append(peer)

    def get_log(self) -> list[dict[str, Any]]:
        return list(self._log)

    # alias
    connect = add_peer


# ── CRDTManager ──────────────────────────────────────────────────────

class CRDTManager:
    """Управляет множеством документов заметок."""

    def __init__(self, replica_id: str | None = None, kind: str = "lww") -> None:
        self.replica_id = replica_id or get_replica_id()
        self.kind = str(kind).lower()
        self._docs: dict[str, LWWDocument | RGAText] = {}

    def get_or_create(self, doc_id: str, note_path: Path | str | None = None) -> LWWDocument | RGAText:
        key = str(doc_id)
        if key in self._docs:
            return self._docs[key]
        doc = create_document(key, replica_id=self.replica_id, kind=self.kind)
        # попытаться загрузить sidecar если указан путь
        if note_path is not None:
            sync = FileSync(doc, Path(note_path))
            loaded = sync.load_doc()
            if loaded is not None and type(loaded) is type(doc):
                doc.merge(loaded)  # type: ignore[arg-type]
        self._docs[key] = doc
        return doc

    def get(self, doc_id: str) -> LWWDocument | RGAText | None:
        return self._docs.get(str(doc_id))

    def merge_payload(self, doc_id: str, payload: dict[str, Any]) -> bool:
        doc = self.get_or_create(doc_id)
        t = str(payload.get("type", "lww")).lower()
        try:
            if t in ("rga", "yjs", "seq"):
                other = RGAText.from_dict(payload)
            else:
                other = LWWDocument.from_dict(payload)
        except Exception:
            return False
        if type(other) is not type(doc):
            return False
        return bool(doc.merge(other))  # type: ignore[arg-type]

    def all_docs(self) -> dict[str, LWWDocument | RGAText]:
        return dict(self._docs)


__all__ = [
    "get_replica_id",
    "LWWRegister",
    "LWWDocument",
    "RGAText",
    "CRDTDocument",
    "SequenceCRDT",
    "TextCRDT",
    "CharId",
    "CharItem",
    "create_document",
    "FileSync",
    "FileCRDTSync",
    "NetworkSyncStub",
    "CRDTManager",
]
