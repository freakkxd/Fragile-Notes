from __future__ import annotations

import datetime

from fragilenotes.core.tasks import (
    compute_next_routine_date_after,
    next_bill_due,
)


def test_daily():
    assert compute_next_routine_date_after("2026-08-14", "daily") == "2026-08-15"
    assert compute_next_routine_date_after("2026-08-14", "every_n_days", 3) == "2026-08-17"


def test_weekly_same_weekday():
    # 2026-08-14 is Friday; weekly → next Friday
    assert compute_next_routine_date_after("2026-08-14", "weekly") == "2026-08-21"


def test_by_weekdays():
    # from Monday(0) 2026-08-10, next Mon/Wed(0,2) is Wed 12
    assert compute_next_routine_date_after("2026-08-10", "by_weekdays", weekday_indices=[0, 2]) == "2026-08-12"
    # from Thursday(3) → next Monday
    assert compute_next_routine_date_after("2026-08-13", "by_weekdays", weekday_indices=[0]) == "2026-08-17"


def test_monthly_clamp():
    # 31 Jan → next month Feb → 28 (2026 not leap)
    assert compute_next_routine_date_after("2026-01-31", "monthly") == "2026-02-28"
    # same-day month
    assert compute_next_routine_date_after("2026-03-15", "monthly") == "2026-04-15"


def test_every_n_months():
    assert compute_next_routine_date_after("2026-01-15", "every_n_months", 2) == "2026-03-15"


def test_strictly_after():
    # результат должен быть строго после from
    assert compute_next_routine_date_after("2026-08-14", "daily", 0) > "2026-08-14"


def test_bill_shift():
    assert next_bill_due(datetime.date(2026, 7, 13)).isoformat() == "2026-08-13"
    assert next_bill_due(datetime.date(2026, 1, 31)).isoformat() == "2026-02-28"


def _mk_task(tmp_path, name, task_type, **fm):
    from pathlib import Path

    from fragilenotes.core.tasks import write_md
    meta = {"task_type": task_type, "status": "active", **fm}
    write_md(Path(tmp_path) / f"{name}.md", meta, body="- [ ] пример #task\n")
    return name


def test_tasks_view_renders_tm_surfaces():
    import tempfile

    from fragilenotes.ui.tasks_view import TasksView  # noqa: E402

    today = datetime.date.today()
    with tempfile.TemporaryDirectory() as tmp:
        _mk_task(tmp, "overdue-task", "regular", due_date=(today - datetime.timedelta(days=10)).isoformat())
        _mk_task(tmp, "bill-task", "bill", due_date=today.isoformat(), amount="1200", currency="RUB")
        _mk_task(tmp, "routine-task", "routine",
                 routine_next_date=today.isoformat(), routine_recurrence_kind="daily")
        _mk_task(tmp, "future-routine", "routine",
                 routine_next_date=(today + datetime.timedelta(days=3)).isoformat(), routine_recurrence_kind="daily")

        settings = {"vault_root": tmp, "daily_folder": "02 Daily",
                    "tm_tasks_folder": ".", "tm_comments_folder": "c",
                    "tm_templates_root": "t"}
        done: list[str] = []
        view = TasksView(settings, lambda kind: done.append(kind))
        view.reload()

        buf = collect_labels(view)
        assert "Просрочено" in buf
        assert "Сегодня" in buf
        assert "Ближайшие рутины (7 дней)" in buf
        assert "🧾" in buf and "оплатить" in buf
        assert "просрочено" in buf or "⚠" in buf


def test_tasks_view_action_done():
    import tempfile

    from fragilenotes.ui.tasks_view import TasksView  # noqa: E402

    here = datetime.date.today()
    with tempfile.TemporaryDirectory() as tmp:
        _mk_task(tmp, "target-routine", "routine",
                 routine_next_date=here.isoformat(), routine_recurrence_kind="daily",
                 routine_interval="1")
        settings = {"vault_root": tmp, "daily_folder": "02 Daily",
                    "tm_tasks_folder": ".", "tm_comments_folder": "c",
                    "tm_templates_root": "t"}
        done: list[str] = []
        view = TasksView(settings, lambda kind: done.append(kind))
        view.reload()
        btn = find_button(view, "✓ Выполнено")
        assert btn is not None
        btn.emit("clicked")
        assert done == ["done"]
        from fragilenotes.core.tasks import load_tasks
        from fragilenotes.paths import resolve_paths
        tasks = load_tasks(resolve_paths(settings).tm_tasks)
        t = [x for x in tasks if x.path.stem == "target-routine"][0]
        assert t.fm["routine_last_done_date"] == here.isoformat()
        assert t.fm["routine_next_date"] == (here + datetime.timedelta(days=1)).isoformat()


def collect_labels(root) -> str:
    out = []
    stack = [root]
    while stack:
        w = stack.pop()
        if hasattr(w, "get_label"):
            out.append(w.get_label())
        children = w.get_first_child()
        while children is not None:
            stack.append(children)
            children = children.get_next_sibling()
    return "\n".join(out)


def find_button(root, label: str):
    stack = [root]
    while stack:
        w = stack.pop()
        if hasattr(w, "get_label") and getattr(w, "get_label", lambda: None)() == label:
            return w
        children = w.get_first_child()
        while children is not None:
            stack.append(children)
            children = children.get_next_sibling()
    return None
