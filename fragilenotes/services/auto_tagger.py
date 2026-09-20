"""Auto-tagging LLM для FragileNotes: 3-5 #тегов в frontmatter через LlmService.

При сохранении заметки LLM анализирует текст (до 4000 символов) и предлагает
теги. Пользователь принимает (merge в frontmatter.tags) или отклоняет.

Хранение: frontmatter поле ``tags`` — список строк без ``#``.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import load_settings

try:
    from .llm import LlmService
except Exception:  # pragma: no cover - тесты без llm
    LlmService = None  # type: ignore

MAX_CONTENT_CHARS: int = 4000
SYSTEM_PROMPT: str = (
    "Ты — ассистент FragileNotes. По тексту заметки предложи 3-5 тематических тегов. "
    "Требования: теги на русском или английском, 1-2 слова, lowercase, без #, "
    "слова через дефис (например: python, машинное-обучение, заметки). "
    "Отвечай ТОЛЬКО JSON-массивом строк, например: [\"python\", \"обзор-книги\"]. "
    "Никакого дополнительного текста."
)

# нормализация: разрешены буквы (ru/en), цифры, дефис, подчёркивание
_TAG_CLEAN_RE = re.compile(r"[^a-zа-яё0-9_-]", re.IGNORECASE)
_TAG_SPLIT_RE = re.compile(r"[,\s;]+")
# для поиска уже существующих #тегов в тексте (не нужно для LLM, но для эвристики)
_WORD_RE = re.compile(r"[a-zа-яё]{3,}", re.IGNORECASE)

# стоп-слова (ru + en) для эвристики
_STOP = {
    "это", "что", "как", "для", "или", "при", "про", "ещё", "еще", "был", "была",
    "было", "быть", "есть", "так", "вот", "уже", "тоже", "чтобы", "который",
    "которые", "которые", "надо", "можно", "нужно", "очень", "самый", "эта",
    "этот", "того", "такой", "тогда", "когда", "где", "если", "чем", "тем",
    "the", "and", "for", "with", "from", "that", "this", "have", "are", "was",
    "were", "will", "would", "can", "not", "but", "you", "your",
}

_cache_lock = threading.RLock()


@dataclass(slots=True)
class TagSuggestionResult:
    ok: bool
    tags: list[str] = field(default_factory=list)
    error: str | None = None
    raw: str | None = None
    cached: bool = False


def _normalize_tag(raw: str) -> str | None:
    """Нормализовать один тег: lowercase, trim #, пробелы→дефис, только разрешённые символы."""
    t = raw.strip().lstrip("#").strip()
    if not t:
        return None
    # пробелы/подчёркивания внутри → дефис, множественные дефисы схлопнуть
    t = re.sub(r"[\s_]+", "-", t)
    t = t.lower()
    # убрать недопустимые символы
    t = _TAG_CLEAN_RE.sub("", t)
    t = re.sub(r"-{2,}", "-", t).strip("-_")
    if len(t) < 2 or len(t) > 30:
        return None
    # не число целиком
    if t.isdigit():
        return None
    return t or None


def _parse_llm_tags(raw: str) -> list[str]:
    """Распарсить ответ LLM в список нормализованных тегов."""
    if not raw:
        return []
    s = raw.strip()
    # 1) пробуем JSON
    try:
        obj = json.loads(s)
        if isinstance(obj, list):
            out: list[str] = []
            for item in obj:
                if isinstance(item, str):
                    nt = _normalize_tag(item)
                    if nt and nt not in out:
                        out.append(nt)
            if out:
                return out[:5]
        elif isinstance(obj, dict):
            # иногда LLM отдаёт {"tags": [...]}
            for key in ("tags", "tag", "result"):
                val = obj.get(key)
                if isinstance(val, list):
                    out2: list[str] = []
                    for item in val:
                        if isinstance(item, str):
                            nt = _normalize_tag(item)
                            if nt and nt not in out2:
                                out2.append(nt)
                    if out2:
                        return out2[:5]
    except (json.JSONDecodeError, ValueError):
        pass
    # 2) поиск JSON-массива внутри текста
    m = re.search(r"\[.*?\]", s, re.DOTALL)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                out = []
                for item in arr:
                    if isinstance(item, str):
                        nt = _normalize_tag(item)
                        if nt and nt not in out:
                            out.append(nt)
                if out:
                    return out[:5]
        except (json.JSONDecodeError, ValueError):
            pass
    # 3) fallback: строки с #тегами или через запятую
    # собрать #теги
    hash_tags = re.findall(r"#([a-zа-яё0-9_-]{2,30})", s, re.IGNORECASE)
    if hash_tags:
        out = []
        for h in hash_tags:
            nt = _normalize_tag(h)
            if nt and nt not in out:
                out.append(nt)
        if out:
            return out[:5]
    # 4) split по запятой/пробелу
    parts = _TAG_SPLIT_RE.split(s)
    out = []
    for p in parts:
        # убрать кавычки/скобки
        p = p.strip().strip("\"'[]`")
        if not p:
            continue
        nt = _normalize_tag(p)
        if nt and nt not in out:
            out.append(nt)
        if len(out) >= 5:
            break
    return out[:5]


