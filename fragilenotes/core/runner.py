"""Ядро AO Task Manager — порт taskManagerModel.ts из плагина ao-runner.

Фреймворк-независимая модель: проекция статуса runner на состояния задач,
история выполнений, персист.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TASK_HISTORY_MAX = 8

# Состояния
IDLE = "idle"
RUNNING = "running"
DONE = "done"
ERROR = "error"

TASK_STATE_LABELS = {
    IDLE: "idle",
    RUNNING: "выполняется",
    DONE: "готово",
    ERROR: "ошибка",
}


@dataclass
class TaskRunRecord:
    at: float
    state: str  # done | error
    detail: str


@dataclass
class TaskViewState:
    state: str = IDLE
    progress_current: int = 0
    progress_total: int | None = None
    started_at: float | None = None
    last_result: str | None = None
    last_result_state: str | None = None
    history: list[TaskRunRecord] = field(default_factory=list)
    last_handled_command: str | None = None


@dataclass
class RunnerStatus:
    current_command: str | None = None
    state: str = IDLE
    progress_current: int = 0
    progress_total: int | None = None
    last_error: str | None = None
    qwen_online: bool | None = None
    qwen_server_profile: str | None = None
    qwen_ctx_size: int | None = None
    llm_token_usage: dict = field(default_factory=dict)
    enrich_pipeline: dict | None = None


@dataclass
class TaskCommandMatch:
    id: str
    command_ids: list[str] = field(default_factory=list)
    command_prefixes: list[str] = field(default_factory=list)


def create_task_state() -> TaskViewState:
    return TaskViewState()


def create_task_states(ids: list[str]) -> dict[str, TaskViewState]:
    return {i: create_task_state() for i in ids}


def _command_matches(def_: TaskCommandMatch, command: str | None) -> bool:
    if not command:
        return False
    if command in def_.command_ids:
        return True
    return any(command.startswith(p) for p in def_.command_prefixes)


def _format_done_detail(status: RunnerStatus) -> str:
    if status.progress_total and status.progress_total > 0:
        return f"Готово · {status.progress_current}/{status.progress_total}"
    return "Готово"


def update_task_states_from_status(
    states: dict[str, TaskViewState],
    status: RunnerStatus,
    defs: list[TaskCommandMatch],
    now: float | None = None,
) -> tuple[dict[str, TaskViewState], list[tuple[str, TaskRunRecord]]]:
    """Вернуть (states, records) — records: новые завершённые записи истории."""
    if now is None:
        import time

        now = time.time() * 1000.0
    next_states = dict(states)
    records: list[tuple[str, TaskRunRecord]] = []

    for def_ in defs:
        st = next_states.get(def_.id) or create_task_state()
        next_states.setdefault(def_.id, st)
        if not _command_matches(def_, status.current_command):
            continue

        if status.state == RUNNING:
            st.state = RUNNING
            st.progress_current = status.progress_current
            st.progress_total = status.progress_total
            if st.started_at is None:
                st.started_at = now
            st.last_handled_command = status.current_command
            continue

        if status.state in (DONE, ERROR):
            finished = (
                st.state == RUNNING
                or st.last_handled_command != status.current_command
                or st.last_result_state is None
            )
            if finished:
                if status.state == ERROR:
                    detail = (status.last_error or "Ошибка").strip()
                else:
                    detail = _format_done_detail(status)
                record = TaskRunRecord(at=now, state=status.state, detail=detail)
                st.history = [record, *st.history][:TASK_HISTORY_MAX]
                st.last_result = detail
                st.last_result_state = status.state
                records.append((def_.id, record))
            st.state = status.state
            st.progress_current = status.progress_current
            st.progress_total = status.progress_total
            st.started_at = None
            st.last_handled_command = status.current_command

    return next_states, records


def merge_persisted_history(
    states: dict[str, TaskViewState],
    persisted: dict | None,
) -> dict[str, TaskViewState]:
    if not persisted:
        return states
    next_states = dict(states)
    for task_id, raw in persisted.items():
        st = next_states.get(task_id)
        if not st or not isinstance(raw, list):
            continue
        valid: list[TaskRunRecord] = []
        for r in raw:
            if (
                isinstance(r, dict)
                and isinstance(r.get("at"), (int, float))
                and r.get("state") in (DONE, ERROR)
                and isinstance(r.get("detail"), str)
            ):
                valid.append(
                    TaskRunRecord(at=r["at"], state=r["state"], detail=r["detail"])
                )
        if not valid:
            continue
        st.history = valid[:TASK_HISTORY_MAX]
        latest = st.history[0]
        st.last_result = latest.detail
        st.last_result_state = latest.state
    return next_states


def history_to_dict(history: list[TaskRunRecord]) -> list[dict]:
    return [
        {"at": r.at, "state": r.state, "detail": r.detail} for r in history
    ]
