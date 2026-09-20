"""Простой Vim-режим для FragileNotes.

Реализует: normal/insert, hjkl, dd, yy, p, i, Esc, :w :q (:wq, :q!).
Макросы: q<a>...q запись, @a воспроизведение (в памяти, VimEngine).

Интеграция: :class:`VimController` — GTK-контроллер для Gtk.TextView / Gtk.TextBuffer.
Также содержит :class:`VimEngine` — чистая логика без GTK (для тестов).

Использование в files_view.py::

    from .vim_mode import VimController
    controller = VimController(editor, buffer, on_save=lambda: ..., on_quit=lambda: ...)
    controller.set_enabled(settings.get("vim_mode", False))
    # подключить EventControllerKey -> controller.handle_key(...)

Настройки: ключ ``vim_mode`` (bool) в settings dict, по умолчанию False.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

try:
    import gi  # type: ignore

    try:
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
    except Exception:
        pass
    from gi.repository import Gdk, Gtk  # type: ignore

    _HAS_GTK = True
except Exception:  # pragma: no cover - headless
    Gdk = None  # type: ignore
    Gtk = None  # type: ignore
    _HAS_GTK = False


MODE_NORMAL = "normal"
MODE_INSERT = "insert"
MODE_COMMAND = "command"


# ── Чистая логика (без GTK) ───────────────────────────────────────────────
@dataclass
class VimState:
    mode: str = MODE_NORMAL
    pending: str = ""  # "d" или "y" — ожидание второго символа для dd/yy; также "q", "@"
    yank: str = ""  # буфер yank
    command: str = ""  # буфер команды после ':'


def _is_valid_macro_register(key: str) -> bool:
    """Проверка валидности регистра для макросов: a-z, A-Z, 0-9, \" * +."""
    if not key or len(key) != 1:
        return False
    if key.isalnum():
        return True
    if key in ('"', "*", "+", "-", "_"):
        return True
    return False


