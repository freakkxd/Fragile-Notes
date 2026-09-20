"""AI-саммари дайджеста: генерация через LlmService, кэш (TTL + disk), сбор контекста."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import APP_DIR
from .llm import LlmService

CACHE_TTL_SECONDS: int = 1800  # 30 минут — кэш саммари
CACHE_FILE: Path = APP_DIR / "ai_summary_cache.json"
MAX_CONTEXT_CHARS: int = 7000
MAX_NOTE_PREVIEW: int = 600
SYSTEM_PROMPT: str = (
    "Ты — ассистент FragileNotes. Сделай краткий дайджест последних заметок пользователя. "
    "Отвечай на русском, структурированно: 1) Главные темы 2) Ключевые идеи/выводы 3) Что требует внимания. "
    "Будь лаконичен (до 12 пунктов), не выдумывай факты. Если заметок нет — честно скажи."
)

_cache_lock = threading.RLock()


@dataclass(slots=True)
class SummaryResult:
    ok: bool
    text: str
    cached: bool = False
    error: str | None = None


def _ensure_app_dir() -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _load_cache() -> dict[str, Any]:
    if not CACHE_FILE.is_file():
        return {}
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(data: dict[str, Any]) -> None:
    _ensure_app_dir()
    tmp = CACHE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except OSError:
        pass


def _compute_hash(parts: list[str]) -> str:
    h = hashlib.sha256("\n".join(parts).encode("utf-8", errors="replace")).hexdigest()
    return h[:16]


def _collect_context(settings: dict) -> tuple[list[Any], Any, str]:
    """Собрать последние заметки + дайджест -> (recent, digest, hash)."""
    from . import vault as vault_svc

    try:
        recent = vault_svc.scan_recent_notes(settings, days=7, limit=10)
    except Exception:
        recent = []
    try:
        digest = vault_svc.latest_digest(settings)
    except Exception:
        digest = None

    hash_parts: list[str] = []
    for n in recent:
        try:
            hash_parts.append(f"{n.path}:{n.mtime}")
        except Exception:
            continue
    if digest is not None:
        try:
            hash_parts.append(f"{digest.path}:{digest.path.stat().st_mtime}")
        except OSError:
            hash_parts.append(str(digest.path))
    ctx_hash = _compute_hash(hash_parts) if hash_parts else "empty"
    return recent, digest, ctx_hash


def _build_prompt_text(settings: dict, recent: list[Any], digest: Any) -> str:
    lines: list[str] = []
    if digest is not None:
        try:
            title = getattr(digest, "title", str(digest.path))
            preview = getattr(digest, "preview", "") or ""
            cands = getattr(digest, "candidates", 0)
            lines.append(f"Дайджест ночного пробега: {title}")
            if preview:
                lines.append(f"Превью: {preview[:800]}")
            lines.append(f"Кандидатов: {cands}")
            lines.append("")
        except Exception:
            pass
    if recent:
        lines.append(f"Последние заметки ({len(recent)}):")
        for n in recent[:10]:
            try:
                title = getattr(n, "title", None)
                if title is None:
                    from .vault import note_title as _nt
                    title = _nt(n.path)
                else:
                    # RecentNote has no title field, title is via path
                    from .vault import note_title as _nt
                    title = _nt(n.path)
            except Exception:
                title = getattr(n.path, "name", str(n.path))
            try:
                raw = n.path.read_text(encoding="utf-8", errors="replace")[:MAX_NOTE_PREVIEW]
                raw = " ".join(raw.split())
            except OSError:
                raw = ""
            snippet = raw[:MAX_NOTE_PREVIEW]
            lines.append(f"- {title}: {snippet}")
    else:
        lines.append("Свежих заметок за неделю нет.")
    text = "\n".join(lines)
    if len(text) > MAX_CONTEXT_CHARS:
        text = text[:MAX_CONTEXT_CHARS] + "\n…(обрезано)"
    return text


def _prompt_messages(context_text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Сделай саммари по материалам:\n\n{context_text}"},
    ]


class AiSummaryService:
    """Генерация саммари через LlmService с TTL-кэшем на диске и в памяти."""

    def __init__(
        self,
        settings: dict | None = None,
        llm: LlmService | None = None,
        cache_ttl: int = CACHE_TTL_SECONDS,
    ) -> None:
        from ..config import load_settings as _load

        self.settings = settings if settings is not None else _load()
        self.llm = llm if llm is not None else LlmService(self.settings)
        self.cache_ttl = int(cache_ttl)
        self._mem_cache: dict[str, Any] | None = None
        self._mem_at: float = 0.0

    # ── Кэш ──────────────────────────────────────────────────
    def _cache_key(self, ctx_hash: str) -> str:
        root = str(self.settings.get("vault_root") or "")
        return f"digest:{root}:{ctx_hash}"

    def get_cached(self, ctx_hash: str | None = None) -> SummaryResult | None:
        """Вернуть кэшированное саммари если есть и не протухло."""
        if ctx_hash is None:
            _, _, ctx_hash = _collect_context(self.settings)
        key = self._cache_key(ctx_hash)
        now = time.time()
        # память
        if self._mem_cache is not None and now - self._mem_at < self.cache_ttl:
            hit = self._mem_cache.get(key)
            if hit is not None and now - hit.get("ts", 0) < self.cache_ttl:
                return SummaryResult(ok=True, text=hit["text"], cached=True)
        with _cache_lock:
            data = _load_cache()
            hit = data.get(key)
            if hit is None or not isinstance(hit, dict):
                return None
            ts = float(hit.get("ts") or 0)
            if now - ts >= self.cache_ttl:
                return None
            text = str(hit.get("text") or "")
            if not text:
                return None
            # прогреть память
            self._mem_cache = data
            self._mem_at = now
            return SummaryResult(ok=True, text=text, cached=True)

    def clear_cache(self) -> None:
        with _cache_lock:
            try:
                if CACHE_FILE.is_file():
                    CACHE_FILE.unlink()
            except OSError:
                pass
            self._mem_cache = None
            self._mem_at = 0.0

    def _store_cache(self, ctx_hash: str, text: str) -> None:
        key = self._cache_key(ctx_hash)
        now = time.time()
        entry = {"text": text, "ts": now, "hash": ctx_hash}
        with _cache_lock:
            data = _load_cache()
            data[key] = entry
            # чистка протухших ключей этого vault
            root = str(self.settings.get("vault_root") or "")
            prefix = f"digest:{root}:"
            to_del = [k for k, v in data.items() if k.startswith(prefix) and k != key and now - float(v.get("ts", 0)) >= self.cache_ttl]
            for k in to_del:
                data.pop(k, None)
            _save_cache(data)
            self._mem_cache = data
            self._mem_at = now

    # ── Генерация ────────────────────────────────────────────
    def generate(
        self,
        force: bool = False,
        max_tokens: int = 900,
        temperature: float = 0.6,
        profile: str | None = None,
    ) -> SummaryResult:
        """Сгенерировать саммари. Если force=False и есть свежий кэш — вернуть кэш."""
        recent, digest, ctx_hash = _collect_context(self.settings)

        # пустой контекст — всё равно пробуем вернуть кэш, иначе ошибка
        if not recent and digest is None:
            cached = self.get_cached(ctx_hash)
            if cached is not None and not force:
                return cached
            # короткий фолбэк без LLM
            return SummaryResult(ok=False, text="", error="нет заметок для саммари")

        if not force:
            cached = self.get_cached(ctx_hash)
            if cached is not None:
                return cached

        context_text = _build_prompt_text(self.settings, recent, digest)
        messages = _prompt_messages(context_text)

        # LlmService.chat — синхронный HTTP вызов
        try:
            ok, reply = self.llm.chat(
                messages,
                profile=profile,
                temperature=float(temperature),
                max_tokens=int(max_tokens),
                timeout=90.0,
            )
        except Exception as exc:  # noqa: BLE001
            return SummaryResult(ok=False, text="", error=str(exc))

        if not ok:
            return SummaryResult(ok=False, text="", error=reply or "LLM недоступен")
        reply = (reply or "").strip()
        if not reply:
            return SummaryResult(ok=False, text="", error="пустой ответ LLM")

        # сохранить в кэш
        try:
            self._store_cache(ctx_hash, reply)
        except Exception:
            pass
        return SummaryResult(ok=True, text=reply, cached=False)

    # alias для удобства UI
    def get_summary(self, force: bool = False) -> SummaryResult:
        return self.generate(force=force)


# ── Удобные функциональные обёртки ──────────────────────────

def generate_summary(
    settings: dict | None = None,
    llm: LlmService | None = None,
    force: bool = False,
) -> SummaryResult:
    svc = AiSummaryService(settings=settings, llm=llm)
    return svc.generate(force=force)


def get_cached_summary(
    settings: dict | None = None,
    llm: LlmService | None = None,
) -> SummaryResult | None:
    svc = AiSummaryService(settings=settings, llm=llm)
    _, _, h = _collect_context(svc.settings)
    return svc.get_cached(h)


def clear_summary_cache() -> None:
    with _cache_lock:
        try:
            if CACHE_FILE.is_file():
                CACHE_FILE.unlink()
        except OSError:
            pass


__all__ = [
    "AiSummaryService",
    "SummaryResult",
    "CACHE_TTL_SECONDS",
    "CACHE_FILE",
    "generate_summary",
    "get_cached_summary",
    "clear_summary_cache",
]
