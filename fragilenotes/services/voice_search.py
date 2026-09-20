"""Голосовой поиск по vault: распознавание речи через whisper / SpeechRecognition.

Поддерживает два движка распознавания:

* **whisper** (openai-whisper) — оффлайн, предпочтительный. Если установлен,
  используется ``whisper.load_model("base").transcribe(..., language="ru")``.
* **SpeechRecognition** (``speech_recognition``) — распознавание из файла
  или микрофона через Google Web Speech API (``recognize_google``) как
  фолбэк. Требует ``SpeechRecognition`` + ``PyAudio``/микрофон для live-режима.

Если ни один движок не установлен — все функции возвращают ``ok=False``
с понятным сообщением, не падают. Поиск идёт через
``services.vault.search_notes`` (TTL-кэш индекса), ранжирование — как у
quick_switcher.

Используется в:

* ``ui.voice_view`` — транскрибация голосовых заметок (делегирует сюда при
  импорте).
* ``ui.quick_switcher`` — кнопка 🎙️ (микрофон) рядом с полем поиска.
  Нажатие запускает фоновый поток: запись → транскрибация → подстановка
  текста в entry → обычный vault-поиск.

Без внешних зависимостей в stdlib-режиме: ``shutil.which``-проверки нет,
только ``try: import`` с флагами ``HAS_*``.
"""

from __future__ import annotations

import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── whisper (опционально) ──────────────────────────────────────────
HAS_WHISPER = False
try:
    import whisper  # type: ignore  # noqa: F401

    HAS_WHISPER = True
except Exception:
    whisper = None  # type: ignore
    HAS_WHISPER = False

# ── SpeechRecognition (опционально) ────────────────────────────────
HAS_SR = False
HAS_SPEECH_RECOGNITION = False
try:
    import speech_recognition as sr  # type: ignore  # noqa: F401

    HAS_SR = True
    HAS_SPEECH_RECOGNITION = True
except Exception:
    sr = None  # type: ignore
    HAS_SR = False
    HAS_SPEECH_RECOGNITION = False

# Алиасы для проверок в UI
HAS_VOICE = HAS_WHISPER or HAS_SR


# ── Результат транскрибации ────────────────────────────────────────

@dataclass(slots=True)
class TranscriptionResult:
    """Результат распознавания речи."""

    ok: bool
    text: str
    engine: str | None = None
    error: str | None = None
    raw: str | None = None


@dataclass(slots=True)
class VoiceSearchResult:
    """Результат голосового поиска: распознанный текст + хиты vault."""

    ok: bool
    query: str
    hits: list[Any]
    engine: str | None = None
    error: str | None = None


# ── availability helpers ───────────────────────────────────────────

def is_whisper_available() -> bool:
    """Установлен ли openai-whisper."""
    return HAS_WHISPER


def is_speech_recognition_available() -> bool:
    """Установлен ли SpeechRecognition."""
    return HAS_SR


# алиасы
is_sr_available = is_speech_recognition_available
is_speech_available = is_speech_recognition_available
tesseract_available = is_whisper_available  # noqa: F401 — для единообразия


def is_voice_available() -> bool:
    """Доступен ли хотя бы один движок распознавания."""
    return HAS_WHISPER or HAS_SR


def is_available() -> bool:
    """Алиас для is_voice_available."""
    return is_voice_available()


def available_engines() -> list[str]:
    """Список доступных движков: ``['whisper', 'speech_recognition']``."""
    out: list[str] = []
    if HAS_WHISPER:
        out.append("whisper")
    if HAS_SR:
        out.append("speech_recognition")
    return out


def get_available_engines() -> list[str]:
    return available_engines()


# ── whisper ────────────────────────────────────────────────────────