class VimEngine:
    """Детерминированная логика без зависимости от GTK.

    Оперирует строками и оффсетами, пригодна для юнит-тестов.
    Поддерживает макросы q<a>...q, @a (хранение в памяти).
    """

    def __init__(self) -> None:
        self.state = VimState()
        # макросы: регистр -> список токенов (каждый токен — один вызов handle_normal_key)
        self.macros: dict[str, list[str]] = {}
        # alias для совместимости с тестами, ожидающими .registers
        self.registers = self.macros  # type: ignore
        self._recording: str | None = None
        self._recording_buffer: list[str] = []
        self._last_macro: str | None = None
        self._is_replaying: bool = False
        self._replay_depth: int = 0
        self._max_replay_depth: int = 10

    # ── helpers для текстовых операций ────────────────────────────────
    @staticmethod
    def _line_bounds(text: str, offset: int) -> tuple[int, int]:
        """Возвращает (start, end) строки содержащей offset, end включает \\n если есть."""
        if not text:
            return 0, 0
        offset = max(0, min(offset, len(text)))
        start = text.rfind("\n", 0, offset)
        start = 0 if start == -1 else start + 1
        end = text.find("\n", offset)
        if end == -1:
            end = len(text)
        else:
            end += 1  # включаем \\n
        return start, end

    @staticmethod
    def _move_offset(text: str, offset: int, direction: str) -> int:
        if not text:
            return 0
        lines = text.split("\n")
        # вычислить line/col по offset
        # offset -> line
        cur = 0
        line_idx = 0
        col = 0
        for idx, line in enumerate(lines):
            line_len_with_nl = len(line) + (1 if idx < len(lines) - 1 else 0)
            if offset < cur + line_len_with_nl:
                line_idx = idx
                col = offset - cur
                break
            cur += line_len_with_nl
        else:
            line_idx = len(lines) - 1
            col = len(lines[-1])

        if direction == "h":
            return max(0, offset - 1) if offset > 0 else 0
        if direction == "l":
            return min(len(text), offset + 1) if offset < len(text) else offset
        if direction == "j":
            if line_idx + 1 >= len(lines):
                return offset
            next_line = lines[line_idx + 1]
            new_col = min(col, len(next_line))
            # вычислить offset следующей строки
            next_start = cur + len(lines[line_idx]) + 1
            return next_start + new_col
        if direction == "k":
            if line_idx == 0:
                return offset
            prev_line = lines[line_idx - 1]
            new_col = min(col, len(prev_line))
            # вычислить offset предыдущей строки
            prev_start = cur - len(prev_line) - 1 if line_idx > 0 else 0
            # need to recompute prev_start correctly: sum lengths up to line_idx-1
            s = 0
            for i in range(line_idx - 1):
                s += len(lines[i]) + 1
            return s + new_col
        return offset

    @staticmethod
    def delete_line(text: str, offset: int) -> tuple[str, str, int]:
        """Удаляет строку под курсором. Возвращает (new_text, yank, new_offset)."""
        if not text:
            return text, "", 0
        start, end = VimEngine._line_bounds(text, offset)
        yank = text[start:end]
        new_text = text[:start] + text[end:]
        # курсор — на начало удалённой строки (или на предыдущую если была последняя)
        new_offset = min(start, len(new_text))
        # если удалили не последнюю строку, new_offset = start; если последнюю — конец
        return new_text, yank, new_offset

    @staticmethod
    def yank_line(text: str, offset: int) -> str:
        if not text:
            return ""
        start, end = VimEngine._line_bounds(text, offset)
        return text[start:end]

    @staticmethod
    def paste_after(text: str, offset: int, yank: str) -> tuple[str, int]:
        if not yank:
            return text, offset
        if not text:
            return yank, len(yank)
        start, end = VimEngine._line_bounds(text, offset)
        # вставить после текущей строки => позицию end
        # если текст не заканчивается \\n и мы вставляем в конец — добавить \\n перед yank если нужно
        insertion = yank
        if end == len(text) and not text.endswith("\n"):
            # если yank не начинается с \\n, нужен перенос
            if not insertion.startswith("\n"):
                insertion = "\n" + insertion
        new_text = text[:end] + insertion + text[end:]
        new_offset = end  # курсор в начале вставленной строки
        # если вставили с ведущим \\n, курсор на 1 дальше
        return new_text, new_offset

    # ── макросы ───────────────────────────────────────────────────────
    @property
    def is_recording(self) -> bool:
        return self._recording is not None

    @property
    def recording_register(self) -> str | None:
        return self._recording

    def get_macro(self, reg: str) -> list[str] | None:
        v = self.macros.get(reg)
        if v is None:
            return None
        return list(v)

    def set_macro(self, reg: str, seq: list[str] | str) -> None:
        if isinstance(seq, str):
            self.macros[reg] = list(seq)
        else:
            self.macros[reg] = list(seq)

    def clear_macro(self, reg: str) -> None:
        self.macros.pop(reg, None)

    def start_recording(self, reg: str) -> bool:
        if not _is_valid_macro_register(reg):
            return False
        if self._recording is not None:
            return False
        self._recording = reg
        self._recording_buffer = []
        self.state.pending = ""
        return True

    def stop_recording(self) -> bool:
        if self._recording is None:
            return False
        reg = self._recording
        seq = list(self._recording_buffer)
        # uppercase -> append to lowercase (vim поведение)
        store_reg = reg
        if reg.isupper():
            lower = reg.lower()
            prev = self.macros.get(lower, [])
            seq = list(prev) + seq
            store_reg = lower
        self.macros[store_reg] = seq
        # keep alias in sync
        self.registers = self.macros
        self._recording = None
        self._recording_buffer = []
        self.state.pending = ""
        return True

    def _record_key(self, key: str) -> None:
        if self._recording is not None and not self._is_replaying:
            self._recording_buffer.append(key)

    def play_macro(self, reg: str, text: str, offset: int) -> tuple[str, int]:
        """Воспроизвести макрос регистра reg над text/offset.

        Возвращает (new_text, new_offset). Рекурсия ограничена.
        """
        # поддержка @@ -> last_macro
        if reg == "@":
            if self._last_macro is None:
                return text, offset
            reg = self._last_macro
        seq = self.macros.get(reg)
        if seq is None or not seq:
            return text, offset
        if isinstance(seq, str):
            keys: list[str] = list(seq)
        else:
            keys = list(seq)
        if not keys:
            return text, offset
        if self._replay_depth >= self._max_replay_depth:
            return text, offset
        self._is_replaying = True
        self._replay_depth += 1
        self._last_macro = reg
        cur_text = text
        cur_offset = offset
        saved_pending = self.state.pending
        self.state.pending = ""
        for k in keys:
            action = self.handle_normal_key(k)
            # вложенный макрос: действие вида "@a"
            if action and action.startswith("@"):
                nested_reg = action[1:]
                # avoid infinite recursion
                if nested_reg in self.macros and self._replay_depth < self._max_replay_depth:
                    cur_text, cur_offset = self.play_macro(nested_reg, cur_text, cur_offset)
                continue
            # применить действие к тексту
            if action in ("h", "j", "k", "l"):
                cur_offset = self._move_offset(cur_text, cur_offset, action)
            elif action == "dd":
                cur_text, yank, cur_offset = self.delete_line(cur_text, cur_offset)
                self.state.yank = yank
            elif action == "yy":
                self.state.yank = self.yank_line(cur_text, cur_offset)
            elif action == "p":
                if self.state.yank:
                    cur_text, cur_offset = self.paste_after(cur_text, cur_offset, self.state.yank)
            elif action == "i":
                # mode already switched in handle_normal_key; для headless оставляем как есть
                pass
            elif action == "esc":
                pass
            elif action and action.startswith("macro_"):
                # start/stop не влияют на текст
                pass
            # остальные None / ":" не меняют текст
        self._is_replaying = False
        self._replay_depth -= 1
        self.state.pending = ""
        return cur_text, cur_offset

    # alias’ для совместимости
    def execute_macro(self, reg: str, text: str, offset: int) -> tuple[str, int]:
        return self.play_macro(reg, text, offset)

    def run_macro(self, reg: str, text: str, offset: int) -> tuple[str, int]:
        return self.play_macro(reg, text, offset)

    def exec_keys(self, keys: list[str] | str, text: str, offset: int) -> tuple[str, int]:
        """Последовательно применить список ключей (для тестов)."""
        if isinstance(keys, str):
            key_list = list(keys)
        else:
            key_list = list(keys)
        cur_text, cur_offset = text, offset
        for k in key_list:
            action = self.handle_normal_key(k)
            if action and action.startswith("@"):
                nested = action[1:]
                cur_text, cur_offset = self.play_macro(nested, cur_text, cur_offset)
                continue
            if action in ("h", "j", "k", "l"):
                cur_offset = self._move_offset(cur_text, cur_offset, action)
            elif action == "dd":
                cur_text, yank, cur_offset = self.delete_line(cur_text, cur_offset)
                self.state.yank = yank
            elif action == "yy":
                self.state.yank = self.yank_line(cur_text, cur_offset)
            elif action == "p":
                if self.state.yank:
                    cur_text, cur_offset = self.paste_after(cur_text, cur_offset, self.state.yank)
        return cur_text, cur_offset

    def handle_normal_key(self, key: str) -> str | None:
        """Обрабатывает одиночную клавишу в normal.

        Возвращает действие: "h","j","k","l","dd","yy","p","i",":", "esc", "macro_start:<reg>", "macro_stop:<reg>", "@<reg>", None
        Поддерживает запись/воспроизведение макросов q<a>...q, @a.
        """
        s = self.state

        # ── pending "q" : выбор регистра для записи ──────────────────
        if s.pending == "q":
            s.pending = ""
            if _is_valid_macro_register(key):
                if self._recording is None:
                    # старт записи (не записываем саму последовательность q<reg>)
                    self._recording = key
                    self._recording_buffer = []
                    return f"macro_start:{key}"
                else:
                    # уже идёт запись — игнорируем вложенный старт
                    return None
            else:
                # невалидный регистр — отмена
                return None

        # ── pending "@" : выбор регистра для воспроизведения ─────────
        if s.pending == "@":
            s.pending = ""
            # @@ -> повторить последний макрос
            if key == "@":
                reg = self._last_macro
                if reg is None or reg not in self.macros:
                    return None
                # записать "@@" если идёт запись
                if self._recording is not None and not self._is_replaying:
                    self._recording_buffer.append("@")
                    self._recording_buffer.append("@")
                return f"@{reg}"
            if _is_valid_macro_register(key):
                # записать "@<reg>" как часть макроса если идёт запись
                if self._recording is not None and not self._is_replaying:
                    self._recording_buffer.append("@")
                    self._recording_buffer.append(key)
                if key in self.macros:
                    self._last_macro = key
                    return f"@{key}"
                else:
                    # пустой регистр — ничего не делаем, но запоминаем попытку
                    return None
            else:
                return None

        # ── клавиша "q" : стоп записи или старт pending ──────────────
        if key == "q":
            if self._recording is not None:
                # стоп записи
                reg = self._recording
                seq = list(self._recording_buffer)
                store_reg = reg
                if reg.isupper():
                    lower = reg.lower()
                    prev = self.macros.get(lower, [])
                    seq = list(prev) + seq
                    store_reg = lower
                self.macros[store_reg] = seq
                self.registers = self.macros
                self._recording = None
                self._recording_buffer = []
                s.pending = ""
                return f"macro_stop:{reg}"
            else:
                s.pending = "q"
                return None

        # ── клавиша "@" : pending воспроизведения ────────────────────
        if key == "@":
            s.pending = "@"
            # запись "@" откладываем до выбора регистра (см. pending "@" выше)
            return None

        # ── pending dd/yy (существующая логика) ──────────────────────
        if s.pending == "d":
            if key == "d":
                s.pending = ""
                # запись обеих "d"
                self._record_key("d")
                # первая "d" уже была записана при первом нажатии, вторая — сейчас
                # но первая была записана внизу (when key == "d" set pending). Тогда сейчас нужно записать только вторую.
                # Однако если запись шла, первая "d" уже в буфере. Добавляем вторую.
                # Выше мы уже вызвали _record_key для второй, это корректно.
                return "dd"
            else:
                s.pending = ""
                # fallthrough to handle key as new command (запись нового key будет ниже)
        if s.pending == "y":
            if key == "y":
                s.pending = ""
                self._record_key("y")
                return "yy"
            else:
                s.pending = ""
        if key == "d":
            s.pending = "d"
            self._record_key("d")
            return None
        if key == "y":
            s.pending = "y"
            self._record_key("y")
            return None
        if key in ("h", "j", "k", "l"):
            self._record_key(key)
            return key
        if key == "p":
            self._record_key(key)
            return "p"
        if key == "i":
            s.mode = MODE_INSERT
            self._record_key(key)
            return "i"
        if key == ":":
            s.mode = MODE_COMMAND
            s.command = ""
            self._record_key(key)
            return ":"
        if key == "Escape":
            s.pending = ""
            self._record_key(key)
            return "esc"
        # неизвестная клавиша — всё равно пишем в макрос если идёт запись (для insert-mode текста)
        self._record_key(key)
        return None


