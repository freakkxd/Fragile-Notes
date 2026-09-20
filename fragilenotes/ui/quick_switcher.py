"""Быстрый переключатель (Ctrl+P): глобальный поиск по заметкам vault.

Popover с поисковой строкой; поиск идёт в фоновом потоке поверх TTL-кэша
кэша индекса services.vault. Enter открывает выбранную заметку.
"""

from __future__ import annotations

import datetime
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

MAX_RESULTS = 14
RECENT_FILES_LIMIT = 5
RECENT_COMMANDS_LIMIT = 5

# Иконки по расширению — берём из vault.FILE_EMOJI, fallback для тестов
try:
    from ..vault import FILE_EMOJI as _FILE_EMOJI  # type: ignore
except Exception:
    _FILE_EMOJI = {
        ".md": "📄",
        ".txt": "📝",
        ".json": "🧩",
        ".yaml": "🧩",
        ".yml": "🧩",
        ".enc": "🔒",
        ".pdf": "📕",
    }

# Панель команд (Obsidian Command palette): id, иконка, заголовок, описание.
COMMANDS = [
    ("new", "➕", "Новая заметка", "Создать .md в текущей папке — Ctrl+N"),
    ("save", "💾", "Сохранить", "Сохранить текущий файл/заметку — Ctrl+S"),
    ("search", "🔍", "Найти заметки", "Открыть быстрый переключатель — Ctrl+P / Ctrl+K / Ctrl+O"),
    ("quick_open", "📂", "Быстрый переход", "Быстрый переход к файлу — Ctrl+O / Ctrl+P (Obsidian)"),
    ("global_search", "🔎", "Глобальный поиск", "Поиск по заметкам в файлах — Ctrl+Shift+F"),
    ("duplicate_line", "⎘", "Дублировать строку", "Скопировать текущую строку под курсором — Ctrl+D"),
    ("toggle_preview", "👁", "Переключить предпросмотр", "Редактор ↔ Просмотр — Ctrl+E / Ctrl+Shift+E / Ctrl+Enter"),
    ("sidebar", "☰", "Переключить сайдбар", "Показать/скрыть колонку навигации — Ctrl+B"),
    ("web_clip", "✂️", "Web Clipper", "Сохранить веб-страницу как markdown — Clippings/  Ctrl+Shift+W"),
    ("quick_capture", "⚡", "Quick Capture", "Быстрый захват в Inbox — Ctrl+Shift+Q"),
    ("files", "🗂", "Заметки", "Открыть вкладку файлов"),
    ("settings", "⚙️", "Настройки", "Рабочее пространство и параметры"),
    ("snippets", "✂️", "Сниппеты — палитра", "Показать палитру сниппетов trigger → expansion — Tab/Space автозамена"),
]


def _age_label(mtime: float) -> str:
    age = datetime.datetime.now().timestamp() - mtime
    if age < 3600:
        return "только что"
    if age < 86400:
        return f"{int(age // 3600)} ч назад"
    return f"{int(age // 86400)} дн назад"


def _file_icon(path: Path | str) -> str:
    """Иконка по расширению файла (vault.FILE_EMOJI)."""
    try:
        suf = Path(path).suffix.lower()
    except Exception:
        return "📄"
    return _FILE_EMOJI.get(suf, "📄")


# быстрый индекс команд по id
_CMD_BY_ID: dict[str, tuple[str, str, str]] = {cid: (icon, title, desc) for cid, icon, title, desc in COMMANDS}


def _snippet(query: str, hit) -> str | None:
    """Фрагмент вокруг совпадения в тексте; None, если слово есть в имени/title."""
    q = query.strip().lower()
    stem = hit.path.stem.lower()
    if not q or not hit.raw or q in hit.title.lower() or q in stem:
        return None
    idx = hit.text.find(q)
    if idx < 0:
        return None
    raw = hit.raw
    start = max(0, idx - 50)
    end = min(len(raw), idx + len(q) + 50)
    piece = " ".join(raw[start:end].split())
    return ("…" if start else "") + piece + ("…" if end < len(raw) else "")