def transcribe_with_whisper(
    audio_path: Path | str,
    language: str = "ru",
    model_name: str = "base",
    fp16: bool = False,
) -> TranscriptionResult:
    """Распознать файл через whisper.

    Args:
        audio_path: путь к wav/mp3/ogg/m4a.
        language: язык (``"ru"`` / ``"en"`` / ``None`` — авто).
        model_name: модель whisper (``"tiny"``, ``"base"``, ``"small"`` ...).
        fp16: использовать fp16 (False для CPU).

    Returns:
        TranscriptionResult(ok, text, engine="whisper").
    """
    p = Path(audio_path)
    if not p.is_file():
        return TranscriptionResult(ok=False, text="", engine="whisper", error=f"файл не найден: {p}", raw=None)
    if not HAS_WHISPER:
        return TranscriptionResult(
            ok=False,
            text="",
            engine="whisper",
            error="whisper не установлен (pip install openai-whisper)",
        )
    try:
        import whisper as _w  # type: ignore

        model = _w.load_model(model_name)
        # language=None -> автоопределение; пустая строка тоже авто
        lang = language if language else None
        kwargs: dict[str, Any] = {"fp16": bool(fp16)}
        if lang:
            kwargs["language"] = lang
        result = model.transcribe(str(p), **kwargs)
        if isinstance(result, dict):
            text = (result.get("text") or "").strip()
        else:
            text = str(result or "").strip()
        if not text:
            return TranscriptionResult(ok=False, text="", engine="whisper", error="пустая транскрибация", raw=text)
        return TranscriptionResult(ok=True, text=text, engine="whisper", error=None, raw=text)
    except Exception as exc:  # noqa: BLE001
        return TranscriptionResult(ok=False, text="", engine="whisper", error=f"ошибка whisper: {exc}")


# алиасы
whisper_transcribe = transcribe_with_whisper
transcribe_whisper = transcribe_with_whisper


# ── SpeechRecognition (файл) ───────────────────────────────────────

def transcribe_with_speech_recognition(
    audio_path: Path | str,
    language: str = "ru-RU",
    timeout: float = 30.0,
) -> TranscriptionResult:
    """Распознать файл через SpeechRecognition (Google Web Speech).

    Args:
        audio_path: путь к аудио (wav/aiff/flac).
        language: язык Google API (``"ru-RU"``, ``"en-US"``).
        timeout: не используется для файла, для единообразия.

    Returns:
        TranscriptionResult(ok, text, engine="speech_recognition").
    """
    p = Path(audio_path)
    if not p.is_file():
        return TranscriptionResult(ok=False, text="", engine="speech_recognition", error=f"файл не найден: {p}")
    if not HAS_SR:
        return TranscriptionResult(
            ok=False,
            text="",
            engine="speech_recognition",
            error="SpeechRecognition не установлен (pip install SpeechRecognition)",
        )
    try:
        import speech_recognition as _sr  # type: ignore

        recognizer = _sr.Recognizer()
        with _sr.AudioFile(str(p)) as source:
            audio = recognizer.record(source)
        # Google Web Speech — требует интернет; при отсутствии — UnknownValueError
        try:
            text = recognizer.recognize_google(audio, language=language)
        except _sr.UnknownValueError:
            return TranscriptionResult(ok=False, text="", engine="speech_recognition", error="не удалось распознать речь")
        except _sr.RequestError as exc:
            return TranscriptionResult(ok=False, text="", engine="speech_recognition", error=f"ошибка Google API: {exc}")
        text = (text or "").strip()
        if not text:
            return TranscriptionResult(ok=False, text="", engine="speech_recognition", error="пустой результат")
        return TranscriptionResult(ok=True, text=text, engine="speech_recognition", error=None, raw=text)
    except Exception as exc:  # noqa: BLE001
        return TranscriptionResult(ok=False, text="", engine="speech_recognition", error=str(exc))


# алиасы
sr_transcribe = transcribe_with_speech_recognition
transcribe_sr = transcribe_with_speech_recognition
transcribe_with_sr = transcribe_with_speech_recognition


