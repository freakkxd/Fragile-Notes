"""Spaced Repetition SM-2 для FragileNotes.

Карточки Q::A в заметках vault, алгоритм SM-2, хранение в vault/_System/SRS/.

Формат карточки в markdown:  Вопрос::Ответ
  - одна строка = одна карточка, разделитель :: (первое вхождение)
  - учитывается за пределами ``` блоков и `inline code`
  - карточки из vault/_System/ игнорируются (включая саму папку SRS)

Хранение: vault/_System/SRS/srs.json  { version, cards: {id: {ef, interval, reps, due, last, lapses, question, answer, source, line_no}} }
Плюс кэш расписания; текст карточки всегда парсится из заметок (источник истины — файл).

SM-2 (SuperMemo 2):
  EF' = EF + (0.1 - (5-q)*(0.08+(5-q)*0.02)),  EF>=1.3
  если q<3: reps=0, interval=1 (переучивание)
  иначе:
    reps==0 -> interval=1
    reps==1 -> interval=6
    иначе interval = round(interval * EF)
    reps += 1
  due = today + interval
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── константы ───────────────────────────────────────────────────────

SRS_REL = "_System/SRS"
DB_NAME = "srs.json"

# качества SM-2 0..5
GRADE_AGAIN = 0  # забыл
GRADE_HARD = 3
GRADE_GOOD = 4
GRADE_EASY = 5

GRADE_LABELS = {
    GRADE_AGAIN: "Снова",
    GRADE_HARD: "Трудно",
    GRADE_GOOD: "Хорошо",
    GRADE_EASY: "Легко",
}

DEFAULT_EF = 2.5
MIN_EF = 1.3

# паттерны
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

# для определения строк внутри ``` блоков при сохранении номеров строк —
# простой state-машина по строкам, а не regex на весь текст
HEAVY_DIRS = {
    "node_modules", ".git", "dist", "build", "target", ".venv", "venv",
    "__pycache__", "bin", "obj", ".cache", ".trash",
    ".obsidian", "Trash", "images", "ao-engine",
}

# ── пути ─────────────────────────────────────────────────────────────

def get_srs_dir(settings: dict) -> Path:
    """Папка SRS: vault/_System/SRS/."""
    root = Path(str(settings.get("vault_root") or Path.home() / "desktop"))
    return root / SRS_REL


def get_srs_db_path(settings: dict) -> Path:
    return get_srs_dir(settings) / DB_NAME


def ensure_srs_dir(settings: dict) -> Path:
    p = get_srs_dir(settings)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _vault_root(settings: dict) -> Path:
    return Path(str(settings.get("vault_root") or Path.home() / "desktop"))

# ── модель ───────────────────────────────────────────────────────────

@dataclass
class SrsCard:
    id: str
    question: str
    answer: str
    source: Path
    line_no: int  # 1-indexed
    ef: float = DEFAULT_EF
    interval: int = 0  # дни
    reps: int = 0
    due: datetime.date | None = None
    last_reviewed: datetime.date | None = None
    lapses: int = 0

    @property
    def is_new(self) -> bool:
        return self.reps == 0 and self.interval == 0

    def is_due(self, today: datetime.date | None = None) -> bool:
        if today is None:
            today = datetime.date.today()
        if self.due is None:
            return True  # новые — к повторению сразу
        return self.due <= today

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "answer": self.answer,
            "source": str(self.source),
            "line_no": self.line_no,
            "ef": self.ef,
            "interval": self.interval,
            "reps": self.reps,
            "due": self.due.isoformat() if self.due else None,
            "last_reviewed": self.last_reviewed.isoformat() if self.last_reviewed else None,
            "lapses": self.lapses,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SrsCard:
        def _pd(v: Any) -> datetime.date | None:
            if not v:
                return None
            try:
                return datetime.date.fromisoformat(str(v).split("T")[0])
            except Exception:
                return None
        return cls(
            id=str(data.get("id") or ""),
            question=str(data.get("question") or ""),
            answer=str(data.get("answer") or ""),
            source=Path(str(data.get("source") or "")),
            line_no=int(data.get("line_no") or 0),
            ef=float(data.get("ef") or DEFAULT_EF),
            interval=int(data.get("interval") or 0),
            reps=int(data.get("reps") or 0),
            due=_pd(data.get("due")),
            last_reviewed=_pd(data.get("last_reviewed")),
            lapses=int(data.get("lapses") or 0),
        )


# ── SM-2 ─────────────────────────────────────────────────────────────

def _calc_ef(ef: float, quality: int) -> float:
    q = max(0, min(5, int(quality)))
    new_ef = ef + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    return max(MIN_EF, new_ef)


def sm2_update(card: SrsCard, quality: int, today: datetime.date | None = None) -> SrsCard:
    """Применить SM-2 к карточке, мутируя её и возвращая ту же.

    quality 0..5, <3 = забыл/ошибка.
    """
    if today is None:
        today = datetime.date.today()
    q = max(0, min(5, int(quality)))
    # lapses
    if q < 3:
        card.lapses += 1
        card.reps = 0
        card.interval = 1
    else:
        if card.reps == 0:
            card.interval = 1
        elif card.reps == 1:
            card.interval = 6
        else:
            card.interval = max(1, round(card.interval * card.ef))
        card.reps += 1
    card.ef = _calc_ef(card.ef, q)
    card.due = today + datetime.timedelta(days=card.interval)
    card.last_reviewed = today
    return card


# alias для совместимости
def update_card_sm2(card: SrsCard, quality: int, today: datetime.date | None = None) -> SrsCard:
    return sm2_update(card, quality, today)


# ── парсинг Q::A ─────────────────────────────────────────────────────

def _card_id(source_rel: str, line_no: int, question: str) -> str:
    """Детерминированный id карточки (12 hex)."""
    h = hashlib.sha256(f"{source_rel}::{line_no}::{question}".encode()).hexdigest()
    return h[:12]


def _strip_code_blocks_per_line(lines: list[str]) -> list[bool]:
    """Для каждой строки — внутри ли она ``` блока (True — пропустить)."""
    inside = False
    out: list[bool] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            inside = not inside
            out.append(True)  # саму границу пропускаем
            continue
        out.append(inside)
    return out


def extract_cards_from_text(text: str, source: Path) -> list[tuple[str, str, int]]:
    """Извлечь (question, answer, line_no) из текста.

    Пропускает ``` блоки. Игнорирует пустые Q/A и строки без ::.
    Поддерживает префикс списка '- ' / '* ' / '1. '.
    """
    lines = text.splitlines()
    skip = _strip_code_blocks_per_line(lines)
    out: list[tuple[str, str, int]] = []
    for idx, raw in enumerate(lines, start=1):
        if skip[idx - 1]:
            continue
        # убрать inline code для проверки? но :: внутри `code` не должна быть карточкой
        # если вся строка внутри `...` — пропускаем грубый эвристический check
        # проще: если :: внутри `...` — считаем что код, пропустим если количество ` нечётно до ::
        # достаточно: удалить `...` сегменты и проверять на оставшемся
        cleaned_for_check = _INLINE_CODE_RE.sub("", raw)
        if "::" not in cleaned_for_check:
            continue
        # оригинальный raw для split — но :: внутри `code` уже удалён из проверки, а split делаем по оригиналу
        # найдём первую :: вне `code` — упрощение: берём первую :: в cleaned и мапим? Проще искать в raw но игнорировать если внутри backticks.
        # Эвристика: если в raw есть ` и :: между ними — считаем карточку недействительной
        # Для простоты: если raw != cleaned_for_check и "::" только внутри удалённого сегмента — уже отфильтровано выше
        # иначе берём первую :: в raw
        if "::" not in raw:
            continue
        # префикс списка
        line = raw.strip()
        # убрать маркеры списка
        #  "- ", "* ", "+ ", "1. ", "1) "
        line = re.sub(r"^[-*+]\s+", "", line)
        line = re.sub(r"^\d+[\.\)]\s+", "", line)
        if "::" not in line:
            continue
        # фронtmаттер разделитель --- — пропускаем
        if line.startswith("---"):
            continue
        q, a = line.split("::", 1)
        q = q.strip()
        a = a.strip()
        if not q or not a:
            continue
        # заголовки "# Q::A" — убрать #
        q = q.lstrip("#").strip()
        if not q:
            continue
        # ограничение длины
        if len(q) > 500:
            q = q[:500]
        if len(a) > 2000:
            a = a[:2000]
        out.append((q, a, idx))
    return out


def extract_cards(text: str) -> list[tuple[str, str]]:
    """Упрощённая версия без source — для тестов."""
    dummy = Path("note.md")
    return [(q, a) for q, a, _ in extract_cards_from_text(text, dummy)]


# ── сканирование vault ───────────────────────────────────────────────

def _iter_notes(root: Path) -> list[tuple[str, float]]:
    """Обход vault для карточек: все .md кроме HEAVY_DIRS и _System."""
    out: list[tuple[str, float]] = []
    skip_system = root / "_System"
    skip_system_str = str(skip_system)
    stack = [root]
    while stack:
        d = stack.pop()
        # пропуск _System целиком — но подпапку SRS мы не сканируем на карточки
        # (cards в SRS не должны считаться карточками)
        if str(d) == skip_system_str or str(d).startswith(skip_system_str + os.sep):
            # разрешаем обход _System для других целей? для карточек — пропускаем весь _System
            continue
        try:
            with os.scandir(d) as it:
                for e in it:
                    name = e.name
                    if name.startswith(".") or name in HEAVY_DIRS:
                        continue
                    # пропуск _System на уровне имени
                    if name == "_System":
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False) and name.lower().endswith(".md"):
                            out.append((e.path, e.stat().st_mtime))
                    except OSError:
                        continue
        except OSError:
            continue
    return out


# ── БД ───────────────────────────────────────────────────────────────

_lock = threading.RLock()

def _load_raw_db(settings: dict) -> dict[str, Any]:
    p = get_srs_db_path(settings)
    if not p.is_file():
        return {"version": 1, "cards": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"version": 1, "cards": {}}
        if "cards" not in data or not isinstance(data["cards"], dict):
            data["cards"] = {}
        return data
    except Exception:
        return {"version": 1, "cards": {}}


def _save_raw_db(settings: dict, data: dict[str, Any]) -> None:
    p = ensure_srs_dir(settings) / DB_NAME
    # атомарная запись
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.replace(p)
    except OSError:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_db(settings: dict) -> dict[str, dict[str, Any]]:
    """Загрузить расписание: {id -> dict}."""
    raw = _load_raw_db(settings)
    return dict(raw.get("cards") or {})


def save_db(settings: dict, cards_map: dict[str, dict[str, Any]]) -> None:
    raw = _load_raw_db(settings)
    raw["cards"] = dict(cards_map)
    raw["version"] = 1
    with _lock:
        _save_raw_db(settings, raw)


def _today_or(d: datetime.date | None) -> datetime.date:
    return d or datetime.date.today()

# ── высокоуровневое API ─────────────────────────────────────────────

def scan_cards(settings: dict, today: datetime.date | None = None) -> list[SrsCard]:
    """Сканировать vault на Q::A и смержить с расписанием из srs.json."""
    today = _today_or(today)
    root = _vault_root(settings)
    db = load_db(settings)
    # карта source_rel для стабильного id
    cards: list[SrsCard] = []
    seen_ids: set[str] = set()
    for path_str, _mt in _iter_notes(root):
        p = Path(path_str)
        try:
            rel = p.relative_to(root).as_posix()
        except Exception:
            rel = p.name
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for q, a, ln in extract_cards_from_text(text, p):
            cid = _card_id(rel, ln, q)
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            # расписание из БД
            sched = db.get(cid)
            if sched is not None:
                try:
                    ef = float(sched.get("ef", DEFAULT_EF))
                    interval = int(sched.get("interval", 0))
                    reps = int(sched.get("reps", 0))
                    due = datetime.date.fromisoformat(str(sched["due"])) if sched.get("due") else today
                    last = datetime.date.fromisoformat(str(sched["last_reviewed"])) if sched.get("last_reviewed") else None
                    lapses = int(sched.get("lapses", 0))
                except Exception:
                    ef, interval, reps, due, last, lapses = DEFAULT_EF, 0, 0, today, None, 0
                # вопрос/ответ могут обновиться в файле — берём из текста, но ef/interval сохраняем
                card = SrsCard(id=cid, question=q, answer=a, source=p, line_no=ln,
                               ef=ef, interval=interval, reps=reps, due=due, last_reviewed=last, lapses=lapses)
            else:
                card = SrsCard(id=cid, question=q, answer=a, source=p, line_no=ln,
                               ef=DEFAULT_EF, interval=0, reps=0, due=today, last_reviewed=None, lapses=0)
            cards.append(card)
    # также карточки удалённые из заметок но есть в БД — не показываем (они висят в БД, но без source)
    # сортировка: сначала просроченные/сегодня, затем новые, затем будущие
    def _sort_key(c: SrsCard):
        due_d = c.due or today
        is_new = 1 if c.is_new else 0
        # новые после due, но перед будущими
        # due раньше -> меньше
        return (due_d, is_new, c.question.lower())
    cards.sort(key=_sort_key)
    return cards


# алиасы
def get_all_cards(settings: dict, today: datetime.date | None = None) -> list[SrsCard]:
    return scan_cards(settings, today)


def get_cards(settings: dict, today: datetime.date | None = None) -> list[SrsCard]:
    return scan_cards(settings, today)


def get_due_cards(settings: dict, today: datetime.date | None = None) -> list[SrsCard]:
    today = _today_or(today)
    return [c for c in scan_cards(settings, today) if c.is_due(today)]


def get_new_cards(settings: dict, today: datetime.date | None = None) -> list[SrsCard]:
    return [c for c in scan_cards(settings, today) if c.is_new]


def get_stats(settings: dict, today: datetime.date | None = None) -> dict[str, int]:
    """Статистика для панели."""
    today = _today_or(today)
    cards = scan_cards(settings, today)
    total = len(cards)
    due = sum(1 for c in cards if c.is_due(today))
    new = sum(1 for c in cards if c.is_new)
    learned = sum(1 for c in cards if not c.is_new and not c.is_due(today))
    overdue = sum(1 for c in cards if c.due is not None and c.due < today)
    return {
        "total": total,
        "due": due,
        "new": new,
        "learned": learned,
        "overdue": overdue,
    }


def review_card(settings: dict, card_id: str, quality: int, today: datetime.date | None = None) -> SrsCard | None:
    """Оценить карточку (SM-2) и сохранить расписание. Возвращает обновлённую карточку или None."""
    today = _today_or(today)
    cards = scan_cards(settings, today)
    target: SrsCard | None = None
    for c in cards:
        if c.id == card_id:
            target = c
            break
    if target is None:
        # может быть карточка удалена — пробуем загрузить из БД напрямую
        db = load_db(settings)
        sched = db.get(card_id)
        if sched is None:
            return None
        # реконструируемcard из БД
        q = str(sched.get("question") or "")
        a = str(sched.get("answer") or "")
        src = Path(str(sched.get("source") or ""))
        ln = int(sched.get("line_no") or 0)
        try:
            ef = float(sched.get("ef", DEFAULT_EF))
            interval = int(sched.get("interval", 0))
            reps = int(sched.get("reps", 0))
            due = datetime.date.fromisoformat(str(sched["due"])) if sched.get("due") else today
            last = datetime.date.fromisoformat(str(sched["last_reviewed"])) if sched.get("last_reviewed") else None
            lapses = int(sched.get("lapses", 0))
        except Exception:
            return None
        target = SrsCard(id=card_id, question=q, answer=a, source=src, line_no=ln,
                         ef=ef, interval=interval, reps=reps, due=due, last_reviewed=last, lapses=lapses)
    sm2_update(target, quality, today)
    # сохранить
    with _lock:
        db = load_db(settings)
        db[target.id] = {
            "ef": target.ef,
            "interval": target.interval,
            "reps": target.reps,
            "due": target.due.isoformat() if target.due else None,
            "last_reviewed": target.last_reviewed.isoformat() if target.last_reviewed else None,
            "lapses": target.lapses,
            "question": target.question,
            "answer": target.answer,
            "source": str(target.source),
            "line_no": target.line_no,
        }
        save_db(settings, db)
    # инвалидация кэшей vault (карточки могут быть использованы в других вьюхах)
    try:
        from ..services import vault as _vault
        _vault.invalidate_vault_cache()
    except Exception:
        pass
    return target


def reset_card(settings: dict, card_id: str) -> bool:
    """Сброс расписания карточки (как новая)."""
    with _lock:
        db = load_db(settings)
        if card_id not in db:
            return False
        db.pop(card_id, None)
        save_db(settings, db)
    return True


def parse_cards(text: str) -> list[tuple[str, str]]:
    """Публичный парсер для тестов (без источника)."""
    return extract_cards(text)


__all__ = [
    "SrsCard",
    "SRS_REL",
    "DB_NAME",
    "GRADE_AGAIN",
    "GRADE_HARD",
    "GRADE_GOOD",
    "GRADE_EASY",
    "GRADE_LABELS",
    "DEFAULT_EF",
    "MIN_EF",
    "get_srs_dir",
    "get_srs_db_path",
    "ensure_srs_dir",
    "sm2_update",
    "update_card_sm2",
    "extract_cards_from_text",
    "extract_cards",
    "parse_cards",
    "scan_cards",
    "get_all_cards",
    "get_cards",
    "get_due_cards",
    "get_new_cards",
    "get_stats",
    "load_db",
    "save_db",
    "review_card",
    "reset_card",
]
