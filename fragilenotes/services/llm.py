"""Управление локальными LLM-серверами (llama-server через manage-llm.sh + HTTP-пинг)."""

from __future__ import annotations

import json
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from ..config import load_settings

DAY = "day"
ARCHIVE = "archive"


@dataclass
class LlmStatus:
    online: bool | None = None
    profile: str | None = None
    ctx_size: int | None = None
    model: str | None = None
    error: str | None = None
    token_usage: dict = field(default_factory=dict)


class LlmService:
    def __init__(self, settings: dict | None = None) -> None:
        self.settings = settings or load_settings()
        self._status: dict[str, LlmStatus] = {DAY: LlmStatus(), ARCHIVE: LlmStatus()}

    # ── Конфигурация ─────────────────────────────────────────
    def port(self, profile: str) -> int:
        return int(self.settings[f"llm_{profile}_port"])

    def base_url(self, profile: str) -> str:
        return f"http://127.0.0.1:{self.port(profile)}"

    def health_url(self, profile: str) -> str:
        return f"{self.base_url(profile)}/health"

    def chat_url(self, profile: str) -> str:
        return f"{self.base_url(profile)}/v1/chat/completions"

    def completion_url(self, profile: str) -> str:
        return f"{self.base_url(profile)}/completion"

    @property
    def script(self) -> Path:
        return Path(self.settings["manage_llm_script"])

    # ── HTTP ping ────────────────────────────────────────────
    def ping(self, profile: str, timeout: float = 8.0) -> LlmStatus:
        st = self._status[profile]
        try:
            with urllib.request.urlopen(self.health_url(profile), timeout=timeout) as resp:
                if resp.status != 200:
                    raise OSError(f"HTTP {resp.status}")
                data = json.loads(resp.read().decode("utf-8"))
            st.online = data.get("status") == "ok"
            st.ctx_size = data.get("n_ctx") or self.settings.get(f"llm_{profile}_ctx")
            st.model = data.get("model")
            st.profile = profile
            st.error = None
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            st.online = False
            st.profile = profile
            st.error = str(exc)
        return st

    def get_status(self, profile: str) -> LlmStatus:
        return self._status[profile]

    # ── Управление сервером ──────────────────────────────────
    def manage(self, profile: str, action: str, timeout: float = 120.0) -> tuple[bool, str]:
        cmd = [str(self.script), "-Profile", profile, "-Action", action]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, ValueError) as exc:
            return False, str(exc)
        out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        return proc.returncode == 0, out

    def start(self, profile: str) -> tuple[bool, str]:
        ok, out = self.manage(profile, "start", timeout=180.0)
        if ok:
            # дать серверу время подняться (как QWEN_BOOT_DELAY_MS)
            import time

            time.sleep(3)
        return ok, out

    def stop(self, profile: str) -> tuple[bool, str]:
        return self.manage(profile, "stop", timeout=60.0)

    # ── Чат (OpenAI-совместимый llama-server) ─────────────────
    def active_profile(self) -> str:
        """Выбрать активный профиль: day если онлайн, иначе archive, иначе day."""
        day = self._status.get(DAY)
        arch = self._status.get(ARCHIVE)
        if day is not None and day.online:
            return DAY
        if arch is not None and arch.online:
            return ARCHIVE
        return DAY

    def chat(
        self,
        messages: list[dict],
        profile: str | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float = 90.0,
    ) -> tuple[bool, str]:
        prof = profile or self.active_profile()
        url = self.chat_url(prof)
        st = self._status.get(prof)
        mdl = model or (st.model if st and st.model else None) or "default"
        payload: dict = {
            "model": mdl,
            "messages": messages,
            "temperature": float(temperature),
            "stream": False,
        }
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status not in (200, 201):
                    raise OSError(f"HTTP {resp.status}")
                body = resp.read().decode("utf-8", errors="replace")
                obj = json.loads(body) if body else {}
        except Exception as exc:  # noqa: BLE001
            # fallback: попробуем /completion если chat недоступен
            try:
                return self._chat_via_completion(messages, prof, timeout=timeout)
            except Exception:
                return False, str(exc)
        # OpenAI-формат
        try:
            if isinstance(obj, dict):
                choices = obj.get("choices")
                if isinstance(choices, list) and choices:
                    first = choices[0]
                    if isinstance(first, dict):
                        msg = first.get("message") or first.get("delta") or {}
                        if isinstance(msg, dict) and msg.get("content"):
                            return True, str(msg["content"])
                        if isinstance(first.get("text"), str):
                            return True, str(first["text"])
                        if isinstance(first.get("content"), str):
                            return True, str(first["content"])
                # прямой content
                if isinstance(obj.get("content"), str):
                    return True, str(obj["content"])
                # llama.cpp completion fallback поле
                if isinstance(obj.get("choices"), list) and not choices:
                    pass
            return True, json.dumps(obj, ensure_ascii=False) if obj else ""
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def _chat_via_completion(
        self, messages: list[dict], profile: str, timeout: float = 90.0
    ) -> tuple[bool, str]:
        """Fallback через /completion (prompt = склейка messages)."""
        prompt = "\n".join(
            f"{m.get('role','user')}: {m.get('content','')}" for m in messages
        )
        url = self.completion_url(profile)
        payload = {"prompt": prompt, "temperature": 0.7, "n_predict": 512, "stream": False}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status not in (200, 201):
                raise OSError(f"HTTP {resp.status}")
            body = resp.read().decode("utf-8", errors="replace")
            obj = json.loads(body) if body else {}
        if isinstance(obj, dict):
            for key in ("content", "response", "completion", "text"):
                if isinstance(obj.get(key), str) and obj[key]:
                    return True, str(obj[key])
            # llama.cpp иногда возвращает {"content":"..."}
            choices = obj.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                c = choices[0]
                if isinstance(c.get("text"), str):
                    return True, str(c["text"])
        return True, json.dumps(obj, ensure_ascii=False)
