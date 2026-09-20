"""Ядро Task Manager (Fragilich Suite): модель задач, рутины, счета, журналы.

Порт чистой логики из fragilich-suite (routineSchedule, taskRules, frontmatter).
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from ..vault import parse_date, read_md, write_md

REGULAR = "regular"
ROUTINE = "routine"
BILL = "bill"

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"

# Индекс дня недели рутины: 0=понедельник … 6=воскресенье
# Python date.weekday() уже даёт Mon=0…Sun=6 (совпадает с jsDayToRoutineIndex)
_ROUTINE_WD = (0, 1, 2, 3, 4, 5, 6)


def ymd(d: date | None) -> str | None:
    return d.isoformat() if d else None


def parse_ymd(s: str | None) -> date | None:
    return parse_date(s)


def js_day_to_routine_index(d: date) -> int:
    return _ROUTINE_WD[d.weekday()]


def _add_days(d: date, days: int) -> date:
    return d + timedelta(days=days)


def _add_months_clamp_day(d: date, months: int, day_of_month: int) -> date:
    dom = max(1, min(int(day_of_month), 31))
    month0 = d.month - 1 + months
    year = d.year + month0 // 12
    month = month0 % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(dom, last))


def next_by_weekdays(from_: date, indices: list[int]) -> date:
    sorted_idx = sorted(i for i in indices if 0 <= i <= 6)
    if not sorted_idx:
        return _add_days(from_, 1)
    cur = js_day_to_routine_index(from_)
    for idx in sorted_idx:
        if idx > cur:
            return _add_days(from_, idx - cur)
    first = sorted_idx[0]
    return _add_days(from_, 7 - cur + first)


def compute_next_routine_date_after(
    from_ymd: str, recurrence_kind: str, interval_n: int = 1,
    weekday_indices: list[int] | None = None, month_day: int | None = None,
) -> str:
    """Следующая дата рутины строго после from_ymd (порт routineSchedule.ts)."""
    from_ = parse_ymd(from_ymd)
    if not from_:
        return from_ymd
    interval = max(1, int(interval_n) or 1)
    wd = weekday_indices or []
    md = month_day if month_day is not None and 1 <= month_day <= 31 else from_.day

    kind = recurrence_kind or "flexible"
    if kind in ("daily", "every_n_days", "flexible"):
        next_d = _add_days(from_, interval)
    elif kind in ("weekly", "every_n_weeks"):
        next_d = _add_days(from_, 7 * interval)
    elif kind == "by_weekdays":
        next_d = next_by_weekdays(from_, wd)
    elif kind == "monthly":
        next_d = _add_months_clamp_day(from_, 1, md)
    elif kind == "every_n_months":
        next_d = _add_months_clamp_day(from_, interval, md)
    else:
        next_d = _add_days(from_, 1)

    guard = 0
    while next_d <= from_ and guard < 64:
        next_d = _add_days(next_d, 1)
        guard += 1
    return next_d.isoformat()


def next_bill_due(due: date) -> date:
    """Сдвиг счёта +1 месяц (monthly-same-day)."""
    return _add_months_clamp_day(due, 1, due.day)


@dataclass
class Task:
    path: Path
    fm: dict
    body: str
    task_type: str = REGULAR
    status: str = STATUS_ACTIVE

    # ── доступ к frontmatter ──
    def get(self, key: str, default=None):
        return self.fm.get(key, default)

    @property
    def title(self) -> str:
        return str(self.fm.get("title") or self.path.stem)

    @property
    def created(self) -> date | None:
        return parse_date(str(self.fm.get("created")))

    @property
    def project(self) -> str | None:
        return str(self.fm["project"]) if self.fm.get("project") else None

    @property
    def priority(self) -> str | None:
        return str(self.fm["priority"]) if self.fm.get("priority") else None

    @property
    def due_date(self) -> date | None:
        return parse_date(str(self.fm.get("due_date")))

    @property
    def scheduled_date(self) -> date | None:
        return parse_date(str(self.fm.get("scheduled_date")))

    @property
    def comment_note(self) -> str | None:
        v = self.fm.get("comment_note")
        return str(v) if v else None

    @property
    def routine_next_date(self) -> date | None:
        return parse_date(str(self.fm.get("routine_next_date")))

    @property
    def routine_last_done(self) -> date | None:
        return parse_date(str(self.fm.get("routine_last_done_date")))

    @property
    def routine_last_skipped(self) -> date | None:
        return parse_date(str(self.fm.get("routine_last_skipped_date")))

    @property
    def is_routine(self) -> bool:
        return self.task_type == ROUTINE

    @property
    def is_bill(self) -> bool:
        return self.task_type == BILL

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    def is_due_on(self, day: date) -> bool:
        if self.is_bill:
            return self.due_date == day
        if self.is_routine:
            return self.routine_next_date == day
        return (self.due_date or self.scheduled_date) == day

    def is_overdue(self, today: date) -> bool:
        d = self.due_date or self.scheduled_date
        return bool(d and self.is_active and d < today and self.task_type != ROUTINE)

    def save(self) -> None:
        write_md(self.path, self.fm, self.body)

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "name": self.path.stem,
            "title": self.title,
            "task_type": self.task_type,
            "status": self.status,
            "project": self.project,
            "priority": self.priority,
            "due_date": ymd(self.due_date),
            "scheduled_date": ymd(self.scheduled_date),
            "routine_next_date": ymd(self.routine_next_date),
            "comment_note": self.comment_note,
        }


def load_task(path: Path) -> Task | None:
    try:
        fm, body = read_md(path)
    except OSError:
        return None
    if not fm:
        return None
    task_type = str(fm.get("task_type", REGULAR)) or REGULAR
    if task_type == "normal":
        task_type = REGULAR
    t = Task(
        path=path,
        fm=fm,
        body=body,
        task_type=task_type,
        status=str(fm.get("status", STATUS_ACTIVE)),
    )
    return t


def load_tasks(folder: Path) -> list[Task]:
    if not folder.exists():
        return []
    tasks = []
    for p in folder.glob("*.md"):
        t = load_task(p)
        if t:
            tasks.append(t)
    return tasks


# ── Мутации рутин ────────────────────────────────────────────
def routine_schedule_from_fm(fm: dict) -> tuple[str, int, list[int], int | None]:
    kind = str(fm.get("routine_recurrence_kind") or "flexible").strip()
    try:
        interval_n = max(1, int(fm.get("routine_interval") or 1))
    except (TypeError, ValueError):
        interval_n = 1
    raw = str(fm.get("routine_weekday_indices") or "").strip()
    wd = [int(x) for x in re.split(r"[,;\s]+", raw) if x.isdigit()]
    md_raw = fm.get("routine_month_day")
    try:
        month_day = int(md_raw) if md_raw not in (None, "") else None
    except (TypeError, ValueError):
        month_day = None
    if not wd and kind == "weekly":
        wd = [js_day_to_routine_index(date.today())]
    return kind, interval_n, wd, month_day


def advance_routine(t: Task, today: date | None = None) -> str | None:
    """Сдвинуть рутину на следующий цикл (после выполнения/пропуска)."""
    today = today or date.today()
    base = (t.routine_next_date or t.scheduled_date or today).isoformat()
    kind, interval, wd, md = routine_schedule_from_fm(t.fm)
    nxt = compute_next_routine_date_after(base, kind, interval, wd, md)
    t.fm["routine_next_date"] = nxt
    return nxt


def mark_routine_done(t: Task, today: date | None = None) -> None:
    today = today or date.today()
    t.fm["status"] = STATUS_ACTIVE
    t.fm["routine_last_done_date"] = today.isoformat()
    advance_routine(t, today)


def mark_routine_skipped(t: Task, today: date | None = None) -> None:
    today = today or date.today()
    t.fm["routine_last_skipped_date"] = today.isoformat()
    advance_routine(t, today)


def pay_bill(t: Task, today: date | None = None) -> None:
    """Оплата счёта: last_paid=today, сдвиг due_date +1 месяц."""
    today = today or date.today()
    t.fm["last_paid"] = today.isoformat()
    t.fm["status"] = STATUS_ACTIVE
    due = t.due_date or today
    t.fm["due_date"] = next_bill_due(due).isoformat()
    t.fm["scheduled_date"] = t.fm["due_date"]
    if isinstance(t.fm.get("last_done_date"), str) or "last_done_date" not in t.fm:
        t.fm["last_done_date"] = today.isoformat()


def append_journal(t: Task, heading: str, line: str, today: date | None = None) -> None:
    """Добавить строку в секцию журнала тела заметки (создавая секцию при отсутствии)."""
    today = today or date.today()
    body = t.body
    marker = f"**{today.isoformat()}** — {line}"
    if heading in body:
        idx = body.index(heading)
        after = body[idx + len(heading):]
        nl = "\n" if not after.startswith("\n") else ""
        body = body[:idx + len(heading)] + nl + "\n" + marker + after
    else:
        body = body.rstrip() + f"\n\n{heading}\n\n{marker}\n"
    t.body = body