# ── Fuzzy-поиск ──────────────────────────────────────────────────
# Подсчёт очков: совпадение по подпоследовательности + бонус за начало слова.
# Используется для команд и заметок.

_WORD_SEPS = set(" /\\-_.,;:()[]\"'")

def _is_word_start(text: str, idx: int) -> bool:
    """Начало слова: первый символ или после разделителя."""
    if idx == 0:
        return True
    return text[idx - 1] in _WORD_SEPS


def _fuzzy_match(query: str, target: str) -> tuple[int | None, list[int]]:
    """Fuzzy-подсчёт: subsequence + бонусы.

    Возвращает (score, indices) где indices — позиции совпавших символов в target.
    Если query не является подпоследовательностью target — (None, []).
    """
    q = query.lower()
    t = target.lower()
    if not q:
        return (0, [])
    if len(q) > len(t):
        return (None, [])
    # — поиск индексов: жадный проход с предпочтением начала слова —
    indices: list[int] = []
    pos = 0
    t_len = len(t)
    for ch in q:
        best_generic = -1
        best_word = -1
        for i in range(pos, t_len):
            if t[i] == ch:
                if best_generic == -1:
                    best_generic = i
                if _is_word_start(t, i):
                    best_word = i
                    break  # ближайший word-start — оптимален
        # выбираем: word-start если он недалеко, иначе ближайший generic
        found = -1
        if best_word != -1:
            if best_generic != -1 and best_word - best_generic > 6:
                found = best_generic
            else:
                found = best_word
        else:
            found = best_generic
        if found == -1:
            return (None, [])
        indices.append(found)
        pos = found + 1

    # — подсчёт очков —
    score = len(indices) * 10  # база
    for k, idx in enumerate(indices):
        if idx == 0:
            score += 12  # бонус за начало строки
        elif _is_word_start(t, idx):
            score += 10  # бонус за начало слова
        if k > 0 and idx == indices[k - 1] + 1:
            score += 8  # подряд идущие символы
        elif k > 0:
            gap = idx - indices[k - 1] - 1
            score -= gap  # штраф за разрывы
    # компактность: чем меньше размах, тем лучше
    span = indices[-1] - indices[0] + 1 if indices else 0
    score -= span // 4
    # точное вхождение даёт доп-бонус
    if t.startswith(q):
        score += 20
    elif q in t:
        score += 10
    return (score, indices)


def _highlight_markup(text: str, indices: list[int]) -> str:
    """Подсветка совпадений через Pango-markup (экранируем текст)."""
    if not indices:
        return GLib.markup_escape_text(text, -1)
    idx_set = set(indices)
    # защита: индексы могут выходить за длину из-за расхождения lower/оригинал (редко)
    out: list[str] = []
    for i, ch in enumerate(text):
        esc = GLib.markup_escape_text(ch, -1)
        if i in idx_set:
            out.append(f'<span foreground="#82a8ff" weight="bold">{esc}</span>')
        else:
            out.append(esc)
    return "".join(out)


def _highlight_substring_markup(text: str, query: str) -> str:
    """Подсветка подстроки (для сниппетов/фолбэка)."""
    q = query.strip()
    if not q:
        return GLib.markup_escape_text(text, -1)
    low = text.lower()
    q_low = q.lower()
    idx = low.find(q_low)
    if idx < 0:
        return GLib.markup_escape_text(text, -1)
    before = GLib.markup_escape_text(text[:idx], -1)
    match = GLib.markup_escape_text(text[idx: idx + len(q)], -1)
    after = GLib.markup_escape_text(text[idx + len(q):], -1)
    return f'{before}<span foreground="#82a8ff" weight="bold">{match}</span>{after}'


