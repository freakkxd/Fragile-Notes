"""Аналитика vault (health-check) — дашборд метрик.

Метрики:
- word count (всего / среднее / медиана) — количество слов в .md
- links density — ссылок на заметку / на 1000 слов
- orphan notes — заметки без входящих и исходящих [[wikilink]]
- broken links — [[цель]] без существующего файла/title
- duplicate titles — одинаковый note_title (stem/frontmatter) у нескольких файлов
- large files — файлы > порога (по умолчанию 500 КБ)

Чистые функции — вне GTK, тестируются без дисплея.
UI — AnalyticsView как вкладка: hero, KPI-strip, списки, кнопка health-check.

Интеграция: зарегистрируй как вкладку "analytics" (VIEW_ICONS/TITLES, sidebar, app.VIEWS).
"""

from __future__ import annotations

import re
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from ..paths import resolve_paths  # noqa: E402
from ..vault import WIKILINK_RE  # noqa: E402
from .widgets import empty_state, status_pill, view_header  # noqa: E402

# ── пороги ───────────────────────────────────────────────────────────────

DEFAULT_LARGE_THRESHOLD_BYTES = 500 * 1024  # 500 КБ — large file
RESCAN_SECONDS = 15

_WORD_RE = re.compile(r"\S+")


# ── чистые функции ───────────────────────────────────────────────────────


def count_words(text: str) -> int:
    r"""Подсчёт слов: кол-во \S+ блоков. Пустая / frontmatter учитывается как текст."""
    if not text:
        return 0
    return len(_WORD_RE.findall(text))


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _strip_frontmatter(text: str) -> str:
    """Убрать YAML frontmatter для подсчёта слов/ссылок в теле."""
    try:
        from ..vault import FRONTMATTER_RE

        m = FRONTMATTER_RE.match(text)
        if m:
            return text[m.end() :]
    except Exception:
        pass
    return text


@dataclass(slots=True)
class FileStat:
    path: Path
    size: int
    words: int
    links: list[str] = field(default_factory=list)


@dataclass(slots=True)
class VaultAnalytics:
    """Снимок метрик vault — используется UI и health-check."""

    total_notes: int = 0
    total_words: int = 0
    avg_words: float = 0.0
    median_words: float = 0.0
    total_links: int = 0
    links_density_per_note: float = 0.0
    links_density_per_1k_words: float = 0.0
    orphan_notes: list[Path] = field(default_factory=list)
    broken_links: list[tuple[Path, str]] = field(default_factory=list)
    duplicate_titles: dict[str, list[Path]] = field(default_factory=dict)
    large_files: list[tuple[Path, int]] = field(default_factory=list)
    per_file: list[FileStat] = field(default_factory=list)
    # здоровье 0..100
    health_score: int = 100
    issues: list[dict[str, Any]] = field(default_factory=list)


