"""Git auto-sync для vault: авто-коммит и пуш при изменениях с debounce 30с.

Сервис отслеживает изменения в vault (уведомляется из app.py vault monitor),
делает `git add -A`, `git commit` и `git push` с задержкой 30 сек (debounce).
Предоставляет статус для статус-бара.

Использование в app.py:
    from ..services.git_sync import GitSyncService
    self.git_sync = GitSyncService(self.settings)
    self.git_sync.start()
    # при изменении vault:
    self.git_sync.schedule_sync()
    # для статус-бара:
    self.status_bar.refresh(..., git_sync=self.git_sync)

Поддерживает как GLib (GTK) таймер, так и fallback на threading.Timer для
headless/py_compile окружений.
"""

from __future__ import annotations

import datetime
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEBOUNCE_MS = 30000
DEBOUNCE_SEC = DEBOUNCE_MS / 1000.0


@dataclass
class GitSyncStatus:
    """Снимок состояния git-sync для UI."""

    enabled: bool = True
    is_repo: bool = False
    state: str = "idle"  # idle | pending | syncing | synced | error | disabled | no_repo
    last_sync: str | None = None
    last_error: str | None = None
    branch: str | None = None
    ahead: int = 0
    behind: int = 0
    has_changes: bool = False

    def label(self) -> str:
        """Человеко-читаемый лейбл для статус-бара."""
        if not self.enabled:
            return "git: выкл"
        if not self.is_repo:
            return "git: нет репо"
        if self.state == "syncing":
            return "git: синхр…"
        if self.state == "pending":
            return "git: ожидание"
        if self.state == "error":
            err = (self.last_error or "ошибка")[:30]
            return f"git: ✗ {err}"
        if self.state == "synced":
            if self.last_sync:
                return f"git: ✓ {self.last_sync}"
            return "git: ✓"
        if self.has_changes:
            return "git: есть изменения"
        return "git: ✓"

    def tone(self) -> str:
        """Тон для чипа статус-бара: idle | ok | warn | error | run."""
        if not self.enabled or not self.is_repo:
            return "idle"
        if self.state == "error":
            return "error"
        if self.state == "syncing":
            return "run"
        if self.state == "pending":
            return "warn"
        if self.state == "synced":
            return "ok"
        if self.has_changes:
            return "warn"
        return "idle"


def _run_git(args: list[str], cwd: Path, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Запуск git команды, безопасно."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _is_git_repo(root: Path) -> bool:
    """Проверка что vault — git репозиторий."""
    if not root.is_dir():
        return False
    # быстрый путь: наличие .git
    if (root / ".git").exists():
        return True
    # fallback: git rev-parse
    if shutil.which("git") is None:
        return False
    try:
        proc = _run_git(["rev-parse", "--is-inside-work-tree"], root, timeout=5)
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except Exception:
        return False


def _get_branch(root: Path) -> str | None:
    try:
        proc = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root, timeout=5)
        if proc.returncode == 0:
            b = proc.stdout.strip()
            return b if b else None
    except Exception:
        pass
    return None


