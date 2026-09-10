"""PDF аннотации (highlight) для FragileNotes.

Просмотр PDF с возможностью highlight-аннотаций (выделение текста),
сохранение в ``.pdf.ann.json`` рядом с исходным PDF.

Хранение:
  ``document.pdf``  →  ``document.pdf.ann.json``

Формат ``.ann.json``:
  {
    "version": 1,
    "pdf": "document.pdf",
    "created": "2026-09-09T12:00:00",
    "highlights": [
      {
        "id": "uuid",
        "page": 0,                 # 0-based
        "rects": [[x1,y1,x2,y2], ...],  # PDF points, origin bottom-left
        "text": "выделенный текст",
        "color": "#ffeb3b",
        "created": "iso8601",
        "comment": ""
      }
    ]
  }

Рендер:
  * Попытка 1 — Poppler (``gi.repository.Poppler``) + Cairo ``DrawingArea``.
    Аннотации рисуются поверх страницы полупрозрачным цветом.
    Выделение создаётся перетаскиванием (drag) → ``Page.get_selection_region``
    → точные ``rects`` для подсветки.
  * Попытка 2 — WebKit WebView + PDF.js (если Poppler недоступен, но WebKit есть).
  * Фоллбэк — плейсхолдер с кнопкой «Открыть внешним просмотрщиком».

Интеграция:
  * как вкладка — ``PdfAnnotateView`` можно добавить в ``app.py`` VIEWS
    (``pdf_annotate``) и ``workspace.py`` / ``sidebar.py``,
  * как часть ``files_view`` — при открытии ``.pdf`` в ``FilesView._open``
    автоматически показывается диалог ``create_dialog``.

Зависимости: только ``PyGObject``, ``Poppler`` и ``cairo`` (опционально).
Без них модуль остаётся импортируемым (``py_compile`` / headless тесты).

API:
  * ``ann_path_for(pdf_path) -> Path``
  * ``is_pdf_path(path) -> bool``
  * ``has_poppler() -> bool``
  * ``has_webkit() -> bool``
  * ``load_annotations(pdf_path) -> dict``
  * ``save_annotations(pdf_path, data) -> bool``
  * ``create_highlight(page, rects, text, color, ...) -> dict``
  * ``PdfAnnotateView(settings, pdf_path=None)``
  * ``create_dialog(parent, settings, pdf_path)``
  * ``open_pdf_annotate(parent, settings, pdf_path)``
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── Константы ──────────────────────────────────────────────────────────
ANNOTATION_SUFFIX = ".ann.json"
ANNOTATION_VERSION = 1

HIGHLIGHT_COLORS: list[str] = [
    "#ffeb3b",  # жёлтый (дефолт)
    "#ff9800",  # оранжевый
    "#8bc34a",  # зелёный
    "#03a9f4",  # голубой
    "#e91e63",  # розовый
    "#9c27b0",  # фиолетовый
]

DEFAULT_COLOR = HIGHLIGHT_COLORS[0]

# WebKit / Poppler lazy flags
_POPPLER_AVAILABLE: bool | None = None
_WEBKIT_AVAILABLE: bool | None = None
_WEBKIT_VERSION: str | None = None


# ── Утилиты (без GTK, безопасны для py_compile/headless) ─────────────


def ann_path_for(pdf_path: Path | str) -> Path:
    """Путь к ``.pdf.ann.json`` рядом с PDF.

    ``document.pdf`` → ``document.pdf.ann.json``.
    Для не-pdf тоже добавляет ``.ann.json`` (универсально).
    """
    p = Path(pdf_path)
    # ``Path.with_suffix`` заменяет последний суффикс, поэтому просто конкатенируем
    # чтобы гарантированно получить ``.pdf.ann.json``.
    return Path(str(p) + ANNOTATION_SUFFIX)


def is_pdf_path(path: Path | str | None) -> bool:
    """``True`` если путь указывает на PDF (расширение ``.pdf``, case-insensitive)."""
    if not path:
        return False
    try:
        return Path(str(path)).suffix.lower() == ".pdf"
    except Exception:
        return False


def _now_iso() -> str:
    try:
        return datetime.datetime.now().isoformat(timespec="seconds")
    except Exception:
        return ""


def has_poppler() -> bool:
    """Есть ли Poppler (``gi.repository.Poppler``)."""
    global _POPPLER_AVAILABLE
    if _POPPLER_AVAILABLE is not None:
        return _POPPLER_AVAILABLE
    try:
        import gi  # type: ignore

        gi.require_version("Poppler", "0.18")
        from gi.repository import Poppler  # noqa: F401  # type: ignore

        _POPPLER_AVAILABLE = True
        return True
    except Exception as e:
        log.debug("Poppler unavailable: %s", e)
        _POPPLER_AVAILABLE = False
        return False


def has_webkit() -> bool:
    """Есть ли WebKitGTK (WebKit 6.0 / 4.1) для WebView."""
    global _WEBKIT_AVAILABLE, _WEBKIT_VERSION
    if _WEBKIT_AVAILABLE is not None:
        return _WEBKIT_AVAILABLE
    try:
        import gi  # type: ignore

        for ver in ("6.0", "4.1", "4.0"):
            try:
                gi.require_version("WebKit", ver)
                from gi.repository import WebKit  # noqa: F401  # type: ignore

                _WEBKIT_AVAILABLE = True
                _WEBKIT_VERSION = ver
                log.debug("WebKit %s available (pdf_annotate)", ver)
                return True
            except Exception:
                continue
        _WEBKIT_AVAILABLE = False
        return False
    except Exception:
        _WEBKIT_AVAILABLE = False
        return False


def get_webkit_version() -> str | None:
    has_webkit()
    return _WEBKIT_VERSION


def _hex_to_rgba(hex_color: str, alpha: float = 0.35) -> tuple[float, float, float, float]:
    """``#rrggbb`` → ``(r,g,b,a)`` 0..1."""
    h = (hex_color or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        h = DEFAULT_COLOR.lstrip("#")
    try:
        r = int(h[0:2], 16) / 255.0
        g = int(h[2:4], 16) / 255.0
        b = int(h[4:6], 16) / 255.0
        return (r, g, b, float(alpha))
    except Exception:
        return (1.0, 0.92, 0.23, float(alpha))


def _sanitize_color(color: str | None) -> str:
    if not color:
        return DEFAULT_COLOR
    c = str(color).strip().lower()
    if c in HIGHLIGHT_COLORS:
        return c
    # допускаем любой #hex
    if c.startswith("#") and len(c) in (4, 7):
        try:
            # валидация hex
            int(c[1:], 16)
            return c
        except Exception:
            pass
    return DEFAULT_COLOR


def create_highlight(
    page: int,
    rects: list[list[float]],
    text: str = "",
    color: str | None = None,
    comment: str = "",
    highlight_id: str | None = None,
) -> dict[str, Any]:
    """Создать словарь highlight-аннотации (валидированный)."""
    try:
        pg = int(page)
    except Exception:
        pg = 0
    pg = max(0, pg)
    # rects: list of [x1,y1,x2,y2] in PDF points, bottom-left origin
    clean_rects: list[list[float]] = []
    for r in rects or []:
        try:
            if not isinstance(r, (list, tuple)) or len(r) != 4:
                continue
            x1, y1, x2, y2 = float(r[0]), float(r[1]), float(r[2]), float(r[3])
            # нормализация: x1<x2, y1<y2
            if x1 > x2:
                x1, x2 = x2, x1
            if y1 > y2:
                y1, y2 = y2, y1
            # игнор вырожденных
            if abs(x2 - x1) < 0.5 or abs(y2 - y1) < 0.5:
                continue
            clean_rects.append([x1, y1, x2, y2])
        except Exception:
            continue
    if not clean_rects:
        # fallback — пустой прямоугольник не создаём, но вернём с одной заглушкой?
        # лучше оставить пустым — вызовет пропуск рендера
        clean_rects = []
    return {
        "id": highlight_id or uuid.uuid4().hex[:12],
        "page": pg,
        "rects": clean_rects,
        "text": str(text or "").strip()[:2000],
        "color": _sanitize_color(color),
        "created": _now_iso(),
        "comment": str(comment or "").strip()[:2000],
    }


def load_annotations(pdf_path: Path | str) -> dict[str, Any]:
    """Загрузить ``.pdf.ann.json`` рядом с PDF.

    Возвращает нормализованный dict с ключами ``version``, ``pdf``, ``highlights``.
    Если файла нет — возвращает пустую структуру (не кидает).
    """
    p = Path(pdf_path)
    ann = ann_path_for(p)
    base: dict[str, Any] = {
        "version": ANNOTATION_VERSION,
        "pdf": p.name,
        "created": _now_iso(),
        "highlights": [],
    }
    if not ann.is_file():
        return base
    try:
        raw = ann.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return base
        # версия
        ver = data.get("version", ANNOTATION_VERSION)
        try:
            ver = int(ver)
        except Exception:
            ver = ANNOTATION_VERSION
        highlights = data.get("highlights")
        if not isinstance(highlights, list):
            highlights = []
        # валидация каждого
        clean: list[dict[str, Any]] = []
        for h in highlights:
            if not isinstance(h, dict):
                continue
            try:
                page = int(h.get("page", 0))
            except Exception:
                continue
            rects = h.get("rects")
            if not isinstance(rects, list):
                continue
            text = str(h.get("text", ""))[:2000]
            color = _sanitize_color(h.get("color"))
            hid = str(h.get("id", uuid.uuid4().hex[:12]))[:32]
            created = str(h.get("created", _now_iso()))
            comment = str(h.get("comment", ""))[:2000]
            clean.append(
                {
                    "id": hid,
                    "page": max(0, page),
                    "rects": rects,
                    "text": text,
                    "color": color,
                    "created": created,
                    "comment": comment,
                }
            )
        return {
            "version": ver,
            "pdf": str(data.get("pdf", p.name)),
            "created": str(data.get("created", base["created"])),
            "highlights": clean,
        }
    except Exception as e:
        log.debug("load_annotations failed for %s: %s", ann, e)
        return base


def save_annotations(pdf_path: Path | str, data: dict[str, Any]) -> bool:
    """Сохранить аннотации в ``.pdf.ann.json`` рядом с PDF.

    ``data`` — dict с ``highlights`` (list). Автоматически дополняет ``version``/``pdf``.
    Возвращает ``True`` при успехе.
    """
    p = Path(pdf_path)
    ann = ann_path_for(p)
    try:
        # нормализация
        highlights = data.get("highlights") if isinstance(data, dict) else []
        if not isinstance(highlights, list):
            highlights = []
        clean: list[dict[str, Any]] = []
        for h in highlights:
            if not isinstance(h, dict):
                continue
            # пропускаем пустые rects — но сохраняем чтобы не терять
            clean.append(
                {
                    "id": str(h.get("id", uuid.uuid4().hex[:12])),
                    "page": int(h.get("page", 0)),
                    "rects": list(h.get("rects", [])),
                    "text": str(h.get("text", ""))[:2000],
                    "color": _sanitize_color(h.get("color")),
                    "created": str(h.get("created", _now_iso())),
                    "comment": str(h.get("comment", ""))[:2000],
                }
            )
        out: dict[str, Any] = {
            "version": ANNOTATION_VERSION,
            "pdf": p.name,
            "created": str(data.get("created", _now_iso()))
            if isinstance(data, dict)
            else _now_iso(),
            "highlights": clean,
        }
        # если есть поле pdf в data — сохраним, но приоритет у реального имени
        try:
            ann.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        ann.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        log.debug("save_annotations failed for %s: %s", ann, e)
        return False


def add_highlight_to_file(
    pdf_path: Path | str,
    page: int,
    rects: list[list[float]],
    text: str = "",
    color: str | None = None,
    comment: str = "",
) -> dict[str, Any] | None:
    """Удобный хелпер: загрузка → добавление → сохранение. Возвращает созданный highlight или None."""
    try:
        data = load_annotations(pdf_path)
        hl = create_highlight(page, rects, text=text, color=color, comment=comment)
        if not hl.get("rects"):
            # без rects не сохраняем
            return None
        data.setdefault("highlights", []).append(hl)
        ok = save_annotations(pdf_path, data)
        return hl if ok else None
    except Exception as e:
        log.debug("add_highlight_to_file failed: %s", e)
        return None


def remove_highlight_from_file(pdf_path: Path | str, highlight_id: str) -> bool:
    """Удалить highlight по id из ``.ann.json``. Возвращает True если удалён."""
    try:
        data = load_annotations(pdf_path)
        before = len(data.get("highlights", []))
        data["highlights"] = [
            h for h in data.get("highlights", []) if str(h.get("id")) != str(highlight_id)
        ]
        if len(data["highlights"]) == before:
            return False
        return bool(save_annotations(pdf_path, data))
    except Exception as e:
        log.debug("remove_highlight_from_file failed: %s", e)
        return False


# ── GTK / Poppler UI (лениво, headless-safe) ─────────────────────────
try:
    import gi  # type: ignore

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

    _GTK_AVAILABLE = True
except Exception as _e:  # pragma: no cover
    _GTK_AVAILABLE = False
    Adw = Gio = GLib = Gtk = Gdk = Pango = None  # type: ignore
    log.debug("pdf_annotate: GTK unavailable: %s", _e)

# Poppler уже проверен функцией has_poppler(), но для UI держим флаг
_POPPLER_IMPORT_OK = has_poppler()
_WEBKIT_IMPORT_OK = has_webkit()

if _GTK_AVAILABLE:
    # ── View ───────────────────────────────────────────────────────
    class PdfAnnotateView(Gtk.Box):  # type: ignore[misc]
        """Просмотр PDF с highlight-аннотациями (Poppler/Cairo, fallback WebView).

        * ``settings`` — dict настроек (vault_root и т.п.), используется для резолва путей.
        * ``pdf_path`` — начальный PDF (опционально).
        """

        def __init__(
            self, settings: dict | None = None, pdf_path: Path | str | None = None
        ) -> None:
            super().__init__(
                orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True, vexpand=True
            )
            self.settings = dict(settings or {})
            self._pdf_path: Path | None = Path(pdf_path) if pdf_path else None
            self._doc: Any | None = None
            self._n_pages: int = 0
            self._zoom: float = 1.35
            self._highlights: list[dict[str, Any]] = []
            self._selected_id: str | None = None
            self._page_sizes: list[tuple[float, float]] = []
            self._alive = True
            self._color: str = DEFAULT_COLOR
            self._page_widgets: dict[int, Gtk.Widget] = {}
            self._draw_areas: dict[int, Gtk.DrawingArea] = {}
            self._selection: dict[int, Any] = {}  # page -> current drag pdf_rect
            self._ann_data: dict[str, Any] = {}
            self.connect("destroy", self._on_destroy)
            self._build_ui()
            if self._pdf_path is not None and self._pdf_path.is_file():
                self.load_pdf(self._pdf_path)

        # ── lifecycle ─────────────────────────────────────────────
        def _on_destroy(self, *_a) -> None:
            self._alive = False

        # ── UI build ──────────────────────────────────────────────
        def _build_ui(self) -> None:
            from .widgets import view_header  # local import to avoid cycle

            self.append(
                view_header(
                    "📄",
                    "PDF Аннотации",
                    "Просмотр PDF + highlight выделения → .pdf.ann.json рядом",
                )
            )

            toolbar = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["toolbar"]
            )
            toolbar.set_margin_start(14)
            toolbar.set_margin_end(14)

            open_btn = Gtk.Button(
                icon_name="document-open-symbolic", tooltip_text="Открыть PDF", css_classes=["flat"]
            )
            open_btn.connect("clicked", self._on_open_clicked)
            toolbar.append(open_btn)

            # зум
            toolbar.append(Gtk.Label(label="Масштаб:", css_classes=["dim-hint"]))
            minus = Gtk.Button(icon_name="zoom-out-symbolic", css_classes=["flat"])
            minus.connect("clicked", lambda *_: self._set_zoom(self._zoom - 0.15))
            toolbar.append(minus)
            self._zoom_label = Gtk.Label(
                label=f"{int(self._zoom * 100)}%", css_classes=["dim-hint"]
            )
            toolbar.append(self._zoom_label)
            plus = Gtk.Button(icon_name="zoom-in-symbolic", css_classes=["flat"])
            plus.connect("clicked", lambda *_: self._set_zoom(self._zoom + 0.15))
            toolbar.append(plus)
            fit_btn = Gtk.Button(label="Вписать", css_classes=["flat"])
            fit_btn.connect("clicked", lambda *_: self._set_zoom(1.35))
            toolbar.append(fit_btn)

            # цвет
            toolbar.append(Gtk.Label(label="Цвет:", css_classes=["dim-hint"]))
            self._color_drop = Gtk.DropDown.new_from_strings(HIGHLIGHT_COLORS)
            try:
                self._color_drop.set_selected(HIGHLIGHT_COLORS.index(self._color))
            except ValueError:
                self._color_drop.set_selected(0)
            self._color_drop.connect("notify::selected", self._on_color_changed)
            toolbar.append(self._color_drop)

            # действия
            self._hl_btn = Gtk.Button(
                label="✏️ Выделить",
                css_classes=["suggested-action"],
                tooltip_text="Создать highlight из текущего выделения (drag по странице)",
            )
            self._hl_btn.connect("clicked", self._on_highlight_clicked)
            self._hl_btn.set_sensitive(False)
            toolbar.append(self._hl_btn)

            self._del_btn = Gtk.Button(
                icon_name="user-trash-symbolic",
                css_classes=["flat", "destructive-action"],
                tooltip_text="Удалить выбранный highlight",
            )
            self._del_btn.connect("clicked", self._on_delete_clicked)
            self._del_btn.set_sensitive(False)
            toolbar.append(self._del_btn)

            save_btn = Gtk.Button(
                icon_name="document-save-symbolic",
                tooltip_text="Сохранить .ann.json",
                css_classes=["flat"],
            )
            save_btn.connect("clicked", lambda *_: self._save())
            toolbar.append(save_btn)

            self._status = Gtk.Label(
                label="",
                css_classes=["dim-hint"],
                hexpand=True,
                halign=Gtk.Align.END,
                xalign=1,
                ellipsize=Pango.EllipsizeMode.MIDDLE,
            )
            toolbar.append(self._status)

            self.append(toolbar)

            # Основная зона: Paned (слева PDF, справа список)
            paned = Gtk.Paned(
                orientation=Gtk.Orientation.HORIZONTAL,
                hexpand=True,
                vexpand=True,
                css_classes=["pdf-paned"],
            )
            paned.set_margin_start(14)
            paned.set_margin_end(14)
            paned.set_margin_bottom(14)
            paned.set_shrink_start_child(False)
            paned.set_shrink_end_child(False)
            try:
                paned.set_wide_handle(True)
            except Exception:
                pass

            # Левая: скролл со страницами
            self._scroll = Gtk.ScrolledWindow(
                hexpand=True, vexpand=True, css_classes=["editor-frame", "pdf-scroll"]
            )
            self._scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            self._pages_box = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=16,
                hexpand=True,
                vexpand=True,
                css_classes=["pdf-pages"],
            )
            self._pages_box.set_margin_top(12)
            self._pages_box.set_margin_bottom(12)
            self._pages_box.set_margin_start(12)
            self._pages_box.set_margin_end(12)
            self._scroll.set_child(self._pages_box)

            # Placeholder для отсутствия PDF
            self._placeholder = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["empty"]
            )
            self._placeholder.set_halign(Gtk.Align.CENTER)
            self._placeholder.set_valign(Gtk.Align.CENTER)
            self._placeholder.append(Gtk.Label(label="📄", css_classes=["empty-icon"]))
            self._placeholder.append(
                Gtk.Label(label="Нет открытого PDF", css_classes=["empty-text"])
            )
            self._placeholder.append(
                Gtk.Label(
                    label="Открой PDF через «Открыть» или выбери .pdf в Заметках — сохранит highlight в .pdf.ann.json рядом",
                    css_classes=["dim-hint", "empty-hint"],
                    wrap=True,
                    justify=Gtk.Justification.CENTER,
                )
            )
            open_ph = Gtk.Button(label="Открыть PDF", css_classes=["suggested-action"])
            open_ph.connect("clicked", self._on_open_clicked)
            open_ph.set_halign(Gtk.Align.CENTER)
            self._placeholder.append(open_ph)
            self._pages_box.append(self._placeholder)

            # WebView контейнер (если Poppler нет)
            self._web_container = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True
            )

            left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
            left_box.append(self._scroll)
            self._left_stack = Gtk.Stack(
                transition_type=Gtk.StackTransitionType.CROSSFADE, hexpand=True, vexpand=True
            )
            self._left_stack.add_named(left_box, "poppler")
            self._left_stack.add_named(self._web_container, "webview")
            # placeholder отдельно через visible
            paned.set_start_child(self._left_stack)

            # Правая: список аннотаций
            right = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["pdf-sidebar"]
            )
            right.set_size_request(320, -1)
            hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            hdr.append(
                Gtk.Label(
                    label="Выделения",
                    css_classes=["dim-hint", "pdf-header"],
                    halign=Gtk.Align.START,
                )
            )
            self._count_label = Gtk.Label(label="", css_classes=["dim-hint"])
            hdr.append(self._count_label)
            clear_btn = Gtk.Button(
                icon_name="edit-clear-symbolic", tooltip_text="Удалить всё", css_classes=["flat"]
            )
            clear_btn.connect("clicked", self._on_clear_all)
            hdr.append(clear_btn)
            right.append(hdr)

            self._list_scroller = Gtk.ScrolledWindow(
                vexpand=True, css_classes=["pdf-list-scroller"]
            )
            self._list_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self._list_box = Gtk.ListBox(css_classes=["pdf-list"])
            self._list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
            self._list_box.connect("row-selected", self._on_list_row_selected)
            self._list_box.connect("row-activated", self._on_list_row_activated)
            self._list_scroller.set_child(self._list_box)
            right.append(self._list_scroller)

            self._list_empty = Gtk.Label(
                label="— нет выделений —\nПеретащи мышью по тексту страницы → «Выделить»",
                css_classes=["dim-hint"],
                wrap=True,
                halign=Gtk.Align.START,
                xalign=0,
            )
            right.append(self._list_empty)

            # Информация
            self._info_label = Gtk.Label(
                label="", css_classes=["dim-hint"], wrap=True, halign=Gtk.Align.START, xalign=0
            )
            right.append(self._info_label)

            paned.set_end_child(right)
            try:
                paned.set_position(760)
            except Exception:
                pass
            self._paned = paned
            self.append(paned)
            self._update_list()

        # ── PDF loading ───────────────────────────────────────────
        def load_pdf(self, pdf_path: Path | str) -> bool:
            """Загрузить PDF по пути. Возвращает True при успехе."""
            p = Path(pdf_path)
            if not p.is_file():
                self._set_status(f"файл не найден: {p.name}", is_error=True)
                return False
            if p.suffix.lower() != ".pdf":
                self._set_status(f"не PDF: {p.name}", is_error=True)
                return False
            self._pdf_path = p
            # сброс
            self._doc = None
            self._n_pages = 0
            self._page_sizes.clear()
            self._page_widgets.clear()
            self._draw_areas.clear()
            self._selection.clear()
            # очистить старые страницы
            while (child := self._pages_box.get_first_child()) is not None:
                self._pages_box.remove(child)
            self._placeholder.set_visible(False)

            if has_poppler():
                ok = self._load_poppler(p)
                if ok:
                    self._load_annotations_for_current()
                    self._rebuild_pages()
                    self._update_list()
                    self._set_status(
                        f"открыт: {p.name} · {self._n_pages} стр. · {len(self._highlights)} highlights"
                    )
                    # info
                    try:
                        self._info_label.set_text(
                            f"{p}\n→ {ann_path_for(p).name} ({len(self._highlights)} аннотаций)"
                        )
                    except Exception:
                        pass
                    return True
                else:
                    self._set_status("Poppler не смог открыть PDF", is_error=True)
            # fallback WebView
            if has_webkit():
                self._load_webview(p)
                self._load_annotations_for_current()
                self._update_list()
                return True
            # полный fallback
            self._pages_box.append(self._build_fallback(p))
            self._set_status(
                "Poppler/WebKit недоступны — открой внешним просмотрщиком", is_error=True
            )
            return False

        def _load_poppler(self, p: Path) -> bool:
            try:
                import gi  # type: ignore

                gi.require_version("Poppler", "0.18")
                from gi.repository import Poppler  # type: ignore

                uri = p.resolve().as_uri() if p.exists() else f"file://{p}"
                # Gio.File альтернатива
                doc = Poppler.Document.new_from_file(uri, None)
                if doc is None:
                    return False
                n = doc.get_n_pages()
                sizes: list[tuple[float, float]] = []
                for i in range(n):
                    page = doc.get_page(i)
                    if page is None:
                        sizes.append((595.0, 842.0))
                    else:
                        try:
                            w, h = page.get_size()
                            sizes.append((float(w), float(h)))
                        except Exception:
                            sizes.append((595.0, 842.0))
                self._doc = doc
                self._n_pages = n
                self._page_sizes = sizes
                self._left_stack.set_visible_child_name("poppler")
                return True
            except Exception as e:
                log.debug("poppler load failed %s: %s", p, e)
                self._doc = None
                self._n_pages = 0
                return False

        def _load_webview(self, p: Path) -> None:
            # очистить pages_box
            while (child := self._pages_box.get_first_child()) is not None:
                self._pages_box.remove(child)
            # создать WebView с file://
            try:
                # ensure version
                has_webkit()
                from gi.repository import WebKit  # type: ignore

                if len(self._web_container.get_first_child() or []) == 0:  # type: ignore
                    pass
                while (c := self._web_container.get_first_child()) is not None:
                    self._web_container.remove(c)
                view = WebKit.WebView()
                view.set_hexpand(True)
                view.set_vexpand(True)
                uri = p.resolve().as_uri()
                view.load_uri(uri)
                # toolbar hint
                hint = Gtk.Label(
                    label="WebView (PDF.js / нативный просмотрщик) — выделение пока только через Poppler",
                    css_classes=["dim-hint"],
                    wrap=True,
                    halign=Gtk.Align.START,
                )
                self._web_container.append(hint)
                sc = Gtk.ScrolledWindow(hexpand=True, vexpand=True, css_classes=["pdf-web-scroll"])
                sc.set_child(view)
                self._web_container.append(sc)
                self._webview = view
                self._left_stack.set_visible_child_name("webview")
                self._set_status(f"открыт (WebView): {p.name} — для highlight нужен Poppler")
            except Exception as e:
                log.debug("webview load failed: %s", e)
                self._web_container.append(
                    Gtk.Label(label=f"WebView ошибка: {e}", css_classes=["dim-hint"])
                )

        def _build_fallback(self, p: Path) -> Gtk.Widget:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["empty"])
            box.set_halign(Gtk.Align.CENTER)
            box.set_valign(Gtk.Align.CENTER)
            box.append(Gtk.Label(label="📄", css_classes=["empty-icon"]))
            box.append(Gtk.Label(label=f"{p.name}", css_classes=["empty-text"]))
            box.append(
                Gtk.Label(
                    label="Poppler и WebKit недоступны.\nУстанови:  apt install gir1.2-poppler-0.18  или  gir1.2-webkit2-4.1",
                    css_classes=["dim-hint"],
                    wrap=True,
                )
            )
            btn = Gtk.Button(
                label="Открыть внешним просмотрщиком", css_classes=["suggested-action"]
            )
            btn.connect("clicked", lambda *_: self._open_external(p))
            box.append(btn)
            return box

        def _open_external(self, p: Path) -> None:
            try:
                import subprocess

                subprocess.Popen(["xdg-open", str(p)])
                self._set_status(f"открыт внешне: {p.name}")
            except Exception as e:
                self._set_status(f"не удалось открыть: {e}", is_error=True)

        def _rebuild_pages(self) -> None:
            # пересоздать виджеты страниц
            while (child := self._pages_box.get_first_child()) is not None:
                self._pages_box.remove(child)
            self._page_widgets.clear()
            self._draw_areas.clear()
            if self._doc is None or self._n_pages == 0:
                self._pages_box.append(self._placeholder)
                self._placeholder.set_visible(True)
                return
            for idx in range(self._n_pages):
                w = self._build_page_widget(idx)
                self._pages_box.append(w)

        def _build_page_widget(self, page_idx: int) -> Gtk.Widget:
            outer = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["pdf-page-outer"]
            )
            label = Gtk.Label(
                label=f"Стр. {page_idx + 1} / {self._n_pages}",
                css_classes=["dim-hint", "pdf-page-label"],
                halign=Gtk.Align.START,
            )
            outer.append(label)
            frame = Gtk.Frame(css_classes=["pdf-page-frame"])
            try:
                w_pt, h_pt = (
                    self._page_sizes[page_idx]
                    if page_idx < len(self._page_sizes)
                    else (595.0, 842.0)
                )
            except Exception:
                w_pt, h_pt = 595.0, 842.0
            draw = Gtk.DrawingArea(css_classes=["pdf-page"])
            # размер в пикселях
            draw.set_content_width(int(w_pt * self._zoom))
            draw.set_content_height(int(h_pt * self._zoom))
            draw.set_hexpand(True)
            draw.set_vexpand(False)
            draw._page_idx = page_idx  # type: ignore[attr-defined]
            draw._w_pt = w_pt  # type: ignore[attr-defined]
            draw._h_pt = h_pt  # type: ignore[attr-defined]
            draw.set_draw_func(self._on_draw_page, page_idx)
            # жесты
            try:
                drag = Gtk.GestureDrag.new()
                drag.set_button(1)
                drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
                drag.connect("drag-begin", self._on_drag_begin, page_idx)
                drag.connect("drag-update", self._on_drag_update, page_idx)
                drag.connect("drag-end", self._on_drag_end, page_idx)
                draw.add_controller(drag)
            except Exception:
                pass
            try:
                click = Gtk.GestureClick.new()
                click.set_button(1)
                click.connect("pressed", self._on_page_click, page_idx)
                draw.add_controller(click)
            except Exception:
                pass
            frame.set_child(draw)
            outer.append(frame)
            self._page_widgets[page_idx] = outer
            self._draw_areas[page_idx] = draw
            return outer

        # ── draw ─────────────────────────────────────────────────
        def _on_draw_page(
            self, area: Gtk.DrawingArea, cr: Any, w: int, h: int, page_idx: int
        ) -> None:
            # фон
            try:
                cr.set_source_rgb(0.99, 0.99, 0.985)
                cr.rectangle(0, 0, w, h)
                cr.fill()
            except Exception:
                pass
            if self._doc is None:
                try:
                    cr.set_source_rgb(0.6, 0.6, 0.65)
                    cr.select_font_face("Sans", 0, 0)  # type: ignore[attr-defined]
                    cr.set_font_size(14)
                    cr.move_to(20, 30)
                    cr.show_text("PDF не загружен")
                except Exception:
                    pass
                return
            try:
                import gi  # type: ignore

                gi.require_version("Poppler", "0.18")

                page = self._doc.get_page(page_idx)
                if page is None:
                    return
                w_pt, h_pt = (
                    self._page_sizes[page_idx]
                    if page_idx < len(self._page_sizes)
                    else (595.0, 842.0)
                )
                scale_x = w / w_pt if w_pt else 1.0
                scale_y = h / h_pt if h_pt else 1.0
                scale = min(scale_x, scale_y) if scale_x and scale_y else self._zoom
                # рендер poppler
                cr.save()
                cr.scale(scale, scale)
                try:
                    page.render(cr)
                except Exception as e:
                    log.debug("page render failed %s: %s", page_idx, e)
                cr.restore()
                # overlay highlights для этой страницы
                for hl in self._highlights:
                    try:
                        if int(hl.get("page", -1)) != page_idx:
                            continue
                        rects = hl.get("rects") or []
                        color = _sanitize_color(hl.get("color"))
                        r, g, b, a = _hex_to_rgba(color, 0.32)
                        # выбранный — ярче
                        is_sel = hl.get("id") == self._selected_id
                        if is_sel:
                            a = 0.52
                        for rect in rects:
                            try:
                                x1, y1, x2, y2 = (
                                    float(rect[0]),
                                    float(rect[1]),
                                    float(rect[2]),
                                    float(rect[3]),
                                )
                                # PDF origin bottom-left → widget top-left
                                wx = x1 * scale
                                wy_top = (h_pt - y2) * scale
                                ww = (x2 - x1) * scale
                                wh = (y2 - y1) * scale
                                if ww <= 0 or wh <= 0:
                                    continue
                                cr.set_source_rgba(r, g, b, a)
                                cr.rectangle(wx, wy_top, ww, wh)
                                cr.fill()
                                if is_sel:
                                    cr.set_source_rgba(r, g, b, 0.9)
                                    cr.set_line_width(1.2)
                                    cr.rectangle(wx, wy_top, ww, wh)
                                    cr.stroke()
                            except Exception:
                                continue
                    except Exception:
                        continue
                # текущее выделение (drag)
                sel = self._selection.get(page_idx)
                if sel is not None:
                    try:
                        x1, y1, x2, y2 = sel  # pdf coords
                        wx = min(x1, x2) * scale
                        wy_top = (h_pt - max(y1, y2)) * scale
                        ww = abs(x2 - x1) * scale
                        wh = abs(y2 - y1) * scale
                        cr.set_source_rgba(0.2, 0.5, 1.0, 0.18)
                        cr.rectangle(wx, wy_top, ww, wh)
                        cr.fill()
                        cr.set_source_rgba(0.2, 0.5, 1.0, 0.85)
                        cr.set_line_width(1.0)
                        # dashed
                        try:
                            cr.set_dash([6.0, 4.0], 0)
                        except Exception:
                            pass
                        cr.rectangle(wx, wy_top, ww, wh)
                        cr.stroke()
                        try:
                            cr.set_dash([], 0)
                        except Exception:
                            pass
                    except Exception:
                        pass
            except Exception as e:
                log.debug("draw failed: %s", e)

        # ── selection (drag) ─────────────────────────────────────
        def _on_drag_begin(self, gesture: Any, x: float, y: float, page_idx: int) -> None:
            try:
                # x,y — offset от начала drag
                start_x, start_y = gesture.get_start_point()
                # для drag-begin start == current
                sx, sy = (
                    float(start_x[0] if isinstance(start_x, tuple) else start_x),
                    float(start_y[0] if isinstance(start_y, tuple) else start_y),
                )  # type: ignore
                # fallback: используем x,y как есть (Gtk4 drag begin x,y уже start)
                if sx == 0 and sy == 0:
                    sx, sy = float(x), float(y)
                # запомним в widget coords
                gesture._start_widget = (sx, sy)  # type: ignore[attr-defined]
                # конвертируем в pdf coords
                w_pt, h_pt = (
                    self._page_sizes[page_idx]
                    if page_idx < len(self._page_sizes)
                    else (595.0, 842.0)
                )
                area = self._draw_areas.get(page_idx)
                if area is None:
                    return
                alloc_w = area.get_content_width() or int(w_pt * self._zoom)
                alloc_h = area.get_content_height() or int(h_pt * self._zoom)
                scale = (alloc_w / w_pt) if w_pt else self._zoom
                pdf_x = sx / scale if scale else sx
                pdf_y = h_pt - (sy / scale if scale else sy)
                gesture._start_pdf = (pdf_x, pdf_y)  # type: ignore[attr-defined]
                self._selection[page_idx] = (pdf_x, pdf_y, pdf_x, pdf_y)
                self._queue_draw_page(page_idx)
            except Exception as e:
                log.debug("drag begin failed: %s", e)

        def _on_drag_update(self, gesture: Any, dx: float, dy: float, page_idx: int) -> None:
            try:
                start = getattr(gesture, "_start_widget", None)
                if start is None:
                    return
                sx, sy = start
                cur_x = sx + float(dx)
                cur_y = sy + float(dy)
                w_pt, h_pt = (
                    self._page_sizes[page_idx]
                    if page_idx < len(self._page_sizes)
                    else (595.0, 842.0)
                )
                area = self._draw_areas.get(page_idx)
                alloc_w = area.get_content_width() if area else int(w_pt * self._zoom)
                scale = (alloc_w / w_pt) if w_pt else self._zoom
                pdf_sx, pdf_sy = getattr(
                    gesture,
                    "_start_pdf",
                    (sx / scale if scale else sx, h_pt - sy / scale if scale else h_pt),
                )
                pdf_cx = cur_x / scale if scale else cur_x
                pdf_cy = h_pt - (cur_y / scale if scale else cur_y)
                self._selection[page_idx] = (pdf_sx, pdf_sy, pdf_cx, pdf_cy)
                self._queue_draw_page(page_idx)
                # показать кнопку выделить если rect достаточный
                w = abs(pdf_cx - pdf_sx)
                h = abs(pdf_cy - pdf_sy)
                self._hl_btn.set_sensitive(w > 4 and h > 4)
            except Exception as e:
                log.debug("drag update failed: %s", e)

        def _on_drag_end(self, gesture: Any, dx: float, dy: float, page_idx: int) -> None:
            try:
                sel = self._selection.get(page_idx)
                if sel is None:
                    return
                x1, y1, x2, y2 = sel
                # игнор слишком маленьких
                if abs(x2 - x1) < 4 or abs(y2 - y1) < 4:
                    self._selection.pop(page_idx, None)
                    self._hl_btn.set_sensitive(False)
                    self._queue_draw_page(page_idx)
                    return
                # нормализация уже в create_highlight, но здесь оставляем
                self._selection[page_idx] = (x1, y1, x2, y2)
                self._hl_btn.set_sensitive(True)
                self._queue_draw_page(page_idx)
                # автоматически не создаём — ждём кнопку «Выделить»
                # но если пользователь хочет — можно сразу выделить найденный текст
                self._set_status(
                    f"выделена область на стр. {page_idx + 1} — нажми «Выделить» ({self._color})"
                )
            except Exception as e:
                log.debug("drag end failed: %s", e)

        def _on_page_click(
            self, gesture: Any, n_press: int, x: float, y: float, page_idx: int
        ) -> None:
            # клик без drag — выбрать highlight под курсором
            if n_press != 1:
                return
            # если был drag — не обрабатывать как клик
            sel = self._selection.get(page_idx)
            if sel is not None:
                # если drag был только что — пропустим
                x1, y1, x2, y2 = sel
                if abs(x2 - x1) > 3 or abs(y2 - y1) > 3:
                    return
            try:
                w_pt, h_pt = (
                    self._page_sizes[page_idx]
                    if page_idx < len(self._page_sizes)
                    else (595.0, 842.0)
                )
                area = self._draw_areas.get(page_idx)
                alloc_w = area.get_content_width() if area else int(w_pt * self._zoom)
                scale = (alloc_w / w_pt) if w_pt else self._zoom
                pdf_x = float(x) / scale if scale else float(x)
                pdf_y = h_pt - float(y) / scale if scale else float(y)
                # поиск highlight под точкой
                found = None
                for hl in self._highlights:
                    if int(hl.get("page", -1)) != page_idx:
                        continue
                    for r in hl.get("rects") or []:
                        try:
                            rx1, ry1, rx2, ry2 = float(r[0]), float(r[1]), float(r[2]), float(r[3])
                            if rx1 <= pdf_x <= rx2 and ry1 <= pdf_y <= ry2:
                                found = hl
                                break
                        except Exception:
                            continue
                    if found:
                        break
                if found is not None:
                    self._selected_id = str(found.get("id"))
                    self._del_btn.set_sensitive(True)
                    self._queue_draw_all()
                    self._select_list_row(found["id"])
                    self._set_status(
                        f"выбран highlight на стр. {page_idx + 1}: «{found.get('text', '')[:60]}»"
                    )
                else:
                    # клик в пустое — снять выбор
                    if self._selected_id is not None:
                        self._selected_id = None
                        self._del_btn.set_sensitive(False)
                        self._queue_draw_all()
                        try:
                            self._list_box.unselect_all()
                        except Exception:
                            pass
            except Exception as e:
                log.debug("page click failed: %s", e)

        def _queue_draw_page(self, page_idx: int) -> None:
            try:
                area = self._draw_areas.get(page_idx)
                if area is not None:
                    area.queue_draw()
            except Exception:
                pass

        def _queue_draw_all(self) -> None:
            for idx in list(self._draw_areas.keys()):
                self._queue_draw_page(idx)

        # ── highlight actions ────────────────────────────────────
        def _on_highlight_clicked(self, _btn: Any) -> None:
            # собрать все активные selection
            created = 0
            for page_idx, sel in list(self._selection.items()):
                if sel is None:
                    continue
                x1, y1, x2, y2 = sel
                if abs(x2 - x1) < 3 or abs(y2 - y1) < 3:
                    continue
                # нормализация pdf_rect
                pdf_rect_x1, pdf_rect_y1 = min(x1, x2), min(y1, y2)
                pdf_rect_x2, pdf_rect_y2 = max(x1, x2), max(y1, y2)
                # Попробовать получить точные glyph rects через Poppler
                rects: list[list[float]] = []
                text = ""
                try:
                    if self._doc is not None and has_poppler():
                        import gi  # type: ignore

                        gi.require_version("Poppler", "0.18")
                        from gi.repository import Poppler  # type: ignore

                        page = self._doc.get_page(page_idx)
                        if page is not None:
                            # Poppler Rectangle (bottom-left)
                            pr = Poppler.Rectangle()
                            pr.x1 = pdf_rect_x1
                            pr.y1 = pdf_rect_y1
                            pr.x2 = pdf_rect_x2
                            pr.y2 = pdf_rect_y2
                            # текст
                            try:
                                text = page.get_text_for_area(pr) or ""
                                text = text.strip()
                            except Exception:
                                text = ""
                            # selection region — точные quads
                            try:
                                regs = page.get_selection_region(
                                    1.0, Poppler.SelectionStyle.GLYPH, pr
                                )
                                if regs:
                                    for r in regs:
                                        rects.append(
                                            [float(r.x1), float(r.y1), float(r.x2), float(r.y2)]
                                        )
                            except Exception as e:
                                log.debug("selection_region failed: %s", e)
                except Exception as e:
                    log.debug("highlight poppler failed: %s", e)
                if not rects:
                    # fallback — сам rect
                    rects = [[pdf_rect_x1, pdf_rect_y1, pdf_rect_x2, pdf_rect_y2]]
                if not text:
                    # попытаться вытащить через rects
                    try:
                        if self._doc is not None:
                            page = self._doc.get_page(page_idx)
                            if page is not None and rects:
                                from gi.repository import Poppler  # type: ignore

                                for r in rects[:1]:
                                    pr2 = Poppler.Rectangle()
                                    pr2.x1, pr2.y1, pr2.x2, pr2.y2 = r
                                    try:
                                        text = page.get_text_for_area(pr2) or text
                                    except Exception:
                                        pass
                                    if text:
                                        break
                    except Exception:
                        pass
                hl = create_highlight(page_idx, rects, text=text, color=self._color)
                if not hl.get("rects"):
                    continue
                self._highlights.append(hl)
                created += 1
                self._selected_id = str(hl["id"])
            # очистить выделения
            self._selection.clear()
            self._hl_btn.set_sensitive(False)
            if created:
                self._save()
                self._update_list()
                self._queue_draw_all()
                self._set_status(f"создано {created} highlight(s) · {len(self._highlights)} всего")
                self._del_btn.set_sensitive(self._selected_id is not None)
            else:
                self._set_status("выдели область перетаскиванием на странице", is_error=True)

        def _on_delete_clicked(self, _btn: Any) -> None:
            if not self._selected_id:
                return
            hid = self._selected_id
            before = len(self._highlights)
            self._highlights = [h for h in self._highlights if str(h.get("id")) != hid]
            if len(self._highlights) == before:
                return
            self._selected_id = None
            self._del_btn.set_sensitive(False)
            self._save()
            self._update_list()
            self._queue_draw_all()
            self._set_status(f"удалён highlight · осталось {len(self._highlights)}")

        def _on_clear_all(self, _btn: Any) -> None:
            if not self._highlights:
                return
            # подтверждение
            try:
                dlg = Adw.AlertDialog(
                    heading="Удалить всё?",
                    body=f"Будет удалено {len(self._highlights)} выделений. Отменить нельзя.",
                )
                dlg.add_response("cancel", "Отмена")
                dlg.add_response("ok", "Удалить")
                dlg.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
                dlg.set_default_response("cancel")
                dlg.set_close_response("cancel")

                def _resp(_d: Any, resp: str) -> None:
                    if resp != "ok":
                        return
                    self._highlights.clear()
                    self._selected_id = None
                    self._del_btn.set_sensitive(False)
                    self._save()
                    self._update_list()
                    self._queue_draw_all()
                    self._set_status("все highlights удалены")

                dlg.connect("response", _resp)
                # present
                root = self.get_root()
                if root is not None:
                    dlg.present(root)
                else:
                    # fallback без диалога
                    self._highlights.clear()
                    self._save()
                    self._update_list()
                    self._queue_draw_all()
            except Exception:
                self._highlights.clear()
                self._save()
                self._update_list()
                self._queue_draw_all()

        # ── list ─────────────────────────────────────────────────
        def _update_list(self) -> None:
            if not hasattr(self, "_list_box"):
                return
            while (child := self._list_box.get_first_child()) is not None:
                self._list_box.remove(child)
            if not self._highlights:
                self._list_empty.set_visible(True)
                self._list_scroller.set_visible(False)
                self._count_label.set_text("")
                return
            self._list_empty.set_visible(False)
            self._list_scroller.set_visible(True)
            # сортировка по странице
            sorted_hls = sorted(
                self._highlights, key=lambda h: (int(h.get("page", 0)), str(h.get("created", "")))
            )
            for hl in sorted_hls:
                row = Gtk.ListBoxRow(css_classes=["pdf-row"], activatable=True, selectable=True)
                if str(hl.get("id")) == self._selected_id:
                    row.add_css_class("pdf-row-selected")
                box = Gtk.Box(
                    orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["pdf-row-box"]
                )
                box.set_margin_top(6)
                box.set_margin_bottom(6)
                box.set_margin_start(8)
                box.set_margin_end(8)
                # заголовок: стр. + цвет
                hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                color = _sanitize_color(hl.get("color"))
                # цветной квадрат
                dot = Gtk.Box(css_classes=["pdf-color-dot"])
                try:
                    # inline style via css? используем ColorButton-подобный
                    r, g, b, a = _hex_to_rgba(color, 1.0)
                    # Gtk4 не позволяет set_style прямо, используем DrawingArea
                    da = Gtk.DrawingArea(
                        width_request=14, height_request=14, css_classes=["pdf-color-dot"]
                    )

                    def _draw_dot(_da, cr, w, h, _data):
                        cr.set_source_rgba(r, g, b, 0.95)
                        cr.arc(w / 2, h / 2, min(w, h) / 2 - 1, 0, 6.28318)
                        cr.fill()

                    da.set_draw_func(_draw_dot, None)
                    hdr.append(da)
                except Exception:
                    hdr.append(Gtk.Label(label="●", css_classes=["pdf-color-label"]))
                pg = int(hl.get("page", 0)) + 1
                hdr.append(
                    Gtk.Label(label=f"Стр. {pg}", css_classes=["dim-hint"], halign=Gtk.Align.START)
                )
                hdr.append(
                    Gtk.Label(
                        label=color,
                        css_classes=["dim-hint", "pdf-color-hex"],
                        halign=Gtk.Align.START,
                    )
                )
                hdr.set_hexpand(True)
                box.append(hdr)
                txt = str(hl.get("text", "")).strip()
                if txt:
                    lbl = Gtk.Label(
                        label=txt[:120] + ("…" if len(txt) > 120 else ""),
                        halign=Gtk.Align.START,
                        xalign=0,
                        wrap=True,
                        ellipsize=Pango.EllipsizeMode.END,
                        css_classes=["pdf-text"],
                    )
                    box.append(lbl)
                else:
                    # показать rects если нет текста
                    rects = hl.get("rects") or []
                    lbl = Gtk.Label(
                        label=f"{len(rects)} rect(s)",
                        css_classes=["dim-hint"],
                        halign=Gtk.Align.START,
                    )
                    box.append(lbl)
                if hl.get("comment"):
                    cmt = Gtk.Label(
                        label=str(hl.get("comment"))[:120],
                        halign=Gtk.Align.START,
                        xalign=0,
                        wrap=True,
                        css_classes=["dim-hint", "pdf-comment"],
                    )
                    box.append(cmt)
                row.set_child(box)
                row._hl_id = str(hl.get("id"))  # type: ignore[attr-defined]
                self._list_box.append(row)
            self._count_label.set_text(f"· {len(self._highlights)}")

        def _select_list_row(self, hl_id: str) -> None:
            try:
                child = self._list_box.get_first_child()
                while child is not None:
                    if getattr(child, "_hl_id", None) == hl_id:
                        self._list_box.select_row(child)
                        break
                    child = child.get_next_sibling()
            except Exception:
                pass

        def _on_list_row_selected(self, _lb: Any, row: Any | None) -> None:
            if row is None:
                return
            hid = getattr(row, "_hl_id", None)
            if hid:
                self._selected_id = str(hid)
                self._del_btn.set_sensitive(True)
                self._queue_draw_all()
                # скролл к странице
                try:
                    for hl in self._highlights:
                        if str(hl.get("id")) == hid:
                            pg = int(hl.get("page", 0))
                            w = self._page_widgets.get(pg)
                            if w is not None:
                                # скролл
                                adj = self._scroll.get_vadjustment()
                                if adj is not None:
                                    # позиция виджета относительно pages_box
                                    # fallback: просто queue
                                    GLib.idle_add(lambda: self._scroll_to_page(pg) or False)
                            break
                except Exception:
                    pass

        def _on_list_row_activated(self, _lb: Any, row: Any) -> None:
            hid = getattr(row, "_hl_id", None)
            if hid:
                # показать диалог редактирования комментария
                self._edit_comment(hid)

        def _edit_comment(self, hl_id: str) -> None:
            hl = next((h for h in self._highlights if str(h.get("id")) == hl_id), None)
            if hl is None:
                return
            try:
                dlg = Adw.Dialog(title="Комментарий к highlight")
                dlg.set_content_width(420)
                body = Gtk.Box(
                    orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["dialog-body"]
                )
                body.set_margin_top(12)
                body.set_margin_bottom(12)
                body.set_margin_start(16)
                body.set_margin_end(16)
                body.append(
                    Gtk.Label(
                        label=f"Стр. {int(hl.get('page', 0)) + 1} · {hl.get('color')} · «{str(hl.get('text', ''))[:80]}»",
                        wrap=True,
                        halign=Gtk.Align.START,
                        css_classes=["dim-hint"],
                    )
                )
                entry = Gtk.Entry(
                    text=str(hl.get("comment", "")), placeholder_text="Комментарий (опционально)"
                )
                body.append(entry)
                color_drop = Gtk.DropDown.new_from_strings(HIGHLIGHT_COLORS)
                try:
                    color_drop.set_selected(
                        HIGHLIGHT_COLORS.index(_sanitize_color(hl.get("color")))
                    )
                except ValueError:
                    color_drop.set_selected(0)
                body.append(
                    Gtk.Label(label="Цвет", halign=Gtk.Align.START, css_classes=["dim-hint"])
                )
                body.append(color_drop)
                btn_row = Gtk.Box(
                    orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END
                )
                cancel = Gtk.Button(label="Отмена", css_classes=["mod-neutral"])
                save = Gtk.Button(label="Сохранить", css_classes=["suggested-action"])
                btn_row.append(cancel)
                btn_row.append(save)
                body.append(btn_row)

                def _do_save(*_):
                    hl["comment"] = entry.get_text().strip()[:2000]
                    idx = int(color_drop.get_selected())
                    if 0 <= idx < len(HIGHLIGHT_COLORS):
                        hl["color"] = HIGHLIGHT_COLORS[idx]
                    self._save()
                    self._update_list()
                    self._queue_draw_all()
                    try:
                        dlg.close()
                    except Exception:
                        pass

                save.connect("clicked", _do_save)
                cancel.connect("clicked", lambda *_: dlg.close())
                entry.connect("activate", _do_save)
                content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                content.append(body)
                dlg.set_child(content)
                root = self.get_root()
                if root is not None:
                    dlg.present(root)
                else:
                    dlg.present(self)
            except Exception as e:
                log.debug("edit comment failed: %s", e)

        def _scroll_to_page(self, page_idx: int) -> bool:
            try:
                w = self._page_widgets.get(page_idx)
                if w is None:
                    return False
                # получить allocation
                alloc = w.get_allocation()
                adj = self._scroll.get_vadjustment()
                if adj is not None:
                    adj.set_value(float(alloc.y))
                return False
            except Exception:
                return False

        # ── zoom / color ─────────────────────────────────────────
        def _set_zoom(self, z: float) -> None:
            z = max(0.6, min(3.0, float(z)))
            if abs(z - self._zoom) < 0.02:
                return
            self._zoom = z
            self._zoom_label.set_text(f"{int(z * 100)}%")
            # пересоздать размеры
            for idx, area in self._draw_areas.items():
                try:
                    w_pt, h_pt = (
                        self._page_sizes[idx] if idx < len(self._page_sizes) else (595.0, 842.0)
                    )
                    area.set_content_width(int(w_pt * z))
                    area.set_content_height(int(h_pt * z))
                    area.queue_draw()
                except Exception:
                    pass

        def _on_color_changed(self, drop: Any, _pspec: Any) -> None:
            try:
                idx = int(drop.get_selected())
                if 0 <= idx < len(HIGHLIGHT_COLORS):
                    self._color = HIGHLIGHT_COLORS[idx]
                    # если есть выбранный — сразу перекрасить
                    if self._selected_id:
                        for hl in self._highlights:
                            if str(hl.get("id")) == self._selected_id:
                                hl["color"] = self._color
                                self._save()
                                self._update_list()
                                self._queue_draw_all()
                                self._set_status(f"цвет изменён → {self._color}")
                                break
            except Exception:
                pass

        # ── file I/O ─────────────────────────────────────────────
        def _load_annotations_for_current(self) -> None:
            if self._pdf_path is None:
                self._highlights = []
                self._ann_data = {}
                return
            try:
                data = load_annotations(self._pdf_path)
                self._ann_data = data
                self._highlights = list(data.get("highlights", []))
            except Exception as e:
                log.debug("load ann failed: %s", e)
                self._highlights = []

        def _save(self) -> bool:
            if self._pdf_path is None:
                self._set_status("не выбран PDF для сохранения", is_error=True)
                return False
            try:
                data = {
                    "version": ANNOTATION_VERSION,
                    "pdf": self._pdf_path.name,
                    "created": self._ann_data.get("created", _now_iso())
                    if isinstance(self._ann_data, dict)
                    else _now_iso(),
                    "highlights": self._highlights,
                }
                ok = save_annotations(self._pdf_path, data)
                if ok:
                    self._set_status(
                        f"сохранено: {ann_path_for(self._pdf_path).name} · {len(self._highlights)} highlights"
                    )
                    # toast если есть overlay
                    try:
                        root = self.get_root()
                        overlay = getattr(root, "toast_overlay", None) if root is not None else None
                        if overlay is not None:
                            t = Adw.Toast.new(f"Аннотации сохранены: {len(self._highlights)}")
                            t.set_timeout(2)
                            overlay.add_toast(t)
                    except Exception:
                        pass
                else:
                    self._set_status("ошибка сохранения .ann.json", is_error=True)
                return ok
            except Exception as e:
                log.debug("save failed: %s", e)
                self._set_status(f"ошибка сохранения: {e}", is_error=True)
                return False

        # ── toolbar actions ──────────────────────────────────────
        def _on_open_clicked(self, _btn: Any) -> None:
            try:
                dlg = Gtk.FileDialog()
                dlg.set_title("Открыть PDF")
                filt = Gtk.FileFilter()
                filt.set_name("PDF (*.pdf)")
                filt.add_pattern("*.pdf")
                filt.add_mime_type("application/pdf")
                allf = Gtk.FileFilter()
                allf.set_name("Все файлы")
                allf.add_pattern("*")
                flist = Gio.ListStore.new(Gtk.FileFilter)
                flist.append(filt)
                flist.append(allf)
                dlg.set_filters(flist)
                dlg.set_default_filter(filt)

                def _on_open(d: Any, res: Any) -> None:
                    try:
                        f = d.open_finish(res)
                        if f is None:
                            return
                        p = Path(f.get_path())
                        self.load_pdf(p)
                    except Exception as e:
                        log.debug("open finish failed: %s", e)

                # GTK 4.10+ async
                dlg.open(self.get_root() if self.get_root() else None, None, _on_open)  # type: ignore[arg-type]
            except Exception as e:
                log.debug("open dialog failed: %s", e)
                self._set_status(f"ошибка диалога: {e}", is_error=True)

        # ── status ───────────────────────────────────────────────
        def _set_status(self, text: str, is_error: bool = False) -> None:
            try:
                self._status.set_text(text)
                if is_error:
                    self._status.add_css_class("error")
                else:
                    self._status.remove_css_class("error")
                # auto-clear через 4 сек если не ошибка
                if not is_error and text:
                    GLib.timeout_add(
                        4000,
                        lambda: self._status.set_text("") or False
                        if self._status.get_text() == text
                        else False,
                    )
            except Exception:
                pass

        # ── public helpers ───────────────────────────────────────
        def get_pdf_path(self) -> Path | None:
            return self._pdf_path

        def get_highlights(self) -> list[dict[str, Any]]:
            return list(self._highlights)

        def reload_annotations(self) -> None:
            self._load_annotations_for_current()
            self._update_list()
            self._queue_draw_all()