def collect_analytics(
    settings: dict,
    large_threshold_bytes: int = DEFAULT_LARGE_THRESHOLD_BYTES,
) -> VaultAnalytics:
    """Сканирование vault и подсчёт всех метрик.

    Не бросает исключений наружу — возвращает пустой VaultAnalytics при ошибке пути.
    Кэш не используется (health-check должен видеть свежий диск), но опирается на
    лёгкие утилиты services.vault где возможно.
    """
    res = VaultAnalytics()
    try:
        root = resolve_paths(settings).root
    except Exception:
        try:
            root = Path(str(settings.get("vault_root") or Path.home() / "desktop"))
        except Exception:
            return res
    if not root.is_dir():
        return res

    # ── итерация по vault (os.scandir через services.vault._iter_notes если доступен)
    note_entries: list[tuple[str, float]] = []
    try:
        from ..services.vault import _iter_notes as _iter

        note_entries = list(_iter(root))
    except Exception:
        # fallback rglob
        for p in root.rglob("*.md"):
            if p.is_file():
                # пропуск heavy dirs вручную (node_modules etc.)
                try:
                    if any(
                        part
                        in {
                            ".git",
                            "node_modules",
                            "__pycache__",
                            ".obsidian",
                            "dist",
                            "build",
                            ".venv",
                        }
                        for part in p.parts
                    ):
                        continue
                    mt = p.stat().st_mtime
                except OSError:
                    mt = 0
                note_entries.append((str(p), mt))

    # фильтруем скрытые/тяжёлые уже в _iter, rglobFallback делает частично
    total_notes = len(note_entries)
    res.total_notes = total_notes
    if total_notes == 0:
        res.health_score = 100
        return res

    # ── карты для резолва ссылок: stem->Path, title->Path
    stem_map: dict[str, Path] = {}
    title_map: dict[str, Path] = {}
    # также нужен lower title для дубликатов
    title_groups: dict[str, list[Path]] = defaultdict(list)
    file_stats: list[FileStat] = []
    total_words = 0
    total_links = 0

    # Первый проход: собрать paths, titles, words, links raw
    # note_title_cached требует mtime кэш — используем прямую note_title для детерминизма
    try:
        from ..services.vault import note_title as _nt
    except Exception:

        def _nt(p: Path) -> str:  # type: ignore[no-redef]
            return p.stem

    # соберём список Path
    paths: list[Path] = []
    for p_str, _mt in note_entries:
        p = Path(p_str)
        if not p.is_file():
            continue
        paths.append(p)

    word_counts: list[int] = []
    # outgoing: source_str -> set(dest)
    outgoing: dict[str, set[str]] = defaultdict(set)
    # incoming count позже
    # сохраним распарсенные targets per file для broken
    per_file_targets: dict[str, list[str]] = {}
    # Также собираем raw ссылки
    for p in paths:
        raw = _read_text_safe(p)
        body = _strip_frontmatter(raw)
        wc = count_words(body)
        total_words += wc
        word_counts.append(wc)
        try:
            sz = p.stat().st_size
        except OSError:
            sz = 0
        # links
        try:
            targets = WIKILINK_RE.findall(body)
        except Exception:
            targets = []
        if not targets and "[[" in body:
            targets = re.findall(r"\[\[([^\]|#]+)", body)
        # нормализуем targets (strip)
        norm_targets = [t.strip() for t in targets if t and t.strip()]
        total_links += len(norm_targets)
        per_file_targets[str(p)] = norm_targets
        file_stats.append(FileStat(path=p, size=sz, words=wc, links=norm_targets))

    res.total_words = total_words
    res.per_file = file_stats
    if word_counts:
        res.avg_words = total_words / len(word_counts)
        s = sorted(word_counts)
        mid = len(s) // 2
        if len(s) % 2 == 1:
            res.median_words = float(s[mid])
        else:
            res.median_words = (s[mid - 1] + s[mid]) / 2.0
    else:
        res.avg_words = 0
        res.median_words = 0

    if total_notes:
        res.links_density_per_note = total_links / total_notes if total_notes else 0
    if total_words:
        res.links_density_per_1k_words = (total_links / total_words * 1000) if total_words else 0

    # ── карты резолва после того как есть title
    for p in paths:
        try:
            t = _nt(p)
        except Exception:
            t = p.stem
        low_stem = p.stem.lower()
        low_title = (t or p.stem).lower().strip()
        # first wins для резолва (как в graph_view)
        stem_map.setdefault(low_stem, p)
        title_map.setdefault(low_title, p)
        # для дубликатов группируем по low_title (и fallback low_stem если title == stem?)
        # используем именно title lower — дубликаты по отображаемому заголовку
        title_groups[low_title].append(p)

    # ── outgoing/incoming графы для orphan
    # резолвим
    for src_str, targets in per_file_targets.items():
        for tgt in targets:
            low = tgt.lower().strip()
            dest = stem_map.get(low)
            if dest is None:
                dest = title_map.get(low)
            if dest is None:
                continue
            dstr = str(dest)
            if dstr == src_str:
                continue
            outgoing[src_str].add(dstr)
    # incoming
    incoming: dict[str, set[str]] = defaultdict(set)
    for src, dests in outgoing.items():
        for d in dests:
            incoming[d].add(src)

    # ── orphan notes: degree == 0 (нет входящих и исходящих)
    orphans: list[Path] = []
    for p in paths:
        s = str(p)
        out_deg = len(outgoing.get(s, set()))
        in_deg = len(incoming.get(s, set()))
        if out_deg == 0 and in_deg == 0:
            orphans.append(p)
    # сортировка по имени для стабильности
    orphans.sort(key=lambda x: x.name.lower())
    res.orphan_notes = orphans

    # ── broken links: targets где резолв не нашёл файл
    broken: list[tuple[Path, str]] = []
    for src_str, targets in per_file_targets.items():
        src = Path(src_str)
        for tgt in targets:
            low = tgt.lower().strip()
            if low in stem_map or low in title_map:
                continue
            broken.append((src, tgt))
    # dedup на уровне (src, low_target) сохраняя первый вариант написания
    seen_broken: set[tuple[str, str]] = set()
    uniq_broken: list[tuple[Path, str]] = []
    for src, tgt in broken:
        key = (str(src), tgt.lower().strip())
        if key in seen_broken:
            continue
        seen_broken.add(key)
        uniq_broken.append((src, tgt))
    uniq_broken.sort(key=lambda x: (x[0].name.lower(), x[1].lower()))
    res.broken_links = uniq_broken

    # ── duplicate titles
    dups: dict[str, list[Path]] = {}
    for low_title, plist in title_groups.items():
        if len(plist) > 1:
            # ключ — отображаемый заголовок первого файла
            try:
                display = _nt(plist[0])
            except Exception:
                display = plist[0].stem
            # используем display как ключ, но гарантируем уникальность
            k = display.strip() or low_title
            # если коллизия ключа (разные low но одинаковый display) — объединим
            if k in dups:
                # merge
                existing = dups[k]
                for p in plist:
                    if p not in existing:
                        existing.append(p)
                dups[k] = sorted(existing, key=lambda x: str(x).lower())
            else:
                dups[k] = sorted(plist, key=lambda x: str(x).lower())
    # сортировка dict по ключу для детерминизма
    res.duplicate_titles = dict(sorted(dups.items(), key=lambda kv: kv[0].lower()))

    # ── large files
    large: list[tuple[Path, int]] = []
    for fs in file_stats:
        if fs.size >= large_threshold_bytes:
            large.append((fs.path, fs.size))
    large.sort(key=lambda x: x[1], reverse=True)
    res.large_files = large

    # ── health score (0..100) — эвристика
    # стартуем 100, штрафы:
    # orphan: -1 за каждый до -20
    # broken: -3 за каждый до -30
    # duplicates: -5 за группу до -20
    # large: -2 за файл до -15
    # links density слишком низкая (<0.1) — -10; пустые заметки >20% — -10
    score = 100
    score -= min(20, len(orphans) * 1)
    score -= min(30, len(uniq_broken) * 3)
    score -= min(20, len(dups) * 5)
    score -= min(15, len(large) * 2)
    if res.links_density_per_note < 0.1 and total_notes >= 5:
        score -= 10
    # пустые заметки (0 слов)
    empty_cnt = sum(1 for fs in file_stats if fs.words == 0)
    if total_notes and empty_cnt / total_notes > 0.2:
        score -= 10
    res.health_score = max(0, min(100, score))

    # ── issues для health-check карточки
    issues: list[dict[str, Any]] = []
    if orphans:
        issues.append(
            {
                "kind": "orphan",
                "severity": "warn",
                "count": len(orphans),
                "label": f"orphan notes: {len(orphans)}",
            }
        )
    if uniq_broken:
        issues.append(
            {
                "kind": "broken",
                "severity": "error",
                "count": len(uniq_broken),
                "label": f"broken links: {len(uniq_broken)}",
            }
        )
    if dups:
        dup_files = sum(len(v) for v in dups.values())
        issues.append(
            {
                "kind": "duplicate",
                "severity": "warn",
                "count": len(dups),
                "label": f"duplicate titles: {len(dups)} групп ({dup_files} файлов)",
            }
        )
    if large:
        issues.append(
            {
                "kind": "large",
                "severity": "warn",
                "count": len(large),
                "label": f"large files: {len(large)} > {large_threshold_bytes // 1024} КБ",
            }
        )
    if res.links_density_per_note < 0.1 and total_notes >= 5:
        issues.append(
            {
                "kind": "links",
                "severity": "info",
                "count": 1,
                "label": f"низкая плотность связей: {res.links_density_per_note:.2f} на заметку",
            }
        )
    if empty_cnt:
        issues.append(
            {
                "kind": "empty",
                "severity": "info",
                "count": empty_cnt,
                "label": f"пустых заметок: {empty_cnt}",
            }
        )
    res.issues = issues
    return res


