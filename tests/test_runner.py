from __future__ import annotations

from fragilenotes.core.runner import (
    TASK_HISTORY_MAX,
    RunnerStatus,
    TaskCommandMatch,
    create_task_states,
    merge_persisted_history,
    update_task_states_from_status,
)


def _status(command=None, state="idle", cur=0, total=None, error=None):
    s = RunnerStatus()
    s.current_command = command
    s.state = state
    s.progress_current = cur
    s.progress_total = total
    s.last_error = error
    return s


def test_running_projection():
    states = create_task_states(["enrich", "health"])
    defs = [
        TaskCommandMatch("enrich", command_ids=["enrich-notes"]),
        TaskCommandMatch("health", command_ids=["health"]),
    ]
    st = _status("enrich-notes", "running", 2, 5)
    next_states, _ = update_task_states_from_status(states, st, defs, 1000)
    assert next_states["enrich"].state == "running"
    assert next_states["enrich"].progress_current == 2
    assert next_states["health"].state == "idle"


def test_history_recorded_once():
    states = create_task_states(["enrich"])
    defs = [TaskCommandMatch("enrich", command_ids=["enrich-notes"])]
    r1, _ = update_task_states_from_status(
        states, _status("enrich-notes", "running", 3, 3), defs, 1000
    )
    r2, recs = update_task_states_from_status(
        r1, _status("enrich-notes", "done", 3, 3), defs, 2000
    )
    r3, recs2 = update_task_states_from_status(
        r2, _status("enrich-notes", "done", 3, 3), defs, 3000
    )
    assert len(recs) == 1
    assert recs[0][0] == "enrich"
    assert recs[0][1].detail == "Готово · 3/3"
    assert recs2 == []
    assert len(r2["enrich"].history) == 1


def test_error_detail():
    states = create_task_states(["health"])
    defs = [TaskCommandMatch("health", command_ids=["health"])]
    r1, _ = update_task_states_from_status(
        states, _status("health", "running"), defs, 1000
    )
    _, recs = update_task_states_from_status(
        r1, _status("health", "error", error="boom"), defs, 2000
    )
    assert recs[0][1].detail == "boom"


def test_prefix_match():
    states = create_task_states(["agent"])
    defs = [TaskCommandMatch("agent", command_prefixes=["agent-ui:"])]
    next_states, _ = update_task_states_from_status(
        states, _status("agent-ui:fragilich", "running"), defs, 1000
    )
    assert next_states["agent"].state == "running"


def test_history_cap():
    states = create_task_states(["h"])
    defs = [TaskCommandMatch("h", command_ids=["health"])]
    for i in range(TASK_HISTORY_MAX + 5):
        s1, _ = update_task_states_from_status(
            states, _status("health", "running"), defs, 1000 + i
        )
        states, _ = update_task_states_from_status(
            s1, _status("health", "done"), defs, 2000 + i
        )
    assert len(states["h"].history) == TASK_HISTORY_MAX


def test_merge_persisted():
    states = create_task_states(["enrich", "health"])
    persisted = {
        "enrich": [{"at": 1, "state": "done", "detail": "x"}, {"at": 2, "state": "nope", "detail": "y"}, None],
        "health": [{"at": 3, "state": "error", "detail": "e"}],
        "unknown": [{"at": 4, "state": "done", "detail": "z"}],
    }
    merged = merge_persisted_history(states, persisted)
    assert len(merged["enrich"].history) == 1
    assert merged["enrich"].last_result == "x"
    assert merged["health"].last_result == "e"


def test_unknown_task_returns_error():
    from fragilenotes.core.runner_controller import RunnerController
    ctrl = RunnerController(settings={}, engine=None, llm=None)
    ok, detail = ctrl.run_operation("no-such-task", "run")
    assert ok is False
    assert "неизвестная" in detail


def test_int_value_fallback():
    from fragilenotes.core.runner_controller import _int_value
    assert _int_value("20", 5) == 20
    assert _int_value(None, 5) == 5
    assert _int_value("abc", 5) == 5
