"""GUI редактор тем: выбор цветов, preview, сохранение в vault/_System/custom.css.

Интегрируется как вкладка "theme_editor" (VIEW_ICONS/ TITLES в ui.workspace, VIEWS в ui.app,
SECTIONS в ui.sidebar).

Функции без GUI (чистые):
- parse_css_variables(css_text) — вытащить --var: value из CSS.
- generate_css(variables, extra_raw) — собрать window { ... } блок.
- hex_to_rgb_string / rgb_string_to_hex — конверсия для rgb-триплетов (accent).
- normalize_hex — валидация.

GUI:
- ThemeEditorView — Gtk.Box вкладка: сетка цветов + preview + raw TextView + toolbar
  Save/Reset/Preview/Open.

Сохранение: vault/_System/custom.css, затем reload через theme_manager/style.
Preview: временный Gtk.CssProvider на preview-контейнере (и опционально глобально).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

try:
    gi.require_version("Adw", "1")
except Exception:
    pass

from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from .widgets import view_header  # noqa: E402

log = logging.getLogger(__name__)

# ── Регулярки ───────────────────────────────────────────────────────────────
_VAR_RE = re.compile(r"--([\w-]+)\s*:\s*([^;]+);")
_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_RGB_TRIPLE_RE = re.compile(r"^\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*$")
_RGBA_RE = re.compile(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})(?:\s*,\s*([\d.]+))?\s*\)", re.I)

# ── Дефолтные переменные (выжимка из style.CSS window { }) ───────────────
DEFAULT_VARS: dict[str, str] = {
    "--ao-bg-deep": "#06080d",
    "--ao-bg-elevated": "#0a0e16",
    "--ao-bg-pane": "#070a12",
    "--ao-surface-glass": "rgba(15, 19, 30, 0.84)",
    "--ao-surface-glass-hover": "rgba(25, 31, 46, 0.97)",
    "--ao-surface-control": "rgba(255, 255, 255, 0.05)",
    "--ao-surface-control-hover": "rgba(255, 255, 255, 0.1)",
    "--ao-border-subtle": "rgba(255, 255, 255, 0.07)",
    "--ao-border-strong": "rgba(255, 255, 255, 0.13)",
    "--ao-accent-focus": "130, 168, 255",
    "--ao-accent-violet": "190, 165, 255",
    "--ao-accent-cyan": "120, 200, 220",
    "--ao-accent-success": "120, 210, 150",
    "--ao-text": "rgba(228, 234, 246, 0.94)",
    "--ao-text-secondary": "rgba(210, 218, 232, 0.88)",
    "--ao-text-muted": "rgba(175, 186, 204, 0.72)",
    "--ao-text-faint": "rgba(140, 152, 172, 0.55)",
}

# Какие переменные показываем в сетке цветов (label, var, default, kind)
# kind: "hex" | "rgb" | "rgba"
EDITABLE_COLORS: list[tuple[str, str, str, str]] = [
    ("--ao-bg-deep", "Фон глубокий", "#06080d", "hex"),
    ("--ao-bg-pane", "Фон панели", "#070a12", "hex"),
    ("--ao-bg-elevated", "Фон elevated", "#0a0e16", "hex"),
    ("--ao-surface-glass", "Стекло", "rgba(15, 19, 30, 0.84)", "rgba"),
    ("--ao-surface-control", "Контрол", "rgba(255, 255, 255, 0.05)", "rgba"),
    ("--ao-border-subtle", "Граница subtle", "rgba(255, 255, 255, 0.07)", "rgba"),
    ("--ao-accent-focus", "Акцент focus", "130, 168, 255", "rgb"),
    ("--ao-accent-violet", "Акцент violet", "190, 165, 255", "rgb"),
    ("--ao-accent-cyan", "Акцент cyan", "120, 200, 220", "rgb"),
    ("--ao-accent-success", "Акцент success", "120, 210, 150", "rgb"),
    ("--ao-text", "Текст основной", "rgba(228, 234, 246, 0.94)", "rgba"),
    ("--ao-text-muted", "Текст muted", "rgba(175, 186, 204, 0.72)", "rgba"),
]

DEFAULT_EDITABLE_HEX: dict[str, str] = {}


# ── Чистые хелперы ─────────────────────────────────────────────────────────

def normalize_hex(value: str) -> str | None:
    """Привести к #rrggbb или None если не hex."""
    s = value.strip().lower()
    if not s:
        return None
    if not s.startswith("#"):
        s = "#" + s
    if not _HEX_RE.match(s):
        return None
    if len(s) == 4:
        s = "#" + s[1] * 2 + s[2] * 2 + s[3] * 2
    return s


def is_valid_hex(value: str) -> bool:
    return normalize_hex(value) is not None


def hex_to_rgb_string(hex_color: str) -> str:
    """#rrggbb -> 'r, g, b'."""
    h = normalize_hex(hex_color) or "#000000"
    h = h.lstrip("#")
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f"{r}, {g}, {b}"


