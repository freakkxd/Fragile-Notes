"""Smart links (авто-предложение [[wikilink]]) для FragileNotes.

При вводе ``[[`` в редакторе показывает попап с предложениями файлов (fuzzy),
``Tab`` / ``Enter`` — автодополнение, ``Escape`` — закрыть, стрелки — навигация.

Дизайн:
- Чистые функции ``get_wikilink_prefix`` / ``fuzzy_score`` / ``rank_candidates``
  покрыты тестами и не требуют GTK.
- Класс ``SmartLinks`` отвечает за GTK-интеграцию (Popover + ListBox) и
  подключается в ``files_view.FilesView`` одной строкой::

      self._smart_links = SmartLinks(self.editor, self.buffer, self._vault)

Использует кэшированный индекс из ``services.vault`` / ``VaultService``
без обхода диска при каждом кейстроуке.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# ── Чистая логика (без GTK) ────────────────────────────────────────────

_WORD_SEPS = set(" /\\-_.,;:()[]\"'")

def _is_word_start(text: str, idx: int) -> bool:
    if idx == 0:
        return True
    return text[idx - 1] in _WORD_SEPS

def fuzzy_match(query: str, target: str) -> tuple[int | None, list[int]]:
    """Fuzzy-подсчёт как в quick_switcher: subsequence + бонусы.

    Returns (score, indices) или (None, []) если не подпоследовательность.
    """
    q = query.lower()
    t = target.lower()
    if not q:
        return (0, [])
    if len(q) > len(t):
        return (None, [])
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
                    break
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

    score = len(indices) * 10
    for k, idx in enumerate(indices):
        if idx == 0:
            score += 12
        elif _is_word_start(t, idx):
            score += 10
        if k > 0 and idx == indices[k - 1] + 1:
            score += 8
        elif k > 0:
            gap = idx - indices[k - 1] - 1
            score -= gap
    span = indices[-1] - indices[0] + 1 if indices else 0
    score -= span // 4
    if t.startswith(q):
        score += 20
    elif q in t:
        score += 10
    return (score, indices)

def fuzzy_score(query: str, target: str) -> int | None:
    s, _ = fuzzy_match(query, target)
    return s

def get_wikilink_prefix(text: str, offset: int) -> tuple[int, str] | None:
    """Находит незакрытый ``[[prefix`` перед курсором.

    Args:
        text: весь текст буфера
        offset: смещение курсора (0..len(text))

    Returns:
        (open_pos, prefix) где open_pos — индекс ``[[``, prefix — текст
        между ``[[`` и курсором (без ``]]``). Если контекста нет — None.

    Правила:
    - берётся последнее ``[[`` перед курсором;
    - если между ``[[`` и курсором есть ``]]`` — контекст закрыт;
    - префикс не должен содержать перевода строки;
    - допускаются символы до ``|`` или ``#`` — они обрезаются (alias/heading).
    """
    if offset < 2:
        return None
    # ограничим offset границами
    if offset > len(text):
        offset = len(text)
    # ищем последнее [[ перед курсором
    open_pos = text.rfind("[[", 0, offset)
    if open_pos == -1:
        return None
    between = text[open_pos:offset]
    # если уже закрыт
    if "]]" in between:
        return None
    prefix = text[open_pos + 2:offset]
    # префикс не должен содержать новой строки и закрывающей скобки
    if "\n" in prefix or "]]" in prefix:
        return None
    # отсекаем alias/heading часть — набираем первую часть до | или #
    # но если пользователь уже ввёл |, считаем что он вводит alias — не показываем
    # автодополнение таргета? для простоты — игнорируем часть после |
    if "|" in prefix:
        # есть pipe — редактируют alias, не таргет
        # всё равно можем предложить по первой части? нет — закрываем
        # иначе будет путаница. Попросту возвращаем None.
        return None
    # обрезаем heading-якорь после #
    if "#" in prefix:
        prefix = prefix.split("#", 1)[0]
    # оставить prefix как есть (включая пробелы) — fuzzy обрежет
    return (open_pos, prefix)

def _collect_candidates(vault_service: Any) -> list[tuple[str, str]]:
    """Собрать кандидатов (title, abs_path) из vault_service без тяжёлого I/O.

    Порядок попыток:
    1. vault_service._vault._notes_index(settings)
    2. vault_service._notes (если FilesView пробросит)
    3. vault_service.ensure_file_tree -> collect_notes
    4. пусто
    """
    # попытка 1: _notes_index
    try:
        settings = getattr(vault_service, "settings", None) or getattr(vault_service, "_settings", None)
        if settings is None:
            settings = {}
        vault_mod = getattr(vault_service, "_vault", None)
        if vault_mod is not None and hasattr(vault_mod, "_notes_index"):
            hits = vault_mod._notes_index(settings)  # type: ignore[attr-defined]
            out: list[tuple[str, str]] = []
            for h in hits:
                try:
                    p = str(h.path)
                    t = getattr(h, "title", "") or Path(p).stem
                    out.append((t, p))
                except Exception:
                    continue
            if out:
                return out
    except Exception:
        pass
    # попытка 2: через ensure_file_tree + collect_notes
    try:
        from ..services.vault_service import collect_notes  # local import to avoid cycle
        settings = getattr(vault_service, "settings", None) or getattr(vault_service, "_settings", None) or {}
        node = vault_service.ensure_file_tree(settings=settings)  # type: ignore
        notes = collect_notes(node)
        out = []
        for fname, fpath in notes:
            try:
                # title via cache if possible
                title = None
                try:
                    title = vault_service.note_title_cached(Path(fpath))  # type: ignore
                except Exception:
                    title = None
                if not title:
                    title = Path(fname).stem if fname else Path(fpath).stem
                out.append((title, fpath))
            except Exception:
                out.append((Path(fpath).stem, fpath))
        return out
    except Exception:
        pass
    # попытка 3: fallback — пусто
    return []

def rank_candidates(query: str, candidates: list[tuple[str, str]], limit: int = 10) -> list[tuple[int, str, str]]:
    """Отранжировать кандидатов по fuzzy_score.

    Args:
        query: префикс внутри ``[[`` (может быть пустым)
        candidates: [(title, path), ...]
        limit: максимум результатов

    Returns:
        [(score, title, path), ...] отсортировано по убыванию score.
        При пустом query — первые ``limit`` по алфавиту.
    """
    q = (query or "").strip()
    if not q:
        # без префикса — просто алфавит, лимит
        sorted_cands = sorted(candidates, key=lambda x: x[0].lower())[:limit]
        return [(0, t, p) for t, p in sorted_cands]
    scored: list[tuple[int, str, str]] = []
    q_low = q.lower()
    for title, path in candidates:
        stem = Path(path).stem
        # пробуем title и stem, берём лучший скор
        best: int | None = None
        for tgt in (title, stem, path):
            s, _ = fuzzy_match(q, tgt)
            if s is not None and (best is None or s > best):
                best = s
        # fallback: подстрока
        if best is None:
            if q_low in title.lower() or q_low in stem.lower():
                best = 5
            else:
                continue
        scored.append((best, title, path))
    scored.sort(key=lambda x: (-x[0], x[1].lower()))
    return scored[:limit]

def get_suggestions(vault_service: Any, prefix: str, limit: int = 8) -> list[tuple[int, str, str]]:
    """Удобная обёртка: собрать кандидатов и отранжировать."""
    cands = _collect_candidates(vault_service)
    return rank_candidates(prefix or "", cands, limit=limit)

# ── GTK-интеграция (опционально) ───────────────────────────────────────
# Попытка импорта GTK — если недоступен (тесты/headless), класс деградирует
# до no-op, но чистые функции остаются доступны.

try:
    import gi  # type: ignore
    gi.require_version("Gtk", "4.0")  # type: ignore
    gi.require_version("Gdk", "4.0")  # type: ignore
    from gi.repository import Gdk, GLib, Gtk, Pango  # type: ignore
    _GTK_AVAILABLE = True
except Exception:  # pragma: no cover
    Gtk = None  # type: ignore
    Gdk = None  # type: ignore
    GLib = None  # type: ignore
    Pango = None  # type: ignore
    _GTK_AVAILABLE = False


class SmartLinks:
    """Контроллер попапа ``[[``-автодополнения.

    Подключается к ``Gtk.TextView``/``Gtk.TextBuffer`` и показывает
    ``Gtk.Popover`` с fuzzy-предложениями. ``Tab``/``Enter`` вставляет
    выбранную заметку, закрывая ``[[prefix`` в ``[[Title]]``.
    """

    def __init__(self, text_view: Any, text_buffer: Any, vault_service: Any, limit: int = 8) -> None:
        self.view = text_view
        self.buffer = text_buffer
        self.vault = vault_service
        self.limit = int(limit) if limit else 8
        self._popover: Any | None = None
        self._listbox: Any | None = None
        self._items: list[tuple[int, str, str]] = []
        self._selected: int = 0
        self._open_pos: int | None = None
        self._prefix: str = ""
        self._key_ctrl: Any | None = None
        self._updating: bool = False  # защита от рекурсии buffer.changed

        if not _GTK_AVAILABLE or text_view is None or text_buffer is None:
            return
        try:
            self._build_popover()
            self._connect_signals()
        except Exception:
            self._popover = None

    # ── построение ──
    def _build_popover(self) -> None:
        pop = Gtk.Popover()
        pop.set_autohide(True)
        pop.set_has_arrow(False)
        # стили как в quick_switcher
        try:
            pop.add_css_class("smart-links-popover")
        except Exception:
            pass
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        header = Gtk.Label(label="Связать заметку — Tab/Enter", halign=Gtk.Align.START, css_classes=["dim-hint"])
        header.set_xalign(0)
        box.append(header)
        scroller = Gtk.ScrolledWindow(max_content_height=220)
        scroller.set_max_content_width(380)
        scroller.set_propagate_natural_height(True)
        lb = Gtk.ListBox(css_classes=["smart-links-list"])
        lb.set_selection_mode(Gtk.SelectionMode.SINGLE)
        lb.connect("row-activated", self._on_row_activated)
        scroller.set_child(lb)
        box.append(scroller)
        pop.set_child(box)
        # привязываем к TextView — позиционируем через pointing rect при показе
        try:
            pop.set_parent(self.view)
        except Exception:
            # fallback: anchor via widget parent
            try:
                pop.set_parent(self.view.get_parent())
            except Exception:
                pass
        self._popover = pop
        self._listbox = lb

    def _connect_signals(self) -> None:
        # buffer changed — debounce через idle? сразу для простоты
        try:
            self.buffer.connect("changed", self._on_buffer_changed)
        except Exception:
            pass
        # key controller на view — Tab/Up/Down/Enter/Esc только когда попап виден
        try:
            ctrl = Gtk.EventControllerKey.new()
            ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            ctrl.connect("key-pressed", self._on_key_pressed)
            self.view.add_controller(ctrl)
            self._key_ctrl = ctrl
        except Exception:
            pass
        # клик вне — скрыть
        try:
            if self._popover is not None:
                self._popover.connect("closed", self._on_popover_closed)
        except Exception:
            pass

    # ── контекст ──
    def _get_text_and_offset(self) -> tuple[str, int] | None:
        try:
            start, end = self.buffer.get_bounds()
            text = self.buffer.get_text(start, end, True)
            it = self.buffer.get_iter_at_mark(self.buffer.get_insert())
            offset = it.get_offset()
            return text, offset
        except Exception:
            return None

    def _check_and_show(self) -> None:
        if self._updating:
            return
        res = self._get_text_and_offset()
        if res is None:
            self._hide()
            return
        text, offset = res
        ctx = get_wikilink_prefix(text, offset)
        if ctx is None:
            self._hide()
            return
        open_pos, prefix = ctx
        self._open_pos = open_pos
        self._prefix = prefix
        # собрать предложения
        try:
            suggestions = get_suggestions(self.vault, prefix, limit=self.limit)
        except Exception:
            suggestions = []
        if not suggestions:
            self._hide()
            return
        self._items = suggestions
        self._selected = 0
        self._show_popover(suggestions)

    def _show_popover(self, items: list[tuple[int, str, str]]) -> None:
        if self._popover is None or self._listbox is None:
            return
        # заполнить ListBox
        try:
            while (child := self._listbox.get_first_child()) is not None:
                self._listbox.remove(child)
        except Exception:
            pass
        for idx, (_score, title, path) in enumerate(items):
            row = Gtk.ListBoxRow(css_classes=["smart-link-row"], activatable=True, selectable=True)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_margin_top(4)
            box.set_margin_bottom(4)
            box.set_margin_start(6)
            box.set_margin_end(6)
            # иконка
            icon = Gtk.Label(label="📄")
            box.append(icon)
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
            t_lbl = Gtk.Label(halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            # подсветка fuzzy-совпадений в заголовке если есть prefix
            if self._prefix.strip():
                _, indices = fuzzy_match(self._prefix.strip(), title)
                if indices:
                    # экранируем и подсвечиваем
                    try:
                        esc_parts: list[str] = []
                        idx_set = set(indices)
                        for i, ch in enumerate(title):
                            esc = GLib.markup_escape_text(ch, -1)
                            if i in idx_set:
                                esc_parts.append(f'<span foreground="#82a8ff" weight="bold">{esc}</span>')
                            else:
                                esc_parts.append(esc)
                        t_lbl.set_markup("".join(esc_parts))
                    except Exception:
                        t_lbl.set_label(title)
                else:
                    t_lbl.set_label(title)
            else:
                t_lbl.set_label(title)
            vbox.append(t_lbl)
            # подстрока — родительская папка
            try:
                parent = Path(path).parent.name if Path(path).parent else ""
                if parent:
                    sub = Gtk.Label(label=parent, css_classes=["dim-hint"], halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END)
                    sub.set_xalign(0)
                    vbox.append(sub)
            except Exception:
                pass
            box.append(vbox)
            row.set_child(box)
            # сохранить данные для вставки
            row._title = title  # type: ignore[attr-defined]
            row._path = path  # type: ignore[attr-defined]
            self._listbox.append(row)
        # выбрать первый
        try:
            first = self._listbox.get_row_at_index(0)
            if first is not None:
                self._listbox.select_row(first)
        except Exception:
            pass
        # позиционирование у курсора
        try:
            it = self.buffer.get_iter_at_mark(self.buffer.get_insert())
            rect = self.view.get_iter_location(it)
            # перевести в координаты виджета
            try:
                win_rect = self.view.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, rect.x, rect.y)
                rect.x, rect.y = win_rect  # type: ignore
            except Exception:
                pass
            # поднять чуть ниже строки курсора
            rect.y += rect.height + 2
            rect.height = 1
            rect.width = 1
            gdk_rect = Gdk.Rectangle()
            gdk_rect.x, gdk_rect.y, gdk_rect.width, gdk_rect.height = rect.x, rect.y, rect.width, rect.height
            self._popover.set_pointing_to(gdk_rect)  # type: ignore
        except Exception:
            pass
        try:
            self._popover.popup()
        except Exception:
            pass

    def _hide(self) -> None:
        if self._popover is not None:
            try:
                if self._popover.get_visible():
                    self._popover.popdown()
            except Exception:
                pass
        self._items = []
        self._selected = 0
        self._open_pos = None

    def _on_popover_closed(self, *_a: Any) -> None:
        self._items = []
        self._selected = 0

    # ── события ──
    def _on_buffer_changed(self, *_a: Any) -> None:
        # дебаунс 50мс? синхронно достаточно для малых файлов
        try:
            GLib.idle_add(self._check_and_show)
        except Exception:
            self._check_and_show()

    def _on_key_pressed(self, _ctrl: Any, keyval: int, _keycode: int, state: Any) -> bool:
        # только когда попап виден
        visible = False
        try:
            visible = bool(self._popover is not None and self._popover.get_visible())
        except Exception:
            visible = bool(self._items)
        if not visible:
            return False
        # Tab — автодополнение
        if keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            self._complete_selected()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._complete_selected()
            return True
        if keyval == Gdk.KEY_Escape:
            self._hide()
            return True
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            self._move(1)
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            self._move(-1)
            return True
        return False

    def _move(self, delta: int) -> None:
        if not self._items or self._listbox is None:
            return
        try:
            n = len(self._items)
            self._selected = (self._selected + delta) % n
            row = self._listbox.get_row_at_index(self._selected)
            if row is not None:
                self._listbox.select_row(row)
        except Exception:
            pass

    def _on_row_activated(self, _lb: Any, row: Any) -> None:
        # клик мышью — вставить
        try:
            # синхронизировать selected
            idx = row.get_index() if hasattr(row, "get_index") else 0
            self._selected = int(idx)
        except Exception:
            pass
        self._complete_selected()

    # ── вставка ──
    def _complete_selected(self) -> None:
        if self._open_pos is None or not self._items:
            self._hide()
            return
        idx = max(0, min(self._selected, len(self._items) - 1))
        try:
            _score, title, _path = self._items[idx]
        except Exception:
            self._hide()
            return
        self._insert_completion(title)
        self._hide()

    def _insert_completion(self, title: str) -> None:
        if self._open_pos is None:
            return
        self._updating = True
        try:
            # диапазон замены: от [[  + prefix  до курсора
            # open_pos указывает на '[', нам нужно заменить prefix и закрыть ]]
            cur_iter = self.buffer.get_iter_at_mark(self.buffer.get_insert())
            cur_offset = cur_iter.get_offset()
            start_iter = self.buffer.get_iter_at_offset(self._open_pos + 2)
            end_iter = self.buffer.get_iter_at_offset(cur_offset)
            # удалить prefix
            try:
                self.buffer.delete(start_iter, end_iter)
            except Exception:
                pass
            # вставить title + ]]
            insert_at = self.buffer.get_iter_at_offset(self._open_pos + 2)
            # после delete смещение может измениться — пересчитать
            try:
                insert_at = self.buffer.get_iter_at_offset(self._open_pos + 2)
            except Exception:
                pass
            completion = f"{title}]]"
            self.buffer.insert(insert_at, completion)
            # поставить курсор после ]]
            try:
                new_offset = self._open_pos + 2 + len(completion)
                new_iter = self.buffer.get_iter_at_offset(new_offset)
                self.buffer.place_cursor(new_iter)
                self.view.scroll_to_iter(new_iter, 0.1, False, 0, 0)
            except Exception:
                pass
        finally:
            self._updating = False

    def destroy(self) -> None:
        try:
            if self._popover is not None:
                self._popover.popdown()
                try:
                    self._popover.unparent()
                except Exception:
                    pass
        except Exception:
            pass

# ── публичный API для файлового вью и тестов ─────────────────────────

__all__ = [
    "SmartLinks",
    "get_wikilink_prefix",
    "fuzzy_match",
    "fuzzy_score",
    "rank_candidates",
    "get_suggestions",
]