# ── Универсальный файл-транскрайб ──────────────────────────────────

def transcribe_audio(
    audio_path: Path | str,
    language: str = "ru",
    sr_language: str = "ru-RU",
    prefer: str = "whisper",
    model_name: str = "base",
) -> TranscriptionResult:
    """Распознать аудиофайл, автоматически выбирая движок.

    Порядок: ``prefer`` (``"whisper"`` по умолчанию) → фолбэк на второй движок.
    Если оба недоступны — ``ok=False``.

    Args:
        audio_path: путь к файлу.
        language: язык для whisper (``"ru"``).
        sr_language: язык для SpeechRecognition (``"ru-RU"``).
        prefer: ``"whisper"`` или ``"sr"``/``"speech_recognition"``.
        model_name: модель whisper.

    Returns:
        TranscriptionResult.
    """
    pref = (prefer or "whisper").strip().lower()
    # whisper первый
    if pref in ("whisper", "openai-whisper"):
        if HAS_WHISPER:
            res = transcribe_with_whisper(audio_path, language=language, model_name=model_name)
            if res.ok:
                return res
            # если whisper вернул ошибку «не установлен» не пробуем второй? — пробуем
            if HAS_SR:
                sr_res = transcribe_with_speech_recognition(audio_path, language=sr_language)
                if sr_res.ok:
                    return sr_res
            return res
        if HAS_SR:
            return transcribe_with_speech_recognition(audio_path, language=sr_language)
        return TranscriptionResult(ok=False, text="", engine=None, error="нет движков: установи whisper или SpeechRecognition")
    # sr первый
    if pref in ("sr", "speech_recognition", "speechrecognition", "google"):
        if HAS_SR:
            res = transcribe_with_speech_recognition(audio_path, language=sr_language)
            if res.ok:
                return res
            if HAS_WHISPER:
                w_res = transcribe_with_whisper(audio_path, language=language, model_name=model_name)
                if w_res.ok:
                    return w_res
            return res
        if HAS_WHISPER:
            return transcribe_with_whisper(audio_path, language=language, model_name=model_name)
        return TranscriptionResult(ok=False, text="", engine=None, error="нет движков: установи whisper или SpeechRecognition")
    # неизвестный prefer — пробуем whisper затем sr
    if HAS_WHISPER:
        res = transcribe_with_whisper(audio_path, language=language, model_name=model_name)
        if res.ok:
            return res
    if HAS_SR:
        return transcribe_with_speech_recognition(audio_path, language=sr_language)
    return TranscriptionResult(ok=False, text="", engine=None, error="нет движков: установи whisper или SpeechRecognition")


# алиасы
transcribe_file = transcribe_audio
recognize_file = transcribe_audio
recognize_speech = transcribe_audio


def transcribe(
    audio_path: Path | str,
    language: str = "ru",
    **kwargs: Any,
) -> tuple[bool, str]:
    """Совместимый кортеж-алиас ``(ok, text_or_error)``."""
    res = transcribe_audio(audio_path, language=language, **kwargs)
    if res.ok:
        return True, res.text
    return False, res.error or "ошибка распознавания"


# ── Микрофон (live) ────────────────────────────────────────────────