def rgb_string_to_hex(rgb: str) -> str:
    """'130, 168, 255' | 'rgb(130,168,255)' -> '#82a8ff'."""
    s = rgb.strip()
    m = _RGB_TRIPLE_RE.match(s)
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"#{r:02x}{g:02x}{b:02x}"
    m2 = _RGBA_RE.match(s)
    if m2:
        r, g, b = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
        return f"#{r:02x}{g:02x}{b:02x}"
    # fallback try hex
    nh = normalize_hex(s)
    if nh:
        return nh
    return "#000000"


def rgba_string_to_hex(rgba: str) -> str:
    """rgba(...) -> #rrggbb (alpha игнорируется)."""
    m = _RGBA_RE.match(rgba.strip())
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"#{r:02x}{g:02x}{b:02x}"
    nh = normalize_hex(rgba)
    if nh:
        return nh
    return "#000000"


def parse_css_variables(css_text: str) -> dict[str, str]:
    """Вытащить все --var: value; из текста."""
    out: dict[str, str] = {}
    if not css_text:
        return out
    # убрать комментарии чтобы не парсить внутри них
    txt = re.sub(r"/\*.*?\*/", "", css_text, flags=re.S)
    for m in _VAR_RE.finditer(txt):
        var = "--" + m.group(1).strip()
        val = m.group(2).strip()
        out[var] = val
    return out


def generate_css(variables: dict[str, str], extra_raw: str = "") -> str:
    """Собрать кастом CSS: window { --var: value; } + extra_raw.

    variables — мапа --var -> значение (hex/rgba/rgb-triple).
    extra_raw — дополнительный CSS (без обёртки) для ручного режима.
    """
    lines: list[str] = []
    lines.append("/* FragileNotes custom theme — generated by Theme Editor */")
    lines.append("/* Сохрани как vault/_System/custom.css */")
    lines.append("window {")
    for var, val in sorted(variables.items()):
        if not var.startswith("--"):
            var = "--" + var.lstrip("-")
        lines.append(f"    {var}: {val};")
    lines.append("}")
    if extra_raw and extra_raw.strip():
        lines.append("")
        lines.append("/* ── extra ── */")
        lines.append(extra_raw.strip())
    lines.append("")
    return "\n".join(lines)


def build_css_from_hex_map(hex_map: dict[str, str]) -> str:
    """Собрать CSS из мапы var->hex (конвертирует rgb/rgba vars правильно)."""
    vars_out: dict[str, str] = {}
    kind_by_var = {v: k for v, _, _, k in EDITABLE_COLORS}
    for var, hex_val in hex_map.items():
        kind = kind_by_var.get(var, "hex")
        h = normalize_hex(hex_val) or hex_val
        if kind == "rgb":
            vars_out[var] = hex_to_rgb_string(h)
        elif kind == "rgba":
            # сохраняем rgba с альфой из дефолта если есть
            default = DEFAULT_VARS.get(var, "")
            m = _RGBA_RE.match(default)
            alpha = m.group(4) if m and m.group(4) else "0.85"
            # hex -> r,g,b
            rgb = hex_to_rgb_string(h)
            r, g, b = [x.strip() for x in rgb.split(",")]
            # эвристика: для текстов/бордеров альфа из дефолта, для стекла 0.84 и т.п.
            try:
                a = float(alpha)
            except Exception:
                a = 0.85
            vars_out[var] = f"rgba({r}, {g}, {b}, {a:g})"
        else:
            vars_out[var] = normalize_hex(h) or h
    # добавим непоказанные дефолты чтобы не терялись? нет — только редактируемые
    return generate_css(vars_out)


def custom_css_path_for(settings: dict) -> Path:
    """Путь vault/_System/custom.css по settings (без импорта theme_manager если нужно)."""
    try:
        from . import theme_manager as _tm

        return _tm.custom_css_path(settings)
    except Exception:
        vault_root = str(settings.get("vault_root") or Path.home() / "desktop")
        return Path(vault_root) / "_System" / "custom.css"


# заполнить DEFAULT_EDITABLE_HEX после определения хелперов
for _v, _l, _d, _k in EDITABLE_COLORS:
    try:
        if _k == "hex":
            DEFAULT_EDITABLE_HEX[_v] = normalize_hex(_d) or _d
        elif _k == "rgb":
            DEFAULT_EDITABLE_HEX[_v] = rgb_string_to_hex(_d)
        else:
            DEFAULT_EDITABLE_HEX[_v] = rgba_string_to_hex(_d)
    except Exception:
        DEFAULT_EDITABLE_HEX[_v] = _d


# ── GUI helpers ────────────────────────────────────────────────────────────