else:  # headless stub — сохраняет API для py_compile/тестов без GTK

    class PdfAnnotateView:  # type: ignore[no-redef]
        """Заглушка для headless/py_compile без GTK — сохраняет API."""

        def __init__(self, *a: Any, **kw: Any) -> None:
            self.settings = kw.get("settings") or (a[0] if a else {})
            self._pdf_path = Path(kw.get("pdf_path")) if kw.get("pdf_path") else None
            self._highlights: list[dict[str, Any]] = []
            if self._pdf_path and self._pdf_path.is_file():
                try:
                    self._highlights = load_annotations(self._pdf_path).get("highlights", [])
                except Exception:
                    self._highlights = []

        def load_pdf(self, pdf_path: Path | str) -> bool:
            self._pdf_path = Path(pdf_path)
            try:
                self._highlights = load_annotations(self._pdf_path).get("highlights", [])
                return True
            except Exception:
                return False

        def get_pdf_path(self) -> Path | None:
            return self._pdf_path

        def get_highlights(self) -> list[dict[str, Any]]:
            return list(self._highlights)

        def reload_annotations(self) -> None:
            if self._pdf_path:
                try:
                    self._highlights = load_annotations(self._pdf_path).get("highlights", [])
                except Exception:
                    pass


# ── Dialog helpers (для files_view / app.py) ─────────────────────────