class GitSyncService:
    """Авто-коммит и пуш vault с debounce 30с."""

    DEBOUNCE_MS = DEBOUNCE_MS
    DEBOUNCE_SEC = DEBOUNCE_SEC

    def __init__(
        self,
        settings: dict[str, Any] | None = None,
        debounce_ms: int = DEBOUNCE_MS,
    ) -> None:
        self._settings: dict[str, Any] = dict(settings) if settings is not None else {}
        self._debounce_ms: int = int(debounce_ms) if debounce_ms else DEBOUNCE_MS
        self._debounce_sec: float = self._debounce_ms / 1000.0
        self._lock = threading.RLock()
        self._status = GitSyncStatus(
            enabled=bool(self._settings.get("git_auto_sync", True)),
            is_repo=False,
            state="idle",
        )
        self._timer_id: int | None = None  # GLib source id
        self._timer_thread: threading.Timer | None = None  # fallback
        self._syncing: bool = False
        self._on_status_changed: Any | None = None
        self._update_repo_state()

    # ── settings ───────────────────────────────────────────────
    @property
    def settings(self) -> dict[str, Any]:
        return self._settings

    def update_settings(self, settings: dict[str, Any]) -> None:
        """SettingsObserver: синхронизация настроек."""
        with self._lock:
            self._settings = dict(settings) if settings is not None else {}
            self._status.enabled = bool(self._settings.get("git_auto_sync", True))
            if not self._status.enabled:
                self._status.state = "disabled"
            elif not self._status.is_repo:
                # перепроверить репо если vault_root сменился
                self._update_repo_state_locked()
            else:
                if self._status.state == "disabled":
                    self._status.state = "idle"

    @property
    def vault_root(self) -> Path:
        return Path(str(self._settings.get("vault_root", "")))

    @property
    def status(self) -> GitSyncStatus:
        with self._lock:
            # копия для безопасности
            return GitSyncStatus(
                enabled=self._status.enabled,
                is_repo=self._status.is_repo,
                state=self._status.state,
                last_sync=self._status.last_sync,
                last_error=self._status.last_error,
                branch=self._status.branch,
                ahead=self._status.ahead,
                behind=self._status.behind,
                has_changes=self._status.has_changes,
            )

    def get_status(self) -> GitSyncStatus:
        return self.status

    def status_text(self) -> tuple[str, str]:
        """(label, tone) для статус-бара."""
        s = self.status
        return s.label(), s.tone()

    def set_status_callback(self, cb: Any) -> None:
        """Колбэк при изменении статуса (вызов вне lock)."""
        self._on_status_changed = cb

    # ── repo detection ─────────────────────────────────────────
    def _update_repo_state(self) -> None:
        with self._lock:
            self._update_repo_state_locked()

    def _update_repo_state_locked(self) -> None:
        root = self.vault_root
        if not root or not str(root).strip():
            self._status.is_repo = False
            self._status.state = "no_repo" if self._status.enabled else "disabled"
            return
        is_repo = _is_git_repo(root)
        self._status.is_repo = is_repo
        if not is_repo:
            self._status.state = "no_repo" if self._status.enabled else "disabled"
            self._status.branch = None
        else:
            if self._status.state in ("no_repo", "disabled"):
                self._status.state = "idle"
            try:
                self._status.branch = _get_branch(root)
            except Exception:
                self._status.branch = None
            # проверить наличие изменений
            try:
                self._refresh_changes_locked()
            except Exception:
                pass

    def _refresh_changes_locked(self) -> None:
        root = self.vault_root
        if not self._status.is_repo or not root.is_dir():
            self._status.has_changes = False
            return
        if shutil.which("git") is None:
            return
        try:
            proc = _run_git(["status", "--porcelain"], root, timeout=5)
            if proc.returncode == 0:
                self._status.has_changes = bool(proc.stdout.strip())
            # ahead/behind если есть upstream
            try:
                bproc = _run_git(["rev-list", "--left-right", "--count", "HEAD...@{upstream}"], root, timeout=5)
                if bproc.returncode == 0 and bproc.stdout.strip():
                    parts = bproc.stdout.strip().split()
                    if len(parts) == 2:
                        self._status.ahead = int(parts[0])
                        self._status.behind = int(parts[1])
            except Exception:
                pass
        except Exception:
            pass

    def is_git_repo(self) -> bool:
        with self._lock:
            return self._status.is_repo

    def has_changes(self) -> bool:
        with self._lock:
            return self._status.has_changes

    # ── debounce scheduling ────────────────────────────────────
    def schedule_sync(self) -> None:
        """Уведомить о изменении — запустить debounce 30с до коммита.

        Вызывается из app.py при событии FileMonitor.
        """
        with self._lock:
            if not self._status.enabled or not self._status.is_repo:
                return
            if self._syncing:
                # уже синхронизирует — перезапустим после
                self._status.state = "pending"
                self._schedule_timer_locked()
                return
            self._status.state = "pending"
            self._status.last_error = None
            self._schedule_timer_locked()
        self._notify()

    def _schedule_timer_locked(self) -> None:
        self._cancel_timer_locked()
        # Пробуем GLib, иначе threading.Timer
        try:
            from gi.repository import GLib  # type: ignore

            # GLib доступен — используем timeout_add
            self._timer_id = GLib.timeout_add(self._debounce_ms, self._on_debounce_glib)
            return
        except Exception:
            pass
        # fallback
        t = threading.Timer(self._debounce_sec, self._on_debounce_thread)
        t.daemon = True
        self._timer_thread = t
        t.start()

    def _cancel_timer_locked(self) -> None:
        if self._timer_id is not None:
            try:
                from gi.repository import GLib  # type: ignore

                GLib.source_remove(self._timer_id)
            except Exception:
                pass
            self._timer_id = None
        if self._timer_thread is not None:
            try:
                self._timer_thread.cancel()
            except Exception:
                pass
            self._timer_thread = None

    def _on_debounce_glib(self) -> bool:
        # GLib callback — должен вернуть False для одноразового
        with self._lock:
            self._timer_id = None
        # запускаем sync в фоне
        threading.Thread(target=self._do_sync, daemon=True).start()
        return False

    def _on_debounce_thread(self) -> None:
        with self._lock:
            self._timer_thread = None
        threading.Thread(target=self._do_sync, daemon=True).start()

    # ── sync logic ─────────────────────────────────────────────
    def sync_now(self) -> None:
        """Немедленный синк (без debounce) — для ручного вызова."""
        with self._lock:
            self._cancel_timer_locked()
            if not self._status.enabled or not self._status.is_repo:
                return
            if self._syncing:
                self._status.state = "pending"
                self._schedule_timer_locked()
                return
        threading.Thread(target=self._do_sync, daemon=True).start()

    def _do_sync(self) -> None:
        with self._lock:
            if self._syncing:
                return
            self._syncing = True
            self._status.state = "syncing"
            self._status.last_error = None
        self._notify()
        try:
            ok, msg = self._perform_git_sync()
            with self._lock:
                if ok:
                    self._status.state = "synced"
                    self._status.last_sync = datetime.datetime.now().strftime("%H:%M:%S")
                    self._status.last_error = None
                    self._status.has_changes = False
                    self._refresh_changes_locked()
                else:
                    # если нет изменений — считаем synced, иначе error
                    if msg and "nothing to commit" in msg.lower():
                        self._status.state = "synced"
                        self._status.last_sync = datetime.datetime.now().strftime("%H:%M:%S")
                        self._status.last_error = None
                        self._status.has_changes = False
                    else:
                        self._status.state = "error"
                        self._status.last_error = (msg or "ошибка git")[:120]
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._status.state = "error"
                self._status.last_error = str(exc)[:120]
        finally:
            with self._lock:
                self._syncing = False
                # если за время синка пришли новые изменения — перепланировать
                pending = self._status.state == "pending"
            self._notify()
            if pending:
                self.schedule_sync()
            # обновить изменения если еще не synced
            try:
                with self._lock:
                    self._refresh_changes_locked()
            except Exception:
                pass

    def _perform_git_sync(self) -> tuple[bool, str]:
        """Выполнить add/commit/push. Возвращает (ok, message)."""
        root = self.vault_root
        if not root.is_dir():
            return False, "vault not found"
        if shutil.which("git") is None:
            return False, "git не найден"
        if not _is_git_repo(root):
            return False, "не git репозиторий"

        auto_push = bool(self._settings.get("git_auto_push", True))
        # commit message template
        tmpl = str(self._settings.get("git_commit_template", "auto-sync: {now}"))
        try:
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            msg = tmpl.format(now=now_str, date=now_str)
        except Exception:
            msg = f"auto-sync: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        # git add -A
        try:
            proc = _run_git(["add", "-A"], root, timeout=30)
            if proc.returncode != 0:
                return False, proc.stderr.strip() or proc.stdout.strip() or "git add failed"
        except subprocess.TimeoutExpired:
            return False, "git add timeout"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

        # check staged changes
        try:
            proc = _run_git(["diff", "--cached", "--quiet"], root, timeout=10)
            # exit 0 -> no changes, 1 -> has changes
            if proc.returncode == 0:
                # нечего коммитить — можно попробовать пуш если ahead
                if auto_push:
                    self._try_push(root)
                return True, "nothing to commit"
        except Exception:
            pass

        # git commit
        try:
            proc = _run_git(["commit", "-m", msg], root, timeout=30)
            if proc.returncode != 0:
                out = (proc.stderr.strip() + " " + proc.stdout.strip()).strip()
                if "nothing to commit" in out.lower():
                    if auto_push:
                        self._try_push(root)
                    return True, out
                return False, out or "git commit failed"
        except subprocess.TimeoutExpired:
            return False, "git commit timeout"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

        # git push если включено
        if auto_push:
            ok, push_msg = self._try_push(root)
            if not ok:
                # коммит уже сделан, но пуш не удался — считаем частичным успехом с ошибкой
                # оставляем state error но сохраняем last_sync
                return False, push_msg
        return True, "ok"

    def _try_push(self, root: Path) -> tuple[bool, str]:
        """Пуш если есть remote."""
        try:
            # есть ли remote
            proc = _run_git(["remote"], root, timeout=5)
            if proc.returncode != 0 or not proc.stdout.strip():
                return True, "no remote"
            # есть ли upstream
            proc = _run_git(["push"], root, timeout=30)
            if proc.returncode == 0:
                return True, "pushed"
            out = (proc.stderr.strip() + " " + proc.stdout.strip()).strip()
            # частая ошибка — нет upstream / нет сети — не критично
            if "no upstream" in out.lower() or "has no upstream" in out.lower():
                return True, out
            return False, out or "git push failed"
        except subprocess.TimeoutExpired:
            return False, "git push timeout"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    # ── lifecycle ──────────────────────────────────────────────
    def start(self) -> None:
        """Запустить сервис: проверить репо и статус."""
        self._update_repo_state()
        self._notify()

    def stop(self) -> None:
        """Остановить таймеры."""
        with self._lock:
            self._cancel_timer_locked()
            self._status.state = "idle" if self._status.enabled else "disabled"

    def shutdown(self) -> None:
        self.stop()

    def _notify(self) -> None:
        cb = self._on_status_changed
        if cb is not None:
            try:
                # если GLib доступен — вызвать через idle_add
                try:
                    from gi.repository import GLib  # type: ignore

                    GLib.idle_add(lambda: (cb(self.status), False)[1])
                    return
                except Exception:
                    pass
                cb(self.status)
            except Exception:
                pass

    # совместимость: некоторые проверки могут искать `schedule`
    def schedule(self) -> None:
        self.schedule_sync()


__all__ = ["GitSyncService", "GitSyncStatus", "DEBOUNCE_MS", "DEBOUNCE_SEC"]
