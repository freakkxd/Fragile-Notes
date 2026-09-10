"""Комментарии к строкам — gutter справа от редактора.

Хранение: ``file.md.comments.json`` (sidecar рядом с заметкой).
Формат JSON: список объектов ``{id, line, text, author, created}``,
где ``line`` — 1-индексированный номер строки.

UI: ``CommentsGutter`` — панель справа от редактора с комментариями к строкам
и кликабельными номерами строк (клик по номеру → добавить комментарий).
Интеграция в ``FilesView`` через ``_build_comments_gutter``.

Без GTK (headless/py_compile) — чистые функции хранения работают standalone.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ── GTK — опционально ────────────────────────────────────────────────────────
try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Pango", "1.0")
    from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402
except Exception:  # pragma: no cover - headless/tests/py_compile
    Adw = Gdk = GLib = Gtk = Pango = None  # type: ignore

# ── Константы ────────────────────────────────────────────────────────────────
COMMENTS_SUFFIX = ".comments.json"
"""Суффикс sidecar — ``file.md`` → ``file.md.comments.json``."""

# ограничение длины текста комментария (защита от огромных JSON)
_MAX_TEXT_LEN = 5000
_MAX_COMMENTS_PER_FILE = 1000


@dataclass
class Comment:
    """Один комментарий к строке."""

    id: str
    line: int  # 1-индексированная
    text: str
    author: str = "user"
    created: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Comment:
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            line=int(data.get("line", 1)),
            text=str(data.get("text", "")),
            author=str(data.get("author", "user")),
            created=str(data.get("created", "")),
        )


# ── Низкоуровневые helpers (без GTK) ───────────────────────────────────────


def get_comments_path(note_path: Path | str) -> Path:
    """Путь к sidecar ``file.md.comments.json``.

    Совместимость: алиас ``comments_path``.
    """
    return Path(str(note_path) + COMMENTS_SUFFIX)


# алиас для совместимости с ожидаемым API
def comments_path(note_path: Path | str) -> Path:
    return get_comments_path(note_path)


def load_comments(note_path: Path | str) -> list[dict[str, Any]]:
    """Загрузить комментарии для заметки.

    Возвращает список словарей ``{id, line, text, author, created}``.
    Пустой список если файла нет или JSON повреждён.
    Сортировка по ``line`` затем ``created``.
    """
    cpath = get_comments_path(note_path)
    if not cpath.is_file():
        return []
    try:
        raw = cpath.read_text(encoding="utf-8")
        if not raw.strip():
            return []
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        out: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                line = int(item.get("line", 0))
                text = str(item.get("text", "")).strip()
                if not text or line < 1:
                    continue
                # нормализация
                rec = {
                    "id": str(item.get("id") or uuid.uuid4().hex[:8]),
                    "line": line,
                    "text": text[:_MAX_TEXT_LEN],
                    "author": str(item.get("author", "user"))[:120],
                    "created": str(item.get("created", ""))[:40],
                }
                out.append(rec)
            except Exception:
                continue
        out.sort(key=lambda x: (int(x["line"]), str(x["created"])))
        return out
    except Exception:
        return []


def save_comments(note_path: Path | str, comments: list[dict[str, Any]]) -> bool:
    """Сохранить список комментариев (атомарно).

    Если список пуст — удаляет sidecar. Возвращает True при успехе.
    """
    cpath = get_comments_path(note_path)
    # фильтр + нормализация
    cleaned: list[dict[str, Any]] = []
    for item in comments:
        if not isinstance(item, dict):
            continue
        try:
            line = int(item.get("line", 0))
            text = str(item.get("text", "")).strip()
            if not text or line < 1:
                continue
            cleaned.append(
                {
                    "id": str(item.get("id") or uuid.uuid4().hex[:8]),
                    "line": line,
                    "text": text[:_MAX_TEXT_LEN],
                    "author": str(item.get("author", "user"))[:120],
                    "created": str(item.get("created", ""))[:40] or _now_iso(),
                }
            )
        except Exception:
            continue
        if len(cleaned) >= _MAX_COMMENTS_PER_FILE:
            break
    cleaned.sort(key=lambda x: (int(x["line"]), str(x["created"])))
    try:
        if not cleaned:
            # удалить sidecar если пуст
            try:
                if cpath.is_file():
                    cpath.unlink()
            except Exception:
                pass
            return True
        cpath.parent.mkdir(parents=True, exist_ok=True)
        tmp = cpath.with_suffix(cpath.suffix + ".tmp")
        tmp.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(cpath)
        return True
    except Exception:
        return False


def _now_iso() -> str:
    try:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")
    except Exception:
        return datetime.now().isoformat()


def add_comment(
    note_path: Path | str,
    line: int,
    text: str,
    author: str = "user",
) -> dict[str, Any] | None:
    """Добавить комментарий к строке (1-индексированной).

    Возвращает созданный объект или None при ошибке валидации.
    """
    txt = str(text or "").strip()
    if not txt or int(line) < 1:
        return None
    if len(txt) > _MAX_TEXT_LEN:
        txt = txt[:_MAX_TEXT_LEN]
    comments = load_comments(note_path)
    if len(comments) >= _MAX_COMMENTS_PER_FILE:
        return None
    rec: dict[str, Any] = {
        "id": uuid.uuid4().hex[:8],
        "line": int(line),
        "text": txt,
        "author": str(author or "user")[:120],
        "created": _now_iso(),
    }
    comments.append(rec)
    if save_comments(note_path, comments):
        return rec
    return None


def delete_comment(note_path: Path | str, comment_id: str) -> bool:
    """Удалить комментарий по id. Возвращает True если удалён."""
    cid = str(comment_id or "").strip()
    if not cid:
        return False
    comments = load_comments(note_path)
    orig_len = len(comments)
    filtered = [c for c in comments if str(c.get("id")) != cid]
    if len(filtered) == orig_len:
        return False
    return save_comments(note_path, filtered)


def update_comment(note_path: Path | str, comment_id: str, text: str) -> bool:
    """Обновить текст комментария. Возвращает True при успехе."""
    cid = str(comment_id or "").strip()
    txt = str(text or "").strip()
    if not cid or not txt:
        return False
    comments = load_comments(note_path)
    found = False
    for c in comments:
        if str(c.get("id")) == cid:
            c["text"] = txt[:_MAX_TEXT_LEN]
            found = True
            break
    if not found:
        return False
    return save_comments(note_path, comments)


def get_comments_for_line(note_path: Path | str, line: int) -> list[dict[str, Any]]:
    """Комментарии для конкретной строки."""
    try:
        ln = int(line)
    except Exception:
        return []
    return [c for c in load_comments(note_path) if int(c.get("line", 0)) == ln]


def get_comments_map(note_path: Path | str) -> dict[int, list[dict[str, Any]]]:
    """Сгруппированные по строкам комментарии ``{line: [comments]}``."""
    grouped: dict[int, list[dict[str, Any]]] = {}
    for c in load_comments(note_path):
        try:
            ln = int(c.get("line", 0))
        except Exception:
            continue
        grouped.setdefault(ln, []).append(c)
    return grouped


def count_comments(note_path: Path | str) -> int:
    """Количество комментариев для файла."""
    return len(load_comments(note_path))


def has_comments(note_path: Path | str, line: int | None = None) -> bool:
    """Есть ли комментарии (вообще или на строке)."""
    if line is None:
        return count_comments(note_path) > 0
    return bool(get_comments_for_line(note_path, line))


# ── GTK-виджет Gutter ───────────────────────────────────────────────────────
if Gtk is not None:  # pragma: no cover — GTK присутствует в рантайме

    class CommentsGutter(Gtk.Box):
        """Gutter справа от редактора с комментариями к строкам.

        Внешний API для FilesView:
            gutter = CommentsGutter(settings, parent_view=files_view)
            parent_box.append(gutter)
            gutter.set_file(Path(...))  # при _open
            gutter.set_file(None)       # при закрытии
            gutter.refresh()

        UI:
         - заголовок «Комментарии» + счётчик
         - скролл номеров строк (клик → добавить комментарий)
         - скролл списка комментариев (строка · текст + удалить)
         - кнопка «Добавить на текущей строке»
        """

        def __init__(
            self, settings: dict[str, Any] | None = None, parent_view: Any | None = None
        ) -> None:
            super().__init__(
                orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["comments-gutter"]
            )
            self._settings = dict(settings) if settings is not None else {}
            self._parent = parent_view
            self._current: Path | None = None
            self._comments: list[dict[str, Any]] = []
            self._alive = True
            self.connect("destroy", self._on_destroy)
            self.set_size_request(240, -1)
            self._build()

        # ── settings observer ─────────────────────────────────────────────
        def update_settings(self, settings: dict[str, Any]) -> None:
            self._settings = dict(settings) if settings is not None else {}

        def _on_destroy(self, _w: Any) -> None:
            self._alive = False

        # ── построение UI ─────────────────────────────────────────────────
        def _build(self) -> None:
            # Заголовок
            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            header.append(
                Gtk.Label(
                    label="Комментарии", halign=Gtk.Align.START, css_classes=["comments-title"]
                )
            )
            self._count_label = Gtk.Label(label="", css_classes=["dim-hint"])
            header.append(self._count_label)
            header.set_hexpand(True)
            self._refresh_btn = Gtk.Button(
                icon_name="view-refresh-symbolic",
                tooltip_text="Обновить комментарии",
                css_classes=["flat", "comments-refresh"],
            )
            self._refresh_btn.connect("clicked", lambda *_: self.refresh())
            header.append(self._refresh_btn)
            self.append(header)

            # Кнопка добавить на текущей строке
            add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            self._add_btn = Gtk.Button(
                label="＋ Комментарий",
                css_classes=["flat", "comments-add"],
                tooltip_text="Добавить комментарий к текущей строке (клик по номеру строки)",
            )
            self._add_btn.connect("clicked", self._on_add_current_line)
            self._add_btn.set_sensitive(False)
            add_row.append(self._add_btn)
            self.append(add_row)

            # Подсказка если файл не открыт
            self._hint = Gtk.Label(
                label="Открой заметку — комментарии хранятся в file.md.comments.json",
                css_classes=["dim-hint", "comments-hint"],
                halign=Gtk.Align.START,
                xalign=0,
                wrap=True,
            )
            self._hint.set_visible(True)
            self.append(self._hint)

            # ── Номера строк (клик → добавить) ──
            # Заголовок секции
            gutter_title = Gtk.Label(
                label="Строки — клик для комментария",
                halign=Gtk.Align.START,
                css_classes=["comments-subtitle"],
            )
            self.append(gutter_title)
            # FlowBox с кнопками номеров строк (компактный gutter)
            self._line_flow = Gtk.FlowBox(
                selection_mode=Gtk.SelectionMode.NONE,
                column_spacing=4,
                row_spacing=4,
                max_children_per_line=8,
                homogeneous=True,
                css_classes=["comments-line-flow"],
            )
            self._line_flow.set_halign(Gtk.Align.START)
            line_scroller = Gtk.ScrolledWindow(css_classes=["comments-line-scroller"])
            line_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            line_scroller.set_child(self._line_flow)
            line_scroller.set_min_content_height(80)
            line_scroller.set_max_content_height(160)
            try:
                line_scroller.set_propagate_natural_height(False)
            except AttributeError:
                pass
            self._line_scroller = line_scroller
            self._line_scroller.set_visible(False)
            self.append(self._line_scroller)

            # ── Список комментариев ──
            self._list = Gtk.ListBox(
                css_classes=["comments-list"], selection_mode=Gtk.SelectionMode.NONE
            )
            scroller = Gtk.ScrolledWindow(
                hexpand=False, vexpand=True, css_classes=["comments-scroller"]
            )
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_child(self._list)
            scroller.set_min_content_height(160)
            try:
                scroller.set_propagate_natural_height(False)
            except AttributeError:
                pass
            self._comments_scroller = scroller
            self._comments_scroller.set_visible(False)
            self.append(self._comments_scroller)

            # пустое состояние списка
            self._empty = Gtk.Label(
                label="— нет комментариев —\nкликни по номеру строки чтобы добавить",
                css_classes=["dim-hint", "comments-empty"],
                halign=Gtk.Align.CENTER,
                justify=Gtk.Justification.CENTER,
                wrap=True,
            )
            self._empty.set_visible(False)
            self.append(self._empty)

            # отслеживать изменения буфера для обновления номеров строк
            self._buffer_handler = None
            self._try_attach_buffer()

        def _try_attach_buffer(self) -> None:
            # подключение к TextBuffer если parent_view уже имеет buffer
            try:
                if (
                    self._parent is not None
                    and hasattr(self._parent, "buffer")
                    and self._parent.buffer is not None
                ):
                    buf = self._parent.buffer
                    # избежать двойного подключения
                    if getattr(self, "_buffer_handler", None) is None:
                        hid = buf.connect("changed", lambda *_: self._on_buffer_changed())
                        self._buffer_handler = hid
            except Exception:
                pass

        def _on_buffer_changed(self) -> None:
            if not self._alive or self._current is None:
                return
            # debounce обновление номеров строк
            try:
                GLib.idle_add(self._refresh_line_numbers)
            except Exception:
                pass

        # ── публичный API для FilesView ───────────────────────────────────
        def set_file(self, path: Path | str | None) -> None:
            """Установить текущий файл и перечитать комментарии."""
            if path is None:
                self._current = None
                self._comments = []
                self._render_empty("Открой заметку — комментарии в file.md.comments.json")
                self._add_btn.set_sensitive(False)
                return
            p = Path(path)
            self._current = p
            self._add_btn.set_sensitive(True)
            self._try_attach_buffer()
            self.refresh()

        def refresh(self) -> None:
            """Перечитать комментарии с диска."""
            if self._current is None:
                self._render_empty("Открой заметку — комментарии в file.md.comments.json")
                return
            try:
                self._comments = load_comments(self._current)
            except Exception:
                self._comments = []
            self._render()

        def _render_empty(self, hint: str) -> None:
            self._hint.set_text(hint)
            self._hint.set_visible(True)
            self._line_scroller.set_visible(False)
            self._comments_scroller.set_visible(False)
            self._empty.set_visible(False)
            self._count_label.set_text("")
            self._clear_line_flow()
            self._clear_list()
            self._add_btn.set_sensitive(self._current is not None)

        def _clear_line_flow(self) -> None:
            while (child := self._line_flow.get_first_child()) is not None:
                self._line_flow.remove(child)

        def _clear_list(self) -> None:
            while (child := self._list.get_first_child()) is not None:
                self._list.remove(child)

        def _refresh_line_numbers(self) -> bool:
            if self._current is None or not self._alive:
                return False
            # определить количество строк в редакторе
            line_count = 1
            try:
                if self._parent is not None and hasattr(self._parent, "buffer"):
                    line_count = self._parent.buffer.get_line_count()
                elif hasattr(self, "_buffer"):
                    line_count = 1
            except Exception:
                line_count = 1
            line_count = max(1, min(line_count, 500))  # лимит отображения номеров в gutter
            # карта строк → есть ли комментарии
            has_map: dict[int, int] = {}
            for c in self._comments:
                try:
                    ln = int(c.get("line", 0))
                    has_map[ln] = has_map.get(ln, 0) + 1
                except Exception:
                    continue
            self._clear_line_flow()
            for n in range(1, line_count + 1):
                cnt = has_map.get(n, 0)
                label = f"{n}·{cnt}" if cnt else str(n)
                css = ["comments-line-btn", "comments-line-has"] if cnt else ["comments-line-btn"]
                btn = Gtk.Button(
                    label=label,
                    css_classes=css,
                    tooltip_text=f"Строка {n} — клик для комментария"
                    + (f" · {cnt} комментарий" if cnt else ""),
                )
                # capture n
                _n = n
                btn.connect("clicked", lambda _b, ln=_n: self._on_line_clicked(ln))
                self._line_flow.append(btn)
            # показать gutter если есть строки
            self._line_scroller.set_visible(True)
            return False

        def _render(self) -> None:
            has_file = self._current is not None
            if not has_file:
                self._render_empty("Открой заметку — комментарии в file.md.comments.json")
                return
            self._hint.set_visible(False)
            count = len(self._comments)
            self._count_label.set_text(f"· {count}" if count else "")
            # обновить номера строк
            self._refresh_line_numbers()
            # список комментариев
            self._clear_list()
            if not self._comments:
                self._comments_scroller.set_visible(False)
                self._empty.set_visible(True)
                return
            self._empty.set_visible(False)
            self._comments_scroller.set_visible(True)
            # сгруппировать уже отсортировано, но рендерим линейно
            for c in self._comments:
                try:
                    cid = str(c.get("id", ""))
                    line = int(c.get("line", 1))
                    text = str(c.get("text", ""))[:400]
                    author = str(c.get("author", "user"))[:30]
                    created = str(c.get("created", ""))[:19].replace("T", " ")
                except Exception:
                    continue
                row = Gtk.ListBoxRow(
                    css_classes=["comments-row"], activatable=False, selectable=False
                )
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                box.set_margin_top(6)
                box.set_margin_bottom(6)
                box.set_margin_start(6)
                box.set_margin_end(6)
                # badge строки
                badge = Gtk.Button(
                    label=str(line),
                    css_classes=["comments-badge"],
                    tooltip_text=f"Клик — перейти к строке {line}",
                )
                _ln = line
                badge.connect("clicked", lambda _b, ln=_ln: self._scroll_to_line(ln))
                box.append(badge)
                # текст
                vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
                txt_lbl = Gtk.Label(
                    label=text,
                    halign=Gtk.Align.START,
                    xalign=0,
                    wrap=True,
                    wrap_mode=Pango.WrapMode.WORD_CHAR,
                    css_classes=["comments-text"],
                )
                txt_lbl.set_tooltip_text(text)
                vbox.append(txt_lbl)
                meta = Gtk.Label(
                    label=f"{author} · {created}" if created else author,
                    halign=Gtk.Align.START,
                    xalign=0,
                    css_classes=["dim-hint", "comments-meta"],
                    ellipsize=Pango.EllipsizeMode.END,
                )
                vbox.append(meta)
                box.append(vbox)
                # кнопки редактировать/удалить
                edit_btn = Gtk.Button(
                    icon_name="document-edit-symbolic",
                    css_classes=["flat", "comments-edit"],
                    tooltip_text="Редактировать",
                )
                _cid = cid
                _txt = text
                edit_btn.connect("clicked", lambda _b, cid=_cid, txt=_txt: self._on_edit(cid, txt))
                box.append(edit_btn)
                del_btn = Gtk.Button(
                    icon_name="user-trash-symbolic",
                    css_classes=["flat", "destructive-action", "comments-del"],
                    tooltip_text="Удалить комментарий",
                )
                del_btn.connect("clicked", lambda _b, cid=_cid: self._on_delete(cid))
                box.append(del_btn)
                row.set_child(box)
                self._list.append(row)

        # ── действия ──────────────────────────────────────────────────────
        def _scroll_to_line(self, line: int) -> None:
            if self._parent is None or not hasattr(self._parent, "buffer"):
                return
            try:
                buf = self._parent.buffer
                editor = getattr(self._parent, "editor", None)
                it = buf.get_iter_at_line(max(0, int(line) - 1))
                buf.place_cursor(it)
                if editor is not None:
                    editor.scroll_to_iter(it, 0.2, False, 0, 0)
                    editor.grab_focus()
            except Exception:
                pass

        def _on_line_clicked(self, line: int) -> None:
            self._show_add_dialog(line)

        def _on_add_current_line(self, _btn: Any) -> None:
            line = 1
            try:
                if self._parent is not None and hasattr(self._parent, "buffer"):
                    buf = self._parent.buffer
                    it = buf.get_iter_at_mark(buf.get_insert())
                    line = it.get_line() + 1
            except Exception:
                line = 1
            self._show_add_dialog(int(line))

        def _show_add_dialog(self, line: int) -> None:
            if self._current is None:
                return
            # диалог ввода текста комментария
            try:
                dlg = Adw.AlertDialog(
                    heading=f"Комментарий к строке {line}",
                    body=f"Заметка: {self._current.name} · строка {line}\nХранение: {self._current.name}.comments.json",
                )
            except Exception:
                dlg = Gtk.Dialog(title=f"Комментарий — строка {line}")
            # Для Adw.AlertDialog — extra_child с Entry
            entry_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            entry_box.set_margin_top(8)
            text_view = Gtk.TextView(
                wrap_mode=Gtk.WrapMode.WORD, css_classes=["comments-entry"], height_request=80
            )
            text_view.set_left_margin(6)
            text_view.set_right_margin(6)
            buf = text_view.get_buffer()
            sc = Gtk.ScrolledWindow(
                hexpand=True, vexpand=False, css_classes=["comments-entry-scroller"]
            )
            sc.set_child(text_view)
            sc.set_min_content_height(80)
            entry_box.append(sc)
            # Adw path
            is_adw = isinstance(dlg, Adw.AlertDialog) if Adw is not None else False
            if is_adw:
                try:
                    dlg.set_extra_child(entry_box)
                    dlg.add_response("cancel", "Отмена")
                    dlg.add_response("add", "Добавить")
                    dlg.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
                    dlg.set_default_response("add")
                    dlg.set_close_response("cancel")

                    def _resp(_d: Any, resp: str) -> None:
                        if resp != "add":
                            return
                        s, e = buf.get_bounds()
                        txt = buf.get_text(s, e, True).strip()
                        if not txt:
                            return
                        self._do_add(line, txt)

                    dlg.connect("response", _resp)
                    dlg.present(self)
                    text_view.grab_focus()
                    return
                except Exception:
                    pass
            # fallback Gtk.Dialog
            try:
                if isinstance(dlg, Gtk.Dialog):
                    dlg = dlg  # уже диалог
                else:
                    dlg = Gtk.Dialog(title=f"Комментарий — строка {line}")
                try:
                    dlg.set_transient_for(self.get_root())  # type: ignore
                except Exception:
                    pass
                dlg.set_default_size(480, 260)
                content = dlg.get_content_area()
                content.set_spacing(8)
                content.set_margin_top(12)
                content.set_margin_bottom(12)
                content.set_margin_start(12)
                content.set_margin_end(12)
                content.append(
                    Gtk.Label(
                        label=f"Строка {line} · {self._current.name}",
                        halign=Gtk.Align.START,
                        css_classes=["dim-hint"],
                    )
                )
                content.append(sc)
                btn_row = Gtk.Box(
                    orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END
                )
                cancel = Gtk.Button(label="Отмена")
                ok = Gtk.Button(label="Добавить", css_classes=["suggested-action"])
                btn_row.append(cancel)
                btn_row.append(ok)
                content.append(btn_row)

                def _do(*_a: Any) -> None:
                    s, e = buf.get_bounds()
                    txt = buf.get_text(s, e, True).strip()
                    if not txt:
                        return
                    dlg.close()
                    self._do_add(line, txt)

                ok.connect("clicked", _do)
                cancel.connect("clicked", lambda *_: dlg.close())
                text_view.connect("key-pressed", lambda *_: None)
                dlg.present()
                text_view.grab_focus()
            except Exception:
                # ultimate fallback: просто добавить пустой?
                pass

        def _do_add(self, line: int, text: str) -> None:
            if self._current is None:
                return
            try:
                rec = add_comment(self._current, line, text)
                if rec is None:
                    self._count_label.set_text("ошибка добавления")
                    return
                self.refresh()
                # toast
                try:
                    root = self.get_root()
                    overlay = getattr(root, "toast_overlay", None) if root is not None else None
                    if overlay is not None:
                        toast = Adw.Toast.new(f"Комментарий к строке {line} добавлен")
                        toast.set_timeout(2)
                        overlay.add_toast(toast)
                except Exception:
                    pass
                # обновить file_label если есть parent
                try:
                    if self._parent is not None and hasattr(self._parent, "file_label"):
                        self._parent.file_label.set_text(f"комментарий: строка {line}")
                except Exception:
                    pass
            except Exception as exc:  # noqa: BLE001
                try:
                    if self._parent is not None and hasattr(self._parent, "file_label"):
                        self._parent.file_label.set_text(f"ошибка комментария: {exc}")
                except Exception:
                    pass

        def _on_delete(self, comment_id: str) -> None:
            if self._current is None or not comment_id:
                return
            # подтверждение
            try:
                dlg = Adw.AlertDialog(
                    heading="Удалить комментарий?",
                    body="Комментарий будет удалён из file.md.comments.json",
                )
                dlg.add_response("cancel", "Отмена")
                dlg.add_response("delete", "Удалить")
                dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
                dlg.set_default_response("cancel")
                dlg.set_close_response("cancel")
                _cid = comment_id

                def _resp(_d: Any, resp: str) -> None:
                    if resp != "delete":
                        return
                    try:
                        ok = delete_comment(self._current, _cid)
                        if ok:
                            self.refresh()
                    except Exception:
                        pass

                dlg.connect("response", _resp)
                dlg.present(self)
                return
            except Exception:
                pass
            # fallback — сразу удалить
            try:
                if delete_comment(self._current, comment_id):
                    self.refresh()
            except Exception:
                pass

        def _on_edit(self, comment_id: str, old_text: str) -> None:
            if self._current is None or not comment_id:
                return
            try:
                dlg = Adw.AlertDialog(heading="Редактировать комментарий", body=f"id: {comment_id}")
            except Exception:
                dlg = Gtk.Dialog(title="Редактировать комментарий")
            entry_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            entry_box.set_margin_top(8)
            text_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD, css_classes=["comments-entry"])
            buf = text_view.get_buffer()
            buf.set_text(old_text or "")
            sc = Gtk.ScrolledWindow(hexpand=True, vexpand=False)
            sc.set_child(text_view)
            sc.set_min_content_height(80)
            entry_box.append(sc)
            is_adw = isinstance(dlg, Adw.AlertDialog) if Adw is not None else False
            if is_adw:
                try:
                    dlg.set_extra_child(entry_box)
                    dlg.add_response("cancel", "Отмена")
                    dlg.add_response("save", "Сохранить")
                    dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
                    dlg.set_default_response("save")
                    dlg.set_close_response("cancel")

                    def _resp(_d: Any, resp: str) -> None:
                        if resp != "save":
                            return
                        s, e = buf.get_bounds()
                        txt = buf.get_text(s, e, True).strip()
                        if not txt:
                            return
                        try:
                            if update_comment(self._current, comment_id, txt):
                                self.refresh()
                        except Exception:
                            pass

                    dlg.connect("response", _resp)
                    dlg.present(self)
                    return
                except Exception:
                    pass
            # fallback Gtk.Dialog
            try:
                if not isinstance(dlg, Gtk.Dialog):
                    dlg = Gtk.Dialog(title="Редактировать комментарий")
                try:
                    dlg.set_transient_for(self.get_root())  # type: ignore
                except Exception:
                    pass
                dlg.set_default_size(480, 240)
                content = dlg.get_content_area()
                content.set_spacing(8)
                content.set_margin_top(12)
                content.set_margin_bottom(12)
                content.set_margin_start(12)
                content.set_margin_end(12)
                content.append(sc)
                btn_row = Gtk.Box(
                    orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END
                )
                cancel = Gtk.Button(label="Отмена")
                ok = Gtk.Button(label="Сохранить", css_classes=["suggested-action"])
                btn_row.append(cancel)
                btn_row.append(ok)
                content.append(btn_row)

                def _do(*_a: Any) -> None:
                    s, e = buf.get_bounds()
                    txt = buf.get_text(s, e, True).strip()
                    if not txt:
                        return
                    dlg.close()
                    try:
                        if update_comment(self._current, comment_id, txt):
                            self.refresh()
                    except Exception:
                        pass

                ok.connect("clicked", _do)
                cancel.connect("clicked", lambda *_: dlg.close())
                dlg.present()
            except Exception:
                pass

    # алиас для совместимости — часть кода ожидает CommentsPanel
    CommentsPanel = CommentsGutter

else:  # headless fallback

    class CommentsGutter:  # type: ignore[no-redef]
        def __init__(self, *a: Any, **kw: Any) -> None:
            raise RuntimeError("GTK недоступен — CommentsGutter требует gi.repository.Gtk")

    CommentsPanel = CommentsGutter  # type: ignore

__all__ = [
    "COMMENTS_SUFFIX",
    "Comment",
    "get_comments_path",
    "comments_path",
    "load_comments",
    "save_comments",
    "add_comment",
    "delete_comment",
    "update_comment",
    "get_comments_for_line",
    "get_comments_map",
    "count_comments",
    "has_comments",
    "CommentsGutter",
    "CommentsPanel",
]