def create_dialog(
    parent: Any | None, settings: dict | None, pdf_path: Path | str | None = None
) -> Any | None:
    """Создать ``Adw.Dialog`` с ``PdfAnnotateView``. Возвращает диалог или None без GTK."""
    if not _GTK_AVAILABLE:
        return None
    try:
        dlg = Adw.Dialog(title="PDF Аннотации — highlight → .pdf.ann.json")
        dlg.set_content_width(1100)
        dlg.set_content_height(720)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        header = Adw.HeaderBar(css_classes=["pdf-headerbar"])
        box.append(header)
        view = PdfAnnotateView(settings or {}, pdf_path=pdf_path)
        box.append(view)
        dlg.set_child(box)
        # present
        if parent is not None:
            try:
                if hasattr(parent, "get_root"):
                    root = parent.get_root()
                    if root is not None:
                        dlg.present(root)
                        return dlg
                if isinstance(parent, Gtk.Widget):
                    dlg.present(parent)
                    return dlg
            except Exception:
                pass
        return dlg
    except Exception as e:
        log.debug("create_dialog failed: %s", e)
        return None


def open_pdf_annotate(
    parent: Any | None, settings: dict | None, pdf_path: Path | str
) -> Any | None:
    """Удобный хелпер для ``files_view``: открыть PDF в диалоге аннотаций."""
    p = Path(pdf_path)
    if not p.is_file():
        log.debug("open_pdf_annotate: not a file %s", p)
        return None
    if not is_pdf_path(p):
        log.debug("open_pdf_annotate: not pdf %s", p)
        return None
    return create_dialog(parent, settings or {}, pdf_path=p)


__all__ = [
    "ANNOTATION_SUFFIX",
    "ANNOTATION_VERSION",
    "HIGHLIGHT_COLORS",
    "DEFAULT_COLOR",
    "ann_path_for",
    "is_pdf_path",
    "has_poppler",
    "has_webkit",
    "get_webkit_version",
    "load_annotations",
    "save_annotations",
    "create_highlight",
    "add_highlight_to_file",
    "remove_highlight_from_file",
    "PdfAnnotateView",
    "create_dialog",
    "open_pdf_annotate",
]