def _heuristic_tags(text: str, limit: int = 5) -> list[str]:
    """Эвристика без LLM: частые слова ≥3 букв, без стоп-слов."""
    if not text:
        return []
    words = [w.lower() for w in _WORD_RE.findall(text)]
    freq: dict[str, int] = {}
    for w in words:
        if w in _STOP or len(w) < 3:
            continue
        freq[w] = freq.get(w, 0) + 1
    # топ по частоте, затем по длине (предпочтение содержательным)
    sorted_words = sorted(freq.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    out: list[str] = []
    for w, _c in sorted_words:
        nt = _normalize_tag(w)
        if nt and nt not in out:
            out.append(nt)
        if len(out) >= limit:
            break
    return out


def _strip_frontmatter_body(text: str) -> str:
    """Убрать frontmatter для анализа — теги не должны влиять на предложение."""
    try:
        from ..vault import parse_frontmatter

        _fm, body = parse_frontmatter(text)
        return body
    except Exception:
        # fallback: ручной strip
        m = re.match(r"^---[ \t]*\r?\n.*?\r?\n---[ \t]*\r?\n?", text, re.DOTALL)
        if m:
            return text[m.end():]
        return text


def _read_existing_tags(path: Path) -> list[str]:
    """Прочитать tags из frontmatter файла."""
    try:
        from ..vault import parse_frontmatter

        raw = path.read_text(encoding="utf-8")
        fm, _body = parse_frontmatter(raw)
        tags = fm.get("tags")
        if isinstance(tags, list):
            out: list[str] = []
            for t in tags:
                if isinstance(t, str):
                    nt = _normalize_tag(t)
                    if nt and nt not in out:
                        out.append(nt)
            return out
        if isinstance(tags, str):
            # иногда tags: "a, b"
            parts = re.split(r"[,\s]+", tags)
            out = []
            for p in parts:
                nt = _normalize_tag(p)
                if nt and nt not in out:
                    out.append(nt)
            return out
    except Exception:
        pass
    return []


def _merge_tags(existing: list[str], suggested: list[str]) -> list[str]:
    """Объединить существующие и предложенные без дублей (case-insensitive)."""
    seen = {t.lower() for t in existing}
    out = list(existing)
    for s in suggested:
        nt = _normalize_tag(s)
        if nt and nt.lower() not in seen:
            out.append(nt)
            seen.add(nt.lower())
    return out


class AutoTaggerService:
    """Сервис авто-тегирования через LlmService + хранение в frontmatter."""

    def __init__(
        self,
        settings: dict | None = None,
        llm: Any | None = None,
    ) -> None:
        self.settings = settings if settings is not None else load_settings()
        if llm is not None:
            self.llm = llm
        else:
            if LlmService is None:
                self.llm = None
            else:
                try:
                    self.llm = LlmService(self.settings)
                except Exception:
                    self.llm = None

    # ── Предложение тегов ──────────────────────────────────────
    def suggest(
        self,
        text: str,
        existing_tags: list[str] | None = None,
        max_tags: int = 5,
        profile: str | None = None,
        use_heuristic_fallback: bool = True,
    ) -> TagSuggestionResult:
        """Предложить 3-5 тегов по тексту заметки."""
        body = _strip_frontmatter_body(text or "")
        body = body.strip()
        if not body or len(body) < 20:
            return TagSuggestionResult(ok=False, error="текст слишком короткий для тегирования")

        # подготовка контекста для LLM (до 4000 символов)
        snippet = body[:MAX_CONTENT_CHARS]
        # упомянуть существующие теги чтобы LLM не дублировал
        existing_str = ""
        if existing_tags:
            existing_str = f" Уже есть теги: {', '.join(existing_tags)}. Не повторяй их."

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Текст заметки:\n{snippet}\n\nПредложи 3-5 тегов JSON-массивом.{existing_str}",
            },
        ]

        raw_reply: str | None = None
        if self.llm is not None:
            try:
                ok, reply = self.llm.chat(
                    messages,
                    profile=profile,
                    temperature=0.4,
                    max_tokens=200,
                    timeout=60.0,
                )
                if ok and reply:
                    raw_reply = reply.strip()
                    tags = _parse_llm_tags(raw_reply)
                    # убрать дубли с existing
                    if existing_tags:
                        low_exist = {t.lower() for t in existing_tags}
                        tags = [t for t in tags if t.lower() not in low_exist]
                    # кламп 3-5: если <3 дополнить эвристикой, если >5 обрезать
                    if len(tags) < 3 and use_heuristic_fallback:
                        heur = _heuristic_tags(body, limit=5 - len(tags))
                        for h in heur:
                            if h.lower() not in {t.lower() for t in tags} and h.lower() not in (low_exist if existing_tags else set()):
                                tags.append(h)
                            if len(tags) >= 3:
                                break
                    tags = tags[: max(3, min(max_tags, 5))]
                    if 3 <= len(tags) <= 5:
                        return TagSuggestionResult(ok=True, tags=tags, raw=raw_reply)
                    if tags:
                        # если получили 1-2 но валидных — всё равно отдать (лучше чем ничего)
                        return TagSuggestionResult(ok=True, tags=tags, raw=raw_reply)
                    # пусто — fallback
            except Exception as exc:  # noqa: BLE001
                raw_reply = str(exc)

        # fallback: эвристика
        if use_heuristic_fallback:
            heur = _heuristic_tags(body, limit=max_tags)
            if existing_tags:
                low_exist = {t.lower() for t in existing_tags}
                heur = [h for h in heur if h.lower() not in low_exist]
            if heur:
                heur = heur[:max_tags]
                # эвристика может дать <3 — это ок для офлайна
                return TagSuggestionResult(ok=True, tags=heur, raw=raw_reply, error=None if raw_reply is None else "LLM недоступен, эвристика")
        return TagSuggestionResult(ok=False, tags=[], error=raw_reply or "LLM недоступен и эвристика не дала тегов", raw=raw_reply)

    def suggest_for_file(
        self,
        path: Path | str,
        max_tags: int = 5,
        profile: str | None = None,
    ) -> TagSuggestionResult:
        """Прочитать файл и предложить теги (учитывает существующие из frontmatter)."""
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            return TagSuggestionResult(ok=False, error=str(exc))
        existing = _read_existing_tags(p)
        return self.suggest(text, existing_tags=existing, max_tags=max_tags, profile=profile)

    # ── Хранение в frontmatter ─────────────────────────────────
    def get_existing_tags(self, path: Path | str) -> list[str]:
        return _read_existing_tags(Path(path))

    def apply_tags(
        self,
        path: Path | str,
        tags: list[str],
        overwrite: bool = False,
    ) -> tuple[bool, str | None]:
        """Добавить теги в frontmatter.tags (merge без дублей).

        Returns: (ok, error)
        """
        p = Path(path)
        if not p.is_file():
            return False, f"файл не найден: {p}"
        # нормализовать входящие
        normed: list[str] = []
        for t in tags:
            nt = _normalize_tag(t)
            if nt and nt not in normed:
                normed.append(nt)
        if not normed:
            return False, "нет валидных тегов для применения"
        if len(normed) < 3 or len(normed) > 5:
            # мягкое предупреждение но не блок — UI уже отфильтровал, режем до 5
            normed = normed[:5]
            if len(normed) < 1:
                return False, "недостаточно тегов"

        try:
            from ..vault import parse_frontmatter, serialize_frontmatter

            raw = p.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(raw)
            existing = fm.get("tags")
            if isinstance(existing, list):
                existing_norm = []
                for t in existing:
                    if isinstance(t, str):
                        nt = _normalize_tag(t)
                        if nt and nt not in existing_norm:
                            existing_norm.append(nt)
            elif isinstance(existing, str) and existing.strip():
                existing_norm = []
                for part in re.split(r"[,\s]+", existing):
                    nt = _normalize_tag(part)
                    if nt and nt not in existing_norm:
                        existing_norm.append(nt)
            else:
                existing_norm = []

            if overwrite:
                merged = normed
            else:
                merged = _merge_tags(existing_norm, normed)

            if not merged:
                return False, "нет тегов после мержа"

            fm["tags"] = merged
            new_content = serialize_frontmatter(fm) + body.lstrip("\n")
            p.write_text(new_content, encoding="utf-8")
            return True, None
        except OSError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def reject_tags(self, path: Path | str, tags: list[str]) -> None:
        """Заглушка для логирования отклонения (пока no-op)."""
        # в будущем можно писать в аналитику/лог
        _ = (path, tags)
        return


# ── Удобные функциональные обёртки ─────────────────────────────

def suggest_tags(
    text: str,
    settings: dict | None = None,
    llm: Any | None = None,
    existing_tags: list[str] | None = None,
) -> TagSuggestionResult:
    svc = AutoTaggerService(settings=settings, llm=llm)
    return svc.suggest(text, existing_tags=existing_tags)


def suggest_for_file(
    path: Path | str,
    settings: dict | None = None,
    llm: Any | None = None,
) -> TagSuggestionResult:
    svc = AutoTaggerService(settings=settings, llm=llm)
    return svc.suggest_for_file(path)


def apply_tags_to_file(
    path: Path | str,
    tags: list[str],
    settings: dict | None = None,
) -> tuple[bool, str | None]:
    svc = AutoTaggerService(settings=settings)
    return svc.apply_tags(path, tags)


def get_tags_from_frontmatter(path: Path | str) -> list[str]:
    return _read_existing_tags(Path(path))


__all__ = [
    "AutoTaggerService",
    "TagSuggestionResult",
    "suggest_tags",
    "suggest_for_file",
    "apply_tags_to_file",
    "get_tags_from_frontmatter",
    "MAX_CONTENT_CHARS",
    "SYSTEM_PROMPT",
]