class QuickSwitcher:
    def __init__(self, settings: dict, on_open, on_command=None) -> None:
        self.settings = settings
        self.on_open = on_open
        self.on_command = on_command or (lambda cmd_id: None)
        self.mode = "notes"  # notes | cmd (Obsidian: Ctrl+P — заметки, Ctrl+Shift+P — команды)
        self._token = 0

        self.popover = Gtk.Popover(css_classes=["qs-popover"])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)

        # ── Поисковая строка + кнопка микрофона (голосовой поиск) ──
        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, hexpand=True)
        self.entry = Gtk.SearchEntry(
            placeholder_text="Поиск по заметкам и содержимому… Enter — открыть",
            hexpand=True,
        )
        self.entry.set_size_request(400, -1)
        search_row.append(self.entry)

        # Кнопка микрофона — голосовой поиск через services.voice_search
        # Иконка символическая; fallback — эмодзи если тема иконок недоступна.
        try:
            self.voice_btn = Gtk.Button(
                icon_name="audio-input-microphone-symbolic",
                tooltip_text="Голосовой поиск — нажми и говори (whisper / SpeechRecognition)",
                css_classes=["flat", "qs-voice-btn"],
            )
        except Exception:
            self.voice_btn = Gtk.Button(
                label="🎙️",
                tooltip_text="Голосовой поиск — нажми и говори (whisper / SpeechRecognition)",
                css_classes=["flat", "qs-voice-btn"],
            )
            self.voice_btn.set_label("🎙️")
        self.voice_btn.set_can_focus(False)
        self.voice_btn.set_size_request(36, -1)
        # доступность
        try:
            self.voice_btn.update_property(Gtk.AccessibleProperty.LABEL, "Голосовой поиск")
            self.voice_btn.update_property(Gtk.AccessibleProperty.DESCRIPTION, "Распознать речь и найти заметки")
        except Exception:
            pass
        # состояние голосового ввода
        self._voice_active = False
        self._voice_token = 0
        # проверить доступность движков — отключить кнопку если ничего нет
        try:
            from ..services import voice_search as _vs

            if not _vs.is_voice_available():
                self.voice_btn.set_sensitive(False)
                self.voice_btn.set_tooltip_text("Голосовой поиск недоступен — установи whisper или SpeechRecognition")
        except Exception:
            # не блокируем UI если модуль не импортируется на этапе конструирования
            pass
        self.voice_btn.connect("clicked", self._on_voice_clicked)
        search_row.append(self.voice_btn)
        box.append(search_row)

        scroller = Gtk.ScrolledWindow(max_content_height=340)
        scroller.set_max_content_width(460)
        self.listbox = Gtk.ListBox(css_classes=["qs-list"])
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        scroller.set_child(self.listbox)
        box.append(scroller)

        self.popover.set_child(box)
        self._setup_handlers()

    def _setup_handlers(self) -> None:
        self.entry.connect("search-changed", self._on_query)
        self.entry.connect("activate", self._on_enter)
        self.listbox.connect("row-activated", self._on_row_activated)

        key = Gtk.EventControllerKey.new()
        key.connect("key-pressed", self._on_key)
        self.entry.add_controller(key)

    def attach(self, anchor: Gtk.Widget) -> None:
        self.popover.set_parent(anchor)

    # ── Голосовой поиск (кнопка микрофона) ───────────────────────
    def _on_voice_clicked(self, _btn: Gtk.Button) -> None:
        """Нажатие 🎙️ — запись с микрофона → транскрибация → подстановка в entry."""
        if getattr(self, "_voice_active", False):
            return
        self._voice_active = True
        self._voice_token = getattr(self, "_voice_token", 0) + 1
        token = self._voice_token
        try:
            self.voice_btn.set_sensitive(False)
            self.voice_btn.set_tooltip_text("Слушаю… говорите")
        except Exception:
            pass
        try:
            self.entry.set_placeholder_text("🎙️ Слушаю… говорите")
        except Exception:
            pass
        self._show_message("🎙️ Слушаю… говорите (5 сек)")
        threading.Thread(target=self._voice_work, args=(token,), daemon=True).start()

    def _voice_work(self, token: int) -> None:
        """Фоновый поток: захват микрофона + распознавание."""
        query = ""
        error: str | None = None
        engine: str | None = None
        try:
            from ..services import voice_search as vs

            # пробуем live-микрофон; таймаут 5c, фраза до 8c
            res = vs.transcribe_microphone(timeout=5.0, phrase_time_limit=8.0)
            if token != getattr(self, "_voice_token", -1):
                return
            if res.ok:
                query = (res.text or "").strip()
                engine = res.engine
            else:
                error = res.error or "не удалось распознать"
        except Exception as exc:  # noqa: BLE001
            error = str(exc) or "ошибка голосового поиска"
        GLib.idle_add(self._voice_done, token, query, error, engine)

    def _voice_done(self, token: int, query: str, error: str | None, engine: str | None) -> bool:
        """Завершение голосового ввода в GUI-потоке."""
        if token != getattr(self, "_voice_token", -1):
            return False
        self._voice_active = False
        try:
            self.voice_btn.set_sensitive(True)
            self.voice_btn.set_tooltip_text("Голосовой поиск — нажми и говори (whisper / SpeechRecognition)")
        except Exception:
            pass
        try:
            self.entry.set_placeholder_text("Поиск по заметкам и содержимому… Enter — открыть")
        except Exception:
            pass
        if error:
            self._show_message(f"Голос не распознан: {error}")
            return False
        if not query:
            self._show_message("Не удалось распознать речь — попробуйте ещё раз")
            return False
        # подставляем распознанный текст — триггерит _on_query через search-changed
        try:
            self.entry.set_text(query)
            self.entry.grab_focus()
            # курсор в конец
            try:
                self.entry.set_position(len(query))
            except Exception:
                pass
        except Exception:
            pass
        # подсказка с движком
        if engine:
            # не мешаем списку — статус виден через placeholder/snippet?
            pass
        return False

    def voice_search_from_file(self, audio_path: str | Path, limit: int = 14) -> None:
        """Программный голосовой поиск по аудиофайлу (для тестов/интеграции).

        Распознаёт файл и подставляет результат в entry.
        """
        p = Path(audio_path)
        self._voice_token = getattr(self, "_voice_token", 0) + 1
        token = self._voice_token
        self._show_message(f"🎙️ Распознаю {p.name}…")
        threading.Thread(target=self._voice_file_work, args=(token, str(p), int(limit)), daemon=True).start()

    def _voice_file_work(self, token: int, audio_path: str, limit: int) -> None:
        query = ""
        error: str | None = None
        try:
            from ..services import voice_search as vs

            res = vs.transcribe_audio(audio_path)
            if token != getattr(self, "_voice_token", -1):
                return
            if res.ok:
                query = (res.text or "").strip()
            else:
                error = res.error
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        GLib.idle_add(self._voice_done, token, query, error, None)

    # ── Жизненный цикл ───────────────────────────────────────
    def open(self) -> None:
        self.mode = "notes"
        self._token += 1
        self.entry.set_text("")
        self._show_message("Загружаю…")
        self.popover.popup()
        self.entry.grab_focus()
        self._on_query(self.entry)

    def open_commands(self) -> None:
        """Панель команд как в Obsidian (Ctrl+Shift+P)."""
        self.mode = "cmd"
        self._token += 1
        self.entry.set_text("")
        self.popover.popup()
        self.entry.grab_focus()
        self._show_commands()

    def close(self) -> None:
        self.popover.popdown()

    # ── Клавиши ──────────────────────────────────────────────
    def _on_key(self, _ctrl, keyval, _keycode, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            self._move(1)
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            self._move(-1)
            return True
        return False

    def _move(self, delta: int) -> None:
        rows = [r for r in self._rows() if r.get_activatable()]
        if not rows:
            return
        chosen = self.listbox.get_selected_row()
        idx = rows.index(chosen) if chosen in rows else -delta
        idx = (idx + delta) % len(rows)
        self.listbox.select_row(rows[idx])
        self.listbox.scroll_to(rows[idx])

    def _rows(self):
        out = []
        child = self.listbox.get_first_child()
        while child is not None:
            out.append(child)
            child = child.get_next_sibling()
        return out

    def _selected(self) -> tuple[str | None, str | None]:
        rows = self._rows()
        if not rows:
            return None, None
        row = self.listbox.get_selected_row() or rows[0]
        return (
            getattr(row, "_path", None),
            getattr(row, "_highlight", None),
        )

    def _on_enter(self, _entry: Gtk.SearchEntry) -> None:
        row = self._selected_row()
        if row is not None and getattr(row, "_cmd", None):
            self._run_cmd(row._cmd)
            return
        path, highlight = self._selected()
        if path:
            self._open_path(path, highlight)

    def _selected_row(self):
        rows = self._rows()
        if not rows:
            return None
        return self.listbox.get_selected_row() or rows[0]

    # ── Поиск ────────────────────────────────────────────────
    def _on_query(self, entry: Gtk.SearchEntry) -> None:
        self._token += 1
        raw = entry.get_text()
        # — префикс ">" как в Obsidian: показать только команды —
        stripped = raw.lstrip()
        if stripped.startswith(">"):
            cmd_q = stripped[1:].lstrip()
            self._show_commands(cmd_q)
            return
        if self.mode == "cmd":
            self._show_commands()
            return
        q = raw.strip()
        token = self._token
        if not self.popover.get_visible():
            return
        settings = dict(self.settings)
        threading.Thread(
            target=self._search_work, args=(settings, q, token), daemon=True
        ).start()

    # ── Recents: команды с mtime ─────────────────────────────
    def _get_recent_commands(self, limit: int = RECENT_COMMANDS_LIMIT) -> list[tuple[str, str, str, str, float]]:
        """Отдать последние команды из settings (mtime-отсортированы)."""
        raw = self.settings.get("qs_recent_commands") or self.settings.get("recent_commands") or []
        out: list[tuple[str, str, str, str, float]] = []
        seen: set[str] = set()
        # settings хранит новые первыми
        for ent in raw:
            try:
                cid = str(ent.get("id") or ent.get("cmd") or "").strip()
                if not cid or cid in seen:
                    continue
                ts = float(ent.get("ts") or ent.get("mtime") or 0)
            except Exception:
                continue
            if cid not in _CMD_BY_ID:
                continue
            icon, title, desc = _CMD_BY_ID[cid]
            out.append((cid, icon, title, desc, ts))
            seen.add(cid)
            if len(out) >= limit:
                break
        # fallback: если без ts — порядок как в raw
        # ts уже убывает т.к. новые в начале; сортируем на случай перемешанных ts
        try:
            out.sort(key=lambda x: -x[4])
        except Exception:
            pass
        return out[:limit]

    def _push_recent_command(self, cid: str) -> None:
        """Записать команду в recents с текущим mtime и сохранить в settings."""
        if cid not in _CMD_BY_ID:
            return
        now = time.time()
        key = "qs_recent_commands" if "qs_recent_commands" in self.settings or "recent_commands" not in self.settings else "recent_commands"
        # читаем существующий список
        raw = list(self.settings.get(key) or [])
        # убрать дубликат
        raw = [e for e in raw if str(e.get("id") or e.get("cmd") or "") != cid]
        raw.insert(0, {"id": cid, "ts": now})
        # лимит — храним до 20, показываем 5
        raw = raw[:20]
        self.settings[key] = raw
        # также синхронизируем алиас
        alt = "recent_commands" if key == "qs_recent_commands" else "qs_recent_commands"
        self.settings[alt] = list(raw)
        try:
            from ..config import save_settings  # type: ignore

            save_settings(self.settings)
        except Exception:
            pass

    def _build_command_row(
        self,
        cid: str,
        icon: str,
        title: str,
        desc: str,
        mtime: float | None = None,
        highlight_q: str = "",
        t_idx: list[int] | None = None,
        d_idx: list[int] | None = None,
    ) -> Gtk.ListBoxRow:
        """Строка команды: иконка + title/desc (+ mtime для recents) с подсветкой."""
        row = Gtk.ListBoxRow(css_classes=["qs-row"], activatable=True)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(5)
        box.set_margin_bottom(5)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        q = (highlight_q or "").strip()
        q_low = q.lower()
        t_idx = t_idx or []
        d_idx = d_idx or []
        title_lbl = Gtk.Label(halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        if t_idx:
            title_lbl.set_markup(_highlight_markup(title, t_idx))
        elif q and q_low in title.lower():
            title_lbl.set_markup(_highlight_substring_markup(title, q))
        else:
            title_lbl.set_label(title)
        # описание + mtime для recents
        desc_text = desc
        if mtime:
            desc_text = f"{desc} · {_age_label(mtime)}"
        desc_lbl = Gtk.Label(halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["qs-snippet"])
        if d_idx and not mtime:
            desc_lbl.set_markup(_highlight_markup(desc, d_idx))
        elif q and q_low in desc.lower() and not mtime:
            desc_lbl.set_markup(_highlight_substring_markup(desc, q))
        else:
            # для recents с mtime — экранируем, подсветку не делаем (т.к. суффикс · mtime)
            if mtime:
                desc_lbl.set_label(desc_text)
            else:
                desc_lbl.set_label(desc)
            # если это recent с mtime и есть совпадение — попробуем подсветить часть desc без суффикса
            if mtime and q and q_low in desc.lower():
                # пересобрать с подсветкой + суффикс
                try:
                    base_markup = _highlight_substring_markup(desc, q)
                    age = GLib.markup_escape_text(f" · {_age_label(mtime)}", -1)
                    desc_lbl.set_markup(base_markup + age)
                except Exception:
                    pass
        info.append(title_lbl)
        info.append(desc_lbl)
        box.append(Gtk.Label(label=icon))
        box.append(info)
        row.set_child(box)
        row._cmd = cid  # type: ignore[attr-defined]
        return row

    def _show_commands(self, cmd_query: str | None = None) -> None:
        """Показать команды с fuzzy-фильтром и секцией Recent Commands."""
        self._clear()
        # определяем запрос: явный cmd_query или из entry (с поддержкой ">")
        if cmd_query is None:
            raw = self.entry.get_text().strip()
            if raw.startswith(">"):
                q = raw[1:].strip().lower()
            else:
                q = raw.lower()
        else:
            q = cmd_query.strip().lower()

        # — пустой запрос: Recent Commands + все команды —
        if not q:
            recents = self._get_recent_commands(limit=RECENT_COMMANDS_LIMIT)
            if recents:
                self.listbox.append(self._section_row("Недавние команды"))
                for cid, icon, title, desc, ts in recents:
                    self.listbox.append(self._build_command_row(cid, icon, title, desc, mtime=ts))
                self.listbox.append(self._section_row("Все команды"))
            for cid, icon, title, desc in COMMANDS:
                # не дублировать recents в основном списке если секция уже показана
                if recents and any(cid == r[0] for r in recents):
                    continue
                self.listbox.append(self._build_command_row(cid, icon, title, desc))
            first = self.listbox.get_row_at_index(0)
            # первый выбираемый — пропускаем секцию-заголовок
            if first is not None and not first.get_activatable():
                for r in self._rows():
                    if r.get_activatable():
                        first = r
                        break
            if first is not None:
                self.listbox.select_row(first)
            return

        # — fuzzy-фильтр по заголовку и описанию —
        scored: list[tuple[int, list[int], list[int], tuple]] = []
        for cid, icon, title, desc in COMMANDS:
            s_title, idx_title = _fuzzy_match(q, title)
            s_desc, idx_desc = _fuzzy_match(q, desc)
            best_score: int | None = None
            best_t_idx: list[int] = []
            best_d_idx: list[int] = []
            if s_title is not None:
                best_score = s_title
                best_t_idx = idx_title
            if s_desc is not None:
                if best_score is None or s_desc > best_score:
                    best_score = s_desc
                    # если лучше совпало в описании — подсвечиваем описание
                    best_d_idx = idx_desc
                    best_t_idx = []
                elif s_desc == best_score:
                    best_d_idx = idx_desc
            # фолбэк: подстрока если fuzzy не сработал
            if best_score is None:
                if q in title.lower() or q in desc.lower():
                    best_score = 1
                else:
                    continue
            scored.append((best_score, best_t_idx, best_d_idx, (cid, icon, title, desc)))

        if not scored:
            self._show_message("Нет таких команд")
            return
        scored.sort(key=lambda x: -x[0])

        for score, t_idx, d_idx, (cid, icon, title, desc) in scored:
            self.listbox.append(self._build_command_row(cid, icon, title, desc, highlight_q=q, t_idx=t_idx, d_idx=d_idx))
        first = self.listbox.get_row_at_index(0)
        if first is not None:
            self.listbox.select_row(first)

    def _run_cmd(self, cid: str) -> None:
        try:
            self._push_recent_command(cid)
        except Exception:
            pass
        self.close()
        self.on_command(cid)

    def _search_work(self, settings: dict, query: str, token: int) -> None:
        try:
            from ..services import vault

            if query:
                # — fuzzy-поиск по заметкам —
                q = query.strip()
                q_low = q.lower()
                # получаем полный индекс (TTL-кэш) и делаем fuzzy-ранжирование
                try:
                    all_hits = vault._notes_index(settings)
                except Exception:
                    # фолбэк: старый substring-поиск
                    hits = vault.search_notes(settings, q, limit=MAX_RESULTS * 3)
                    all_hits = hits
                scored: list[tuple[int, object, list[int]]] = []
                for h in all_hits:
                    # оцениваем по title и stem, берём лучший скор
                    s_title, idx_title = _fuzzy_match(q_low, h.title.lower())
                    s_stem, idx_stem = _fuzzy_match(q_low, h.path.stem.lower())
                    best_score: int | None = None
                    best_idx: list[int] = []
                    if s_title is not None:
                        best_score = s_title
                        best_idx = idx_title
                    if s_stem is not None and (best_score is None or s_stem > best_score):
                        best_score = s_stem
                        best_idx = idx_stem
                    # фолбэк: совпадение в содержимом (низкий приоритет)
                    if best_score is None:
                        if q_low in h.text:
                            best_score = 5
                            best_idx = []
                        else:
                            continue
                    scored.append((best_score, h, best_idx))
                # сортировка: очки убывают, затем свежеть
                scored.sort(key=lambda x: (-x[0], -x[1].mtime))
                top = scored[:MAX_RESULTS]
                rows = [
                    (h.path, h.title, h.mtime, _snippet(q, h), idx) for _, h, idx in top
                ]
            else:
                # пустой запрос — секция Recent Files (5 последних) + Recent Commands ниже в _apply
                recent = vault.scan_recent_notes(settings, days=30, limit=RECENT_FILES_LIMIT)
                rows = [
                    (n.path, vault.note_title(n.path), n.mtime, None, [])
                    for n in recent
                ]
        except Exception:  # noqa: BLE001
            rows = []
        GLib.idle_add(self._apply, token, rows, query)

    def _apply(
        self, token: int, rows: list[tuple[Path, str, float, str | None, list[int]]] | list[tuple[Path, str, float, str | None]], query: str
    ) -> bool:
        if token != self._token:
            return False
        self._clear()
        is_empty_query = not query.strip()
        # пустой запрос: показываем Recent Files (с mtime и иконками по расширению) + Recent Commands
        if is_empty_query:
            has_content = False
            if rows:
                self.listbox.append(self._section_row("Недавние файлы"))
                has_content = True
                for item in rows:
                    if len(item) == 5:
                        path, title, mtime, snippet, indices = item  # type: ignore
                    else:
                        path, title, mtime, snippet = item  # type: ignore
                        indices = []
                    row = Gtk.ListBoxRow(css_classes=["qs-row"], activatable=True)
                    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                    box.set_margin_top(5)
                    box.set_margin_bottom(5)
                    box.append(Gtk.Label(label=_file_icon(path)))
                    info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
                    title_text = title or Path(path).stem
                    title_lbl = Gtk.Label(halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END)
                    if indices:
                        title_lbl.set_markup(_highlight_markup(title_text, indices))
                    elif query.strip() and query.strip().lower() in title_text.lower():
                        title_lbl.set_markup(_highlight_substring_markup(title_text, query.strip()))
                    else:
                        title_lbl.set_label(title_text)
                    info.append(title_lbl)
                    if snippet:
                        snip_lbl = Gtk.Label(halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["qs-snippet"])
                        if query.strip() and query.strip().lower() in snippet.lower():
                            snip_lbl.set_markup(_highlight_substring_markup(snippet, query.strip()))
                        else:
                            snip_lbl.set_label(snippet)
                        info.append(snip_lbl)
                    info.append(Gtk.Label(
                        label=f"{Path(path).parent.name} · {_age_label(mtime)}",
                        css_classes=["dim-hint"], halign=Gtk.Align.START, xalign=0,
                    ))
                    box.append(info)
                    row.set_child(box)
                    row._path = str(path)  # type: ignore[attr-defined]
                    row._highlight = query if snippet else None  # type: ignore[attr-defined]
                    self.listbox.append(row)
            # секция Recent Commands с mtime и иконками
            try:
                recents = self._get_recent_commands(limit=RECENT_COMMANDS_LIMIT)
            except Exception:
                recents = []
            if recents:
                self.listbox.append(self._section_row("Недавние команды"))
                has_content = True
                for cid, icon, title, desc, ts in recents:
                    self.listbox.append(self._build_command_row(cid, icon, title, desc, mtime=ts))
            if not has_content:
                self._show_message("Ничего не найдено")
                return False
            # выбор первой activatable строки
            first = self.listbox.get_row_at_index(0)
            if first is not None and not first.get_activatable():
                for r in self._rows():
                    if r.get_activatable():
                        first = r
                        break
            if first is not None:
                self.listbox.select_row(first)
            return False
        # — обычный поиск: список заметок с подсветкой —
        if not rows:
            self._show_message("Ничего не найдено")
            return False
        for item in rows:
            # поддержка старого формата (4 элемента) и нового (5 с индексами)
            if len(item) == 5:
                path, title, mtime, snippet, indices = item  # type: ignore
            else:
                path, title, mtime, snippet = item  # type: ignore
                indices = []
            row = Gtk.ListBoxRow(css_classes=["qs-row"], activatable=True)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_margin_top(5)
            box.set_margin_bottom(5)
            box.append(Gtk.Label(label=_file_icon(path)))
            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
            # заголовок с подсветкой fuzzy-совпадений
            title_text = title or Path(path).stem
            title_lbl = Gtk.Label(halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            if indices:
                title_lbl.set_markup(_highlight_markup(title_text, indices))
            elif query.strip() and query.strip().lower() in title_text.lower():
                title_lbl.set_markup(_highlight_substring_markup(title_text, query.strip()))
            else:
                title_lbl.set_label(title_text)
            info.append(title_lbl)
            if snippet:
                snip_lbl = Gtk.Label(halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["qs-snippet"])
                if query.strip() and query.strip().lower() in snippet.lower():
                    snip_lbl.set_markup(_highlight_substring_markup(snippet, query.strip()))
                else:
                    snip_lbl.set_label(snippet)
                info.append(snip_lbl)
            info.append(Gtk.Label(
                label=f"{Path(path).parent.name} · {_age_label(mtime)}",
                css_classes=["dim-hint"], halign=Gtk.Align.START, xalign=0,
            ))
            box.append(info)
            row.set_child(box)
            row._path = str(path)  # type: ignore[attr-defined]
            row._highlight = query if snippet else None  # type: ignore[attr-defined]
            self.listbox.append(row)
        first = self.listbox.get_row_at_index(0)
        if first is not None and not first.get_activatable():
            for r in self._rows():
                if r.get_activatable():
                    first = r
                    break
        if first is not None:
            self.listbox.select_row(first)
        return False

    def _section_row(self, text: str) -> Gtk.ListBoxRow:
        """Неактивная строка-заголовок секции (например, 'Недавние')."""
        row = Gtk.ListBoxRow(activatable=False, selectable=False, css_classes=["qs-section-row"])
        lbl = Gtk.Label(label=text, halign=Gtk.Align.START, xalign=0, css_classes=["dim-label", "qs-section"])
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(2)
        box.set_margin_start(4)
        box.append(lbl)
        row.set_child(box)
        return row

    def _show_message(self, text: str) -> None:
        self._clear()
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        row.set_child(Gtk.Label(label=text, css_classes=["qs-empty"]))
        self.listbox.append(row)

    def _clear(self) -> None:
        while (child := self.listbox.get_first_child()) is not None:
            self.listbox.remove(child)

    def _on_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if getattr(row, "_cmd", None):
            self._run_cmd(row._cmd)
            return
        path = getattr(row, "_path", None)
        if path:
            self._open_path(path, getattr(row, "_highlight", None))

    def _open_path(self, path: str, highlight: str | None = None) -> None:
        self.close()
        self.on_open(path, highlight)