def health_check(
    settings: dict, large_threshold_bytes: int = DEFAULT_LARGE_THRESHOLD_BYTES
) -> dict:
    """Health-check: собрать метрики и вернуть отчёт для UI/теста.

    Возвращает dict с ключами: analytics (VaultAnalytics), status (ok/warn/error),
    summary (str), issues.
    """
    a = collect_analytics(settings, large_threshold_bytes=large_threshold_bytes)
    # статус по score / наличию error-issue
    has_error = any(i.get("severity") == "error" and i.get("count", 0) > 0 for i in a.issues)
    if has_error or a.health_score < 60:
        status = "error"
    elif a.issues or a.health_score < 85:
        status = "warn"
    else:
        status = "ok"
    summary = f"health {a.health_score}/100 · {a.total_notes} заметок · {a.total_words} слов"
    if a.issues:
        summary += " · " + ", ".join(i["label"] for i in a.issues[:3])
        if len(a.issues) > 3:
            summary += f" +{len(a.issues) - 3}"
    return {
        "analytics": a,
        "status": status,
        "summary": summary,
        "issues": a.issues,
        "score": a.health_score,
    }


# ── UI ───────────────────────────────────────────────────────────────────


class AnalyticsView(Gtk.Box):
    """Вкладка Аналитика: метрики vault + кнопка health-check."""

    def __init__(self, settings: dict, on_open=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.settings = settings
        self.on_open = on_open
        self._last_scan = 0.0
        self._analytics: VaultAnalytics | None = None
        self._busy = False

        self.append(
            view_header(
                "📊",
                "Аналитика",
                "Здоровье vault · word count, плотность связей, orphan, битые ссылки, дубликаты, крупные файлы",
            )
        )

        self._build_toolbar()
        self._build_hero()
        self._build_kpi()

        # скролл для деталей
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.set_margin_start(14)
        body.set_margin_end(14)
        body.set_margin_bottom(16)
        body.set_margin_top(2)
        clamp = Adw.Clamp(maximum_size=960, tightening_threshold=640)
        clamp.set_child(body)
        scroller.set_child(clamp)

        # карточки деталей
        self._health_card, self._health_box = self._glass_card("🩺 Health-check", count="—")
        self._health_info = Gtk.Label(
            label="Нажми «Health-check» чтобы просканировать vault",
            halign=Gtk.Align.START,
            xalign=0,
            wrap=True,
            css_classes=["dim-hint"],
        )
        self._health_box.append(self._health_info)
        body.append(self._health_card)

        self._words_card, self._words_box = self._glass_card("📝 Слова и заметки")
        body.append(self._words_card)
        self._links_card, self._links_box = self._glass_card("🔗 Связи")
        body.append(self._links_card)
        self._orphan_card, self._orphan_box = self._glass_card("🕳 Orphan notes")
        body.append(self._orphan_card)
        self._broken_card, self._broken_box = self._glass_card("⛓ Broken links")
        body.append(self._broken_card)
        self._dup_card, self._dup_box = self._glass_card("📑 Duplicate titles")
        body.append(self._dup_card)
        self._large_card, self._large_box = self._glass_card("📦 Large files")
        body.append(self._large_card)

        self.append(scroller)
        # первичный скан в фоне
        GLib.idle_add(self._scan_async)

    # ── toolbar ────────────────────────────────────────────────────────
    def _build_toolbar(self) -> None:
        bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            css_classes=["toolbar", "analytics-toolbar"],
        )
        bar.set_margin_start(14)
        bar.set_margin_end(14)
        self._health_btn = Gtk.Button(
            label="🩺 Health-check",
            css_classes=["suggested-action", "mod-cta"],
            tooltip_text="Просканировать vault заново",
        )
        self._health_btn.connect("clicked", lambda *_: self._on_health_check())
        bar.append(self._health_btn)
        self._refresh_btn = Gtk.Button(
            icon_name="view-refresh-symbolic", tooltip_text="Обновить метрики"
        )
        self._refresh_btn.connect("clicked", lambda *_: self._scan_async(force=True))
        bar.append(self._refresh_btn)
        self._spinner = Gtk.Spinner(spinning=False, visible=False)
        bar.append(self._spinner)
        self._status_lbl = Gtk.Label(
            label="",
            css_classes=["dim-hint"],
            hexpand=True,
            halign=Gtk.Align.START,
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
        )
        bar.append(self._status_lbl)
        self.append(bar)

    def _build_hero(self) -> None:
        hero = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=3,
            css_classes=["tm-hero-v2", "tm-hero-v2--dash"],
        )
        hero.append(
            Gtk.Label(
                label="Здоровье vault",
                css_classes=["tm-hero-v2__eyebrow"],
                halign=Gtk.Align.START,
                xalign=0,
            )
        )
        self._hero_title = Gtk.Label(
            label="…", css_classes=["tm-hero-v2__title"], halign=Gtk.Align.START, xalign=0
        )
        hero.append(self._hero_title)
        self._hero_sub = Gtk.Label(
            label="", css_classes=["tm-hero-v2__date"], halign=Gtk.Align.START, xalign=0
        )
        hero.append(self._hero_sub)
        try:
            root = Path(str(self.settings.get("vault_root") or ""))
            hero.set_tooltip_text(str(root))
        except Exception:
            pass
        self.append(hero)

    def _build_kpi(self) -> None:
        strip = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["kpi-strip"]
        )
        strip.set_vexpand(False)
        self._kpi: dict[str, tuple[Gtk.Label, Gtk.Box]] = {}
        for key, label in [
            ("notes", "📝 заметок"),
            ("words", "🔤 слов"),
            ("links", "🔗 связей/заметку"),
            ("orphan", "🕳 orphan"),
            ("broken", "⛓ битых"),
            ("dup", "📑 дублей"),
            ("large", "📦 крупных"),
        ]:
            chip = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=1, css_classes=["kpi-chip"]
            )
            val = Gtk.Label(
                label="…", css_classes=["kpi-chip__value"], halign=Gtk.Align.START, xalign=0
            )
            cap = Gtk.Label(
                label=label, css_classes=["kpi-chip__label"], halign=Gtk.Align.START, xalign=0
            )
            chip.append(val)
            chip.append(cap)
            strip.append(chip)
            self._kpi[key] = (val, chip)
        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC, vscrollbar_policy=Gtk.PolicyType.NEVER
        )
        scroller.set_child(strip)
        scroller.set_vexpand(False)
        self.append(scroller)

    def _glass_card(self, title: str, count: str | None = None) -> tuple[Gtk.Box, Gtk.Box]:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["glass-card"])
        head = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["glass-card__head"]
        )
        head.set_vexpand(False)
        head.append(Gtk.Label(label=title, css_classes=["glass-card__title"]))
        count_lbl = Gtk.Label(label=count or "", css_classes=["glass-card__count"])
        count_lbl.set_vexpand(False)
        head.append(count_lbl)
        card._head_row = head  # type: ignore[attr-defined]
        card._count = count_lbl  # type: ignore[attr-defined]
        card.append(head)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        body.set_margin_top(4)
        body.set_margin_bottom(6)
        body.set_margin_start(10)
        body.set_margin_end(10)
        body.set_vexpand(True)
        card.append(body)
        return card, body

    # ── логика ─────────────────────────────────────────────────────────
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._health_btn.set_sensitive(not busy)
        self._refresh_btn.set_sensitive(not busy)
        self._spinner.set_visible(busy)
        self._spinner.set_spinning(busy)
        if busy:
            self._status_lbl.set_text("Сканирую vault…")
            self._health_card.add_css_class("glass-card--emphasis")
        else:
            self._health_card.remove_css_class("glass-card--emphasis")

    def _on_health_check(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._health_info.set_text("⏳ Выполняю health-check…")
        settings_copy = dict(self.settings)
        # инвалидируем кэш чтобы увидеть свежий диск
        try:
            from ..services import vault as _vault

            _vault.invalidate_vault_cache()
        except Exception:
            pass

        def work() -> None:
            try:
                report = health_check(settings_copy)
                analytics = report["analytics"]
                GLib.idle_add(lambda: self._apply_report(report, analytics) or False)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc) or "ошибка health-check"
                GLib.idle_add(lambda: self._apply_error(msg) or False)

        threading.Thread(target=work, daemon=True).start()

    def _scan_async(self, force: bool = False) -> bool:
        if self._busy:
            return False
        now = GLib.get_monotonic_time() / 1_000_000 if hasattr(GLib, "get_monotonic_time") else 0
        # throttle без force — не чаще RESCAN_SECONDS
        if not force and self._last_scan and now and now - self._last_scan < RESCAN_SECONDS:
            return False
        self._last_scan = now if now else 0
        self._set_busy(True)
        settings_copy = dict(self.settings)
        if force:
            try:
                from ..services import vault as _vault

                _vault.invalidate_vault_cache()
            except Exception:
                pass

        def work() -> None:
            try:
                a = collect_analytics(settings_copy)
                GLib.idle_add(lambda: self._apply_analytics(a) or False)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(lambda exc=exc: self._apply_error(str(exc)) or False)

        threading.Thread(target=work, daemon=True).start()
        return False

    def _apply_error(self, msg: str) -> bool:
        self._set_busy(False)
        self._status_lbl.set_text(msg)
        self._health_info.set_text(msg)
        self._hero_title.set_text("Ошибка сканирования")
        self._hero_sub.set_text(msg[:120])
        return False

    def _apply_report(self, report: dict, a: VaultAnalytics) -> bool:
        self._set_busy(False)
        self._analytics = a
        summary = report.get("summary", "")
        status = report.get("status", "idle")
        self._status_lbl.set_text(summary)
        # toast
        self._toast(summary)
        self._apply_analytics(a, status_hint=status)
        return False

    def _apply_analytics(self, a: VaultAnalytics, status_hint: str | None = None) -> bool:
        self._set_busy(False)
        self._analytics = a
        # hero
        score = a.health_score
        status = status_hint or ("ok" if score >= 85 else "warn" if score >= 60 else "error")
        tone = (
            {"ok": "здоров", "warn": "требует внимания", "error": "проблемы"}[status]
            if status in ("ok", "warn", "error")
            else status
        )
        self._hero_title.set_text(f"Health {score}/100 · {tone}")
        self._hero_sub.set_text(
            f"{a.total_notes} заметок · {a.total_words} слов · avg {a.avg_words:.0f} / median {a.median_words:.0f} · "
            f"связей {a.total_links} · плотность {a.links_density_per_note:.2f}/заметку ({a.links_density_per_1k_words:.1f}/1k слов)"
        )
        # hero tone класс
        for cls in ("tm-hero-v2--ok", "tm-hero-v2--warn", "tm-hero-v2--error"):
            try:
                self._hero_title.get_parent().remove_css_class(cls)  # type: ignore[union-attr]
            except Exception:
                pass
        try:
            self._hero_title.get_parent().add_css_class(f"tm-hero-v2--{status}")  # type: ignore[union-attr]
        except Exception:
            pass

        # KPI
        self._set_kpi("notes", str(a.total_notes), "idle")
        self._set_kpi("words", str(a.total_words), "idle")
        self._set_kpi(
            "links",
            f"{a.links_density_per_note:.2f}",
            "ok"
            if a.links_density_per_note >= 0.5
            else "warn"
            if a.links_density_per_note >= 0.1
            else "error",
        )
        self._set_kpi("orphan", str(len(a.orphan_notes)), "error" if a.orphan_notes else "ok")
        self._set_kpi("broken", str(len(a.broken_links)), "error" if a.broken_links else "ok")
        self._set_kpi("dup", str(len(a.duplicate_titles)), "error" if a.duplicate_titles else "ok")
        self._set_kpi("large", str(len(a.large_files)), "warn" if a.large_files else "ok")

        # карточки деталей
        self._render_health(a, status)
        self._render_words(a)
        self._render_links(a)
        self._render_orphan(a)
        self._render_broken(a)
        self._render_dup(a)
        self._render_large(a)

        # статус бар
        if not getattr(self, "_status_lbl", None) or not self._status_lbl.get_text():
            self._status_lbl.set_text(f"готово · health {score}/100")
        return False

    def _set_kpi(self, key: str, value: str, tone: str) -> None:
        val, chip = self._kpi[key]
        val.set_text(value)
        for cls in (
            "kpi-chip--ok",
            "kpi-chip--error",
            "kpi-chip--warn",
            "kpi-chip--run",
            "kpi-chip--idle",
        ):
            chip.remove_css_class(cls)
        chip.add_css_class(f"kpi-chip--{tone}")

    # ── рендер карточек ────────────────────────────────────────────────
    def _clear(self, box: Gtk.Box) -> None:
        while (child := box.get_first_child()) is not None:
            box.remove(child)

    def _render_health(self, a: VaultAnalytics, status: str) -> None:
        self._clear(self._health_box)
        # заново добавляем info label (был удалён clear)
        self._health_info = Gtk.Label(
            label="", halign=Gtk.Align.START, xalign=0, wrap=True, css_classes=["dim-hint"]
        )
        self._health_box.append(self._health_info)
        self._health_card._count.set_text(f"{a.health_score}/100 · {status}")  # type: ignore[attr-defined]
        if not a.issues:
            self._health_info.set_text("✓ Vault здоров — проблем не обнаружено")
            self._health_box.append(status_pill("ok", "ok"))
        else:
            pills = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=6, css_classes=["health-pills"]
            )
            pills.set_margin_top(4)
            for iss in a.issues:
                tone = {"error": "error", "warn": "warn", "info": "idle"}.get(
                    iss.get("severity", "idle"), "idle"
                )
                pills.append(status_pill(iss.get("label", ""), tone))
            self._health_box.append(pills)
            self._health_info.set_text(" · ".join(i["label"] for i in a.issues))
        if status == "error":
            self._health_card.add_css_class("glass-card--emphasis")
        else:
            self._health_card.remove_css_class("glass-card--emphasis")

    def _render_words(self, a: VaultAnalytics) -> None:
        self._clear(self._words_box)
        self._words_card._count.set_text(f"{a.total_notes} файлов")  # type: ignore[attr-defined]
        if a.total_notes == 0:
            self._words_box.append(
                empty_state("📭", "Vault пуст", hint="Создай заметку — метрики появятся здесь")
            )
            return
        # краткая сводка
        summary = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12, css_classes=["analytics-summary"]
        )
        for label, value in [
            ("всего слов", str(a.total_words)),
            ("avg/заметку", f"{a.avg_words:.0f}"),
            ("median", f"{a.median_words:.0f}"),
            ("крупнейший", str(max((fs.words for fs in a.per_file), default=0))),
        ]:
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            col.append(
                Gtk.Label(
                    label=value, css_classes=["kpi-chip__value"], halign=Gtk.Align.START, xalign=0
                )
            )
            col.append(
                Gtk.Label(
                    label=label, css_classes=["kpi-chip__label"], halign=Gtk.Align.START, xalign=0
                )
            )
            summary.append(col)
        self._words_box.append(summary)
        # топ крупнейших по словам (5)
        top = sorted(a.per_file, key=lambda fs: fs.words, reverse=True)[:5]
        if top:
            self._words_box.append(
                Gtk.Label(
                    label="Крупнейшие по словам:",
                    css_classes=["section-title"],
                    halign=Gtk.Align.START,
                    xalign=0,
                )
            )
            for fs in top:
                row = Gtk.Box(
                    orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["glass-row"]
                )
                row.set_hexpand(True)
                row.append(Gtk.Label(label="📄"))
                title = self._display_title(fs.path)
                lbl = Gtk.Label(
                    label=title,
                    hexpand=True,
                    halign=Gtk.Align.START,
                    xalign=0,
                    ellipsize=Pango.EllipsizeMode.END,
                    css_classes=["task-title"],
                )
                row.append(lbl)
                row.append(
                    Gtk.Label(
                        label=f"{fs.words} слов · {fs.size // 1024} КБ", css_classes=["dim-hint"]
                    )
                )
                row.append(self._open_btn(fs.path))
                self._words_box.append(row)

    def _render_links(self, a: VaultAnalytics) -> None:
        self._clear(self._links_box)
        self._links_card._count.set_text(f"{a.total_links} ссылок")  # type: ignore[attr-defined]
        if a.total_notes == 0:
            self._links_box.append(Gtk.Label(label="нет данных", css_classes=["dim-hint"]))
            return
        info = Gtk.Label(
            label=f"плотность: {a.links_density_per_note:.2f} на заметку · {a.links_density_per_1k_words:.1f} на 1000 слов  ·  изолированных (orphan) {len(a.orphan_notes)} · битых {len(a.broken_links)}",
            halign=Gtk.Align.START,
            xalign=0,
            wrap=True,
            css_classes=["dim-hint"],
        )
        info.set_margin_top(2)
        self._links_box.append(info)
        # шкала: простая прогресс-бар имитация через pill
        tone = (
            "ok"
            if a.links_density_per_note >= 0.5
            else "warn"
            if a.links_density_per_note >= 0.1
            else "error"
        )
        self._links_box.append(status_pill(f"плотность {a.links_density_per_note:.2f}", tone))

    def _render_orphan(self, a: VaultAnalytics) -> None:
        self._clear(self._orphan_box)
        n = len(a.orphan_notes)
        self._orphan_card._count.set_text(str(n))  # type: ignore[attr-defined]
        if n == 0:
            self._orphan_box.append(
                empty_state(
                    "✨",
                    "Orphan нет — все заметки связаны",
                    hint="Orphan = 0 входящих и 0 исходящих [[wikilink]]",
                )
            )
            return
        self._orphan_box.append(
            Gtk.Label(
                label="Без связей (0 входящих и 0 исходящих):",
                css_classes=["dim-hint"],
                halign=Gtk.Align.START,
                xalign=0,
            )
        )
        for p in a.orphan_notes[:20]:
            row = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["glass-row"]
            )
            row.set_hexpand(True)
            row.append(Gtk.Label(label="🕳"))
            title = self._display_title(p)
            lbl = Gtk.Label(
                label=title,
                hexpand=True,
                halign=Gtk.Align.START,
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
            )
            row.append(lbl)
            row.append(Gtk.Label(label=p.parent.name, css_classes=["dim-hint"]))
            row.append(self._open_btn(p))
            self._orphan_box.append(row)
        if n > 20:
            self._orphan_box.append(
                Gtk.Label(
                    label=f"и ещё {n - 20}…",
                    css_classes=["dim-hint"],
                    halign=Gtk.Align.START,
                    xalign=0,
                )
            )

    def _render_broken(self, a: VaultAnalytics) -> None:
        self._clear(self._broken_box)
        n = len(a.broken_links)
        self._broken_card._count.set_text(str(n))  # type: ignore[attr-defined]
        if n == 0:
            self._broken_box.append(empty_state("✓", "Битых ссылок нет"))
            return
        for src, tgt in a.broken_links[:20]:
            row = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["glass-row"]
            )
            row.set_hexpand(True)
            row.append(Gtk.Label(label="⛓"))
            src_lbl = Gtk.Label(
                label=self._display_title(src),
                hexpand=True,
                halign=Gtk.Align.START,
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
            )
            row.append(src_lbl)
            row.append(Gtk.Label(label="→", css_classes=["dim-hint"]))
            row.append(status_pill(f"[[{tgt}]]", "error"))
            row.append(self._open_btn(src))
            self._broken_box.append(row)
        if n > 20:
            self._broken_box.append(
                Gtk.Label(
                    label=f"и ещё {n - 20}…",
                    css_classes=["dim-hint"],
                    halign=Gtk.Align.START,
                    xalign=0,
                )
            )

    def _render_dup(self, a: VaultAnalytics) -> None:
        self._clear(self._dup_box)
        n = len(a.duplicate_titles)
        self._dup_card._count.set_text(str(n))  # type: ignore[attr-defined]
        if n == 0:
            self._dup_box.append(empty_state("✓", "Дубликатов заголовков нет"))
            return
        for title, plist in list(a.duplicate_titles.items())[:12]:
            grp = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["glass-row"]
            )
            grp.set_hexpand(True)
            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            head.append(Gtk.Label(label="📑"))
            head.append(
                Gtk.Label(
                    label=title,
                    hexpand=True,
                    halign=Gtk.Align.START,
                    xalign=0,
                    ellipsize=Pango.EllipsizeMode.END,
                    css_classes=["task-title"],
                )
            )
            head.append(status_pill(f"{len(plist)} файла", "warn"))
            grp.append(head)
            for p in plist:
                r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                r.set_margin_start(18)
                r.append(Gtk.Label(label="—", css_classes=["dim-hint"]))
                lbl = Gtk.Label(
                    label=str(
                        p.relative_to(Path(str(self.settings.get("vault_root"))))
                        if _is_relative_to(p, Path(str(self.settings.get("vault_root"))))
                        else p.name
                    ),
                    hexpand=True,
                    halign=Gtk.Align.START,
                    xalign=0,
                    ellipsize=Pango.EllipsizeMode.MIDDLE,
                    css_classes=["dim-hint"],
                )
                r.append(lbl)
                r.append(self._open_btn(p))
                grp.append(r)
            self._dup_box.append(grp)
        if n > 12:
            self._dup_box.append(
                Gtk.Label(
                    label=f"и ещё {n - 12} групп…",
                    css_classes=["dim-hint"],
                    halign=Gtk.Align.START,
                    xalign=0,
                )
            )

    def _render_large(self, a: VaultAnalytics) -> None:
        self._clear(self._large_box)
        n = len(a.large_files)
        self._large_card._count.set_text(str(n))  # type: ignore[attr-defined]
        if n == 0:
            self._large_box.append(
                empty_state(
                    "✓",
                    "Крупных файлов нет",
                    hint=f"порог {DEFAULT_LARGE_THRESHOLD_BYTES // 1024} КБ",
                )
            )
            return
        self._large_box.append(
            Gtk.Label(
                label=f"Порог {DEFAULT_LARGE_THRESHOLD_BYTES // 1024} КБ — крупнейшие сверху:",
                css_classes=["dim-hint"],
                halign=Gtk.Align.START,
                xalign=0,
            )
        )
        for p, sz in a.large_files[:20]:
            row = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=8, css_classes=["glass-row"]
            )
            row.set_hexpand(True)
            row.append(Gtk.Label(label="📦"))
            title = self._display_title(p)
            lbl = Gtk.Label(
                label=title,
                hexpand=True,
                halign=Gtk.Align.START,
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
            )
            row.append(lbl)
            row.append(Gtk.Label(label=f"{sz / 1024:.0f} КБ", css_classes=["dim-hint"]))
            row.append(self._open_btn(p))
            self._large_box.append(row)
        if n > 20:
            self._large_box.append(
                Gtk.Label(
                    label=f"и ещё {n - 20}…",
                    css_classes=["dim-hint"],
                    halign=Gtk.Align.START,
                    xalign=0,
                )
            )

    # ── helpers ────────────────────────────────────────────────────────
    def _display_title(self, path: Path) -> str:
        try:
            from ..services.vault import note_title_cached as _ntc

            return _ntc(path)
        except Exception:
            return path.stem

    def _open_btn(self, path: Path) -> Gtk.Button:
        btn = Gtk.Button(label="Открыть", css_classes=["link-btn"])
        btn.connect("clicked", lambda *_: self._open_path(path))
        return btn

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

    def _toast(self, msg: str) -> None:
        try:
            win = self.get_root()
            if win is not None and hasattr(win, "toast_overlay"):
                toast = Adw.Toast.new(msg)
                toast.set_timeout(3)
                win.toast_overlay.add_toast(toast)  # type: ignore[attr-defined]
        except Exception:
            pass

    # для внешнего refresh_all — alias
    def refresh(self) -> None:
        self._scan_async(force=True)


def _is_relative_to(p: Path, base: Path) -> bool:
    try:
        p.relative_to(base)
        return True
    except Exception:
        return False


__all__ = [
    "AnalyticsView",
    "VaultAnalytics",
    "FileStat",
    "collect_analytics",
    "health_check",
    "count_words",
    "DEFAULT_LARGE_THRESHOLD_BYTES",
]
