"""Переиспользуемые визуальные виджеты: шапки, секции, пилюли, точки, пустые состояния."""

from __future__ import annotations

from gi.repository import Gtk

DOT_COLORS = {
    "ok": (0.47, 0.82, 0.59),
    "error": (0.86, 0.51, 0.57),
    "run": (0.51, 0.66, 1.0),
    "warn": (0.90, 0.69, 0.43),
    "idle": (0.61, 0.65, 0.75),
}

PILL_TONES = ("idle", "ok", "error", "run", "warn")


def view_header(emoji: str, title: str, subtitle: str | None = None) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["view-header"])
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    if emoji:
        row.append(Gtk.Label(label=emoji, css_classes=["view-emoji"]))
    t = Gtk.Label(label=title, css_classes=["view-title"], halign=Gtk.Align.START, xalign=0)
    row.append(t)
    box.append(row)
    if subtitle:
        s = Gtk.Label(label=subtitle, css_classes=["dim-label", "view-sub"], halign=Gtk.Align.START, xalign=0, wrap=True)
        box.append(s)
    return box


def section_title(text: str, count: int | None = None) -> Gtk.Label:
    label = text if count is None else f"{text} · {count}"
    return Gtk.Label(label=label, css_classes=["section-title"], halign=Gtk.Align.START, xalign=0)


def _hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def status_dot(tone: str = "idle", size: int = 10) -> Gtk.DrawingArea:
    da = Gtk.DrawingArea(width_request=size, height_request=size)
    da._tone = tone
    da._size = size
    da.set_draw_func(_draw_dot, da._tone)
    return da


def _draw_dot(_da, cr, w, h, tone: str) -> None:
    color = DOT_COLORS.get(tone, DOT_COLORS["idle"])
    cr.arc(w / 2, h / 2, min(w, h) / 2 - 0.5, 0, 6.28318)
    cr.set_source_rgb(*color)
    cr.fill()


def set_dot_tone(da: Gtk.DrawingArea, tone: str) -> None:
    da._tone = tone
    da.set_draw_func(_draw_dot, tone)
    da.queue_draw()


def status_pill(text: str, tone: str = "idle") -> Gtk.Label:
    tone = tone if tone in PILL_TONES else "idle"
    return Gtk.Label(label=text, css_classes=["pill", f"pill-{tone}"])


def empty_state(
    emoji: str,
    text: str,
    hint: str | None = None,
    action_label: str | None = None,
    on_action=None,
) -> Gtk.Box:
    """Унифицированное пустое состояние: .empty, иконка 2.4em, текст, опц. hint и CTA."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["empty"])
    box.set_halign(Gtk.Align.FILL)
    box.set_valign(Gtk.Align.CENTER)
    icon = Gtk.Label(label=emoji, css_classes=["empty-icon"], halign=Gtk.Align.CENTER)
    box.append(icon)
    title = Gtk.Label(
        label=text, css_classes=["empty-text"], halign=Gtk.Align.CENTER,
        wrap=True, justify=Gtk.Justification.CENTER, xalign=0.5,
    )
    box.append(title)
    if hint:
        h = Gtk.Label(
            label=hint, css_classes=["empty-hint", "dim-hint"],
            halign=Gtk.Align.CENTER, wrap=True, justify=Gtk.Justification.CENTER, xalign=0.5,
        )
        box.append(h)
    if action_label and on_action is not None:
        btn = Gtk.Button(label=action_label, css_classes=["suggested-action", "mod-cta"])
        btn.set_halign(Gtk.Align.CENTER)
        btn.set_margin_top(4)
        btn.connect("clicked", lambda *_: on_action())
        box.append(btn)
    elif action_label:
        # без колбэка — неинтерактивная подсказка-кнопка (disabled для консистентности стиля)
        btn = Gtk.Button(label=action_label, css_classes=["suggested-action", "mod-cta"])
        btn.set_halign(Gtk.Align.CENTER)
        btn.set_margin_top(4)
        btn.set_sensitive(False)
        box.append(btn)
    return box
