"""Презентация из markdown для FragileNotes.

Каждый H1/H2 — отдельный слайд, навигация стрелками, fullscreen.
Парсинг markdown на секции по заголовкам. Интеграция как вкладка.

Публичный API:
- Slide — датакласс секции
- parse_slides(text) -> list[Slide] — чистый парсер без GTK
- split_markdown_to_slides / get_slides — алиасы
- SlidesView — Gtk.Box вкладка
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .widgets import empty_state, view_header  # noqa: E402

# ── Модель слайда ─────────────────────────────────────────────────

@dataclass(slots=True)
class Slide:
    """Секция презентации (один слайд)."""
    index: int
    level: int  # 1=H1, 2=H2, 0=интро без заголовка
    title: str
    raw: str  # markdown исходник секции включая заголовок
    body: str  # markdown без заголовка (для рендера)
    line: int = 0  # номер строки заголовка (0-index), -1 для интро

    @property
    def is_title_slide(self) -> bool:
        return self.level == 1

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "level": self.level,
            "title": self.title,
            "raw": self.raw,
            "body": self.body,
            "line": self.line,
        }


# ── Парсер ────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,2})\s+(.*\S)\s*$")
_FENCE_RE = re.compile(r"^\s*```")
_FRONT_DELIM = re.compile(r"^---\s*$")


def parse_slides(text: str) -> list[Slide]:
    """Разбить markdown на слайды по H1/H2.

    Каждый H1/H2 начинает новый слайд. Содержимое до первого заголовка
    становится интро-слайдом (level 0) если непустое. Заголовки внутри
    ``` fences игнорируются. Frontmatter ``---`` в начале пропускается.

    Args:
        text: исходный markdown
    Returns:
        list[Slide] — в порядке следования, индексы 0..n-1
    """
    if not text or not text.strip():
        return []

    lines = text.splitlines()
    n = len(lines)

    # frontmatter: --- ... --- на старте
    start_idx = 0
    if n > 0 and lines[0].strip() == "---":
        for j in range(1, n):
            if lines[j].strip() == "---":
                start_idx = j + 1
                break
        # если не нашли закрывающий --- — считаем что frontmatter нет ( start_idx остаётся 0 )
        # но если нашли, пропускаем пустые строки после него
        while start_idx < n and not lines[start_idx].strip():
            # оставляем одну пустую? просто идём дальше — пре-буфер поймает
            break

    slides: list[Slide] = []
    in_fence = False
    current_lines: list[str] | None = None
    current_title = ""
    current_level = 0
    current_line_no = -1
    pre_lines: list[str] = []

    def _flush_current() -> None:
        nonlocal current_lines, current_title, current_level, current_line_no
        if current_lines is not None:
            raw = "\n".join(current_lines).strip("\n")
            # raw может быть только заголовок — оставляем как есть
            if raw.strip():
                # body — всё после первой строки
                if len(current_lines) > 1:
                    body = "\n".join(current_lines[1:]).strip("\n")
                else:
                    body = ""
                slides.append(Slide(
                    index=len(slides),
                    level=current_level,
                    title=current_title,
                    raw=raw,
                    body=body.strip(),
                    line=current_line_no,
                ))
            current_lines = None
            current_title = ""
            current_level = 0
            current_line_no = -1

    for idx in range(start_idx, n):
        line = lines[idx]
        # fence toggle (учитываем ```lang и ```)
        if _FENCE_RE.match(line):
            in_fence = not in_fence

        if not in_fence:
            m = _HEADING_RE.match(line)
            if m:
                # новый слайд
                _flush_current()
                # если это первый заголовок и есть pre_lines непустые — создать интро
                if not slides and pre_lines:
                    pre_raw = "\n".join(pre_lines).strip()
                    if pre_raw:
                        slides.append(Slide(
                            index=len(slides),
                            level=0,
                            title="",
                            raw=pre_raw,
                            body=pre_raw,
                            line=-1,
                        ))
                    pre_lines = []
                # старт нового
                level = len(m.group(1))
                title = m.group(2).strip()
                current_lines = [line]
                current_title = title
                current_level = level
                current_line_no = idx
                continue

        # не заголовок
        if current_lines is not None:
            current_lines.append(line)
        else:
            pre_lines.append(line)

    # хвост
    _flush_current()
    if not slides and pre_lines:
        pre_raw = "\n".join(pre_lines).strip()
        if pre_raw:
            slides.append(Slide(
                index=0,
                level=0,
                title="",
                raw=pre_raw,
                body=pre_raw,
                line=-1,
            ))

    # если весь документ без H1/H2 и с одним параграфом — уже есть 1 слайд
    # если документ имел только заголовки без тела — каждый уже добавлен
    return slides


# алиасы для совместимости
split_markdown_to_slides = parse_slides
get_slides = parse_slides
extract_slides = parse_slides
parse_slides_from_markdown = parse_slides


def parse_slides_from_file(path: str | Path) -> list[Slide]:
    """Прочитать файл и разбить на слайды (удобно для тестов)."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []
    return parse_slides(text)


# ── Вкладка презентации ───────────────────────────────────────────

class SlidesView(Gtk.Box):
    """Вкладка 'Презентация': H1/H2 → слайды, стрелки, fullscreen."""

    def __init__(self, settings: dict) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self._slides: list[Slide] = []
        self._current: int = 0
        self._current_path: Path | None = None
        self._is_fullscreen: bool = False
        self._build_ui()
        self._setup_keys()
        # попробовать загрузить демо из vault если есть
        GLib.idle_add(self._auto_load)

    # ── UI ───────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.append(view_header("🎞️", "Презентация", "Каждый H1 / H2 — отдельный слайд · ← → навигация · F11 fullscreen"))

        # toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["toolbar", "slides-toolbar"])
        toolbar.set_margin_start(14)
        toolbar.set_margin_end(14)

        open_btn = Gtk.Button(label="Открыть .md", tooltip_text="Выбрать markdown файл из vault")
        open_btn.connect("clicked", self._on_open_file)
        toolbar.append(open_btn)

        self._file_label = Gtk.Label(label="· нет файла", css_classes=["dim-hint", "slides-file"], hexpand=True, halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        toolbar.append(self._file_label)

        self._counter = Gtk.Label(label="— / —", css_classes=["dim-hint", "slides-counter"])
        toolbar.append(self._counter)

        self._prev_btn = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Предыдущий слайд (← / PageUp)")
        self._prev_btn.connect("clicked", lambda *_: self.prev_slide())
        toolbar.append(self._prev_btn)

        self._next_btn = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Следующий слайд (→ / PageDown / Пробел)")
        self._next_btn.connect("clicked", lambda *_: self.next_slide())
        toolbar.append(self._next_btn)

        self._fullscreen_btn = Gtk.Button(icon_name="view-fullscreen-symbolic", tooltip_text="На весь экран (F11)")
        self._fullscreen_btn.connect("clicked", lambda *_: self.toggle_fullscreen())
        toolbar.append(self._fullscreen_btn)

        self.append(toolbar)

        # основной контейнер слайда
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT, hexpand=True, vexpand=True)

        # empty — нет слайдов
        self._empty = empty_state(
            "🎞️", "Нет слайдов — откройте markdown",
            hint="Каждый H1 (#) и H2 (##) станет отдельным слайдом. Остальной markdown — содержимое слайда.",
            action_label="Открыть файл",
            on_action=self._on_open_file,
        )
        self._stack.add_named(self._empty, "empty")

        # slide — отображение
        slide_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, hexpand=True, vexpand=True, css_classes=["slides-slide-box"])

        # заголовок слайда
        self._slide_title = Gtk.Label(
            label="",
            css_classes=["slides-title"],
            halign=Gtk.Align.START, xalign=0,
            wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
        )
        self._slide_title.set_margin_start(18)
        self._slide_title.set_margin_end(18)
        self._slide_title.set_margin_top(12)
        title_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        title_wrap.append(self._slide_title)
        title_wrap.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL, css_classes=["slides-sep"]))
        slide_box.append(title_wrap)

        # контент слайда — MarkdownView в скролле
        try:
            from .markdown import MarkdownView  # noqa: WPS433
            self._markdown = MarkdownView()
        except Exception:
            # fallback — TextView
            self._markdown = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD, css_classes=["editor"])
            self._markdown._buf = self._markdown.get_buffer()  # type: ignore[attr-defined]

        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True, css_classes=["editor-frame", "slides-scroller"])
        scroller.set_child(self._markdown)
        # clamp для читаемости как в files_view
        clamp = Adw.Clamp(maximum_size=920, tightening_threshold=720)
        clamp.set_child(scroller)
        slide_box.append(clamp)

        # нижняя навигация (дубль для удобства)
        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["slides-bottom"])
        bottom.set_margin_start(14)
        bottom.set_margin_end(14)
        bottom.set_margin_bottom(10)
        bottom.set_margin_top(6)
        # индикатор точек / прогресс
        self._progress = Gtk.ProgressBar(show_text=False, css_classes=["slides-progress"], hexpand=True, valign=Gtk.Align.CENTER)
        bottom.append(self._progress)
        bottom_prev = Gtk.Button(label="← Назад", css_classes=["mod-neutral"])
        bottom_prev.connect("clicked", lambda *_: self.prev_slide())
        bottom.append(bottom_prev)
        bottom_next = Gtk.Button(label="Далее →", css_classes=["suggested-action", "mod-cta"])
        bottom_next.connect("clicked", lambda *_: self.next_slide())
        bottom.append(bottom_next)
        slide_box.append(bottom)

        self._stack.add_named(slide_box, "slide")
        self._stack.set_visible_child_name("empty")
        self.append(self._stack)

        # боковая панель — оглавление слайдов (опционально)
        # делаем через overlay: слева список заголовков, справа слайд
        # Для простоты — добавим ListBox снизу тулбара как outline? Пока компактно в bottom уже есть
        # Вместо этого добавим краткий outline в виде FlowBox над слайдом при >10 слайдов? Пока не нужен.

        self._sync_nav()

    def _setup_keys(self) -> None:
        key = Gtk.EventControllerKey.new()
        key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)
        # также на markdown view чтобы перехватить когда фокус там
        try:
            mk_key = Gtk.EventControllerKey.new()
            mk_key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            mk_key.connect("key-pressed", self._on_key)
            if hasattr(self, "_markdown"):
                self._markdown.add_controller(mk_key)
        except Exception:
            pass

    # ── Навигация ────────────────────────────────────────────────
    def _on_key(self, _ctrl, keyval: int, _code: int, state: Gdk.ModifierType) -> bool:
        # fullscreen toggle
        if keyval in (Gdk.KEY_F11,):
            self.toggle_fullscreen()
            return True
        if keyval == Gdk.KEY_Escape and self._is_fullscreen:
            self.toggle_fullscreen()
            return True
        # навигация только когда есть слайды
        if not self._slides:
            return False
        # стрелки, PageUp/Down, Space, Home/End
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Down, Gdk.KEY_Page_Down, Gdk.KEY_KP_Page_Down, Gdk.KEY_space):
            # с Ctrl не перехватываем? но space без модификаторов — следующий
            if keyval == Gdk.KEY_space and bool(state & Gdk.ModifierType.CONTROL_MASK):
                return False
            self.next_slide()
            return True
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Up, Gdk.KEY_Page_Up, Gdk.KEY_KP_Page_Up, Gdk.KEY_BackSpace):
            self.prev_slide()
            return True
        if keyval in (Gdk.KEY_Home, Gdk.KEY_KP_Home):
            self.go_to(0)
            return True
        if keyval in (Gdk.KEY_End, Gdk.KEY_KP_End):
            self.go_to(len(self._slides) - 1)
            return True
        return False

    def next_slide(self) -> None:
        if not self._slides:
            return
        if self._current < len(self._slides) - 1:
            self.go_to(self._current + 1)

    def prev_slide(self) -> None:
        if not self._slides:
            return
        if self._current > 0:
            self.go_to(self._current - 1)

    def go_to(self, idx: int) -> None:
        if not self._slides:
            return
        idx = max(0, min(idx, len(self._slides) - 1))
        if idx == self._current and self._stack.get_visible_child_name() == "slide":
            # уже там — всё равно обновить
            pass
        self._current = idx
        self._show_current()

    def _show_current(self) -> None:
        if not self._slides:
            self._stack.set_visible_child_name("empty")
            self._sync_nav()
            return
        slide = self._slides[self._current]
        # заголовок
        if slide.level == 0 or not slide.title:
            self._slide_title.set_visible(False)
        else:
            # уровень 1 — крупнее, 2 — поменьше
            self._slide_title.set_text(slide.title)
            self._slide_title.set_visible(True)
            # css в зависимости от уровня
            for cls in ("slides-title-h1", "slides-title-h2", "slides-title-intro"):
                self._slide_title.remove_css_class(cls)
            if slide.level == 1:
                self._slide_title.add_css_class("slides-title-h1")
            elif slide.level == 2:
                self._slide_title.add_css_class("slides-title-h2")
            else:
                self._slide_title.add_css_class("slides-title-intro")

        # тело — без первой строки заголовка если есть
        body = slide.body or ""
        # если body пустой но raw есть заголовок — показать placeholder
        if not body.strip():
            body = "*— пустой слайд —*"
        try:
            self._markdown.set_markdown(body)
        except Exception:
            try:
                buf = self._markdown.get_buffer()  # type: ignore[attr-defined]
                buf.set_text(body)
            except Exception:
                pass

        self._stack.set_visible_child_name("slide")
        self._sync_nav()
        # доступность: проговорить номер слайда
        try:
            self._slide_title.update_property(Gtk.AccessibleProperty.LABEL, f"Слайд {self._current+1} из {len(self._slides)}: {slide.title}")
        except Exception:
            pass

    def _sync_nav(self) -> None:
        n = len(self._slides)
        cur = self._current
        if n == 0:
            self._counter.set_text("— / —")
            self._prev_btn.set_sensitive(False)
            self._next_btn.set_sensitive(False)
            self._progress.set_fraction(0.0)
            self._progress.set_text("")
            return
        self._counter.set_text(f"{cur+1} / {n}")
        self._prev_btn.set_sensitive(cur > 0)
        self._next_btn.set_sensitive(cur < n - 1)
        frac = (cur + 1) / max(1, n)
        self._progress.set_fraction(frac)
        self._progress.set_text(f"{cur+1}/{n}")

    # ── Fullscreen ───────────────────────────────────────────────
    def toggle_fullscreen(self) -> None:
        win = self.get_root()
        if win is None or not hasattr(win, "fullscreen"):
            return
        try:
            if not self._is_fullscreen:
                win.fullscreen()  # type: ignore[attr-defined]
                self._is_fullscreen = True
                self._fullscreen_btn.set_icon_name("view-restore-symbolic")
                self._fullscreen_btn.set_tooltip_text("Выйти из полноэкранного (Esc / F11)")
            else:
                win.unfullscreen()  # type: ignore[attr-defined]
                self._is_fullscreen = False
                self._fullscreen_btn.set_icon_name("view-fullscreen-symbolic")
                self._fullscreen_btn.set_tooltip_text("На весь экран (F11)")
        except Exception:
            pass

    # ── Загрузка ─────────────────────────────────────────────────
    def load_markdown(self, text: str, source: str | Path | None = None) -> None:
        """Загрузить markdown строку и разбить на слайды."""
        self._slides = parse_slides(text or "")
        self._current = 0
        if source is not None:
            try:
                p = Path(source)
                self._current_path = p
                # относительный путь для лейбла
                root = Path(str(self.settings.get("vault_root") or ""))
                try:
                    rel = p.relative_to(root) if root and str(p).startswith(str(root)) else p
                    self._file_label.set_text(str(rel))
                    self._file_label.set_tooltip_text(str(p))
                except Exception:
                    self._file_label.set_text(p.name)
                    self._file_label.set_tooltip_text(str(p))
            except Exception:
                self._file_label.set_text(str(source))
        if not self._slides:
            self._file_label.set_text("· пустой документ" if source is None else str(source))
            self._stack.set_visible_child_name("empty")
            self._sync_nav()
            return
        self._show_current()

    def load_file(self, path: str | Path) -> None:
        """Открыть файл и показать как презентацию."""
        p = Path(path)
        if not p.is_file():
            self._notify(f"файл не найден: {p.name}")
            return
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:  # noqa: BLE001
                self._notify(f"ошибка чтения: {exc}")
                return
        self.load_markdown(text, source=p)

    def open_path(self, path: str | Path) -> None:
        """Алиас для интеграции извне (как в files_view.open_path)."""
        self.load_file(path)

    def reload(self) -> None:
        """Перечитать текущий файл если он менялся на диске."""
        if self._current_path is not None and self._current_path.is_file():
            cur_idx = self._current
            self.load_file(self._current_path)
            # попытаться сохранить позицию
            if 0 <= cur_idx < len(self._slides):
                self.go_to(cur_idx)

    # ── Файловый диалог ──────────────────────────────────────────
    def _on_open_file(self, *_args) -> None:
        vault_root = Path(str(self.settings.get("vault_root") or Path.home()))
        # Gtk.FileDialog (4.10+)
        try:
            dlg = Gtk.FileDialog()
            dlg.set_title("Открыть markdown для презентации")
            filt = Gio.ListStore.new(Gtk.FileFilter)
            f = Gtk.FileFilter()
            f.set_name("Markdown (*.md)")
            f.add_pattern("*.md")
            filt.append(f)
            fa = Gtk.FileFilter()
            fa.set_name("Все файлы")
            fa.add_pattern("*")
            filt.append(fa)
            dlg.set_filters(filt)
            try:
                if vault_root.is_dir():
                    dlg.set_initial_folder(Gio.File.new_for_path(str(vault_root)))
            except Exception:
                pass
            dlg.open(self.get_root(), None, self._on_open_done)
            return
        except Exception:
            pass
        self._fallback_open()

    def _on_open_done(self, dlg: Gtk.FileDialog, res) -> None:
        try:
            f = dlg.open_finish(res)
            if f is None:
                return
            path = Path(f.get_path() or "")
            if path:
                self.load_file(path)
        except Exception as exc:  # noqa: BLE001
            self._notify(f"ошибка открытия: {exc}")

    def _fallback_open(self) -> None:
        dialog = Adw.Dialog(title="Открыть файл")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
        box.set_size_request(420, -1)
        lbl = Gtk.Label(label="Путь к markdown (от vault или абсолютный):", halign=Gtk.Align.START)
        vault_root = Path(str(self.settings.get("vault_root") or Path.home()))
        entry = Gtk.Entry(placeholder_text="например: Презентации/demo.md")
        if self._current_path is not None:
            try:
                entry.set_text(str(self._current_path.relative_to(vault_root)))
            except Exception:
                entry.set_text(str(self._current_path))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Отмена")
        ok = Gtk.Button(label="Открыть", css_classes=["suggested-action"])
        row.append(cancel)
        row.append(ok)
        box.append(lbl)
        box.append(entry)
        box.append(row)
        dialog.set_child(box)
        cancel.connect("clicked", lambda *_: dialog.close())

        def _do(*_):
            raw = entry.get_text().strip()
            if not raw:
                return
            p = Path(raw)
            if not p.is_absolute():
                p = vault_root / p
            dialog.close()
            self.load_file(p)

        ok.connect("clicked", _do)
        entry.connect("activate", _do)
        dialog.present(self.get_root())

    def _auto_load(self) -> bool:
        """Если в vault есть Презентации/demo — открыть для превью, иначе пусто."""
        # не мешаем если уже загружено извне
        if self._slides:
            return False
        # пробуем найти любой md с H1/H2 в vault
        try:
            root = Path(str(self.settings.get("vault_root") or ""))
            if root.is_dir():
                # ищем презентационный файл эвристикой
                cand: Path | None = None
                # приоритет: папка Презентации / Slides / Presentation
                for name in ("Презентации", "Slides", "Presentation", "презентации", "slides"):
                    p = root / name
                    if p.is_dir():
                        for q in p.rglob("*.md"):
                            cand = q
                            break
                    if cand is not None:
                        break
                if cand is not None and cand.is_file():
                    self.load_file(cand)
        except Exception:
            pass
        return False

    def _notify(self, msg: str) -> None:
        try:
            win = self.get_root()
            if win is not None and hasattr(win, "toast_overlay"):
                toast = Adw.Toast.new(msg)
                toast.set_timeout(3)
                win.toast_overlay.add_toast(toast)  # type: ignore[attr-defined]
                return
        except Exception:
            pass
        self._file_label.set_text(msg)


__all__ = ["Slide", "SlidesView", "parse_slides", "split_markdown_to_slides", "get_slides", "extract_slides", "parse_slides_from_file"]