def _rgba_from_hex(hex_color: str, alpha: float = 1.0) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    h = normalize_hex(hex_color) or "#000000"
    try:
        ok = rgba.parse(h)
        if not ok:
            rgba.parse("#000000")
    except Exception:
        try:
            rgba.parse("#000000")
        except Exception:
            pass
    if alpha < 1.0:
        try:
            rgba.alpha = float(alpha)
        except Exception:
            pass
    return rgba


def _hex_from_rgba(rgba: Gdk.RGBA) -> str:
    r = int(round(rgba.red * 255))
    g = int(round(rgba.green * 255))
    b = int(round(rgba.blue * 255))
    r = max(0, min(255, r))
    g = max(0, min(255, g))
    b = max(0, min(255, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def _make_color_button(initial_hex: str):
    """Создать кнопку выбора цвета: пробуем ColorDialogButton, fallback ColorButton."""
    # Gtk4.10+ ColorDialogButton
    try:
        if hasattr(Gtk, "ColorDialog"):
            dlg = Gtk.ColorDialog(title="Выбор цвета", with_alpha=False)
            btn = Gtk.ColorDialogButton(dialog=dlg)
            rgba = _rgba_from_hex(initial_hex)
            try:
                btn.set_rgba(rgba)
            except Exception:
                pass
            return btn
    except Exception:
        pass
    # fallback
    try:
        btn = Gtk.ColorButton()
        rgba = _rgba_from_hex(initial_hex)
        try:
            btn.set_rgba(rgba)
        except Exception:
            pass
        try:
            btn.set_title("Выбор цвета")
        except Exception:
            pass
        return btn
    except Exception:
        # ultimate fallback — entry only
        return Gtk.Entry(text=initial_hex, width_chars=9)


def _get_button_rgba(btn) -> Gdk.RGBA | None:
    try:
        if hasattr(btn, "get_rgba"):
            return btn.get_rgba()
    except Exception:
        pass
    return None


def _set_button_rgba(btn, rgba: Gdk.RGBA) -> None:
    try:
        if hasattr(btn, "set_rgba"):
            btn.set_rgba(rgba)
    except Exception:
        pass


# ── ThemeEditorView ───────────────────────────────────────────────────────

class ThemeEditorView(Gtk.Box):
    """Вкладка редактора тем."""

    def __init__(self, settings: dict, on_open=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self.on_open = on_open
        self._updating = False
        self._preview_provider: Gtk.CssProvider | None = None
        self._global_preview_provider: Gtk.CssProvider | None = None
        # hex_map для редактируемых цветов
        self._hex_map: dict[str, str] = {}
        # extra raw CSS (вне window { })
        self._extra_raw: str = ""
        # UI refs
        self._color_buttons: dict[str, object] = {}
        self._color_entries: dict[str, Gtk.Entry] = {}
        self._status_lbl: Gtk.Label | None = None

        self._load_initial()
        self._build_ui()
        self._sync_preview()

    # ── загрузка ───────────────────────────────────────────────────────
    def _load_initial(self) -> None:
        # пробуем прочитать существующий custom.css
        css_text: str | None = None
        try:
            from . import theme_manager as _tm

            css_text = _tm.load_custom_css(self.settings)
        except Exception:
            try:
                from .style import load_custom_css as _lc

                css_text = _lc(self.settings)
            except Exception:
                css_text = None
        if css_text is None:
            # пробуем напрямую файл
            try:
                p = custom_css_path_for(self.settings)
                if p.is_file():
                    css_text = p.read_text(encoding="utf-8")
            except Exception:
                css_text = None

        parsed: dict[str, str] = {}
        if css_text:
            parsed = parse_css_variables(css_text)
            # extra_raw — всё кроме window { } блока (эвристика: хвост после })
            try:
                # найдём последний } window-блока
                m = re.search(r"window\s*\{[^}]*\}", css_text, flags=re.S)
                if m:
                    tail = css_text[m.end():].strip()
                    # убрать комменты extra заголовка
                    tail = re.sub(r"/\*.*?custom theme.*?vault/_System/custom\.css.*?\*/", "", tail, flags=re.S | re.I).strip()
                    tail = re.sub(r"/\*\s*─+\s*extra\s*─+\s*\*/", "", tail, flags=re.I).strip()
                    self._extra_raw = tail
                else:
                    # нет window блока — считаем весь файл extra
                    if parsed:
                        self._extra_raw = ""
                    else:
                        self._extra_raw = css_text.strip()
            except Exception:
                self._extra_raw = ""

        # строим hex_map из parsed или дефолтов
        for var, label, default, kind in EDITABLE_COLORS:
            raw = parsed.get(var)
            if raw is not None:
                if kind == "rgb":
                    hexv = rgb_string_to_hex(raw)
                elif kind == "rgba":
                    hexv = rgba_string_to_hex(raw)
                else:
                    hexv = normalize_hex(raw) or normalize_hex(default) or default
                self._hex_map[var] = hexv
            else:
                if kind == "rgb":
                    self._hex_map[var] = rgb_string_to_hex(default)
                elif kind == "rgba":
                    self._hex_map[var] = rgba_string_to_hex(default)
                else:
                    self._hex_map[var] = normalize_hex(default) or default

    # ── UI ───────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.append(view_header("🎨", "Редактор темы", "Кастом CSS · выбор цветов · предпросмотр · сохранение в vault/_System/custom.css"))

        # toolbar
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["toolbar", "theme-editor-toolbar"])
        bar.set_margin_start(14)
        bar.set_margin_end(14)
        save_btn = Gtk.Button(label="💾 Сохранить", css_classes=["suggested-action", "mod-cta"], tooltip_text="Сохранить в vault/_System/custom.css")
        save_btn.connect("clicked", lambda *_: self._on_save())
        bar.append(save_btn)
        self._save_btn = save_btn

        preview_btn = Gtk.Button(label="👁️ Превью", css_classes=["mod-neutral"], tooltip_text="Применить превью глобально без сохранения")
        preview_btn.connect("clicked", lambda *_: self._on_preview_global())
        bar.append(preview_btn)

        clear_preview_btn = Gtk.Button(label="↩︎ Сбросить превью", css_classes=["mod-neutral"], tooltip_text="Убрать превью (перезагрузить сохранённый custom.css)")
        clear_preview_btn.connect("clicked", lambda *_: self._on_clear_preview())
        bar.append(clear_preview_btn)

        reset_btn = Gtk.Button(label="↺ Сбросить", css_classes=["mod-neutral"], tooltip_text="Вернуть дефолтные цвета")
        reset_btn.connect("clicked", lambda *_: self._on_reset())
        bar.append(reset_btn)

        open_btn = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Открыть папку _System")
        open_btn.connect("clicked", lambda *_: self._on_open_folder())
        bar.append(open_btn)

        reload_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Перечитать custom.css с диска")
        reload_btn.connect("clicked", lambda *_: self._on_reload())
        bar.append(reload_btn)

        self._status_lbl = Gtk.Label(label="", css_classes=["dim-hint"], hexpand=True, halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        bar.append(self._status_lbl)

        self.append(bar)

        # info path
        try:
            cpath = custom_css_path_for(self.settings)
            path_lbl = Gtk.Label(label=f"Файл: {cpath}", halign=Gtk.Align.START, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, css_classes=["dim-label", "dim-hint"])
            path_lbl.set_margin_start(14)
            path_lbl.set_margin_end(14)
            self.append(path_lbl)
            self._path_lbl = path_lbl
        except Exception:
            self._path_lbl = None

        # content: paned-like horizontal
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, hexpand=True, vexpand=True)
        content.set_margin_start(14)
        content.set_margin_end(14)
        content.set_margin_bottom(14)

        # left: colors + raw editor (vertical)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, vexpand=True, hexpand=True)
        left.set_size_request(420, -1)

        # colors card
        colors_card, colors_box = self._glass_card("🎨 Цвета темы", count=f"{len(EDITABLE_COLORS)}")
        self._build_colors_grid(colors_box)
        left.append(colors_card)

        # raw editor card
        raw_card, raw_box = self._glass_card("📝 CSS (raw)")
        self._build_raw_editor(raw_box)
        left.append(raw_card)

        content.append(left)

        # right: preview
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, vexpand=False)
        right.set_size_request(420, -1)
        preview_card, preview_box = self._glass_card("👁️ Превью")
        self._build_preview_box(preview_box)
        right.append(preview_card)

        # help card
        help_card, help_box = self._glass_card("💡 Подсказки")
        help_box.append(Gtk.Label(label="Цвета сохраняются как CSS-переменные в window { }.\nМожно дописать любой CSS в поле Raw — он добавится после блока.\nПревью применяет стили только к правой панели; кнопка «Превью» — ко всему приложению (без записи на диск).", wrap=True, halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"]))
        help_box.append(Gtk.Label(label="Файл: vault/_System/custom.css — отслеживается FileMonitor, изменения подхватываются автоматически.", wrap=True, halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"]))
        help_box.append(Gtk.Label(label="Совет: для полного кастома — правь Raw CSS напрямую (например переопредели .card, .glass-card и т.д.).", wrap=True, halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"]))
        right.append(help_card)

        content.append(right)

        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        clamp = Adw.Clamp(maximum_size=1280, tightening_threshold=860)
        clamp.set_child(content)
        scroller.set_child(clamp)
        self.append(scroller)

        # initial sync raw
        self._update_raw_from_map()

    def _glass_card(self, title: str, count: str | None = None) -> tuple[Gtk.Box, Gtk.Box]:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["glass-card"])
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["glass-card__head"])
        head.append(Gtk.Label(label=title, css_classes=["glass-card__title"]))
        if count is not None:
            head.append(Gtk.Label(label=count, css_classes=["glass-card__count"]))
        card.append(head)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.set_margin_top(8)
        body.set_margin_bottom(10)
        body.set_margin_start(12)
        body.set_margin_end(12)
        card.append(body)
        return card, body

    def _build_colors_grid(self, parent: Gtk.Box) -> None:
        grid = Gtk.Grid(column_spacing=8, row_spacing=6, column_homogeneous=False)
        grid.set_margin_top(4)
        # header
        grid.attach(Gtk.Label(label="Переменная", halign=Gtk.Align.START, css_classes=["dim-hint", "settings-label"]), 0, 0, 1, 1)
        grid.attach(Gtk.Label(label="Цвет", halign=Gtk.Align.CENTER, css_classes=["dim-hint"]), 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="HEX", halign=Gtk.Align.START, css_classes=["dim-hint"]), 2, 0, 1, 1)
        grid.attach(Gtk.Label(label="", halign=Gtk.Align.CENTER), 3, 0, 1, 1)

        for idx, (var, label, default, kind) in enumerate(EDITABLE_COLORS, start=1):
            row = idx
            name_lbl = Gtk.Label(label=label, halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["settings-label"])
            name_lbl.set_tooltip_text(f"{var} · {kind} · дефолт {default}")
            var_lbl = Gtk.Label(label=var, halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"])
            var_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            vbox.append(name_lbl)
            vbox.append(var_lbl)
            grid.attach(vbox, 0, row, 1, 1)

            init_hex = self._hex_map.get(var, rgb_string_to_hex(default) if kind in ("rgb", "rgba") else default)
            if kind in ("rgb", "rgba") and not init_hex.startswith("#"):
                init_hex = rgb_string_to_hex(init_hex) if kind == "rgb" else rgba_string_to_hex(init_hex)

            btn = _make_color_button(init_hex)
            btn.set_size_request(44, 28)
            # connect
            if hasattr(btn, "connect"):
                try:
                    # ColorDialogButton emits notify::rgba, ColorButton emits color-set
                    if hasattr(btn, "connect") and "notify::rgba" in str(type(btn)):
                        pass
                    # try both signals
                    try:
                        btn.connect("notify::rgba", lambda w, *_a, v=var: self._on_color_changed(v, w))
                    except Exception:
                        pass
                    try:
                        btn.connect("color-set", lambda w, v=var: self._on_color_changed(v, w))
                    except Exception:
                        pass
                    # also generic change for Entry fallback
                    if isinstance(btn, Gtk.Entry):
                        btn.connect("changed", lambda w, v=var: self._on_entry_changed(v, w))
                except Exception:
                    pass
            grid.attach(btn, 1, row, 1, 1)
            self._color_buttons[var] = btn

            entry = Gtk.Entry(text=init_hex, width_chars=9, max_width_chars=9, css_classes=["theme-hex-entry"])
            entry.set_tooltip_text("HEX #rrggbb")
            entry.connect("changed", lambda w, v=var: self._on_entry_changed(v, w))
            entry.connect("activate", lambda *_: self._apply_entry_to_button())
            grid.attach(entry, 2, row, 1, 1)
            self._color_entries[var] = entry

            reset = Gtk.Button(label="↺", css_classes=["ws-arrow"], tooltip_text="Сбросить дефолт")
            reset.connect("clicked", lambda _w, v=var, d=default, k=kind: self._reset_one(v, d, k))
            grid.attach(reset, 3, row, 1, 1)

        parent.append(grid)
        # global hex sync button
        sync_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        sync_row.set_margin_top(8)
        to_raw = Gtk.Button(label="→ В CSS", css_classes=["mod-neutral", "btn-sm"], tooltip_text="Обновить Raw CSS из цветов")
        to_raw.connect("clicked", lambda *_: self._update_raw_from_map())
        from_raw = Gtk.Button(label="← Из CSS", css_classes=["mod-neutral", "btn-sm"], tooltip_text="Распарсить Raw CSS и обновить цвета")
        from_raw.connect("clicked", lambda *_: self._update_map_from_raw())
        sync_row.append(to_raw)
        sync_row.append(from_raw)
        parent.append(sync_row)

    def _build_raw_editor(self, parent: Gtk.Box) -> None:
        self._raw_buffer = Gtk.TextBuffer()
        self._raw_view = Gtk.TextView(buffer=self._raw_buffer, wrap_mode=Gtk.WrapMode.WORD, css_classes=["editor"], monospace=True)
        self._raw_view.set_top_margin(6)
        self._raw_view.set_bottom_margin(6)
        self._raw_view.set_left_margin(8)
        self._raw_view.set_right_margin(8)
        try:
            self._raw_view.set_monospace(True)
        except Exception:
            pass
        self._raw_buffer.connect("changed", self._on_raw_changed)
        scroller = Gtk.ScrolledWindow(vexpand=True, min_content_height=180, max_content_height=320, css_classes=["editor-frame"])
        scroller.set_child(self._raw_view)
        scroller.set_vexpand(True)
        parent.append(Gtk.Label(label="Raw custom.css — можно править напрямую. Кнопка «Сохранить» запишет сюда файл.", halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"], wrap=True))
        parent.append(scroller)

    def _build_preview_box(self, parent: Gtk.Box) -> None:
        self._preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["theme-preview"])
        self._preview_box.set_margin_top(4)

        # title row
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["view-header"])
        head.append(Gtk.Label(label="🎨", css_classes=["view-emoji"]))
        head.append(Gtk.Label(label="Превью темы", css_classes=["view-title"]))
        self._preview_box.append(head)

        # card preview
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["card", "glass-card"])
        card.append(Gtk.Label(label="Карточка", halign=Gtk.Align.START, css_classes=["glass-card__title"]))
        card.append(Gtk.Label(label="Пример текста и muted текст для проверки контраста.", wrap=True, halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"]))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.append(Gtk.Button(label="Кнопка", css_classes=["suggested-action"]))
        row.append(Gtk.Button(label="Вторичная", css_classes=["mod-neutral"]))
        row.append(Gtk.Label(label="chip", css_classes=["pill", "pill-ok"]))
        card.append(row)
        self._preview_box.append(card)

        # KPI strip
        kpi = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["kpi-strip"])
        for tone in ("idle", "ok", "warn", "error"):
            chip = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, css_classes=["kpi-chip", f"kpi-chip--{tone}"])
            chip.append(Gtk.Label(label="42", css_classes=["kpi-chip__value"]))
            chip.append(Gtk.Label(label=tone, css_classes=["kpi-chip__label"]))
            kpi.append(chip)
        self._preview_box.append(kpi)

        # editor preview
        buf = Gtk.TextBuffer()
        buf.set_text("# Заголовок\n\nТекст **жирный** и `код` — проверка типографики.\n\n- пункт 1\n- пункт 2\n")
        tv = Gtk.TextView(buffer=buf, wrap_mode=Gtk.WrapMode.WORD, css_classes=["editor", "prose"], editable=False)
        tv.set_size_request(-1, 90)
        sc = Gtk.ScrolledWindow(css_classes=["editor-frame"], min_content_height=90)
        sc.set_child(tv)
        self._preview_box.append(sc)

        # border preview
        border_demo = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        border_demo.append(Gtk.Label(label="Границы:", css_classes=["dim-hint"]))
        for cls in ("card", "glass-card"):
            b = Gtk.Box(css_classes=[cls])
            b.set_size_request(60, 28)
            b.append(Gtk.Label(label="▭", halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER))
            border_demo.append(b)
        self._preview_box.append(border_demo)

        parent.append(self._preview_box)

    # ── Синхронизация ──────────────────────────────────────────────────
    def _on_color_changed(self, var: str, btn) -> None:
        if self._updating:
            return
        rgba = _get_button_rgba(btn)
        if rgba is None:
            return
        hexv = _hex_from_rgba(rgba)
        self._updating = True
        try:
            self._hex_map[var] = hexv
            ent = self._color_entries.get(var)
            if ent is not None and ent.get_text().strip().lower() != hexv.lower():
                ent.set_text(hexv)
            self._update_raw_from_map()
            self._sync_preview()
            self._set_status(f"{var} → {hexv}")
        finally:
            self._updating = False

    def _on_entry_changed(self, var: str, entry: Gtk.Entry) -> None:
        if self._updating:
            return
        txt = entry.get_text().strip()
        nh = normalize_hex(txt)
        if nh is None:
            # не валидный hex — не обновляем кнопку, но помечаем
            try:
                entry.add_css_class("error")
            except Exception:
                pass
            return
        try:
            entry.remove_css_class("error")
        except Exception:
            pass
        # обновить кнопку
        self._updating = True
        try:
            self._hex_map[var] = nh
            btn = self._color_buttons.get(var)
            if btn is not None and hasattr(btn, "set_rgba"):
                _set_button_rgba(btn, _rgba_from_hex(nh))
            self._update_raw_from_map()
            self._sync_preview()
        finally:
            self._updating = False

    def _apply_entry_to_button(self) -> None:
        for var, ent in self._color_entries.items():
            nh = normalize_hex(ent.get_text().strip())
            if nh:
                btn = self._color_buttons.get(var)
                if btn is not None and hasattr(btn, "set_rgba"):
                    _set_button_rgba(btn, _rgba_from_hex(nh))

    def _reset_one(self, var: str, default: str, kind: str) -> None:
        hexv = normalize_hex(default) if kind == "hex" else (rgb_string_to_hex(default) if kind == "rgb" else rgba_string_to_hex(default))
        hexv = hexv or default
        self._updating = True
        try:
            self._hex_map[var] = hexv
            btn = self._color_buttons.get(var)
            if btn is not None and hasattr(btn, "set_rgba"):
                _set_button_rgba(btn, _rgba_from_hex(hexv))
            ent = self._color_entries.get(var)
            if ent is not None:
                ent.set_text(hexv)
            self._update_raw_from_map()
            self._sync_preview()
            self._set_status(f"{var} сброшен → {hexv}")
        finally:
            self._updating = False

    def _on_raw_changed(self, _buf) -> None:
        # debounce — не парсим на каждый keystroke, только по кнопке «← Из CSS» или перед сохранением/превью
        pass

    def _update_raw_from_map(self) -> None:
        css = build_css_from_hex_map(self._hex_map)
        # добавить extra_raw если есть
        if self._extra_raw and self._extra_raw.strip():
            css = css.rstrip() + "\n\n" + self._extra_raw.strip() + "\n"
        self._updating = True
        try:
            self._raw_buffer.set_text(css)
        finally:
            self._updating = False
        self._sync_preview()

    def _update_map_from_raw(self) -> None:
        start, end = self._raw_buffer.get_bounds()
        text = self._raw_buffer.get_text(start, end, True)
        parsed = parse_css_variables(text)
        # tail extra
        m = re.search(r"window\s*\{[^}]*\}", text, flags=re.S)
        if m:
            tail = text[m.end():].strip()
            tail = re.sub(r"/\*.*?custom theme.*?vault/_System/custom\.css.*?\*/", "", tail, flags=re.S | re.I).strip()
            tail = re.sub(r"/\*\s*─+\s*extra\s*─+\s*\*/", "", tail, flags=re.I).strip()
            self._extra_raw = tail
        else:
            self._extra_raw = ""
        changed = 0
        self._updating = True
        try:
            for var, label, default, kind in EDITABLE_COLORS:
                raw = parsed.get(var)
                if raw is None:
                    continue
                if kind == "rgb":
                    hexv = rgb_string_to_hex(raw)
                elif kind == "rgba":
                    hexv = rgba_string_to_hex(raw)
                else:
                    hexv = normalize_hex(raw)
                    if hexv is None:
                        continue
                self._hex_map[var] = hexv
                btn = self._color_buttons.get(var)
                if btn is not None and hasattr(btn, "set_rgba"):
                    _set_button_rgba(btn, _rgba_from_hex(hexv))
                ent = self._color_entries.get(var)
                if ent is not None:
                    ent.set_text(hexv)
                changed += 1
            self._sync_preview()
            self._set_status(f"импорт из CSS: {changed} цветов обновлено" if changed else "в CSS не найдены редактируемые переменные")
        finally:
            self._updating = False

    def _sync_preview(self) -> None:
        """Применить текущий CSS только к preview-контейнеру."""
        start, end = self._raw_buffer.get_bounds()
        css_text = self._raw_buffer.get_text(start, end, True) if hasattr(self, "_raw_buffer") else ""
        if not css_text.strip():
            return
        # применяем к preview_box через провайдер
        try:
            if self._preview_provider is not None:
                try:
                    Gtk.StyleContext.remove_provider_for_display(Gdk.Display.get_default(), self._preview_provider)
                except Exception:
                    pass
                self._preview_provider = None
            # css для превью — оборачиваем preview_box в window-селектор не сработает,
            # поэтому генерируем прямой CSS для дочерних классов превью через переменные
            # Самый простой: создать провайдер с исходным css_text ограниченным window { } -> заменим window на .theme-preview
            preview_css = css_text
            # заменить window { на .theme-preview {
            preview_css = re.sub(r"\bwindow\s*\{", ".theme-preview {", preview_css)
            # также window без пробела
            provider = Gtk.CssProvider()
            provider.load_from_string(preview_css)
            # применяем к display USER но с низк приоритетом? Для превью достаточно display
            display = Gdk.Display.get_default()
            if display is not None:
                # вместо display применяем к конкретному виджету если возможно (Gtk4.10+)
                try:
                    self._preview_box.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)
                    self._preview_provider = provider
                except Exception:
                    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER - 1)
                    self._preview_provider = provider
        except Exception as exc:
            log.debug("preview css failed: %s", exc)

    # ── Действия ─────────────────────────────────────────────────────────
    def _current_css_text(self) -> str:
        start, end = self._raw_buffer.get_bounds()
        return self._raw_buffer.get_text(start, end, True)

    def _on_save(self) -> None:
        text = self._current_css_text()
        if not text.strip():
            self._set_status("пустой CSS — не сохранён")
            return
        # валидация через провайдер (не падает но логирует)
        try:
            p = Gtk.CssProvider()
            p.load_from_string(text)
        except Exception as exc:
            self._set_status(f"ошибка CSS: {exc}")
            return
        path = custom_css_path_for(self.settings)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            self._set_status(f"ошибка записи {path}: {exc}")
            return
        # применить глобально
        ok = False
        try:
            from .style import reload_custom_css as _reload

            ok = _reload(self.settings)
            if not ok:
                from . import theme_manager as _tm

                ok = _tm.reload_custom_css(self.settings)
        except Exception:
            try:
                from . import theme_manager as _tm2

                ok = _tm2.reload_custom_css(self.settings)
            except Exception:
                ok = False
        # сбросить глобальный превью провайдер если был
        self._clear_global_preview()
        self._sync_preview()
        self._set_status(f"сохранено ✓ {path} ({len(text)} байт)" if ok else f"сохранено {path} (но не применён — проверь CSS)")
        self._toast(f"тема сохранена → {path.name}")

    def _on_preview_global(self) -> None:
        text = self._current_css_text()
        if not text.strip():
            self._set_status("пустой CSS")
            return
        try:
            p = Gtk.CssProvider()
            p.load_from_string(text)
        except Exception as exc:
            self._set_status(f"ошибка CSS: {exc}")
            return
        display = Gdk.Display.get_default()
        if display is None:
            self._set_status("превью: нет display (headless)")
            return
        # снять старый превью
        self._clear_global_preview()
        try:
            provider = Gtk.CssProvider()
            provider.load_from_string(text)
            Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)
            self._global_preview_provider = provider
            self._set_status("превью применён глобально (без сохранения) — «Сбросить превью» чтобы откатить")
            self._toast("превью применён — не сохранён на диск")
        except Exception as exc:
            self._set_status(f"превью не удалось: {exc}")

    def _clear_global_preview(self) -> None:
        if self._global_preview_provider is not None:
            try:
                display = Gdk.Display.get_default()
                if display is not None:
                    Gtk.StyleContext.remove_provider_for_display(display, self._global_preview_provider)
            except Exception:
                pass
            self._global_preview_provider = None

    def _on_clear_preview(self) -> None:
        self._clear_global_preview()
        # перезагрузить сохранённый файл
        try:
            from .style import reload_custom_css as _reload

            ok = _reload(self.settings)
        except Exception:
            try:
                from . import theme_manager as _tm

                ok = _tm.reload_custom_css(self.settings)
            except Exception:
                ok = False
        self._sync_preview()
        self._set_status("превью сброшен" if ok else "превью сброшен (файл не найден)")

    def _on_reset(self) -> None:
        self._updating = True
        try:
            for var, label, default, kind in EDITABLE_COLORS:
                hexv = normalize_hex(default) if kind == "hex" else (rgb_string_to_hex(default) if kind == "rgb" else rgba_string_to_hex(default))
                hexv = hexv or default
                self._hex_map[var] = hexv
                btn = self._color_buttons.get(var)
                if btn is not None and hasattr(btn, "set_rgba"):
                    _set_button_rgba(btn, _rgba_from_hex(hexv))
                ent = self._color_entries.get(var)
                if ent is not None:
                    ent.set_text(hexv)
            self._extra_raw = ""
            self._update_raw_from_map()
            self._sync_preview()
            self._set_status("сброшено к дефолтам — нажми Сохранить")
        finally:
            self._updating = False

    def _on_reload(self) -> None:
        self._load_initial()
        self._updating = True
        try:
            for var, ent in self._color_entries.items():
                hexv = self._hex_map.get(var, "")
                ent.set_text(hexv)
                btn = self._color_buttons.get(var)
                if btn is not None and hasattr(btn, "set_rgba"):
                    _set_button_rgba(btn, _rgba_from_hex(hexv))
            self._update_raw_from_map()
            self._sync_preview()
            self._set_status("перечитано с диска")
        finally:
            self._updating = False

    def _on_open_folder(self) -> None:
        try:
            from gi.repository import Gio

            p = custom_css_path_for(self.settings).parent
            if not p.is_dir():
                p.mkdir(parents=True, exist_ok=True)
            Gio.AppInfo.launch_default_for_uri(f"file://{p}", None)
        except Exception:
            try:
                import subprocess

                p = custom_css_path_for(self.settings).parent
                subprocess.Popen(["xdg-open", str(p)])
            except Exception:
                pass

    def _set_status(self, msg: str) -> None:
        if self._status_lbl is not None:
            self._status_lbl.set_text(msg)

    def _toast(self, msg: str) -> None:
        try:
            win = self.get_root()
            if win is not None and hasattr(win, "toast_overlay"):
                toast = Adw.Toast.new(msg)
                toast.set_timeout(3)
                win.toast_overlay.add_toast(toast)  # type: ignore[attr-defined]
        except Exception:
            pass


__all__ = [
    "ThemeEditorView",
    "DEFAULT_VARS",
    "EDITABLE_COLORS",
    "parse_css_variables",
    "generate_css",
    "build_css_from_hex_map",
    "normalize_hex",
    "is_valid_hex",
    "hex_to_rgb_string",
    "rgb_string_to_hex",
    "rgba_string_to_hex",
    "custom_css_path_for",
]