def transcribe_microphone(
    language: str = "ru-RU",
    whisper_language: str = "ru",
    timeout: float | None = 5.0,
    phrase_time_limit: float | None = 8.0,
    prefer: str = "whisper",
    model_name: str = "base",
    energy_threshold: int | None = None,
) -> TranscriptionResult:
    """Записать с микрофона и распознать.

    Использует ``speech_recognition.Microphone`` для захвата аудио.
    Если ``prefer=="whisper"`` и whisper установлен — сохраняет временный
    wav и прогоняет через whisper (оффлайн). Иначе — ``recognize_google``.

    Args:
        language: язык для Google (ru-RU).
        whisper_language: язык для whisper (ru).
        timeout: ожидание речи (сек), None — бесконечно.
        phrase_time_limit: макс длительность фразы (сек).
        prefer: предпочтительный движок.
        model_name: модель whisper.
        energy_threshold: порог энергии микрофона (None — дефолт SR).

    Returns:
        TranscriptionResult. Если микрофон недоступен — ok=False.
    """
    if not HAS_SR:
        # без SpeechRecognition даже захват с микрофона невозможен
        if HAS_WHISPER:
            return TranscriptionResult(
                ok=False,
                text="",
                engine=None,
                error="для записи с микрофона нужен SpeechRecognition (pip install SpeechRecognition PyAudio)",
            )
        return TranscriptionResult(ok=False, text="", engine=None, error="нет движков: установи whisper/SpeechRecognition")

    try:
        import speech_recognition as _sr  # type: ignore

        recognizer = _sr.Recognizer()
        if energy_threshold is not None:
            recognizer.energy_threshold = int(energy_threshold)
        # dynamic adjustment может замедлить но улучшает качество
        try:
            mic = _sr.Microphone()
        except (OSError, AttributeError) as exc:
            return TranscriptionResult(ok=False, text="", engine=None, error=f"микрофон недоступен: {exc}")

        with mic as source:
            try:
                recognizer.adjust_for_ambient_noise(source, duration=0.6)
            except Exception:
                pass
            try:
                audio = recognizer.listen(source, timeout=timeout, phrase_time_limit=phrase_time_limit)
            except _sr.WaitTimeoutError:
                return TranscriptionResult(ok=False, text="", engine=None, error="таймаут — речь не обнаружена")

        pref = (prefer or "whisper").strip().lower()
        # если whisper предпочтительнее — сохраняем wav во временный файл и whisper'им
        if pref in ("whisper", "openai-whisper") and HAS_WHISPER:
            tmp_path: Path | None = None
            try:
                wav_data = audio.get_wav_data()
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                    tf.write(wav_data)
                    tmp_path = Path(tf.name)
                w_res = transcribe_with_whisper(tmp_path, language=whisper_language, model_name=model_name)
                if w_res.ok:
                    return w_res
                # фолбэк на Google если whisper дал пусто/ошибку
                try:
                    text = recognizer.recognize_google(audio, language=language)
                    text = (text or "").strip()
                    if text:
                        return TranscriptionResult(ok=True, text=text, engine="speech_recognition", error=None, raw=text)
                except Exception:
                    pass
                return w_res
            except Exception as exc:  # noqa: BLE001
                # пробуем Google
                try:
                    text = recognizer.recognize_google(audio, language=language)
                    text = (text or "").strip()
                    if text:
                        return TranscriptionResult(ok=True, text=text, engine="speech_recognition", error=None, raw=text)
                except Exception:
                    pass
                return TranscriptionResult(ok=False, text="", engine="whisper", error=str(exc))
            finally:
                if tmp_path is not None:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
        # иначе — сразу Google
        try:
            text = recognizer.recognize_google(audio, language=language)
            text = (text or "").strip()
            if not text:
                return TranscriptionResult(ok=False, text="", engine="speech_recognition", error="пустой результат")
            return TranscriptionResult(ok=True, text=text, engine="speech_recognition", error=None, raw=text)
        except _sr.UnknownValueError:
            return TranscriptionResult(ok=False, text="", engine="speech_recognition", error="не удалось распознать речь")
        except _sr.RequestError as exc:
            return TranscriptionResult(ok=False, text="", engine="speech_recognition", error=f"ошибка Google API: {exc}")

    except ImportError as exc:
        return TranscriptionResult(ok=False, text="", engine=None, error=f"SpeechRecognition не установлен: {exc}")
    except Exception as exc:  # noqa: BLE001
        return TranscriptionResult(ok=False, text="", engine=None, error=str(exc))