# ── GTK-контроллер ──────────────────────────────────────────────────────────
class VimController:
    """GTK-контроллер привязывающий VimEngine к Gtk.TextView.

    Параметры:
        editor: Gtk.TextView
        buffer: Gtk.TextBuffer
        on_save: () -> None — вызывается на :w / :wq
        on_quit: () -> None — вызывается на :q / :wq / :q!
        on_status: (mode: str) -> None — обновление индикатора (опционально)
        status_label: Gtk.Label — если передан, обновляется автоматически
        command_entry: Gtk.Entry — если передан, используется для ввода команд
        command_revealer: Gtk.Revealer — скрывает/показывает command_entry
    """

    def __init__(
        self,
        editor=None,
        buffer=None,
        on_save: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        status_label=None,
        command_entry=None,
        command_revealer=None,
    ) -> None:
        self.editor = editor
        self.buffer = buffer
        self.on_save = on_save
        self.on_quit = on_quit
        self.on_status = on_status
        self.status_label = status_label
        self.command_entry = command_entry
        self.command_revealer = command_revealer
        self.enabled: bool = False
        self.mode: str = MODE_NORMAL
        self.pending: str = ""
        self.yank: str = ""
        self._command_text: str = ""
        # ── макросы (в памяти) ──────────────────────────────────
        self.macros: dict[str, list[str]] = {}
        self.registers = self.macros  # alias
        self._recording: str | None = None
        self._recording_buffer: list[str] = []
        self._last_macro: str | None = None
        self._is_replaying: bool = False
        self._replay_depth: int = 0
        self._max_replay_depth: int = 10

        # если передали виджеты — подключить сигналы command_entry
        if self.command_entry is not None and _HAS_GTK:
            try:
                self.command_entry.connect("activate", self._on_command_activate)
                # Esc в command_entry — отмена
                ctrl = Gtk.EventControllerKey.new()
                ctrl.connect("key-pressed", self._on_command_key)
                self.command_entry.add_controller(ctrl)
            except Exception:
                pass

    # ── enable ──────────────────────────────────────────────────────────
    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        if not self.enabled:
            self.mode = MODE_INSERT  # в выключенном состоянии — всегда insert (обычный редактор)
            self.pending = ""
            self._hide_command()
        else:
            self.mode = MODE_NORMAL
            self.pending = ""
        self._update_status()

    def is_enabled(self) -> bool:
        return self.enabled

    # ── status ──────────────────────────────────────────────────────────
    def _update_status(self) -> None:
        # запись макроса имеет приоритет
        if self._recording is not None:
            text = f"-- RECORDING @{self._recording} --"
            try:
                if self.status_label is not None:
                    self.status_label.set_text(text)
                    self.status_label.set_visible(True)
                if self.on_status is not None:
                    self.on_status(text)
            except Exception:
                pass
            return
        text = ""
        if not self.enabled:
            text = ""
        elif self.mode == MODE_NORMAL:
            text = "-- NORMAL --"
        elif self.mode == MODE_INSERT:
            text = "-- INSERT --"
        elif self.mode == MODE_COMMAND:
            text = "-- COMMAND --"
        try:
            if self.status_label is not None:
                self.status_label.set_text(text)
                self.status_label.set_visible(bool(text))
            if self.on_status is not None:
                self.on_status(text)
        except Exception:
            pass

    # ── макросы (контроллер) ────────────────────────────────────────
    @property
    def is_recording(self) -> bool:
        return self._recording is not None

    @property
    def recording_register(self) -> str | None:
        return self._recording

    def get_macro(self, reg: str) -> list[str] | None:
        v = self.macros.get(reg)
        if v is None:
            return None
        return list(v)

    def set_macro(self, reg: str, seq: list[str] | str) -> None:
        if isinstance(seq, str):
            self.macros[reg] = list(seq)
        else:
            self.macros[reg] = list(seq)

    def start_recording(self, reg: str) -> bool:
        if not _is_valid_macro_register(reg):
            return False
        if self._recording is not None:
            return False
        self._recording = reg
        self._recording_buffer = []
        self.pending = ""
        self._update_status()
        return True

    def stop_recording(self) -> bool:
        if self._recording is None:
            return False
        reg = self._recording
        seq = list(self._recording_buffer)
        store_reg = reg
        if reg.isupper():
            lower = reg.lower()
            prev = self.macros.get(lower, [])
            seq = list(prev) + seq
            store_reg = lower
        self.macros[store_reg] = seq
        self.registers = self.macros
        self._recording = None
        self._recording_buffer = []
        self.pending = ""
        self._update_status()
        return True

    def _record_key(self, key: str) -> None:
        if self._recording is not None and not self._is_replaying:
            self._recording_buffer.append(key)

    def _replay_macro(self, reg: str) -> bool:
        # поддержка @@
        if reg == "@":
            if self._last_macro is None:
                return False
            reg = self._last_macro
        seq = self.macros.get(reg)
        if not seq:
            return False
        if isinstance(seq, str):
            keys: list[str] = list(seq)
        else:
            keys = list(seq)
        if not keys:
            return False
        if self._replay_depth >= self._max_replay_depth:
            return False
        self._is_replaying = True
        self._replay_depth += 1
        self._last_macro = reg
        saved_pending = self.pending
        self.pending = ""
        # воспроизводим каждый токен через внутренний обработчик без записи
        for k in keys:
            # k — строка ключа как в handle_key (например "h","j","Escape","q")
            # обрабатываем через _handle_str_key_without_record
            action = self._handle_str_key_internal(k, record=False)
            if action and action.startswith("@"):
                nested = action[1:]
                if nested in self.macros and self._replay_depth < self._max_replay_depth:
                    # рекурсивный вызов с увеличенной глубиной
                    self._is_replaying = (
                        False  # временно снять чтобы вложенный мог зайти? нет, оставляем флаг
                    )
                    # проще: напрямую рекурсировать
                    self._replay_depth += 1
                    nested_seq = self.macros.get(nested, [])
                    if isinstance(nested_seq, str):
                        nkeys = list(nested_seq)
                    else:
                        nkeys = list(nested_seq)
                    for nk in nkeys:
                        self._handle_str_key_internal(nk, record=False)
                    self._replay_depth -= 1
                    self._is_replaying = True
                continue
            # действия уже выполнены внутри _handle_str_key_internal (движение, удаление и т.д.)
            # поэтому дополнительной обработки не нужно
        self._is_replaying = False
        self._replay_depth -= 1
        self.pending = ""
        self._update_status()
        return True

    def _handle_str_key_internal(self, key: str, record: bool = True) -> str | None:
        """Внутренняя обработка строкового ключа с выполнением GTK-действий.

        Возвращает действие как в VimEngine (включая macro_* и @).
        Если record=True и идёт запись — записывает ключ (кроме macro-контроля).
        """
        # pending q
        if self.pending == "q":
            self.pending = ""
            if _is_valid_macro_register(key):
                if self._recording is None:
                    self._recording = key
                    self._recording_buffer = []
                    self._update_status()
                    return f"macro_start:{key}"
                return None
            return None
        if self.pending == "@":
            self.pending = ""
            if key == "@":
                reg = self._last_macro
                if reg is None or reg not in self.macros:
                    return None
                if record and self._recording is not None and not self._is_replaying:
                    self._recording_buffer.append("@")
                    self._recording_buffer.append("@")
                self._last_macro = reg
                return f"@{reg}"
            if _is_valid_macro_register(key):
                if record and self._recording is not None and not self._is_replaying:
                    self._recording_buffer.append("@")
                    self._recording_buffer.append(key)
                if key in self.macros:
                    self._last_macro = key
                    return f"@{key}"
                return None
            return None
        if key == "q":
            if self._recording is not None:
                reg = self._recording
                seq = list(self._recording_buffer)
                store_reg = reg
                if reg.isupper():
                    lower = reg.lower()
                    prev = self.macros.get(lower, [])
                    seq = list(prev) + seq
                    store_reg = lower
                self.macros[store_reg] = seq
                self.registers = self.macros
                self._recording = None
                self._recording_buffer = []
                self.pending = ""
                self._update_status()
                return f"macro_stop:{reg}"
            else:
                self.pending = "q"
                return None
        if key == "@":
            self.pending = "@"
            return None
        # pending dd/yy
        if self.pending == "d":
            if key == "d":
                self.pending = ""
                if record:
                    self._record_key("d")
                self._delete_line()
                return "dd"
            else:
                self.pending = ""
        if self.pending == "y":
            if key == "y":
                self.pending = ""
                if record:
                    self._record_key("y")
                self._yank_line()
                return "yy"
            else:
                self.pending = ""
        if key == "d":
            self.pending = "d"
            if record:
                self._record_key("d")
            return None
        if key == "y":
            self.pending = "y"
            if record:
                self._record_key("y")
            return None
        if key in ("h", "j", "k", "l"):
            if record:
                self._record_key(key)
            self._move_hjkl(key)
            return key
        if key == "p":
            if record:
                self._record_key(key)
            self._paste()
            return "p"
        if key == "i":
            self.mode = MODE_INSERT
            self.pending = ""
            if record:
                self._record_key(key)
            self._update_status()
            return "i"
        if record:
            self._record_key(key)
        return None

    # ── команды :w :q ─────────────────────────────────────────────────
    def _hide_command(self) -> None:
        self._command_text = ""
        if self.command_entry is not None:
            try:
                self.command_entry.set_text("")
            except Exception:
                pass
        if self.command_revealer is not None:
            try:
                self.command_revealer.set_reveal_child(False)
            except Exception:
                pass
        # вернуть фокус в редактор
        if self.editor is not None and self.mode != MODE_COMMAND:
            try:
                self.editor.grab_focus()
            except Exception:
                pass

    def _show_command(self) -> None:
        if self.command_revealer is not None:
            try:
                self.command_revealer.set_reveal_child(True)
            except Exception:
                pass
        if self.command_entry is not None:
            try:
                self.command_entry.set_text(":")
                self.command_entry.grab_focus()
                # поставить курсор в конец
                self.command_entry.set_position(-1)
            except Exception:
                pass
        self._update_status()

    def _on_command_key(self, _ctrl, keyval, _keycode, _state) -> bool:
        if not _HAS_GTK:
            return False
        if keyval == Gdk.KEY_Escape:
            self.mode = MODE_NORMAL
            self._hide_command()
            self._update_status()
            return True
        return False

    def _on_command_activate(self, entry) -> None:
        try:
            cmd = entry.get_text().strip()
        except Exception:
            cmd = self._command_text
        self._execute_command(cmd)
        self.mode = MODE_NORMAL
        self._hide_command()
        self._update_status()

    def _execute_command(self, raw: str) -> bool:
        cmd = raw.strip()
        if cmd.startswith(":"):
            cmd = cmd[1:].strip()
        if not cmd:
            return False
        # поддержка :w, :q, :wq, :q!, :w!, :x
        if cmd in ("w", "w!", "write"):
            if self.on_save is not None:
                try:
                    self.on_save()
                except Exception:
                    pass
            return True
        if cmd in ("q", "quit", "q!", "quit!"):
            if self.on_quit is not None:
                try:
                    self.on_quit()
                except Exception:
                    pass
            return True
        if cmd in ("wq", "wq!", "x", "xit", "exit"):
            if self.on_save is not None:
                try:
                    self.on_save()
                except Exception:
                    pass
            if self.on_quit is not None:
                try:
                    self.on_quit()
                except Exception:
                    pass
            return True
        # неизвестная команда — игнорируем
        return False

    # ── операции с буфером ────────────────────────────────────────────
    def _move_hjkl(self, direction: str) -> bool:
        if self.buffer is None or self.editor is None:
            return False
        buf = self.buffer
        try:
            it = buf.get_iter_at_mark(buf.get_insert())
            line = it.get_line()
            col = it.get_line_offset()
            if direction == "h":
                # влево на 1 символ, не уходим за начало строки? vim позволяет на 0
                if it.get_offset() > 0:
                    # backward_char возвращает bool
                    if it.backward_char():
                        buf.place_cursor(it)
                        self.editor.scroll_to_iter(it, 0.1, False, 0, 0)
                        return True
            elif direction == "l":
                # вправо
                nxt = it.copy()
                if nxt.forward_char():
                    # в vim l не переходит на следующую строку через \n? Но Gtk TextIter forward_char переходит
                    # оставим как есть
                    buf.place_cursor(nxt)
                    self.editor.scroll_to_iter(nxt, 0.1, False, 0, 0)
                    return True
            elif direction == "j":
                if line + 1 < buf.get_line_count():
                    try:
                        nit = buf.get_iter_at_line_offset(line + 1, col)
                    except Exception:
                        nit = buf.get_iter_at_line(line + 1)
                        # попытаться сохранить колонку вручную
                        for _ in range(col):
                            if not nit.forward_char() or nit.get_line() != line + 1:
                                break
                    buf.place_cursor(nit)
                    self.editor.scroll_to_iter(nit, 0.1, False, 0, 0)
                    return True
            elif direction == "k":
                if line > 0:
                    try:
                        nit = buf.get_iter_at_line_offset(line - 1, col)
                    except Exception:
                        nit = buf.get_iter_at_line(line - 1)
                        for _ in range(col):
                            if not nit.forward_char() or nit.get_line() != line - 1:
                                break
                    buf.place_cursor(nit)
                    self.editor.scroll_to_iter(nit, 0.1, False, 0, 0)
                    return True
        except Exception:
            return False
        return False

    def _delete_line(self) -> bool:
        if self.buffer is None:
            return False
        buf = self.buffer
        try:
            it = buf.get_iter_at_mark(buf.get_insert())
            line = it.get_line()
            start = buf.get_iter_at_line(line)
            if line + 1 < buf.get_line_count():
                end = buf.get_iter_at_line(line + 1)
            else:
                end = buf.get_end_iter()
            text = buf.get_text(start, end, True)
            self.yank = text
            # delete
            buf.delete(start, end)
            # курсор: на начало строки line (которая теперь следующая) или на конец если удалили последнюю
            if buf.get_line_count() > 0:
                target_line = min(line, buf.get_line_count() - 1)
                nit = buf.get_iter_at_line(target_line)
                # первый не-пробельный символ как в vim
                try:
                    # найти первый непробельный
                    line_start = buf.get_iter_at_line(target_line)
                    line_end = (
                        buf.get_iter_at_line(target_line + 1)
                        if target_line + 1 < buf.get_line_count()
                        else buf.get_end_iter()
                    )
                    line_text = buf.get_text(line_start, line_end, True)
                    col = len(line_text) - len(line_text.lstrip(" \t"))
                    nit = buf.get_iter_at_line_offset(target_line, col)
                except Exception:
                    pass
                buf.place_cursor(nit)
                if self.editor is not None:
                    self.editor.scroll_to_iter(nit, 0.1, False, 0, 0)
            return True
        except Exception:
            return False

    def _yank_line(self) -> bool:
        if self.buffer is None:
            return False
        buf = self.buffer
        try:
            it = buf.get_iter_at_mark(buf.get_insert())
            line = it.get_line()
            start = buf.get_iter_at_line(line)
            if line + 1 < buf.get_line_count():
                end = buf.get_iter_at_line(line + 1)
            else:
                end = buf.get_end_iter()
                # для последней строки без \\n — yank без добавления \\n? оставляем как есть
            text = buf.get_text(start, end, True)
            # yank должен содержать \\n для корректного paste после строки
            if text and not text.endswith("\n"):
                text += "\n"
            self.yank = text
            return True
        except Exception:
            return False

    def _paste(self) -> bool:
        if self.buffer is None or not self.yank:
            return False
        buf = self.buffer
        try:
            it = buf.get_iter_at_mark(buf.get_insert())
            line = it.get_line()
            if line + 1 < buf.get_line_count():
                pit = buf.get_iter_at_line(line + 1)
            else:
                pit = buf.get_end_iter()
                # если буфер не пустой и не заканчивается на \\n — нужен \\n перед вставкой
                if buf.get_char_count() > 0:
                    try:
                        end = buf.get_end_iter()
                        # проверить последний символ
                        # получ текст последнего байта
                        start_check = buf.get_iter_at_offset(max(0, end.get_offset() - 1))
                        last = buf.get_text(start_check, end, True)
                        if last != "\n" and not self.yank.startswith("\n"):
                            buf.insert(pit, "\n")
                            # обновить pit
                            pit = buf.get_end_iter()
                    except Exception:
                        pass
            buf.insert(pit, self.yank)
            # курсор в начало вставленной строки
            # pit был до вставки, теперь текст сдвинут — поставить курсор на pit
            # проще: offset pit + len(yank) - len(last line) ??? ставим на начало вставки
            try:
                # после вставки pit инвалидирован, берём offset старого pit
                # нам нужен offset до вставки — сохраним
                pass
            except Exception:
                pass
            # установить курсор в начало вставленного текста
            # используем pit (до вставки) как ориентир — после вставки это начало yank
            # но pit после insert указывает на конец вставки, поэтому ставим на старый offset
            # вычисляем через line
            try:
                nit = buf.get_iter_at_line(line + 1)
                buf.place_cursor(nit)
                if self.editor is not None:
                    self.editor.scroll_to_iter(nit, 0.1, False, 0, 0)
            except Exception:
                pass
            return True
        except Exception:
            return False

    # ── главный обработчик клавиш ───────────────────────────────────
    def handle_key(self, keyval: int, keycode: int, state) -> bool:
        """Обработка нажатия клавиши.

        Вызывается из Gtk.EventControllerKey::key-pressed.
        Возвращает True если событие поглощено (не передавать дальше).
        """
        if not self.enabled:
            return False
        if not _HAS_GTK:
            return False

        # игнорируем Ctrl/Alt модификаторы кроме Shift для ':'
        try:
            ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
            alt = bool(state & Gdk.ModifierType.ALT_MASK)
            # super/meta тоже игнорим
            if ctrl or alt:
                return False
        except Exception:
            pass

        # в режиме COMMAND — не перехватываем (command_entry активен)
        if self.mode == MODE_COMMAND:
            return False

        # INSERT режим: только Esc возвращает в NORMAL, остальное пропускаем
        if self.mode == MODE_INSERT:
            if keyval == Gdk.KEY_Escape:
                self.mode = MODE_NORMAL
                self.pending = ""
                # запись Esc в макрос если идёт запись
                if self._recording is not None and not self._is_replaying:
                    self._recording_buffer.append("Escape")
                self._update_status()
                return True
            # в insert режиме остальные клавиши не перехватываем, но если идёт запись — записываем печатаемые символы
            if self._recording is not None and not self._is_replaying:
                try:
                    # попытаться получить символ из keyval
                    ch = Gdk.keyval_to_unicode(keyval)
                    if ch and 32 <= ch <= 126 or ch > 127:
                        self._recording_buffer.append(chr(ch))
                    else:
                        # fallback: имя ключа
                        name = Gdk.keyval_name(keyval)
                        if name and len(name) == 1:
                            self._recording_buffer.append(name)
                except Exception:
                    pass
            return False

        # NORMAL режим
        # Esc — сброс pending
        if keyval == Gdk.KEY_Escape:
            self.pending = ""
            if self._recording is not None and not self._is_replaying:
                self._recording_buffer.append("Escape")
            self._update_status()
            return True

        # ":"  — Shift+; на US, или GDK_KEY_colon
        if keyval in (Gdk.KEY_colon, Gdk.KEY_semicolon):
            # проверить Shift для ':'; если colon — сразу команда, если ; без shift — не ':'
            # GDK_KEY_colon уже означает Shift+;
            if keyval == Gdk.KEY_colon:
                self.mode = MODE_COMMAND
                self._show_command()
                self._update_status()
                if self._recording is not None and not self._is_replaying:
                    self._recording_buffer.append(":")
                return True
            # если semicolon но shift зажат — тоже ':'
            try:
                shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
                if shift and keyval == Gdk.KEY_semicolon:
                    self.mode = MODE_COMMAND
                    self._show_command()
                    self._update_status()
                    if self._recording is not None and not self._is_replaying:
                        self._recording_buffer.append(":")
                    return True
            except Exception:
                pass

        # префиксные d/y и макросы q/@ — маппим keyval -> символ
        key = None
        try:
            # GDK_KEY_h etc
            if keyval == Gdk.KEY_h or keyval == Gdk.KEY_H:
                key = "h"
            elif keyval == Gdk.KEY_j or keyval == Gdk.KEY_J:
                key = "j"
            elif keyval == Gdk.KEY_k or keyval == Gdk.KEY_K:
                key = "k"
            elif keyval == Gdk.KEY_l or keyval == Gdk.KEY_L:
                key = "l"
            elif keyval == Gdk.KEY_d or keyval == Gdk.KEY_D:
                key = "d"
            elif keyval == Gdk.KEY_y or keyval == Gdk.KEY_Y:
                key = "y"
            elif keyval == Gdk.KEY_p or keyval == Gdk.KEY_P:
                key = "p"
            elif keyval == Gdk.KEY_i or keyval == Gdk.KEY_I:
                key = "i"
            elif keyval == Gdk.KEY_q or keyval == Gdk.KEY_Q:
                key = "q"
            elif keyval == Gdk.KEY_at:
                key = "@"
            # также проверяем ручной маппинг для регистра (a-z)
            else:
                # попытка generic: a-z, 0-9 для макросов
                name = Gdk.keyval_name(keyval)
                if name and len(name) == 1 and name.isalnum():
                    # для q/@ pending мы обрабатываем регистр отдельно ниже
                    # здесь возвращаем как есть для pending обработки
                    key = name
                elif keyval == Gdk.KEY_at:
                    key = "@"
        except Exception:
            pass

        if key is None:
            # неизвестная клавиша — проверим, является ли она символом регистра для pending q/@
            if self.pending in ("q", "@"):
                try:
                    name = Gdk.keyval_name(keyval)
                    if name and len(name) == 1 and _is_valid_macro_register(name):
                        key = name
                    elif keyval == Gdk.KEY_at and self.pending == "@":
                        key = "@"
                except Exception:
                    pass
            if key is None:
                # неизвестная клавиша — сброс pending если нажата другая буква
                # но не сбрасываем на модификаторах
                if keyval not in (
                    Gdk.KEY_Shift_L,
                    Gdk.KEY_Shift_R,
                    Gdk.KEY_Control_L,
                    Gdk.KEY_Control_R,
                    Gdk.KEY_Alt_L,
                    Gdk.KEY_Alt_R,
                ):
                    if self.pending:
                        self.pending = ""
                return False

        # ── макросы ───────────────────────────────────────────────
        # pending "q" — выбор регистра для старта записи
        if self.pending == "q":
            # key — регистр
            if _is_valid_macro_register(key):
                if self._recording is None:
                    self._recording = key
                    self._recording_buffer = []
                    self.pending = ""
                    self._update_status()
                    return True
            self.pending = ""
            return True  # поглотили даже если невалидный

        # pending "@" — выбор регистра для воспроизведения
        if self.pending == "@":
            self.pending = ""
            # @@ -> last macro
            if key == "@":
                reg = self._last_macro
                if reg is not None and reg in self.macros:
                    if self._recording is not None and not self._is_replaying:
                        self._recording_buffer.append("@")
                        self._recording_buffer.append("@")
                    # воспроизвести
                    self._replay_macro(reg)
                return True
            if _is_valid_macro_register(key):
                if self._recording is not None and not self._is_replaying:
                    self._recording_buffer.append("@")
                    self._recording_buffer.append(key)
                if key in self.macros:
                    self._last_macro = key
                    self._replay_macro(key)
                return True
            return True

        # клавиша "q" — стоп или старт pending
        if key == "q":
            if self._recording is not None:
                # стоп
                reg = self._recording
                seq = list(self._recording_buffer)
                store_reg = reg
                if reg.isupper():
                    lower = reg.lower()
                    prev = self.macros.get(lower, [])
                    seq = list(prev) + seq
                    store_reg = lower
                self.macros[store_reg] = seq
                self.registers = self.macros
                self._recording = None
                self._recording_buffer = []
                self.pending = ""
                self._update_status()
                return True
            else:
                self.pending = "q"
                return True

        # клавиша "@" — pending
        if key == "@":
            self.pending = "@"
            return True

        # ── обработка pending dd/yy и обычные действия ───────────
        # используем внутренний обработчик для остальных ключей чтобы не дублировать логику записи
        # но для совместимости вызываем его с record=True
        # он уже включает движение, yank/paste/i
        # Если он вернул действие, считаем что поглотили
        # Чтобы избежать двойного выполнения, просто делегируем
        # Проверяем, был ли pending d/y уже обработан выше? Нет, теперь обрабатываем
        if self.pending == "d":
            if key == "d":
                self.pending = ""
                if self._recording is not None and not self._is_replaying:
                    self._recording_buffer.append("d")
                self._delete_line()
                return True
            else:
                self.pending = ""
                # fallthrough handle new key
        if self.pending == "y":
            if key == "y":
                self.pending = ""
                if self._recording is not None and not self._is_replaying:
                    self._recording_buffer.append("y")
                self._yank_line()
                return True
            else:
                self.pending = ""
        if key == "d":
            self.pending = "d"
            if self._recording is not None and not self._is_replaying:
                self._recording_buffer.append("d")
            return True
        if key == "y":
            self.pending = "y"
            if self._recording is not None and not self._is_replaying:
                self._recording_buffer.append("y")
            return True

        if key in ("h", "j", "k", "l"):
            if self._recording is not None and not self._is_replaying:
                self._recording_buffer.append(key)
            self._move_hjkl(key)
            return True
        if key == "p":
            if self._recording is not None and not self._is_replaying:
                self._recording_buffer.append(key)
            self._paste()
            return True
        if key == "i":
            self.mode = MODE_INSERT
            self.pending = ""
            if self._recording is not None and not self._is_replaying:
                self._recording_buffer.append(key)
            self._update_status()
            return True

        return False

    # ── утилиты для создания виджетов ───────────────────────────────
    def create_widgets(self) -> tuple[object, object, object]:
        """Создаёт status_label, command_entry, revealer для интеграции в FilesView.

        Возвращает (status_label, command_entry, revealer). Если GTK недоступен — (None,None,None).
        """
        if not _HAS_GTK:
            return None, None, None
        try:
            status = Gtk.Label(label="", css_classes=["dim-hint", "vim-status"])
            status.set_visible(False)
            entry = Gtk.Entry(css_classes=["vim-command"], placeholder_text=":w :q :wq")
            revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
            revealer.set_child(entry)
            revealer.set_reveal_child(False)
            # свяжем с контроллером
            self.status_label = status
            self.command_entry = entry
            self.command_revealer = revealer
            # подключить сигналы если ещё не подключены
            try:
                entry.connect("activate", self._on_command_activate)
                ctrl = Gtk.EventControllerKey.new()
                ctrl.connect("key-pressed", self._on_command_key)
                entry.add_controller(ctrl)
            except Exception:
                pass
            self._update_status()
            return status, entry, revealer
        except Exception:
            return None, None, None
