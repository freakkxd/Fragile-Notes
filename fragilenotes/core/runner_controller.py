"""Контроллер AO Task Manager: определения задач, состояния, выполнение операций.

Логика фреймворк-независима; выполнение — синхронные функции, вызываемые из
фонового потока UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..services.engine import EngineBridge
from ..services.llm import ARCHIVE, DAY, LlmService
from .runner import (
    TASK_HISTORY_MAX,
    TaskCommandMatch,
    TaskRunRecord,
    TaskViewState,
    create_task_states,
    merge_persisted_history,
    update_task_states_from_status,
)

LLM = "llm"
ENRICH = "enrich"
PIPELINE = "pipeline"
INBOX = "inbox"
SYSTEM = "system"

GROUP_LABELS = {
    LLM: "🧠 LLM",
    ENRICH: "🔄 Обогащение",
    PIPELINE: "🌙 Пайплайн",
    INBOX: "📥 Inbox / Therapy",
    SYSTEM: "⚙️ Система",
}
GROUP_ORDER = [LLM, ENRICH, PIPELINE, INBOX, SYSTEM]


@dataclass
class TaskAction:
    kind: str  # run | start | stop | ping | open
    label: str
    primary: bool = False


@dataclass
class TaskDef:
    id: str
    group: str
    title: str
    icon: str
    help: str
    command_ids: list[str] = field(default_factory=list)
    command_prefixes: list[str] = field(default_factory=list)
    requires_llm: bool = False
    actions: list[TaskAction] = field(default_factory=list)


def build_task_defs() -> list[TaskDef]:
    return [
        TaskDef(
            id="llm-day",
            group=LLM,
            title="Qwen3‑14B (day)",
            icon="💎",
            help="llama-server · :11435 · ctx 12k\nenrich --profile day",
            command_ids=["start-qwen3-day", "stop-qwen3-day", "llm-ping-day"],
            actions=[
                TaskAction("start", "▶ Запустить", primary=True),
                TaskAction("stop", "⏹ Стоп"),
                TaskAction("ping", "Ping"),
            ],
        ),
        TaskDef(
            id="llm-archive",
            group=LLM,
            title="Gemma‑4‑26B (archive)",
            icon="🌙",
            help="llama-server · :11434\nenrich --profile archive_night",
            command_ids=["start-qwen3-archive", "stop-qwen3-archive", "llm-ping-archive"],
            actions=[
                TaskAction("start", "▶ Запустить", primary=True),
                TaskAction("stop", "⏹ Стоп"),
                TaskAction("ping", "Ping"),
            ],
        ),
        TaskDef(
            id="llm-ping",
            group=LLM,
            title="Проверка LLM",
            icon="📡",
            help="llm-ping активного сервера",
            command_ids=["llm-ping"],
            actions=[TaskAction("run", "▶ Ping", primary=True)],
        ),
        TaskDef(
            id="enrich-1",
            group=ENRICH,
            title="Enrich 1 (тест)",
            icon="🧪",
            help="enrich-notes --limit 1",
            command_ids=["enrich-notes-1"],
            requires_llm=True,
            actions=[TaskAction("run", "▶ Запустить", primary=True)],
        ),
        TaskDef(
            id="enrich-batch",
            group=ENRICH,
            title="Enrich пакет",
            icon="🔄",
            help="enrich-notes --limit N (лимит из настроек)",
            command_ids=["enrich-notes"],
            requires_llm=True,
            actions=[TaskAction("run", "▶ Запустить", primary=True)],
        ),
        TaskDef(
            id="night-run",
            group=PIPELINE,
            title="Night Run",
            icon="🌌",
            help="night-run + enrich --profile archive_night",
            command_ids=["night-run"],
            requires_llm=True,
            actions=[TaskAction("run", "▶ Запустить", primary=True)],
        ),
        TaskDef(
            id="health",
            group=SYSTEM,
            title="Health Check",
            icon="🩺",
            help="health --json",
            command_ids=["health"],
            actions=[TaskAction("run", "▶ Проверить", primary=True)],
        ),
        TaskDef(
            id="audit",
            group=SYSTEM,
            title="Mega-audit",
            icon="🔎",
            help="npm run audit:mega",
            command_ids=["audit:mega"],
            actions=[TaskAction("run", "▶ Запустить", primary=True)],
        ),
        TaskDef(
            id="inbox-collect",
            group=INBOX,
            title="Собрать материалы",
            icon="📥",
            help="wrap-web-clips + Telegram RSS",
            command_ids=["inbox-collect"],
            actions=[TaskAction("run", "▶ Собрать", primary=True)],
        ),
        TaskDef(
            id="therapy-backfill",
            group=INBOX,
            title="Therapy → Sheets",
            icon="📓",
            help="therapy-export --from-first",
            command_ids=["therapy-export", "therapy-backfill", "therapy-backfill-from-first"],
            actions=[TaskAction("run", "▶ Backfill", primary=True)],
        ),
    ]


class RunnerController:
    def __init__(
        self,
        settings: dict,
        engine: EngineBridge,
        llm: LlmService,
        history: dict | None = None,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.llm = llm
        self.defs = build_task_defs()
        self.task_states = create_task_states([d.id for d in self.defs])
        self.task_history: dict[str, list[TaskRunRecord]] = dict(history or {})
        self.merge_history()
        self.status = _new_status()

    @property
    def matches(self) -> list[TaskCommandMatch]:
        return [
            TaskCommandMatch(
                id=d.id, command_ids=d.command_ids, command_prefixes=d.command_prefixes
            )
            for d in self.defs
        ]

    def merge_history(self) -> None:
        persisted = {k: [r.to_dict() if hasattr(r, "to_dict") else r for r in v] for k, v in self.task_history.items()}
        merged = merge_persisted_history(self.task_states, persisted)
        self.task_states = merged

    # ── Жизненный цикл операции ─────────────────────────────
    def begin(self, command: str, total: int | None = None) -> None:
        self.status.current_command = command
        self.status.state = "running"
        self.status.progress_current = 0
        self.status.progress_total = total
        self.status.last_error = None

    def set_progress(self, current: int) -> None:
        self.status.progress_current = current

    def finish(self, ok: bool, detail: str | None = None, command: str | None = None) -> list[tuple[str, TaskRunRecord]]:
        self.status.state = "done" if ok else "error"
        if not ok:
            self.status.last_error = detail or "Ошибка"
        if ok and self.status.progress_total is not None:
            self.status.progress_current = self.status.progress_total
        _, records = self._advance()
        self._commit_records(records)
        self.status.current_command = None
        self.status.state = "idle"
        return records

    def _advance(self) -> tuple[dict[str, TaskViewState], list[tuple[str, TaskRunRecord]]]:
        states, records = update_task_states_from_status(
            self.task_states, self.status, self.matches
        )
        self.task_states = states
        return states, records

    def _commit_records(self, records: list[tuple[str, TaskRunRecord]]) -> None:
        for task_id, rec in records:
            arr = self.task_history.setdefault(task_id, [])
            arr.insert(0, rec)
            del arr[TASK_HISTORY_MAX:]

    def poll_status(self) -> list[tuple[str, TaskRunRecord]]:
        """Прогнать текущий статус через модель (вызывается из таймера)."""
        _, records = self._advance()
        self._commit_records(records)
        return records

    # ── Выполнение операций (синхронно; вызывается в потоке) ─
    def _active_profile(self) -> str:
        day_ok = self.llm.get_status(DAY).online
        arch_ok = self.llm.get_status(ARCHIVE).online
        if arch_ok and not day_ok:
            return "archive_night"
        return "day"

    def run_operation(self, task_id: str, action: str) -> tuple[bool, str]:
        defs = {d.id: d for d in self.defs}
        d = defs.get(task_id)
        if d is None:
            return False, f"неизвестная задача {task_id}"
        if action == "start":
            return self._op_start(d)
        if action == "stop":
            return self._op_stop(d)
        if action == "ping":
            return self._op_ping(d)
        if action == "run":
            return self._op_run(d)
        return False, f"unknown action {action}"

    def _op_start(self, d: TaskDef) -> tuple[bool, str]:
        profile = DAY if d.id == "llm-day" else ARCHIVE
        self.begin(f"start-qwen3-{profile}", 1)
        ok, out = self.llm.start(profile)
        if ok:
            self.set_progress(1)
            ping = self.llm.ping(profile)
            detail = f"OK · {out[:80]}" if ping.online else f"server up, но ping: {ping.error}"
            self.finish(ping.online is not False, detail, command=f"start-qwen3-{profile}")
            return ping.online is not False, detail
        self.finish(False, out, command=f"start-qwen3-{profile}")
        return False, out

    def _op_stop(self, d: TaskDef) -> tuple[bool, str]:
        profile = DAY if d.id == "llm-day" else ARCHIVE
        self.begin(f"stop-qwen3-{profile}", 1)
        ok, out = self.llm.stop(profile)
        self.set_progress(1)
        self.finish(ok, out[:120] if not ok else "Остановлен", command=f"stop-qwen3-{profile}")
        return ok, out

    def _op_ping(self, d: TaskDef) -> tuple[bool, str]:
        profile = DAY if d.id == "llm-day" else (ARCHIVE if d.id == "llm-archive" else self._active_profile())
        cmd = "llm-ping-day" if profile == DAY else "llm-ping-archive"
        self.begin(cmd, 1)
        st = self.llm.ping(profile)
        self.set_progress(1)
        res = self.engine.llm_ping(profile)
        detail = f"ВКЛ ctx={st.ctx_size}" if st.online else (st.error or "недоступен")
        self.finish(st.online is True, detail, command=cmd)
        return st.online is True, detail

    def _op_run(self, d: TaskDef) -> tuple[bool, str]:
        if d.id == "enrich-1":
            self.begin("enrich-notes-1", 1)
            res = self.engine.enrich_notes(limit=1, profile=self._active_profile())
            self.finish(res.ok, _tail(res.output), command="enrich-notes-1")
            return res.ok, _tail(res.output)
        if d.id == "enrich-batch":
            limit = _int_value(self.settings.get("enrich_default_limit"), 20)
            self.begin("enrich-notes", limit)
            res = self.engine.enrich_notes(limit=limit, profile=self._active_profile())
            self.finish(res.ok, _tail(res.output), command="enrich-notes")
            return res.ok, _tail(res.output)
        if d.id == "night-run":
            self.begin("night-run", 1)
            res = self.engine.night_run()
            self.finish(res.ok, _tail(res.output), command="night-run")
            return res.ok, _tail(res.output)
        if d.id == "health":
            self.begin("health", 1)
            res = self.engine.health()
            self.finish(res.ok, _tail(res.output), command="health")
            return res.ok, _tail(res.output)
        if d.id == "audit":
            self.begin("audit:mega", 1)
            res = self.engine.audit_mega()
            self.finish(res.ok, _tail(res.output), command="audit:mega")
            return res.ok, _tail(res.output)
        if d.id == "inbox-collect":
            self.begin("inbox-collect", 1)
            res = self.engine.inbox_collect()
            self.finish(res.ok, _tail(res.output), command="inbox-collect")
            return res.ok, _tail(res.output)
        if d.id == "therapy-backfill":
            self.begin("therapy-export", 1)
            res = self.engine.therapy_export(from_first=True)
            self.finish(res.ok, _tail(res.output), command="therapy-export")
            return res.ok, _tail(res.output)
        if d.id == "llm-ping":
            return self._op_ping(d)
        return False, f"no executor for {d.id}"


def _tail(text: str, n: int = 200) -> str:
    t = (text or "").strip()
    return t[-n:] if len(t) > n else t


def _int_value(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _new_status():
    from .runner import RunnerStatus

    return RunnerStatus()
