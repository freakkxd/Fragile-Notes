"""Мост к ao-engine CLI: запуск Node-процесса, парсинг JSON-ответа.

Команды: node <engine>/dist/cli/cli.js <cmd> --vault <vault> [args]
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import load_settings

# Лог-строки могут идти до JSON в stdout; JSON может быть многострочным.
_JSON_BLOCK_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)


@dataclass
class EngineCommandResult:
    ok: bool
    output: str
    stdout: str
    stderr: str
    exit_code: int | None
    command_line: str
    json: object | None = None


def _extract_json(stdout: str) -> object | None:
    m = _JSON_BLOCK_RE.search(stdout)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


class EngineBridge:
    def __init__(self, settings: dict | None = None) -> None:
        self.settings = settings or load_settings()

    @property
    def vault_root(self) -> str:
        return str(self.settings["vault_root"])

    @property
    def engine_cli(self) -> Path:
        return (
            Path(self.vault_root)
            / str(self.settings["engine_cli"])
        )

    @property
    def node_path(self) -> str:
        return str(self.settings["node_path"] or "node")

    def command_line(self, args: list[str]) -> str:
        full = [self.node_path, str(self.engine_cli), *args, "--vault", self.vault_root]
        return " ".join(shlex.quote(a) for a in full)

    def run(
        self,
        args: list[str],
        cwd: Path | None = None,
        timeout: float | None = None,
        env: dict | None = None,
    ) -> EngineCommandResult:
        cmd = [self.node_path, str(self.engine_cli), *args, "--vault", self.vault_root]
        base_env = dict(env or {})
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd or Path(self.vault_root)),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=base_env or None,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_val = exc.stdout
            if isinstance(stdout_val, bytes):
                stdout_str = stdout_val.decode("utf-8", "replace")
            else:
                stdout_str = stdout_val or ""
            return EngineCommandResult(
                ok=False,
                output="timeout",
                stdout=stdout_str,
                stderr=str(exc) or "timeout",
                exit_code=None,
                command_line=" ".join(shlex.quote(a) for a in cmd),
                json=None,
            )
        except (OSError, ValueError) as exc:
            return EngineCommandResult(
                ok=False,
                output=str(exc),
                stdout="",
                stderr=str(exc),
                exit_code=None,
                command_line=" ".join(shlex.quote(a) for a in cmd),
                json=None,
            )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        output = (stdout + "\n" + stderr).strip()
        return EngineCommandResult(
            ok=proc.returncode == 0,
            output=output,
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode,
            command_line=" ".join(shlex.quote(a) for a in cmd),
            json=_extract_json(stdout),
        )

    # ── Команды ──────────────────────────────────────────────
    def llm_ping(self, profile: str | None = None) -> EngineCommandResult:
        args = ["llm-ping-day"] if not profile or profile == "day" else ["llm-ping-archive"]
        return self.run(args, timeout=90)

    def enrich_notes(
        self,
        limit: int = 1,
        profile: str | None = None,
        force: bool = False,
    ) -> EngineCommandResult:
        args = ["enrich-notes", "--limit", str(limit)]
        if profile:
            args += ["--profile", profile]
        if force:
            args.append("--force")
        return self.run(args, timeout=None)

    def enrich_status(self) -> EngineCommandResult:
        return self.run(["enrich-status"], timeout=30)

    def health(self) -> EngineCommandResult:
        return self.run(["health", "--json"], timeout=120)

    def inbox_collect(self) -> EngineCommandResult:
        return self.run(["inbox-collect"], timeout=600)

    def night_run(self, limit: int | None = None) -> EngineCommandResult:
        args = ["night-run"]
        if limit:
            args += ["--limit", str(limit)]
        return self.run(args, timeout=None)

    def therapy_export(self, from_first: bool = False) -> EngineCommandResult:
        args = ["therapy-export"]
        if from_first:
            args.append("--from-first")
        return self.run(args, timeout=600)

    def audit_mega(self) -> EngineCommandResult:
        engine_dir = Path(self.vault_root) / "_System/ArchiveOrganism/ao-engine"
        cmd = ["npm", "run", "audit:mega"]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(engine_dir),
                capture_output=True,
                text=True,
                timeout=None,
            )
        except (OSError, ValueError) as exc:
            return EngineCommandResult(False, str(exc), "", str(exc), None, "npm run audit:mega")
        return EngineCommandResult(
            proc.returncode == 0,
            (proc.stdout or "") + "\n" + (proc.stderr or ""),
            proc.stdout or "",
            proc.stderr or "",
            proc.returncode,
            "npm run audit:mega",
            _extract_json(proc.stdout or ""),
        )
