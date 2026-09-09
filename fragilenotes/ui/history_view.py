"""История версий заметки — git history UI для FragileNotes.

Панель показывает `git log -- <file>`, diff выбранного коммита и
позволяет восстановить файл к выбранной версии.

Архитектура:
* чистые функции без GTK — git-обвязка (log/diff/show/restore) для headless-тестов
* Gtk-виджет ``HistoryPanel`` — панель для ``FilesView`` (справа под редактором)
* интеграция: ``FilesView._build_history_panel`` + ``set_file`` при ``_open``

Без зависимости от ``git_sync.py`` — дублирует минимальные ``_run_git``/``_is_git_repo``.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── GTK — опционально для headless/py_compile ──────────────────────────────
try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Pango", "1.0")
    from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402
except Exception:  # pragma: no cover
    Adw = Gdk = GLib = Gtk = Pango = None  # type: ignore

# ── Модель коммита ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CommitInfo:
    """Один коммит из ``git log -- <file>``."""

    hash: str
    short_hash: str
    author: str
    date: str  # iso string или форматированная
    subject: str
    rel_path: str | None = None

    def title(self) -> str:
        return f"{self.short_hash} · {self.subject}"

    def subtitle(self) -> str:
        return f"{self.author} · {self.date}"


# ── Низкоуровневые git-хелперы (без GTK) ───────────────────────────────────

def _run_git(args: list[str], cwd: Path, timeout: float = 15.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def is_git_repo(vault_root: Path | str) -> bool:
    """Проверка что vault — git-репозиторий."""
    root = Path(vault_root)
    if not root.is_dir():
        return False
    if (root / ".git").exists():
        return True
    if shutil.which("git") is None:
        return False
    try:
        proc = _run_git(["rev-parse", "--is-inside-work-tree"], root, timeout=5)
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except Exception:
        return False


def _rel_path(file_path: Path | str, vault_root: Path | str) -> str | None:
    """Относительный путь файла от vault_root для git-команд, или None если вне."""
    try:
        fp = Path(file_path).resolve()
        root = Path(vault_root).resolve()
        rel = fp.relative_to(root)
        return rel.as_posix()
    except Exception:
        # fallback: имя файла
        try:
            return Path(file_path).name
        except Exception:
            return None


def get_commits_for_file(
    file_path: Path | str,
    vault_root: Path | str,
    limit: int = 50,
) -> list[CommitInfo]:
    """``git log --pretty=format:%H%x1f%an%x1f%ad%x1f%s --date=iso --follow -- <file>``.

    Возвращает список CommitInfo (новые → старые). Пустой список если не репо
    или файл без истории. Не бросает исключений наружу — возвращает [].
    """
    root = Path(vault_root)
    if not is_git_repo(root):
        return []
    if shutil.which("git") is None:
        return []
    rel = _rel_path(file_path, root)
    if rel is None:
        return []
    # формат: hash\x1f author \x1f date \x1f subject
    fmt = "%H%x1f%an%x1f%ad%x1f%s"
    args = [
        "log",
        f"--max-count={int(limit)}",
        f"--pretty=format:{fmt}",
        "--date=iso",
        "--follow",
        "--",
        rel,
    ]
    try:
        proc = _run_git(args, root, timeout=15)
        if proc.returncode != 0:
            # если файл ещё не закоммичен — git log вернёт 0 с пустым stdout или 128
            return []
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        out: list[CommitInfo] = []
        for ln in lines:
            parts = ln.split("\x1f")
            if len(parts) < 4:
                continue
            h, author, date, subject = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()
            if not h:
                continue
            out.append(CommitInfo(
                hash=h,
                short_hash=h[:7],
                author=author,
                date=date,
                subject=subject,
                rel_path=rel,
            ))
        return out
    except Exception:
        return []


def get_diff(
    file_path: Path | str,
    commit_hash: str,
    vault_root: Path | str,
    unified: int = 3,
) -> str:
    """Дифф коммита для файла: ``git show <hash> -- <file>`` (патч без заголовка).

    Возвращает строку diff или "" если не удалось.
    """
    root = Path(vault_root)
    if not is_git_repo(root) or not commit_hash:
        return ""
    rel = _rel_path(file_path, root)
    if rel is None:
        return ""
    # git show --pretty=format: --unified=3 <hash> -- <file>
    args = ["show", "--pretty=format:", f"--unified={int(unified)}", commit_hash, "--", rel]
    try:
        proc = _run_git(args, root, timeout=10)
        if proc.returncode != 0:
            # fallback: diff с родителем
            try:
                proc2 = _run_git(["diff", f"{commit_hash}^", commit_hash, "--", rel], root, timeout=10)
                if proc2.returncode == 0:
                    return proc2.stdout
            except Exception:
                pass
            return proc.stderr.strip() or ""
        return proc.stdout
    except Exception as exc:  # noqa: BLE001
        return str(exc)


def get_file_content_at_commit(
    file_path: Path | str,
    commit_hash: str,
    vault_root: Path | str,
) -> str | None:
    """Содержимое файла на коммите: ``git show <hash>:<rel_path>``.

    Возвращает текст или None если не удалось (бинарный/удалён).
    """
    root = Path(vault_root)
    if not is_git_repo(root) or not commit_hash:
        return None
    rel = _rel_path(file_path, root)
    if rel is None:
        return None
    spec = f"{commit_hash}:{rel}"
    try:
        proc = _run_git(["show", spec], root, timeout=10)
        if proc.returncode != 0:
            return None
        return proc.stdout
    except Exception:
        return None


def restore_file_at_commit(
    file_path: Path | str,
    commit_hash: str,
    vault_root: Path | str,
) -> tuple[bool, str]:
    """Восстановить файл к версии на коммите (перезаписать рабочую копию).

    Возвращает (ok, message). Не делает commit — только checkout содержимого.
    """
    content = get_file_content_at_commit(file_path, commit_hash, vault_root)
    if content is None:
        return False, "не удалось получить версию (файл удалён или бинарный?)"
    # если файл был удалён в коммите — get_file_content вернёт None выше
    fp = Path(file_path)
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        # если исходный файл — .md.enc, восстанавливаем как есть (по байтам git show уже текст)
        # но diff/show для шифрованных покажет шифроблоб — это ожидаемо
        fp.write_text(content, encoding="utf-8")
        return True, f"восстановлено к {commit_hash[:7]}"
    except OSError as exc:
        return False, f"ошибка записи: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# ── GTK-виджет HistoryPanel ────────────────────────────────────────────────

if Gtk is not None:  # pragma: no cover — GTK присутствует в рантайме

    class HistoryPanel(Gtk.Box):
        """Панель истории git для одного файла — встраивается в FilesView.

        Внешний API для FilesView:
            panel = HistoryPanel(settings, parent_view=files_view)
            panel.set_file(Path(...))  # при _open
            panel.set_file(None)       # при закрытии/удалении

        Внутри: загрузка в фоне (thread + GLib.idle_add), ListBox коммитов,
        TextView diff, кнопки восстановления/копирования хеша.
        """

        def __init__(self, settings: dict[str, Any] | None = None, parent_view: Any | None = None) -> None:
            super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["history-panel"])
            self._settings = dict(settings) if settings is not None else {}
            self._parent = parent_view
            self._current: Path | None = None
            self._commits: list[CommitInfo] = []
            self._selected: str | None = None
            self._gen: int = 0
            self._alive = True
            self.connect("destroy", self._on_destroy)
            self._build()

        # ── settings observer ─────────────────────────────────────────
        def update_settings(self, settings: dict[str, Any]) -> None:
            self._settings = dict(settings) if settings is not None else {}

        @property
        def vault_root(self) -> Path:
            return Path(str(self._settings.get("vault_root", "")))

        def _on_destroy(self, _w: Any) -> None:
            self._alive = False
            self._gen += 1

        # ── построение UI ─────────────────────────────────────────────
        def _build(self) -> None:
            # Заголовок
            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            header.append(Gtk.Label(label="История", halign=Gtk.Align.START, css_classes=["history-title"]))
            self._count_label = Gtk.Label(label="", css_classes=["dim-hint"])
            header.append(self._count_label)
            header.set_hexpand(True)
            # кнопки заголовка
            self._refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Обновить git log", css_classes=["flat", "history-refresh"])
            self._refresh_btn.connect("clicked", lambda *_: self.refresh())
            header.append(self._refresh_btn)
            self._copy_btn = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Копировать хеш выбранного коммита", css_classes=["flat"])
            self._copy_btn.set_sensitive(False)
            self._copy_btn.connect("clicked", self._on_copy_hash)
            header.append(self._copy_btn)
            self.append(header)

            # Подсказка если не репо / нет файла
            self._hint = Gtk.Label(label="Открой заметку для просмотра истории git", css_classes=["dim-hint", "history-hint"], halign=Gtk.Align.START, xalign=0, wrap=True)
            self._hint.set_visible(True)
            self.append(self._hint)

            # Список коммитов — ListBox в ScrolledWindow (виртуализация не нужна: 50 коммитов)
            self._list = Gtk.ListBox(css_classes=["history-list"], selection_mode=Gtk.SelectionMode.SINGLE)
            self._list.connect("row-activated", self._on_row_activated)
            # также слушать selection changed через row-selected?
            self._list.connect("row-selected", self._on_row_selected)
            scroller = Gtk.ScrolledWindow(vexpand=False, css_classes=["history-scroller"])
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_child(self._list)
            scroller.set_min_content_height(160)
            scroller.set_max_content_height(260)
            try:
                scroller.set_propagate_natural_height(False)
            except AttributeError:
                pass
            self._list_scroller = scroller
            self._list_scroller.set_visible(False)
            self.append(self._list_scroller)

            # Diff — моноширинный TextView в ScrolledWindow
            self._diff_buf = Gtk.TextBuffer()
            self._diff_view = Gtk.TextView(
                buffer=self._diff_buf,
                editable=False,
                cursor_visible=False,
                wrap_mode=Gtk.WrapMode.NONE,
                monospace=True,
                css_classes=["history-diff"],
                left_margin=8, right_margin=8, top_margin=6, bottom_margin=6,
            )
            # теги для подсветки diff
            try:
                tbl = self._diff_buf.get_tag_table()
                tbl.add(Gtk.TextTag.new("diff_add"))
                tbl.lookup("diff_add").set_property("foreground", "#78d296")
                tbl.add(Gtk.TextTag.new("diff_del"))
                tbl.lookup("diff_del").set_property("foreground", "#e06c75")
                tbl.add(Gtk.TextTag.new("diff_hunk"))
                tbl.lookup("diff_hunk").set_property("foreground", "#8ab4ff")
                tbl.add(Gtk.TextTag.new("diff_meta"))
                tbl.lookup("diff_meta").set_property("foreground", "#8c98ac")
            except Exception:
                try:
                    self._diff_buf.create_tag("diff_add", foreground="#78d296")
                    self._diff_buf.create_tag("diff_del", foreground="#e06c75")
                    self._diff_buf.create_tag("diff_hunk", foreground="#8ab4ff")
                    self._diff_buf.create_tag("diff_meta", foreground="#8c98ac")
                except Exception:
                    pass
            diff_scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True, css_classes=["history-diff-scroller"])
            diff_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            diff_scroller.set_child(self._diff_view)
            diff_scroller.set_min_content_height(120)
            try:
                diff_scroller.set_propagate_natural_height(False)
            except AttributeError:
                pass
            self._diff_scroller = diff_scroller
            self._diff_scroller.set_visible(False)
            self.append(self._diff_scroller)

            # Нижняя строка действий
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            self._restore_btn = Gtk.Button(label="Восстановить эту версию", css_classes=["suggested-action"], tooltip_text="Перезаписать файл содержимым выбранного коммита (без авто-коммита)")
            self._restore_btn.set_sensitive(False)
            self._restore_btn.connect("clicked", self._on_restore)
            actions.append(self._restore_btn)
            self._diff_btn = Gtk.Button(label="Показать diff", css_classes=["flat"], tooltip_text="Загрузить diff выбранного коммита")
            self._diff_btn.set_sensitive(False)
            self._diff_btn.connect("clicked", self._on_show_diff)
            actions.append(self._diff_btn)
            self._open_btn = Gtk.Button(label="Открыть версию", css_classes=["flat"], tooltip_text="Показать содержимое файла на выбранном коммите в диалоге")
            self._open_btn.set_sensitive(False)
            self._open_btn.connect("clicked", self._on_open_version)
            actions.append(self._open_btn)
            self._actions = actions
            self._actions.set_visible(False)
            self.append(self._actions)

            # revealer для сворачивания (управляется FilesView._history_toggle)
            # Панель сама не сворачивается — FilesView держит внешний Revealer.
            # Но для совместимости предоставляем set_revealed.
            self._revealed = True

        # ── публичный API для FilesView ─────────────────────────────────
        def set_file(self, path: Path | str | None) -> None:
            """Установить текущий файл и перезагрузить историю."""
            if path is None:
                self._current = None
                self._commits = []
                self._selected = None
                self._render_empty("Открой заметку для просмотра истории git")
                return
            p = Path(path)
            # если путь вне vault — всё равно пробуем, но hint покажет
            self._current = p
            self.refresh()

        def refresh(self) -> None:
            """Перечитать git log для текущего файла в фоне."""
            if self._current is None:
                self._render_empty("Открой заметку для просмотра истории git")
                return
            root = self.vault_root
            if not str(root).strip() or not root.is_dir():
                self._render_empty("Vault не найден — проверь настройки")
                return
            if not is_git_repo(root):
                self._render_empty("Vault не является git-репозиторием — инициализируй `git init` для версионирования")
                return
            # показать загрузку
            self._count_label.set_text("загружаю…")
            self._hint.set_text("загружаю историю…")
            self._hint.set_visible(True)
            self._list_scroller.set_visible(False)
            self._diff_scroller.set_visible(False)
            self._actions.set_visible(False)
            self._clear_list()
            self._gen += 1
            gen = self._gen
            cur = self._current
            settings_copy = dict(self._settings)

            def work() -> None:
                commits = get_commits_for_file(cur, Path(str(settings_copy.get("vault_root", ""))), limit=50)
                GLib.idle_add(lambda: self._on_commits_loaded(gen, commits))

            threading.Thread(target=work, daemon=True).start()

        def set_revealed(self, revealed: bool) -> None:
            self._revealed = bool(revealed)
            self.set_visible(self._revealed)

        # ── внутренние ──────────────────────────────────────────────────
        def _render_empty(self, hint: str) -> None:
            self._count_label.set_text("")
            self._hint.set_text(hint)
            self._hint.set_visible(True)
            self._list_scroller.set_visible(False)
            self._diff_scroller.set_visible(False)
            self._actions.set_visible(False)
            self._clear_list()
            self._diff_buf.set_text("")
            for btn in (getattr(self, "_restore_btn", None), getattr(self, "_diff_btn", None), getattr(self, "_open_btn", None), getattr(self, "_copy_btn", None)):
                if btn is not None:
                    try:
                        btn.set_sensitive(False)
                    except Exception:
                        pass

        def _clear_list(self) -> None:
            while (child := self._list.get_first_child()) is not None:
                self._list.remove(child)

        def _on_commits_loaded(self, gen: int, commits: list[CommitInfo]) -> bool:
            if gen != self._gen or not self._alive:
                return False
            self._commits = commits
            self._clear_list()
            if not commits:
                # проверить: файл неотслеживаемый?
                rel = _rel_path(self._current, self.vault_root) if self._current is not None else None
                hint = "Нет коммитов для этого файла — файл ещё не закоммичен или вне git"
                if rel is not None:
                    hint += f" ({rel})"
                self._render_empty(hint)
                # показать пустой список с подсказкой? оставляем hint
                return False
            # заполнить ListBox
            for c in commits:
                row = Gtk.ListBoxRow(css_classes=["history-row"])
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["history-row-box"])
                box.set_margin_top(4)
                box.set_margin_bottom(4)
                box.set_margin_start(6)
                box.set_margin_end(6)
                # левая колонка: hash + subject
                text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
                title = Gtk.Label(label=c.title(), halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["history-subject"])
                # усечь subject до 120 символов
                if len(c.subject) > 120:
                    title.set_tooltip_text(c.subject)
                text_box.append(title)
                sub = Gtk.Label(label=c.subtitle(), halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["dim-hint", "history-meta"])
                sub.set_tooltip_text(c.subtitle())
                text_box.append(sub)
                box.append(text_box)
                # справа — короткая дата (если есть время, показать только дату)
                # date уже iso: "2026-09-03 14:22:10 +0300" — оставим как есть
                row.set_child(box)
                row._commit_hash = c.hash  # type: ignore[attr-defined]
                row._commit_info = c  # type: ignore[attr-defined]
                self._list.append(row)
            self._hint.set_visible(False)
            self._list_scroller.set_visible(True)
            self._diff_scroller.set_visible(False)
            self._actions.set_visible(True)
            # автовыбор первого (самый свежий) — но без авто-diff чтобы не дергать git лишний раз
            # подсветку делаем, diff подгрузим по клику
            self._count_label.set_text(f"· {len(commits)} коммитов")
            # выбрать первую строку
            try:
                first = self._list.get_row_at_index(0)
                if first is not None:
                    self._list.select_row(first)
                    self._selected = getattr(first, "_commit_hash", None)
                    self._update_action_sensitivity()
            except Exception:
                pass
            return False

        def _on_row_selected(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
            if row is None:
                self._selected = None
                self._update_action_sensitivity()
                return
            h = getattr(row, "_commit_hash", None)
            if h != self._selected:
                self._selected = h
                self._update_action_sensitivity()

        def _on_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
            h = getattr(row, "_commit_hash", None)
            if h is None:
                return
            self._selected = h
            self._update_action_sensitivity()
            self._load_diff(h)

        def _update_action_sensitivity(self) -> None:
            has = self._selected is not None and self._current is not None
            for btn in (self._restore_btn, self._diff_btn, self._open_btn, self._copy_btn):
                try:
                    btn.set_sensitive(bool(has))
                except Exception:
                    pass

        def _on_show_diff(self, _btn: Any) -> None:
            if self._selected is None:
                return
            self._load_diff(self._selected)

        def _load_diff(self, commit_hash: str) -> None:
            if self._current is None:
                return
            self._diff_buf.set_text("загружаю diff…")
            self._diff_scroller.set_visible(True)
            cur = self._current
            root = self.vault_root
            gen = self._gen

            def work() -> None:
                txt = get_diff(cur, commit_hash, root)
                GLib.idle_add(lambda: self._on_diff_loaded(gen, commit_hash, txt))

            threading.Thread(target=work, daemon=True).start()

        def _on_diff_loaded(self, gen: int, commit_hash: str, text: str) -> bool:
            if gen != self._gen or not self._alive:
                return False
            if commit_hash != self._selected:
                # если пользователь переключился — не перезаписывать
                return False
            if not text.strip():
                text = "(пустой diff — возможно первый коммит или бинарный файл)"
            self._set_diff_text(text)
            return False

        def _set_diff_text(self, text: str) -> None:
            self._diff_buf.set_text(text)
            # подсветка по префиксам
            try:
                # снять старые теги? set_text уже сбрасывает
                lines = text.splitlines()
                offset = 0
                for line in lines:
                    tag = None
                    if line.startswith("+") and not line.startswith("+++"):
                        tag = "diff_add"
                    elif line.startswith("-") and not line.startswith("---"):
                        tag = "diff_del"
                    elif line.startswith("@@"):
                        tag = "diff_hunk"
                    elif line.startswith("diff --git") or line.startswith("index ") or line.startswith("+++") or line.startswith("---"):
                        tag = "diff_meta"
                    if tag is not None:
                        try:
                            s = self._diff_buf.get_iter_at_offset(offset)
                            e = self._diff_buf.get_iter_at_offset(offset + len(line))
                            self._diff_buf.apply_tag_by_name(tag, s, e)
                        except Exception:
                            pass
                    offset += len(line) + 1
            except Exception:
                pass
            self._diff_scroller.set_visible(True)

        def _on_copy_hash(self, _btn: Any) -> None:
            if self._selected is None:
                return
            try:
                display = Gdk.Display.get_default()
                if display is not None:
                    clipboard = display.get_clipboard()
                    clipboard.set(self._selected)
                    # toast
                    root = self.get_root()
                    overlay = getattr(root, "toast_overlay", None) if root is not None else None
                    if overlay is not None:
                        toast = Adw.Toast.new(f"хеш {self._selected[:7]} скопирован")
                        toast.set_timeout(2)
                        overlay.add_toast(toast)
            except Exception:
                pass

        def _on_open_version(self, _btn: Any) -> None:
            if self._current is None or self._selected is None:
                return
            cur = self._current
            h = self._selected
            root = self.vault_root

            def work() -> None:
                txt = get_file_content_at_commit(cur, h, root)
                GLib.idle_add(lambda: self._show_version_dialog(h, txt))

            threading.Thread(target=work, daemon=True).start()

        def _show_version_dialog(self, commit_hash: str, content: str | None) -> bool:
            if content is None:
                content = "(не удалось получить содержимое — файл удалён или бинарный)"
            # диалог предпросмотра версии
            info = next((c for c in self._commits if c.hash == commit_hash), None)
            title = f"Версия {commit_hash[:7]}"
            if info is not None:
                title += f" · {info.subject[:40]}"
            dlg = Adw.Dialog(title=title)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            box.set_margin_top(12)
            box.set_margin_bottom(12)
            box.set_margin_start(12)
            box.set_margin_end(12)
            box.set_size_request(720, 520)
            header = Gtk.Label(label=f"{commit_hash} · {info.subtitle() if info else ''}", halign=Gtk.Align.START, css_classes=["dim-hint"], wrap=True, xalign=0)
            box.append(header)
            buf = Gtk.TextBuffer()
            buf.set_text(content)
            tv = Gtk.TextView(buffer=buf, editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD, css_classes=["history-diff"])
            tv.set_left_margin(8)
            tv.set_right_margin(8)
            sc = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
            sc.set_child(tv)
            box.append(sc)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
            close = Gtk.Button(label="Закрыть")
            close.connect("clicked", lambda *_: dlg.close())
            restore = Gtk.Button(label="Восстановить эту версию", css_classes=["suggested-action"])
            def _do_restore(*_a: Any) -> None:
                dlg.close()
                self._on_restore(None)
            restore.connect("clicked", _do_restore)
            row.append(close)
            row.append(restore)
            box.append(row)
            dlg.set_child(box)
            try:
                dlg.present(self.get_root())
            except Exception:
                try:
                    dlg.present(None)
                except Exception:
                    pass
            return False

        def _on_restore(self, _btn: Any) -> None:
            if self._current is None or self._selected is None:
                return
            cur = self._current
            h = self._selected
            info = next((c for c in self._commits if c.hash == h), None)
            subject = info.subject if info is not None else h[:7]
            dlg = Adw.AlertDialog(heading="Восстановить версию?", body=f"«{cur.name}» будет перезаписан содержимым коммита {h[:7]} · {subject}\n\nТекущие несохранённые правки будут потеряны. Сделай коммит перед восстановлением если нужно сохранить.")
            dlg.add_response("cancel", "Отмена")
            dlg.add_response("restore", "Восстановить")
            dlg.set_response_appearance("restore", Adw.ResponseAppearance.SUGGESTED)
            dlg.set_default_response("cancel")
            dlg.set_close_response("cancel")

            def _resp(_d: Any, resp: str) -> None:
                if resp != "restore":
                    return
                root = self.vault_root
                # выполнить восстановление в фоне
                def work() -> None:
                    ok, msg = restore_file_at_commit(cur, h, root)
                    GLib.idle_add(lambda: self._on_restore_done(ok, msg, cur))

                threading.Thread(target=work, daemon=True).start()

            dlg.connect("response", _resp)
            try:
                dlg.present(self.get_root())
            except Exception:
                try:
                    dlg.present(None)
                except Exception:
                    pass

        def _on_restore_done(self, ok: bool, msg: str, path: Path) -> bool:
            # toast + попросить FilesView перечитать файл
            try:
                root = self.get_root()
                overlay = getattr(root, "toast_overlay", None) if root is not None else None
                if overlay is not None:
                    toast = Adw.Toast.new(msg if ok else f"ошибка: {msg}")
                    toast.set_timeout(4)
                    overlay.add_toast(toast)
            except Exception:
                pass
            if ok and self._parent is not None:
                # перечитать файл в редакторе
                try:
                    if hasattr(self._parent, "_read_cache"):
                        self._parent._read_cache.pop(str(path), None)
                    if hasattr(self._parent, "_open"):
                        self._parent._open(path)
                    if hasattr(self._parent, "reload"):
                        # обновить линки/теги? _open уже делает, но дерево может обновить
                        pass
                except Exception:
                    pass
                # обновить историю после восстановления (новый файл не закоммичен, но лог прежний)
                try:
                    self.refresh()
                except Exception:
                    pass
            # также обновить file_label если есть parent
            try:
                if self._parent is not None and hasattr(self._parent, "file_label"):
                    self._parent.file_label.set_text(msg if ok else f"ошибка восстановления: {msg}")
            except Exception:
                pass
            return False

else:  # headless fallback — заглушка для py_compile без GTK

    class HistoryPanel:  # type: ignore[no-redef]
        def __init__(self, *a: Any, **kw: Any) -> None:
            raise RuntimeError("GTK недоступен — HistoryPanel требует gi.repository.Gtk")

__all__ = [
    "CommitInfo",
    "HistoryPanel",
    "get_commits_for_file",
    "get_diff",
    "get_file_content_at_commit",
    "restore_file_at_commit",
    "is_git_repo",
]