# алиасы live
record_and_transcribe = transcribe_microphone
listen_and_transcribe = transcribe_microphone
recognize_microphone = transcribe_microphone
microphone_transcribe = transcribe_microphone


# ── Поиск по vault ─────────────────────────────────────────────────

def search_by_text(
    settings: dict[str, Any],
    query: str,
    limit: int = 14,
) -> list[Any]:
    """Поиск по vault по тексту (обёртка над services.vault.search_notes)."""
    q = (query or "").strip()
    if not q:
        return []
    try:
        from . import vault as vault_svc

        return vault_svc.search_notes(settings, q, limit=limit)
    except Exception:
        return []


# алиас
search_notes = search_by_text
search_vault = search_by_text


def voice_search(
    settings: dict[str, Any],
    audio_path: Path | str,
    limit: int = 14,
    language: str = "ru",
    sr_language: str = "ru-RU",
    prefer: str = "whisper",
    model_name: str = "base",
) -> VoiceSearchResult:
    """Распознать аудиофайл и найти заметки по распознанному запросу.

    Args:
        settings: настройки (vault_root и т.д.).
        audio_path: путь к аудио.
        limit: лимит хитов.
        language: язык whisper.
        sr_language: язык SR.
        prefer: предпочтительный движок.
        model_name: модель whisper.

    Returns:
        VoiceSearchResult(ok, query, hits, engine, error).
    """
    res = transcribe_audio(audio_path, language=language, sr_language=sr_language, prefer=prefer, model_name=model_name)
    if not res.ok:
        return VoiceSearchResult(ok=False, query="", hits=[], engine=res.engine, error=res.error or "ошибка распознавания")
    query = (res.text or "").strip()
    if not query:
        return VoiceSearchResult(ok=False, query="", hits=[], engine=res.engine, error="пустой запрос после распознавания")
    hits = search_by_text(settings, query, limit=limit)
    return VoiceSearchResult(ok=True, query=query, hits=hits, engine=res.engine, error=None)


def voice_search_text(
    settings: dict[str, Any],
    query: str,
    limit: int = 14,
) -> VoiceSearchResult:
    """Поиск по уже распознанному тексту (без аудио)."""
    q = (query or "").strip()
    if not q:
        return VoiceSearchResult(ok=False, query="", hits=[], engine=None, error="пустой запрос")
    hits = search_by_text(settings, q, limit=limit)
    return VoiceSearchResult(ok=True, query=q, hits=hits, engine=None, error=None)


# алиасы голосового поиска
search_by_voice = voice_search
voice_search_by_audio = voice_search
query_by_voice = voice_search


# ── Сервис (SettingsObserver + DI, как в VaultService/LlmService) ──

