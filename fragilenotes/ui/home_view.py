"""Рабочий стол — стартовый вид: статус пайплайна, входящие, свежие заметки, задачи, дайджест.

Порт home/dashboard из Obsidian (ao-home-dashboard + tm-ui-kit KPI): единая точка
входа после ночного пробега, чтобы результат был виден сразу.
"""

from __future__ import annotations

import datetime
import subprocess
import threading
import time
from pathlib import Path

from gi.repository import Adw, GLib, Gtk, Pango

from ..paths import resolve_paths
from ..services import vault
from ..services.ai_summary import AiSummaryService
from .widgets import empty_state, status_pill

RESCAN_SECONDS = 15


def _age_label(mtime: float) -> str:
    age = time.time() - mtime
    if age < 3600:
        return "только что"
    if age < 86400:
        return f"{int(age // 3600)} ч назад"
    return f"{int(age // 86400)} дн назад"


def _fmt_date(mtime: float) -> str:
    return datetime.datetime.fromtimestamp(mtime).strftime("%d.%m %H:%M")


class HomeView(Gtk.Box):
    def __init__(self, settings: dict, on_nav) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self.on_nav = on_nav
        self._last_scan = 0.0

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_start(14)
        body.set_margin_end(14)
        body.set_margin_bottom(16)
        body.set_margin_top(2)
        content = Adw.Clamp(maximum_size=960, tightening_threshold=640)
        content.set_child(body)
        scroller.set_child(content)

        body.append(self._build_hero())

        body.append(self._build_kpi())

        self.enrich_card, self.enrich_bar, self.enrich_info = self._build_enrich_card()
        body.append(self.enrich_card)

        self.tasks_card, self.tasks_box = self._build_card(
            "✅ Задачи на сегодня", "Открыть задачи",
            lambda *_: self.on_nav("tasks"),
        )
        body.append(self.tasks_card)

        self.inbox_card, self.inbox_box = self._build_card(
            "📥 Входящие", "Открыть папку",
            lambda *_: self._open(resolve_paths(self.settings).sort),
        )
        body.append(self.inbox_card)

        self.notes_card, self.notes_box = self._build_card("🕘 Свежие заметки")
        body.append(self.notes_card)

        self.digest_card, self.digest_box = self._build_card(
            "🌙 Ночной пробег", "Открыть дайджест",
            lambda *_: self._open_digest(),
        )
        body.append(self.digest_card)

        self.ai_summary_card, self.ai_summary_box = self._build_ai_summary_card()
        body.append(self.ai_summary_card)
        # показать кэш саммари сразу (без LLM)
        GLib.idle_add(self._load_cached_ai_summary)

        self.append(scroller)

        # данные грузим в фоне: окно отрисовывается сразу
        GLib.idle_add(self._scan_async)

    # ── Сборка ───────────────────────────────────────────────
    def _build_hero(self) -> Gtk.Box:
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3,
                       css_classes=["tm-hero-v2", "tm-hero-v2--dash", "tm-hero-v2--inset"])
        hero.append(Gtk.Label(label="Рабочий стол", css_classes=["tm-hero-v2__eyebrow"], halign=Gtk.Align.START, xalign=0))
        hero.append(Gtk.Label(label="Итоги ночного пробега", css_classes=["tm-hero-v2__title"], halign=Gtk.Align.START, xalign=0))
        d = datetime.date.today()
        hero.append(Gtk.Label(label=d.strftime("%A, %d %B %Y").capitalize(),
                              css_classes=["tm-hero-v2__date"], halign=Gtk.Align.START, xalign=0))
        return hero

    def _build_kpi(self) -> Gtk.Widget:
        strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["kpi-strip"])
        strip.set_vexpand(False)
        self._kpi: dict[str, tuple[Gtk.Label, Gtk.Box]] = {}
        for key, label in [
            ("day", "💎 day"),
            ("arch", "🌙 archive"),
            ("enrich", "🔄 enrich"),
            ("today", "✅ сегодня"),
            ("overdue", "⚠ просрочено"),
            ("inbox", "📥 входящие"),
        ]:
            chip = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, css_classes=["kpi-chip"])
            val = Gtk.Label(label="…", css_classes=["kpi-chip__value"], halign=Gtk.Align.START, xalign=0)
            cap = Gtk.Label(label=label, css_classes=["kpi-chip__label"], halign=Gtk.Align.START, xalign=0)
            chip.append(val)
            chip.append(cap)
            strip.append(chip)
            self._kpi[key] = (val, chip)
        # На узких окнах — горизонтальный скролл вместо клипа (Obsidian: KPI не ломают лейаут)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                      vscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(strip)
        scroller.set_vexpand(False)
        return scroller

    def _build_enrich_card(self) -> tuple[Gtk.Box, Gtk.ProgressBar, Gtk.Label]:
        card, body = self._glass_card("🔄 Enrich-пайплайн", count="live")
        self._enrich_title = Gtk.Label(
            label="…", halign=Gtk.Align.START, xalign=0, wrap=True,
            css_classes=["rv-card-title"],
        )
        self._enrich_title.set_margin_top(4)
        body.append(self._enrich_title)
        bar = Gtk.ProgressBar(show_text=True, css_classes=["live-bar"])
        bar.set_visible(False)
        bar.set_margin_top(6)
        body.append(bar)
        info = Gtk.Label(label="", halign=Gtk.Align.START, xalign=0, wrap=True, css_classes=["dim-hint"])
        info.set_margin_top(4)
        body.append(info)
        return card, bar, info

    def _build_card(self, title: str, link_label: str | None = None, on_link=None) -> tuple[Gtk.Box, Gtk.Box]:
        card, body = self._glass_card(title)
        if link_label:
            btn = Gtk.Button(label=link_label, css_classes=["link-btn"])
            btn.connect("clicked", on_link)
            card._head_row.append(btn)
        return card, body

    def _glass_card(self, title: str, count: str | None = None) -> tuple[Gtk.Box, Gtk.Box]:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["glass-card"])
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["glass-card__head"])
        head.set_vexpand(False)
        head.append(Gtk.Label(label=title, css_classes=["glass-card__title"]))
        count_lbl = Gtk.Label(label=count or "", css_classes=["glass-card__count"])
        count_lbl.set_vexpand(False)
        head.append(count_lbl)
        card._head_row = head
        card._count = count_lbl
        card.append(head)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        body.set_margin_top(4)
        body.set_margin_bottom(6)
        body.set_margin_start(10)
        body.set_margin_end(10)
        body.set_vexpand(True)
        card.append(body)
        return card, body

    def _build_ai_summary_card(self) -> tuple[Gtk.Box, Gtk.Box]:
        """Карточка AI-саммари дайджеста: кэш + кнопка генерации через LlmService."""
        card, body = self._glass_card("✨ AI Саммари", count="кэш")
        # хедер-кнопки
        self._ai_busy = False
        self.ai_summary_btn = Gtk.Button(label="Сгенерировать", css_classes=["suggested-action", "pill-btn"])
        self.ai_summary_btn.set_tooltip_text("Сгенерировать саммари последних заметок через LLM (кэш 30м)")
        self.ai_summary_btn.connect("clicked", lambda *_: self._on_generate_ai_summary(force=False))
        self.ai_summary_refresh_btn = Gtk.Button(label="↻", css_classes=["link-btn"])
        self.ai_summary_refresh_btn.set_tooltip_text("Перегенерировать (игнор кэша)")
        self.ai_summary_refresh_btn.connect("clicked", lambda *_: self._on_generate_ai_summary(force=True))
        self.ai_summary_spinner = Gtk.Spinner(spinning=False, visible=False)
        self.ai_summary_spinner.set_margin_start(4)
        card._head_row.append(self.ai_summary_btn)
        card._head_row.append(self.ai_summary_refresh_btn)
        card._head_row.append(self.ai_summary_spinner)

        self.ai_summary_info = Gtk.Label(label="Саммари последних заметок и дайджеста — кэшируется 30 мин", halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"], wrap=True)
        self.ai_summary_info.set_margin_top(2)
        body.append(self.ai_summary_info)

        self.ai_summary_label = Gtk.Label(label="Нажми «Сгенерировать» чтобы получить AI-дайджест", halign=Gtk.Align.START, xalign=0, wrap=True, selectable=True, css_classes=["task-title"])
        self.ai_summary_label.set_margin_top(6)
        self.ai_summary_label.set_max_width_chars(90)
        self.ai_summary_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        body.append(self.ai_summary_label)

        self.ai_summary_meta = Gtk.Label(label="", halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"], wrap=True)
        self.ai_summary_meta.set_margin_top(4)
        body.append(self.ai_summary_meta)
        return card, body

    def _set_ai_busy(self, busy: bool) -> None:
        self._ai_busy = busy
        self.ai_summary_btn.set_sensitive(not busy)
        self.ai_summary_refresh_btn.set_sensitive(not busy)
        self.ai_summary_spinner.set_visible(busy)
        self.ai_summary_spinner.set_spinning(busy)
        if busy:
            self.ai_summary_info.set_text("Генерирую саммари через LLM…")
        # card подсветка во время генерации
        if busy:
            self.ai_summary_card.add_css_class("glass-card--emphasis")
        else:
            self.ai_summary_card.remove_css_class("glass-card--emphasis")

    def _load_cached_ai_summary(self) -> bool:
        """Показать кэш без обращения к LLM — вызывается idle_add при старте."""
        def work() -> None:
            try:
                svc = AiSummaryService(settings=dict(self.settings))
                res = svc.get_cached()
                if res is not None and res.text:
                    GLib.idle_add(lambda: self._apply_ai_summary(True, res.text, cached=True) or False)
                else:
                    GLib.idle_add(lambda: self._apply_ai_summary_empty() or False)
            except Exception:
                GLib.idle_add(lambda: self._apply_ai_summary_empty() or False)
        threading.Thread(target=work, daemon=True).start()
        return False

    def _apply_ai_summary_empty(self) -> bool:
        self.ai_summary_label.set_text("Нажми «Сгенерировать» чтобы получить AI-дайджест")
        self.ai_summary_meta.set_text("")
        self.ai_summary_card._count.set_text("кэш · нет")
        self.ai_summary_info.set_text("Саммари последних заметок и дайджеста — кэшируется 30 мин")
        return False

    def _apply_ai_summary(self, ok: bool, text: str, cached: bool = False) -> bool:
        self._set_ai_busy(False)
        if not ok:
            self.ai_summary_label.set_text(text or "Ошибка генерации саммари")
            self.ai_summary_meta.set_text("")
            self.ai_summary_card._count.set_text("ошибка")
            self.ai_summary_info.set_text("Попробуй ещё раз или проверь статус LLM")
            return False
        self.ai_summary_label.set_text(text.strip())
        age = "кэш" if cached else "свежее"
        when = time.strftime("%H:%M")
        self.ai_summary_meta.set_text(f"{age} · {when} · через LlmService ({'кэш' if cached else 'LLM'})")
        self.ai_summary_card._count.set_text(age)
        self.ai_summary_info.set_text("Готово ✓ — кэш 30 мин, ↻ для перегенерации")
        return False

    def _on_generate_ai_summary(self, force: bool = False) -> None:
        if getattr(self, "_ai_busy", False):
            return
        # нужен живой llm инстанс — берём из окна если доступно, иначе создаём
        self._set_ai_busy(True)
        self.ai_summary_label.set_text("⏳ Генерация саммари…")
        self.ai_summary_meta.set_text("")
        settings_copy = dict(self.settings)
        # попытаться взять llm из parent window (FragileWindow) через иерархию GTK
        llm = None
        try:
            root = self.get_root()
            if root is not None and hasattr(root, "llm"):
                llm = root.llm  # type: ignore[attr-defined]
        except Exception:
            llm = None

        def work() -> None:
            try:
                svc = AiSummaryService(settings=settings_copy, llm=llm)
                res = svc.generate(force=force)
                if res.ok:
                    GLib.idle_add(lambda: self._apply_ai_summary(True, res.text, cached=res.cached) or False)
                else:
                    msg = res.error or "LLM недоступен — проверь day/archive сервер (manage-llm.sh)"
                    if not msg.strip():
                        msg = "LLM недоступен"
                    GLib.idle_add(lambda: self._apply_ai_summary(False, msg) or False)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc) or "ошибка"
                GLib.idle_add(lambda: self._apply_ai_summary(False, msg) or False)
        threading.Thread(target=work, daemon=True).start()

    # ── Действия ─────────────────────────────────────────────
    def _open(self, path: Path) -> None:
        subprocess.Popen(["xdg-open", str(path)])

    def _open_digest(self) -> None:
        d = vault.latest_digest(self.settings)
        if d is not None:
            self._open(d.path)

    # ── Обновление ───────────────────────────────────────────
    def refresh(self, controller, llm, enrich, due_today: int, overdue: int) -> None:
        day = llm.get_status("day")
        arch = llm.get_status("archive")
        self._set_kpi("day", "ВКЛ" if day.online else "ВЫКЛ" if day.online is False else "?",
                      "ok" if day.online else ("error" if day.online is False else "idle"))
        self._set_kpi("arch", "ВКЛ" if arch.online else "ВЫКЛ" if arch.online is False else "?",
                      "ok" if arch.online else ("error" if arch.online is False else "idle"))

        running = enrich.state == "running"
        self._set_kpi("enrich", enrich.stage or enrich.state or "—", "run" if running else "idle")
        self._set_kpi("today", str(due_today), "warn" if due_today else "ok")
        self._set_kpi("overdue", str(overdue), "error" if overdue else "idle")

        if enrich.online:
            stage = enrich.stage or enrich.state or "idle"
            scanned, total = enrich.scanned, enrich.total
            self._enrich_title.set_text(f"{stage}{(' · ' + enrich.profile) if enrich.profile else ''}")
            self.enrich_info.set_text(enrich.summary())
            if running:
                self.enrich_card.add_css_class("glass-card--emphasis")
            else:
                self.enrich_card.remove_css_class("glass-card--emphasis")
            if running and total:
                self.enrich_bar.set_fraction(min(1.0, scanned / total))
                self.enrich_bar.set_text(f"{scanned}/{total}")
                self.enrich_bar.set_visible(True)
            else:
                self.enrich_bar.set_visible(False)
        else:
            self._enrich_title.set_text("enrich-сервер недоступен")
            self.enrich_info.set_text(enrich.error or "проверьте engine_status_url")
            self.enrich_bar.set_visible(False)

        self._maybe_rescan()

    def _set_kpi(self, key: str, value: str, tone: str) -> None:
        val, chip = self._kpi[key]
        val.set_text(value)
        for cls in ("kpi-chip--ok", "kpi-chip--error", "kpi-chip--warn", "kpi-chip--run", "kpi-chip--idle"):
            chip.remove_css_class(cls)
        chip.add_css_class(f"kpi-chip--{tone}")

    def _maybe_rescan(self) -> None:
        now = time.monotonic()
        if now - self._last_scan < RESCAN_SECONDS:
            return
        self._last_scan = now
        self._scan_async()

    # ── Фоновый сбор данных ──────────────────────────────────
    def _scan_async(self) -> bool:
        """Данные собираются в потоке, виджеты обновляются в GUI-потоке."""
        settings = dict(self.settings)
        threading.Thread(target=self._scan_work, args=(settings,), daemon=True).start()
        return False

    def _scan_work(self, settings: dict) -> None:
        from ..core import tasks as tm
        paths = resolve_paths(settings)
        today = datetime.date.today()

        try:
            all_tasks = tm.load_tasks(paths.tm_tasks)
        except Exception:  # noqa: BLE001
            all_tasks = []
        items = [t for t in all_tasks if t.is_active and (t.is_overdue(today) or t.is_due_on(today))]
        items.sort(key=lambda t: (not t.is_overdue(today), t.priority or ""))
        task_rows = [
            {
                "icon": "🔁" if t.is_routine else ("🧾" if t.is_bill else "☑️"),
                "title": t.title,
                "meta": " · ".join(x for x in [
                    "просрочено" if t.is_overdue(today) else "сегодня",
                    t.priority or "",
                    t.project or "",
                ] if x),
            }
            for t in items[:6]
        ]

        try:
            inbox_raw, inbox_total, undecided = vault.scan_inbox(settings, limit=8)
            inbox = [
                {
                    "title": vault.note_title(it.path),
                    "status": it.status,
                    "mtime": it.mtime,
                    "path": str(it.path),
                }
                for it in inbox_raw[:8]
            ]
        except Exception:  # noqa: BLE001
            inbox, inbox_total, undecided = [], 0, 0

        try:
            notes_raw = vault.scan_recent_notes(settings, days=7, limit=8)
            notes = [
                {
                    "title": vault.note_title(n.path),
                    "parent": n.path.parent.name,
                    "mtime": n.mtime,
                    "path": str(n.path),
                }
                for n in notes_raw[:8]
            ]
        except Exception:  # noqa: BLE001
            notes = []

        d = vault.latest_digest(settings)
        digest = (
            {"title": d.title, "preview": d.preview, "candidates": d.candidates, "path": str(d.path)}
            if d is not None else None
        )

        GLib.idle_add(
            self._apply_scan, task_rows, len(items), undecided, inbox_total, inbox, notes, digest,
        )

    def _clear(self, box: Gtk.Box) -> None:
        while (child := box.get_first_child()) is not None:
            box.remove(child)

    def _apply_scan(self, task_rows: list, total_items: int, undecided: int,
                     inbox_total: int, inbox_rows: list, note_rows: list, digest) -> bool:
        # Задачи — empty_state с CTA
        self.tasks_card._count.set_text(str(total_items))
        self._clear(self.tasks_box)
        if not task_rows:
            self.tasks_box.append(empty_state(
                "🎉", "На сегодня задач нет — можно заниматься чем угодно",
                action_label="Создать задачу", on_action=lambda: self.on_nav("tasks"),
            ))
        else:
            for r in task_rows:
                row = self._row(r["icon"], r["title"], r["meta"])
                row.add_css_class("glass-row")
                self.tasks_box.append(row)
            if total_items > 6:
                self.tasks_box.append(self._row("…", f"и ещё {total_items - 6}", ""))

        # Входящие — empty_state с CTA
        self._set_kpi("inbox", str(undecided), "warn" if undecided else "ok")
        self.inbox_card._count.set_text(f"{undecided} неразобрано · {inbox_total} всего")
        self._clear(self.inbox_box)
        if not inbox_rows:
            self.inbox_box.append(empty_state(
                "📭", "Входящие пусты — свежих материалов нет",
                action_label="Создать заметку", on_action=lambda: self.on_nav("files"),
            ))
        else:
            for it in inbox_rows:
                icon = "🕐" if it["status"] == "undecided" else "✓"
                tone = "warn" if it["status"] == "undecided" else "ok"
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                row.add_css_class("glass-row")
                row.set_hexpand(True)
                row.append(Gtk.Label(label=icon))
                lbl = Gtk.Label(label=it["title"], hexpand=True,
                                halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END)
                row.append(lbl)
                row.append(status_pill(it["status"], tone))
                row.append(Gtk.Label(label=_fmt_date(it["mtime"]), css_classes=["dim-hint"]))
                row.append(self._open_btn(Path(it["path"])))
                self.inbox_box.append(row)

        # Свежие заметки — empty_state с CTA
        self.notes_card._count.set_text(str(len(note_rows)))
        self._clear(self.notes_box)
        if not note_rows:
            self.notes_box.append(empty_state(
                "🗒", "За неделю новых заметок нет",
                action_label="Создать заметку", on_action=lambda: self.on_nav("files"),
            ))
        else:
            for n in note_rows:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                row.add_css_class("glass-row")
                row.set_hexpand(True)
                row.append(Gtk.Label(label="📄"))
                lbl = Gtk.Label(label=n["title"], hexpand=True,
                                halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END)
                row.append(lbl)
                row.append(Gtk.Label(label=f"{n['parent']} · {_age_label(n['mtime'])}", css_classes=["dim-hint"]))
                row.append(self._open_btn(Path(n["path"])))
                self.notes_box.append(row)

        # Дайджест — empty_state с CTA
        self._clear(self.digest_box)
        if digest is None:
            self.digest_box.append(empty_state(
                "🌙", "Дайджестов ночного пробега ещё нет",
                action_label="Создать заметку", on_action=lambda: self.on_nav("daily"),
            ))
        else:
            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            head.add_css_class("glass-row")
            head.append(Gtk.Label(label="📰"))
            title_lbl = Gtk.Label(label=digest["title"], hexpand=True, halign=Gtk.Align.START, xalign=0,
                                  ellipsize=Pango.EllipsizeMode.END, css_classes=["task-title"])
            head.append(title_lbl)
            head.append(status_pill(f"{digest['candidates']} кандидатов", "run"))
            head.append(self._open_btn(Path(digest["path"])))
            self.digest_box.append(head)
            if digest["preview"]:
                prev = Gtk.Label(label=digest["preview"], wrap=True, halign=Gtk.Align.START, xalign=0, css_classes=["dim-hint"])
                prev.set_margin_start(30)
                prev.set_margin_top(2)
                self.digest_box.append(prev)
        return False

    def _open_btn(self, path: Path) -> Gtk.Button:
        btn = Gtk.Button(label="Открыть", css_classes=["link-btn"])
        btn.connect("clicked", lambda *_, p=path: self._open(p))
        return btn

    def _row(self, icon: str, text: str, meta: str) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_hexpand(True)
        row.append(Gtk.Label(label=icon))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        box.append(Gtk.Label(label=text, halign=Gtk.Align.START, xalign=0, ellipsize=Pango.EllipsizeMode.END))
        if meta:
            box.append(Gtk.Label(label=meta, css_classes=["dim-hint"], halign=Gtk.Align.START, xalign=0))
        row.append(box)
        return row
