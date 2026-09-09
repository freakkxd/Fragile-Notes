"""Панель повторения SRS — SM-2, карточки Q::A.

Хранение: vault/_System/SRS/srs.json
Карточки: Question::Answer в заметках (см. core/srs.py).
Вкладка: герой, KPI, флип-карточка с оценками 0/3/4/5, список предстоящих.
"""

from __future__ import annotations

import datetime
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from ..core.srs import (  # noqa: E402
    GRADE_AGAIN,
    GRADE_EASY,
    GRADE_GOOD,
    GRADE_HARD,
    GRADE_LABELS,
    SrsCard,
    get_srs_dir,
    get_stats,
    review_card,
    scan_cards,
)
from .widgets import empty_state, view_header  # noqa: E402


class SrsView(Gtk.Box):
    """Вкладка Повторение: SM-2, flip-карточка, статистика."""

    def __init__(self, settings: dict, on_open=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self.on_open = on_open
        self._cards: list[SrsCard] = []
        self._due: list[SrsCard] = []
        self._idx: int = 0
        self._show_answer: bool = False

        self.append(view_header("🧠", "Повторение", "SM-2 · карточки Q::A в заметках · vault/_System/SRS/"))

        self._build_toolbar()
        self._build_hero()
        self._build_kpi()
        self._build_reviewer()
        self._build_list()

        self.reload()

    # ── toolbar ──────────────────────────────────────────────────
    def _build_toolbar(self) -> None:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["toolbar", "srs-toolbar"])
        bar.set_margin_start(14)
        bar.set_margin_end(14)

        self._today_btn = Gtk.Button(label="Сегодня", css_classes=["pill"])
        self._today_btn.connect("clicked", lambda *_: self.reload(force=True))
        bar.append(self._today_btn)

        self._refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Обновить")
        self._refresh_btn.connect("clicked", lambda *_: self.reload(force=True))
        bar.append(self._refresh_btn)

        bar.append(Gtk.Box(hexpand=True))

        self._hint_lbl = Gtk.Label(label="Q::A → карточка", css_classes=["dim-hint"], halign=Gtk.Align.END, xalign=1)
        bar.append(self._hint_lbl)

        self.append(bar)

    def _build_hero(self) -> None:
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, css_classes=["tm-hero-v2", "tm-hero-v2--dash"])
        hero.append(Gtk.Label(label="Spaced Repetition · SM-2", css_classes=["tm-hero-v2__eyebrow"], halign=Gtk.Align.START, xalign=0))
        self._hero_title = Gtk.Label(label="Карточки из заметок", css_classes=["tm-hero-v2__title"], halign=Gtk.Align.START, xalign=0)
        hero.append(self._hero_title)
        self._hero_sub = Gtk.Label(label="Формат: Вопрос::Ответ  ·  хранение в vault/_System/SRS/srs.json", css_classes=["tm-hero-v2__date"], halign=Gtk.Align.START, xalign=0)
        hero.append(self._hero_sub)
        self.append(hero)

    def _build_kpi(self) -> None:
        strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, css_classes=["tm-today-strip"])
        strip.append(Gtk.Label(label="SRS", css_classes=["tm-today-strip__label"]))
        self.kpi: dict[str, tuple[Gtk.Label, Gtk.Label]] = {}
        for key, label, tone in [
            ("total", "всего", ""),
            ("due", "к повторению", "warn"),
            ("new", "новые", "run"),
            ("learned", "изучено", "ok"),
            ("overdue", "просрочено", "error"),
        ]:
            chip = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1,
                           css_classes=["tm-kpi-chip"] + ([f"tm-kpi-chip--{tone}"] if tone else []))
            val = Gtk.Label(label="…", css_classes=["tm-kpi-chip__value"])
            lab = Gtk.Label(label=label, css_classes=["tm-kpi-chip__label"])
            chip.append(val)
            chip.append(lab)
            self.kpi[key] = (val, lab)
            strip.append(chip)
        self.append(strip)

    def _build_reviewer(self) -> None:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["card", "srs-reviewer"])
        card.set_margin_start(14)
        card.set_margin_end(14)

        # вопрос/ответ
        self._q_label = Gtk.Label(label="загрузка…", css_classes=["srs-q"], halign=Gtk.Align.CENTER, xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER)
        self._q_label.set_ellipsize(Pango.EllipsizeMode.NONE)
        card.append(self._q_label)

        self._a_label = Gtk.Label(label="", css_classes=["srs-a", "dim-hint"], halign=Gtk.Align.CENTER, xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER)
        card.append(self._a_label)

        self._source_label = Gtk.Label(label="", css_classes=["dim-hint", "srs-source"], halign=Gtk.Align.CENTER, xalign=0.5, ellipsize=Pango.EllipsizeMode.MIDDLE)
        card.append(self._source_label)

        # прогресс
        self._progress = Gtk.ProgressBar(show_text=False, css_classes=["srs-progress"])
        self._progress.set_hexpand(True)
        card.append(self._progress)

        self._status_label = Gtk.Label(label="", css_classes=["dim-hint"], halign=Gtk.Align.CENTER, xalign=0.5)
        card.append(self._status_label)

        # кнопки
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.CENTER)

        self._show_btn = Gtk.Button(label="Показать ответ", css_classes=["suggested-action", "mod-cta"])
        self._show_btn.connect("clicked", self._on_show_answer)
        btn_row.append(self._show_btn)

        # оценки скрыты до Показать
        self._grade_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._grade_box.set_visible(False)
        for grade, tone in [(GRADE_AGAIN, "error"), (GRADE_HARD, "warn"), (GRADE_GOOD, "ok"), (GRADE_EASY, "run")]:
            lbl = GRADE_LABELS[grade]
            b = Gtk.Button(label=lbl, css_classes=["pill", f"pill-{tone}"] if tone != "error" else ["pill", "pill-error"])
            # tooltip с ожидаемым интервалом
            b.set_tooltip_text(f"Оценка {grade}")
            b.connect("clicked", self._on_grade, grade)
            self._grade_box.append(b)
        btn_row.append(self._grade_box)

        self._skip_btn = Gtk.Button(label="Пропустить →", css_classes=["flat"])
        self._skip_btn.connect("clicked", self._on_skip)
        btn_row.append(self._skip_btn)

        # открыть источник
        self._open_btn = Gtk.Button(label="Открыть заметку", css_classes=["link-btn"])
        self._open_btn.connect("clicked", self._on_open_source)
        btn_row.append(self._open_btn)

        card.append(btn_row)

        self._reviewer_card = card
        self.append(card)

    def _build_list(self) -> None:
        self._list_label = Gtk.Label(label="Предстоящие", css_classes=["section-title"], halign=Gtk.Align.START, xalign=0)
        self._list_label.set_margin_start(14)
        self._list_label.set_margin_top(4)
        self.append(self._list_label)

        self._list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sc = Gtk.ScrolledWindow(vexpand=True, hexpand=True, css_classes=["card"])
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc.set_child(self._list_box)
        sc.set_min_content_height(180)
        sc.set_margin_start(14)
        sc.set_margin_end(14)
        sc.set_margin_bottom(14)
        self.append(sc)

    # ── данные ───────────────────────────────────────────────────

    def reload(self, force: bool = False) -> None:
        if force:
            try:
                from ..services import vault as _vault
                _vault.invalidate_vault_cache()
            except Exception:
                pass
        for k in self.kpi:
            self.kpi[k][0].set_text("…")
        self._q_label.set_text("загрузка…")
        self._a_label.set_text("")
        self._source_label.set_text("")
        self._status_label.set_text("")
        self._progress.set_fraction(0.0)
        self._show_answer = False
        self._show_btn.set_visible(True)
        self._grade_box.set_visible(False)
        self._clear_box(self._list_box)
        self._list_box.append(Gtk.Label(label="загрузка…", css_classes=["dim-hint"]))

        settings_copy = dict(self.settings)

        def work() -> None:
            try:
                cards = scan_cards(settings_copy)
                stats = get_stats(settings_copy)
            except Exception:
                cards = []
                stats = {"total": 0, "due": 0, "new": 0, "learned": 0, "overdue": 0}
            GLib.idle_add(self._apply_data, cards, stats)

        threading.Thread(target=work, daemon=True).start()

    def _apply_data(self, cards: list[SrsCard], stats: dict) -> bool:
        self._cards = cards
        today = datetime.date.today()
        self._due = [c for c in cards if c.is_due(today)]
        self._idx = 0
        self._show_answer = False

        # KPI
        self.kpi["total"][0].set_text(str(stats.get("total", 0)))
        self.kpi["due"][0].set_text(str(stats.get("due", 0)))
        self.kpi["new"][0].set_text(str(stats.get("new", 0)))
        self.kpi["learned"][0].set_text(str(stats.get("learned", 0)))
        self.kpi["overdue"][0].set_text(str(stats.get("overdue", 0)))

        # hero
        total = stats.get("total", 0)
        due = stats.get("due", 0)
        if total == 0:
            self._hero_title.set_text("Нет карточек — добавь Q::A в заметки")
            self._hero_sub.set_text("Пример: Столица Франции::Париж  ·  хранение в vault/_System/SRS/srs.json")
        else:
            self._hero_title.set_text(f"Карточек {total} · к повторению {due}")
            self._hero_sub.set_text(f"SM-2 · интервал × EF · сегодня {today.isoformat()} · vault/_System/SRS/")

        # reviewer
        self._render_reviewer()

        # list upcoming (next 20 not due or all)
        self._clear_box(self._list_box)
        if not cards:
            self._list_box.append(empty_state("🧠", "Пока нет карточек", hint="Добавь в любую заметку строку с :: — например: Вопрос::Ответ. Карточки появятся здесь для повторения по SM-2."))
            return False
        # показать предстоящие (future due)
        future = [c for c in cards if not c.is_due(today)]
        if future:
            self._list_label.set_text(f"Предстоящие · {len(future)}")
            for c in future[:20]:
                self._list_box.append(self._card_row(c, future=True))
            if len(future) > 20:
                self._list_box.append(Gtk.Label(label=f"и ещё {len(future)-20}…", css_classes=["dim-hint"], halign=Gtk.Align.START, xalign=0))
        else:
            if due == 0:
                self._list_label.set_text("Все изучено — предстоящих нет")
                self._list_box.append(Gtk.Label(label="Все карточки повторены, следующие появятся по расписанию SM-2.", css_classes=["dim-hint"], halign=Gtk.Align.START, xalign=0))
            else:
                self._list_label.set_text(f"К повторению · {due}")
                # показать due список компактно
                for c in self._due[:20]:
                    self._list_box.append(self._card_row(c, future=False))
        return False

    def _render_reviewer(self) -> None:
        if not self._due:
            if not self._cards:
                self._q_label.set_text("Нет карточек")
                self._a_label.set_text("Добавь Q::A в заметки — они появятся здесь.")
                self._source_label.set_text("")
                self._status_label.set_text(f"vault/_System/SRS/ · {get_srs_dir(self.settings)}")
            else:
                self._q_label.set_text("✓ Все повторено!")
                self._a_label.set_text("На сегодня карточек к повторению нет. Отлично!")
                self._source_label.set_text("")
                self._status_label.set_text(f"изучено {len(self._cards)} · следующее повторение по расписанию")
            self._progress.set_fraction(1.0 if self._cards else 0.0)
            self._show_btn.set_visible(False)
            self._grade_box.set_visible(False)
            self._skip_btn.set_visible(False)
            self._open_btn.set_visible(False)
            return

        # есть due
        self._skip_btn.set_visible(True)
        self._open_btn.set_visible(True)
        total_due = len(self._due)
        if self._idx >= total_due:
            self._idx = 0
        card = self._due[self._idx]
        # progress fraction
        self._progress.set_fraction((self._idx) / max(1, total_due))

        # question always visible
        self._q_label.set_text(card.question)
        self._source_label.set_text(f"{card.source.name} · строка {card.line_no} · EF {card.ef:.2f} · интервал {card.interval} дн. · повт {card.reps}")
        try:
            self._q_label.update_property(Gtk.AccessibleProperty.LABEL, card.question)
        except Exception:
            pass
        if self._show_answer:
            self._a_label.set_text(card.answer)
            self._status_label.set_text(f"карточка {self._idx + 1}/{total_due} · оцени как помнишь")
            self._show_btn.set_visible(False)
            self._grade_box.set_visible(True)
            # tooltip с прогнозом интервала
            # рассчитать прогноз для каждой оценки без мутации
            for idx, grade in enumerate([GRADE_AGAIN, GRADE_HARD, GRADE_GOOD, GRADE_EASY]):
                try:
                    btn = self._grade_box.get_first_child()
                    # собрать кнопки по индексу
                    cur = self._grade_box.get_first_child()
                    for _ in range(idx):
                        cur = cur.get_next_sibling() if cur else None
                    if cur is None:
                        continue
                    # прогноз
                    proxy = SrsCard(id=card.id, question=card.question, answer=card.answer, source=card.source, line_no=card.line_no,
                                    ef=card.ef, interval=card.interval, reps=card.reps, due=card.due, last_reviewed=card.last_reviewed, lapses=card.lapses)
                    from ..core.srs import sm2_update as _sm2
                    _sm2(proxy, grade, datetime.date.today())
                    cur.set_tooltip_text(f"{GRADE_LABELS[grade]} → через {proxy.interval} дн. (EF {proxy.ef:.2f})")
                except Exception:
                    pass
        else:
            self._a_label.set_text("— нажми «Показать ответ» —")
            self._status_label.set_text(f"карточка {self._idx + 1}/{total_due} · {total_due} к повторению сегодня")
            self._show_btn.set_visible(True)
            self._grade_box.set_visible(False)

    def _card_row(self, card: SrsCard, future: bool = False) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["glass-row"])
        row.set_hexpand(True)
        icon = "⏳" if future else "🧠"
        row.append(Gtk.Label(label=icon))
        # question
        q = card.question
        if len(q) > 64:
            q = q[:62] + "…"
        lbl = Gtk.Label(label=q, hexpand=True, halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        row.append(lbl)
        due_str = card.due.isoformat() if card.due else "—"
        row.append(Gtk.Label(label=due_str, css_classes=["dim-hint"]))
        if not future:
            row.append(Gtk.Label(label=f"EF {card.ef:.1f}", css_classes=["dim-hint"]))
        else:
            row.append(Gtk.Label(label=f"×{card.interval}д", css_classes=["dim-hint"]))
        btn = Gtk.Button(label="Открыть", css_classes=["link-btn"])
        btn.connect("clicked", lambda *_: self._open_path(card.source))
        row.append(btn)
        return row

    def _open_path(self, path: Path) -> None:
        if self.on_open is not None:
            try:
                self.on_open(str(path))
                return
            except Exception:
                pass
        try:
            import subprocess
            subprocess.Popen(["xdg-open", str(path)])
        except Exception:
            pass

    # ── actions ──────────────────────────────────────────────────
    def _on_show_answer(self, _btn: Gtk.Button) -> None:
        self._show_answer = True
        self._render_reviewer()

    def _on_grade(self, _btn: Gtk.Button, grade: int) -> None:
        if not self._due:
            return
        if self._idx >= len(self._due):
            self._idx = 0
        card = self._due[self._idx]
        # сохранить оценку
        try:
            review_card(self.settings, card.id, grade, datetime.date.today())
        except Exception as exc:  # noqa: BLE001
            self._toast(f"ошибка сохранения: {exc}")
            return
        # toast
        self._toast(f"{GRADE_LABELS.get(grade, str(grade))} · {card.question[:32]}")
        # перейти к следующей (пересканировать? оптимистично сдвинуть)
        self._due.pop(self._idx)
        # если был единственный — покажем done; иначе остаёмся на том же индексе (следующая карточка)
        if self._idx >= len(self._due) and self._due:
            self._idx = 0
        self._show_answer = False
        if not self._due:
            # перезагрузить для KPI
            self.reload()
        else:
            self._render_reviewer()

    def _on_skip(self, _btn: Gtk.Button) -> None:
        if not self._due:
            return
        self._idx = (self._idx + 1) % len(self._due)
        self._show_answer = False
        self._render_reviewer()

    def _on_open_source(self, _btn: Gtk.Button) -> None:
        if not self._due:
            return
        card = self._due[self._idx] if self._idx < len(self._due) else None
        if card is None:
            return
        self._open_path(card.source)

    def _toast(self, msg: str) -> None:
        try:
            win = self.get_root()
            if win is not None and hasattr(win, "toast_overlay"):
                toast = Adw.Toast.new(msg)
                toast.set_timeout(3)
                win.toast_overlay.add_toast(toast)  # type: ignore[attr-defined]
                return
        except Exception:
            pass

    def _clear_box(self, box: Gtk.Box) -> None:
        while (child := box.get_first_child()) is not None:
            box.remove(child)

    # alias для app refresh
    def refresh(self) -> None:
        self.reload()

    def reload_if_needed(self) -> None:
        self.reload()


__all__ = ["SrsView"]