class VoiceSearchService:
    """Сервис голосового поиска: распознавание + поиск по vault.

    Хранит копию ``settings``, поддерживает ``update_settings()`` для
    синхронизации из ``app.py``. DI для vault: ``vault_module``.

    Пример:
        svc = VoiceSearchService(settings)
        res = svc.transcribe_file(Path("voice.wav"))
        if res.ok:
            hits = svc.search(res.text)
        # live
        res = svc.transcribe_microphone(timeout=5)
        vs = svc.voice_search_file(Path("voice.wav"))
    """

    def __init__(
        self,
        settings: dict[str, Any] | None = None,
        vault: Any | None = None,
        vault_module: Any | None = None,
        prefer: str = "whisper",
        language: str = "ru",
        sr_language: str = "ru-RU",
        model_name: str = "base",
    ) -> None:
        from ..config import load_settings as _load

        self._settings: dict[str, Any] = dict(settings) if settings is not None else {}
        if not self._settings:
            try:
                self._settings = _load()
            except Exception:
                self._settings = {}
        # DI vault
        mod = vault if vault is not None else vault_module
        if mod is not None:
            self._vault = mod
        else:
            try:
                from . import vault as _vault

                self._vault = _vault
            except Exception:
                self._vault = None
        self.prefer: str = prefer
        self.language: str = language
        self.sr_language: str = sr_language
        self.model_name: str = model_name
        self._lock = threading.RLock()

    # ── settings ──────────────────────────────────────────────────
    @property
    def settings(self) -> dict[str, Any]:
        return self._settings

    @settings.setter
    def settings(self, value: dict[str, Any]) -> None:
        self._settings = dict(value) if value is not None else {}

    def update_settings(self, settings: dict[str, Any]) -> None:
        """SettingsObserver: синхронизация копии настроек."""
        self._settings = dict(settings) if settings is not None else {}

    def _s(self, settings: dict[str, Any] | None) -> dict[str, Any]:
        return settings if settings is not None else self._settings

    # ── availability ──────────────────────────────────────────────
    def is_available(self, engine: str | None = None) -> bool:
        if engine is None:
            return is_voice_available()
        e = engine.strip().lower()
        if e in ("whisper", "openai-whisper"):
            return is_whisper_available()
        if e in ("sr", "speech_recognition", "speechrecognition", "google"):
            return is_speech_recognition_available()
        return is_voice_available()

    def is_whisper_available(self) -> bool:
        return is_whisper_available()

    def is_sr_available(self) -> bool:
        return is_speech_recognition_available()

    def available_engines(self) -> list[str]:
        return available_engines()

    # ── транскрибация ─────────────────────────────────────────────
    def transcribe_file(
        self,
        audio_path: Path | str,
        language: str | None = None,
        prefer: str | None = None,
        model_name: str | None = None,
        settings: dict[str, Any] | None = None,  # noqa: ARG002 — для совместимости сигнатур
    ) -> TranscriptionResult:
        """Распознать файл."""
        return transcribe_audio(
            audio_path,
            language=language or self.language,
            sr_language=self.sr_language,
            prefer=prefer or self.prefer,
            model_name=model_name or self.model_name,
        )

    def transcribe_audio(self, audio_path: Path | str, **kwargs: Any) -> TranscriptionResult:
        return self.transcribe_file(audio_path, **kwargs)

    def transcribe_microphone(
        self,
        language: str | None = None,
        timeout: float | None = 5.0,
        phrase_time_limit: float | None = 8.0,
        prefer: str | None = None,
        model_name: str | None = None,
    ) -> TranscriptionResult:
        """Записать с микрофона и распознать."""
        return transcribe_microphone(
            language=language or self.sr_language,
            whisper_language=self.language if (language is None) else (language.split("-")[0] if "-" in language else language),
            timeout=timeout,
            phrase_time_limit=phrase_time_limit,
            prefer=prefer or self.prefer,
            model_name=model_name or self.model_name,
        )

    # алиасы
    def listen(self, **kwargs: Any) -> TranscriptionResult:
        return self.transcribe_microphone(**kwargs)

    def recognize_microphone(self, **kwargs: Any) -> TranscriptionResult:
        return self.transcribe_microphone(**kwargs)

    def record_and_transcribe(self, **kwargs: Any) -> TranscriptionResult:
        return self.transcribe_microphone(**kwargs)

    # ── поиск ─────────────────────────────────────────────────────
    def search(
        self,
        query: str,
        limit: int = 14,
        settings: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Поиск по распознанному тексту."""
        return search_by_text(self._s(settings), query, limit=limit)

    def search_notes(self, query: str, limit: int = 14, settings: dict[str, Any] | None = None) -> list[Any]:
        return self.search(query, limit=limit, settings=settings)

    # ── голосовой поиск (аудио → хиты) ────────────────────────────
    def voice_search_file(
        self,
        audio_path: Path | str,
        limit: int = 14,
        language: str | None = None,
        prefer: str | None = None,
        model_name: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> VoiceSearchResult:
        """Распознать файл и найти заметки."""
        res = self.transcribe_file(audio_path, language=language, prefer=prefer, model_name=model_name)
        if not res.ok:
            return VoiceSearchResult(ok=False, query="", hits=[], engine=res.engine, error=res.error)
        hits = self.search(res.text, limit=limit, settings=settings)
        return VoiceSearchResult(ok=True, query=res.text, hits=hits, engine=res.engine, error=None)

    def voice_search_microphone(
        self,
        limit: int = 14,
        language: str | None = None,
        timeout: float | None = 5.0,
        phrase_time_limit: float | None = 8.0,
        prefer: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> VoiceSearchResult:
        """Записать с микрофона и найти заметки."""
        res = self.transcribe_microphone(language=language, timeout=timeout, phrase_time_limit=phrase_time_limit, prefer=prefer)
        if not res.ok:
            return VoiceSearchResult(ok=False, query="", hits=[], engine=res.engine, error=res.error)
        hits = self.search(res.text, limit=limit, settings=settings)
        return VoiceSearchResult(ok=True, query=res.text, hits=hits, engine=res.engine, error=None)

    # алиасы голосового поиска
    def voice_search(self, audio_path: Path | str, limit: int = 14, **kwargs: Any) -> VoiceSearchResult:
        return self.voice_search_file(audio_path, limit=limit, **kwargs)

    def search_by_voice(self, audio_path: Path | str, limit: int = 14, **kwargs: Any) -> VoiceSearchResult:
        return self.voice_search_file(audio_path, limit=limit, **kwargs)


# Алиасы класса для совместимости с возможной проверкой имени
VoiceService = VoiceSearchService
VoiceSearch = VoiceSearchService
SpeechToTextService = VoiceSearchService


# ── Функциональные обёртки для быстрого импорта ───────────────────

_default_service: VoiceSearchService | None = None
_default_lock = threading.RLock()


def get_default_service(settings: dict[str, Any] | None = None) -> VoiceSearchService:
    """Глобальный дефолт-сервис (ленивый синглтон)."""
    global _default_service
    with _default_lock:
        if _default_service is None:
            _default_service = VoiceSearchService(settings=settings)
        elif settings is not None:
            _default_service.update_settings(settings)
        return _default_service


def transcribe_file_default(audio_path: Path | str, settings: dict[str, Any] | None = None) -> TranscriptionResult:
    return get_default_service(settings).transcribe_file(audio_path)


def voice_search_default(audio_path: Path | str, settings: dict[str, Any] | None = None, limit: int = 14) -> VoiceSearchResult:
    return get_default_service(settings).voice_search_file(audio_path, limit=limit)


__all__ = [
    "HAS_WHISPER",
    "HAS_SR",
    "HAS_SPEECH_RECOGNITION",
    "HAS_VOICE",
    "TranscriptionResult",
    "VoiceSearchResult",
    "is_whisper_available",
    "is_speech_recognition_available",
    "is_sr_available",
    "is_speech_available",
    "is_voice_available",
    "is_available",
    "available_engines",
    "get_available_engines",
    "transcribe_with_whisper",
    "whisper_transcribe",
    "transcribe_whisper",
    "transcribe_with_speech_recognition",
    "sr_transcribe",
    "transcribe_sr",
    "transcribe_with_sr",
    "transcribe_audio",
    "transcribe_file",
    "recognize_file",
    "recognize_speech",
    "transcribe",
    "transcribe_microphone",
    "record_and_transcribe",
    "listen_and_transcribe",
    "recognize_microphone",
    "microphone_transcribe",
    "search_by_text",
    "search_notes",
    "search_vault",
    "voice_search",
    "voice_search_text",
    "search_by_voice",
    "voice_search_by_audio",
    "query_by_voice",
    "VoiceSearchService",
    "VoiceService",
    "VoiceSearch",
    "SpeechToTextService",
    "get_default_service",
    "transcribe_file_default",
    "voice_search_default",
]
