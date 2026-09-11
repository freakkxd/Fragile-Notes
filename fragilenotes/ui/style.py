"""Загрузка CSS-стилей приложения (дизайн-токены AO Glass — 1в1 с Obsidian).

Порт дизайн-системы «Fragilich Glass» (ao-glass-tokens/components/shell + tm-ui-kit)
из vault/.obsidian на GTK4. GTK не поддерживает backdrop-filter и размытые тени,
поэтому «стекло» имитируется полупрозрачными поверхностями, градиентами-светом
и тонкими внутренними бликами (inset box-shadow).
"""

from __future__ import annotations

import re
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gtk  # noqa: E402

CSS = """
/* ═══ Токены (ao-glass-tokens.css → :root) ═══ */
window {
    /* Фоны: три слоя глубины — канвас / панели / стекло */
    --ao-bg-deep: #06080d;
    --ao-bg-elevated: #0a0e16;
    --ao-bg-pane: #070a12;
    --ao-surface-glass: rgba(15, 19, 30, 0.84);
    --ao-surface-glass-hover: rgba(25, 31, 46, 0.97);
    --ao-surface-glass-soft: rgba(255, 255, 255, 0.035);
    --ao-surface-control: rgba(255, 255, 255, 0.05);
    --ao-surface-control-hover: rgba(255, 255, 255, 0.1);
    --ao-surface-row-hover: rgba(255, 255, 255, 0.05);
    --ao-surface-panel: rgba(10, 14, 22, 0.92);
    --ao-surface-panel-elevated: rgba(14, 18, 28, 0.96);
    --ao-surface-inset: rgba(0, 0, 0, 0.3);
    --ao-highlight: rgba(255, 255, 255, 0.045);

    /* Границы */
    --ao-border-subtle: rgba(255, 255, 255, 0.07);
    --ao-border-strong: rgba(255, 255, 255, 0.13);

    /* Акцент (rgb-триплеты, как в aos-glass-tokens) */
    --ao-accent-focus: 130, 168, 255;
    --ao-accent-violet: 190, 165, 255;
    --ao-accent-cyan: 120, 200, 220;
    --ao-accent-success: 120, 210, 150;
    --ao-accent-warning: 230, 175, 110;
    --ao-accent-danger: 220, 130, 145;
    --ao-accent-neutral: 155, 165, 190;
    --ao-selected: rgba(130, 168, 255, 0.16);
    --ao-selected-border: rgba(130, 168, 255, 0.38);
    --ao-focus-ring: 0 0 0 2px rgba(130, 168, 255, 0.35);

    /* Текст */
    --ao-text: rgba(228, 234, 246, 0.94);
    --ao-text-secondary: rgba(210, 218, 232, 0.88);
    --ao-text-muted: rgba(175, 186, 204, 0.72);
    --ao-text-faint: rgba(140, 152, 172, 0.55);

    --ao-radius-xs: 6px;
    --ao-radius-sm: 9px;
    --ao-radius-md: 12px;
    --ao-radius-lg: 14px;
    --ao-radius-xl: 18px;
    --ao-radius-pill: 999px;

    /* Ритм отступов (4px база) */
    --ao-space-1: 4px;
    --ao-space-2: 8px;
    --ao-space-3: 12px;
    --ao-space-4: 16px;
    --ao-space-5: 20px;

    /* Глубина и переходы (карточка/подъём) */
    --ao-shadow-card: inset 0 1px 0 rgba(255, 255, 255, 0.05), 0 8px 24px rgba(0, 0, 0, 0.22);
    --ao-shadow-lift: 0 10px 32px rgba(0, 0, 0, 0.28);
    --ao-transition-fast: 120ms ease;

    background-color: var(--ao-bg-pane);
    color: var(--ao-text-secondary);
    font-family: "Inter", "InterVariable", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    font-size: 15px;
    line-height: 1.5;
}

/* ── Canvas Obsidian: ambient glow (theme.css §3) + deep-канвас ── */
.fragile-shell {
    background-color: var(--ao-bg-deep);
    background-image: radial-gradient(120% 80% at 10% -10%, rgba(130, 168, 255, 0.05), transparent 50%),
        radial-gradient(70% 60% at 100% 0%, rgba(190, 165, 255, 0.045), transparent 55%);
    color: var(--ao-text-secondary);
}

/* ── Ribbon (theme.css §4 → .workspace-ribbon) ─────────────── */
/* Иконки прозрачны: единый фон рисует левая колонка side-column,
   поэтому панель сайдбара и иконки — одна поверхность без швов. */
.workspace-ribbon {
    background-color: transparent;
    border-right: none;
    padding: 6px 4px;
    min-width: 44px;
}
.ribbon-btn {
    min-width: 36px;
    min-height: 36px;
    padding: 0;
    border-radius: var(--ao-radius-xs);
    border: 1px solid transparent;
    background-color: transparent;
    color: var(--ao-text-muted);
    font-size: 16px;
    transition: background var(--ao-transition-fast), color var(--ao-transition-fast);
}
.ribbon-btn label {
    min-height: 0;
    padding: 0;
}
.ribbon-btn:hover {
    background-color: var(--ao-surface-control-hover);
    color: var(--ao-text);
}
.ribbon-btn:checked {
    background-color: rgba(130, 168, 255, 0.14);
    color: rgba(130, 168, 255, 1);
}
.ribbon-sep {
    min-height: 1px;
    background-color: var(--ao-border-subtle);
    margin: 8px 8px;
}

/* ── Shell: левая колонка (иконки + сайдбар) как одна поверхность ── */
/* .side-column — единый фон + правая кромка от контента (как Obsidian:
   тонкий ribbon и панель выглядят одной колонкой, без слоёв). */
.side-column {
    background-color: rgba(10, 14, 22, 0.92);
    background-image: linear-gradient(180deg, rgba(255, 255, 255, 0.02), transparent 150px);
    border-right: 1px solid var(--ao-border-subtle);
    min-width: 44px;
    transition: min-width 180ms ease;
}
.side-column.expanded {
    min-width: 232px;
}

/* ── Paned handle — тонкая ручка Obsidian (drag мышью) ── */
paned.workspace-paned {
    background: transparent;
}
paned.workspace-paned > separator {
    min-width: 6px;
    background: rgba(255, 255, 255, 0.02);
    border-left: 1px solid var(--ao-border-subtle);
    margin: 0;
    transition: background 140ms ease;
}
paned.workspace-paned > separator:hover {
    background: rgba(130, 168, 255, 0.14);
    box-shadow: inset 1px 0 0 rgba(130, 168, 255, 0.32);
}
paned.workspace-paned > separator:active {
    background: rgba(130, 168, 255, 0.22);
    box-shadow: inset 1px 0 0 rgba(130, 168, 255, 0.5);
}
.sidebar {
    background-color: transparent;
    background-image: none;
    border-right: none;
    min-width: 0;
}
.sb-logo {
    font-size: 1.15em;
}
.sb-appname {
    font-size: 0.95em;
    font-weight: 650;
    color: var(--ao-text);
    letter-spacing: 0.01em;
}
.sb-head {
    min-height: 96px;
}
.sb-vault {
    font-size: 0.78em;
    color: var(--ao-text-faint);
}
.nav-list {
    padding-top: 4px;
    background-color: transparent;
}
.nav-list row.nav-item {
    min-height: 36px;
    border-radius: var(--ao-radius-sm);
    margin: 1px 8px;
    padding: 0;
    color: var(--ao-text-muted);
    background-color: transparent;
    border: 1px solid transparent;
    transition: background var(--ao-transition-fast), color var(--ao-transition-fast);
}
.nav-list row.sb-section-row {
    min-height: 17px;
    margin: 0 8px;
    padding: 0;
    background-color: transparent;
}
.nav-list row:hover {
    background-color: var(--ao-surface-row-hover);
    color: var(--ao-text-secondary);
}
.nav-list row:selected {
    background-image: linear-gradient(135deg, rgba(130, 168, 255, 0.16), rgba(130, 168, 255, 0.06));
    border: 1px solid rgba(130, 168, 255, 0.22);
    box-shadow: inset 2px 0 0 rgba(130, 168, 255, 0.7);
    color: var(--ao-text);
}
.nav-list row:selected label {
    color: var(--ao-text);
}
.sb-nav-icon-box {
    min-width: 28px;
    min-height: 28px;
}
.sb-nav-icon {
    font-size: 15px;
}
.sb-nav-text {
    font-size: 0.92em;
    font-weight: 500;
}
.sb-section {
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ao-text-faint);
    padding: 4px 0;
    margin: 0 8px;
    min-height: 9px;
}
.sb-footer {
    font-size: 0.72em;
    color: var(--ao-text-faint);
    margin: 8px 10px;
}

/* ── Файловое дерево (ao-glass-shell → .nav-files-container / обсидиановский tree) ── */
treeview.view {
    background-color: transparent;
    color: var(--ao-text-muted);
    border: none;
    outline: none;
}
treeview.view row {
    padding: 5px 8px 5px 6px;
    min-height: 28px;
    border-radius: var(--ao-radius-sm);
    border: 1px solid transparent;
}
treeview.view row:hover {
    background-color: var(--ao-surface-row-hover);
    color: var(--ao-text-secondary);
}
treeview.view row:selected {
    background-image: linear-gradient(135deg, rgba(130, 168, 255, 0.16), rgba(130, 168, 255, 0.06));
    border: 1px solid rgba(130, 168, 255, 0.22);
    box-shadow: inset 2px 0 0 rgba(130, 168, 255, 0.7);
    color: var(--ao-text);
}
treeview.view:selected {
    color: var(--ao-text);
}
treeview.view row:selected:active {
    background-image: linear-gradient(135deg, rgba(130, 168, 255, 0.26), rgba(130, 168, 255, 0.12));
}
treeview.view expander {
    color: var(--ao-text-faint);
    margin: 0 2px;
}
treeview.view expander:hover {
    color: var(--ao-text-muted);
}

/* ── Shell: статус-бар (ao-glass-shell → .status-bar, во всю ширину) ── */
.sb {
    background-color: rgba(10, 14, 22, 0.92);
    border-top: 1px solid var(--ao-border-subtle);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
    padding: 2px 14px;
    min-height: 24px;
}
.sb-op {
    font-size: 0.72em;
    color: var(--ao-text-faint);
}
.sb-chip-ok {
    border-color: rgba(34, 197, 94, 0.4);
    background-color: rgba(34, 197, 94, 0.14);
    color: #4ade80;
}
.sb-chip-error {
    border-color: rgba(239, 68, 68, 0.45);
    background-color: rgba(239, 68, 68, 0.14);
    color: #f87171;
}
.sb-chip-warn {
    border-color: rgba(234, 179, 8, 0.45);
    background-color: rgba(234, 179, 8, 0.14);
    color: #facc15;
}
.sb-chip-run {
    border-color: rgba(130, 168, 255, 0.4);
    background-color: rgba(130, 168, 255, 0.12);
    color: var(--ao-accent-focus);
}
.sb-llm-row {
    font-size: 0.9em;
    color: var(--ao-text-secondary);
}

/* ═══ Общие элементы (ao-glass-shell → .view-header leaf) ═══ */
.view-header {
    border-bottom: 1px solid var(--ao-border-subtle);
    background-color: rgba(255, 255, 255, 0.015);
    padding: 8px 18px;
}
.view-emoji {
    font-size: 1.1em;
}
.view-title {
    font-size: 0.94em;
    font-weight: 600;
    letter-spacing: 0.01em;
    color: var(--ao-text);
}
.view-sub {
    font-size: 0.78em;
    color: var(--ao-text-muted);
}

.section-title {
    font-size: 0.72em;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--ao-text-muted);
    padding: 2px 4px;
}

/* ═══ Карточки (ao-glass-components → .ao-glass-card) ═══ */
.card {
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
    padding: 12px 14px;
    background-color: var(--ao-surface-glass);
    box-shadow: inset 0 1px 0 var(--ao-highlight);
}
.card-hover {
    border: 1px solid var(--ao-border-strong);
    background-color: var(--ao-surface-glass-hover);
}

/* ═══ Пилюли (ao-pill canon) ═══ */
.pill,
.sb-chip,
.glass-card__count {
    border-radius: var(--ao-radius-pill);
    padding: 2px 10px;
    font-size: 0.74em;
    font-weight: 600;
    letter-spacing: 0.04em;
    border: 1px solid var(--ao-border-subtle);
    background-color: var(--ao-surface-control);
    color: var(--ao-text-secondary);
}
.pill-ok {
    border-color: rgba(120, 210, 150, 0.35);
    background-color: rgba(120, 210, 150, 0.12);
    color: var(--ao-accent-success);
}
.pill-error {
    border-color: rgba(220, 130, 145, 0.45);
    background-color: rgba(220, 130, 145, 0.12);
    color: var(--ao-accent-danger);
}
.pill-run {
    border-color: rgba(130, 168, 255, 0.4);
    background-color: rgba(130, 168, 255, 0.12);
    color: var(--ao-accent-focus);
}
.pill-warn {
    border-color: rgba(230, 175, 110, 0.4);
    background-color: rgba(230, 175, 110, 0.12);
    color: var(--ao-accent-warning);
}

/* ── Плоский поиск заметок (ao-glass-popups → suggestion-item) ── */
.file-list {
    background-color: transparent;
}
.file-list row {
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: var(--ao-radius-sm);
    margin: 1px 6px;
    padding: 1px 4px;
    color: var(--ao-text-muted);
    transition: background var(--ao-transition-fast), color var(--ao-transition-fast);
}
.file-list row:hover {
    background-color: var(--ao-surface-row-hover);
    color: var(--ao-text-secondary);
}
.file-list row:selected {
    background-image: linear-gradient(135deg, rgba(130, 168, 255, 0.16), rgba(130, 168, 255, 0.06));
    border: 1px solid rgba(130, 168, 255, 0.22);
    box-shadow: inset 2px 0 0 rgba(130, 168, 255, 0.7);
    color: var(--ao-text);
}
.file-list row:selected label {
    color: var(--ao-text);
}

/* ═══ Кнопки (ao-glass-controls) ═══ */
button {
    border-radius: var(--ao-radius-sm);
    border: 1px solid var(--ao-border-subtle);
    background-color: var(--ao-surface-control);
    color: var(--ao-text-secondary);
    transition: 120ms ease;
    min-height: 32px;
    padding: 0 14px;
}
button:hover {
    background-color: var(--ao-surface-control-hover);
    border-color: var(--ao-border-strong);
    color: var(--ao-text);
}
button:disabled {
    opacity: 0.42;
}
/* primary-действие: GTK suggested-action == Obsidian button.mod-cta (ao-glass-controls) */
button.suggested-action,
button.mod-cta {
    background-color: rgba(130, 168, 255, 0.18);
    border-color: rgba(130, 168, 255, 0.42);
    color: var(--ao-accent-focus);
    font-weight: 600;
}
button.suggested-action:hover,
button.mod-cta:hover {
    background-color: rgba(130, 168, 255, 0.26);
    border-color: rgba(130, 168, 255, 0.55);
    color: var(--ao-text);
}
button.destructive-action {
    background-color: rgba(220, 130, 145, 0.12);
    border-color: rgba(220, 130, 145, 0.38);
    color: var(--ao-accent-danger);
}
button.destructive-action:hover {
    background-color: rgba(220, 130, 145, 0.2);
}
/* нейтральная кнопка (Obsidian .mod-neutral) */
button.mod-neutral {
    background-color: transparent;
    border-color: var(--ao-border-subtle);
    color: var(--ao-text-secondary);
}
button.mod-neutral:hover {
    background-color: var(--ao-surface-control);
    color: var(--ao-text);
    border-color: var(--ao-border-strong);
}
/* ряд кнопок и тело диалога (Obsidian prompt-палитра) */
.btn-row {
    margin-top: 2px;
}
.dialog-body {
    min-width: 360px;
}
button.flat {
    background-color: transparent;
    border-color: transparent;
    color: var(--ao-text-muted);
}
button.flat:hover {
    background-color: var(--ao-surface-control);
    border-color: var(--ao-border-subtle);
    color: var(--ao-text-secondary);
}

/* ═══ KPI strip + chips (tm-ui-kit → .tm-daily-kpi-*) ═══ */
.kpi-strip {
    border-radius: var(--ao-radius-md);
    background-color: var(--ao-surface-glass-soft);
    border: 1px solid var(--ao-border-subtle);
    padding: 12px 14px;
}
.kpi-chip {
    border-radius: var(--ao-radius-sm);
    background-color: var(--ao-surface-inset);
    border: 1px solid var(--ao-border-subtle);
    padding: 8px 12px;
    min-width: 72px;
}
.kpi-chip--warn {
    border-color: rgba(230, 175, 110, 0.4);
    background-color: rgba(230, 175, 110, 0.08);
}
.kpi-chip--error {
    border-color: rgba(220, 130, 145, 0.4);
    background-color: rgba(220, 130, 145, 0.08);
}
.kpi-chip--ok {
    border-color: rgba(120, 210, 150, 0.35);
    background-color: rgba(120, 210, 150, 0.08);
}
.kpi-chip--run {
    border-color: rgba(130, 168, 255, 0.4);
    background-color: rgba(130, 168, 255, 0.08);
}
.kpi-chip--idle {
    border-color: var(--ao-border-subtle);
    background-color: var(--ao-surface-inset);
}
.kpi-chip__value {
    font-size: 1.15em;
    font-weight: 650;
    color: var(--ao-text);
}
.kpi-chip__label {
    font-size: 0.62em;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--ao-text-muted);
    margin-top: 2px;
}

/* ═══ Glass card + head (tm-daily-card canon) ═══ */
.glass-card {
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
    background-color: var(--ao-surface-glass);
    box-shadow: inset 0 1px 0 var(--ao-highlight);
}
.glass-card--emphasis {
    border-color: rgba(130, 168, 255, 0.24);
}
.glass-card__head {
    padding: 12px 16px 8px 16px;
    border-bottom: 1px solid var(--ao-border-subtle);
}
.glass-card__title {
    font-size: 0.7em;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ao-text-muted);
}
.glass-row {
    border-radius: var(--ao-radius-sm);
    padding: 9px 12px;
}
.glass-row:hover {
    background-color: var(--ao-surface-glass-hover);
}
.glass-row + .glass-row {
    border-top: 1px solid var(--ao-border-subtle);
}

/* ── Task Manager (tm-daily-card canon) ── */
.card--overdue {
    background-image: linear-gradient(145deg, rgba(230, 175, 110, 0.09), var(--ao-surface-glass) 55%);
    box-shadow: inset 3px 0 0 rgba(230, 175, 110, 0.85);
}
.card--today {
    background-image: linear-gradient(145deg, rgba(130, 168, 255, 0.09), var(--ao-surface-glass) 55%);
    box-shadow: inset 3px 0 0 rgba(130, 168, 255, 0.8);
}
.card--routine {
    background-image: linear-gradient(145deg, rgba(120, 200, 220, 0.08), var(--ao-surface-glass) 55%);
    box-shadow: inset 3px 0 0 rgba(120, 200, 220, 0.8);
}
.prio-pill {
    font-size: 0.68em;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: var(--ao-radius-xs);
    border: 1px solid rgba(230, 175, 110, 0.35);
    background: rgba(230, 175, 110, 0.12);
    color: rgba(255, 190, 140, 0.95);
}
.tm-hero {
    border: 1px solid rgba(130, 168, 255, 0.22);
    border-radius: var(--ao-radius-md);
    background-color: rgba(130, 168, 255, 0.07);
    padding: 12px 16px;
    margin: 0 14px 4px;
}
.tm-hero__count {
    font-size: 0.72em;
    font-weight: 600;
    padding: 3px 9px;
    border-radius: var(--ao-radius-pill);
    background-color: var(--ao-surface-control);
    border: 1px solid var(--ao-border-subtle);
    color: var(--ao-text-muted);
}

.link-btn {
    color: rgba(130, 168, 255, 0.95);
    font-size: 0.78em;
    font-weight: 600;
    background: none;
    border: none;
    padding: 4px 10px;
}
.link-btn:hover {
    background-color: rgba(130, 168, 255, 0.1);
    border-radius: var(--ao-radius-xs);
    color: #a8c8ff;
}

/* ═══ Пустые состояния (ao-empty — Obsidian: solid, centered, glass) ═══ */
.empty {
    padding: 40px 24px;
    margin: 8px;
    border-radius: var(--ao-radius-md);
    background-color: var(--ao-surface-glass);
    background-image: linear-gradient(145deg, rgba(255, 255, 255, 0.035), transparent 60%);
    border: 1px solid var(--ao-border-strong);
    box-shadow: inset 0 1px 0 var(--ao-highlight), 0 8px 24px rgba(0, 0, 0, 0.12);
}
.empty-icon {
    font-size: 2.4em;
    opacity: 0.95;
}
.empty-text {
    color: var(--ao-text-muted);
    font-size: 0.9em;
}
.empty-hint {
    color: var(--ao-text-faint);
    font-size: 0.82em;
    margin-top: 2px;
}

/* ═══ Progress bars ═══ */
progressbar {
    min-height: 6px;
}
progressbar trough {
    background-color: var(--ao-surface-inset);
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-pill);
    min-height: 6px;
}
progressbar progress {
    background-color: rgba(130, 168, 255, 0.85);
    border-radius: var(--ao-radius-pill);
    min-height: 6px;
}
progressbar.live-bar progress {
    background-color: rgba(120, 200, 220, 0.9);
}

/* ═══ Runner (AO Tasks) ═══ */
.rv-overview {
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
    padding: 12px 14px;
    background-color: var(--ao-surface-glass-soft);
}
.rv-overview-title {
    font-size: 0.9em;
    font-weight: 700;
    color: var(--ao-text);
}
.rv-progress {
    margin-top: 6px;
}
.rv-card-title {
    font-size: 1.02em;
    font-weight: 600;
    color: var(--ao-text);
}
.rv-card-help {
    font-size: 0.86em;
    color: var(--ao-text-muted);
}
.rv-card-icon {
    font-size: 1.1em;
}
.rv-error-box {
    border-radius: var(--ao-radius-xs);
    background-color: rgba(220, 130, 145, 0.1);
    border: 1px solid rgba(220, 130, 145, 0.25);
    padding: 6px 10px;
    font-size: 0.85em;
    color: var(--ao-accent-danger);
}
.rv-hist-time {
    font-size: 0.8em;
    color: var(--ao-text-faint);
    min-width: 56px;
}
.rv-hist-tag {
    border-radius: var(--ao-radius-pill);
    padding: 0 6px;
    font-size: 0.72em;
    font-weight: 600;
}
.rv-hist-ok {
    background-color: rgba(120, 210, 150, 0.2);
    color: var(--ao-accent-success);
}
.rv-hist-err {
    background-color: rgba(220, 130, 145, 0.2);
    color: var(--ao-accent-danger);
}

/* ═══ Задачи (Сегодня) ═══ */
.task-title {
    font-size: 1.0em;
    font-weight: 600;
    color: var(--ao-text);
}
.task-meta {
    font-size: 0.82em;
    color: var(--ao-text-muted);
}

/* ═══ Редактор / Daily (ao-glass-note) ═══ */
.editor {
    font-family: "JetBrains Mono", "Source Code Pro", "Fira Code", monospace;
    font-size: 13px;
    padding: 10px 12px;
    color: var(--ao-text-secondary);
    background-color: rgba(4, 6, 10, 0.72);
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
    caret-color: var(--ao-accent-focus);
}
.editor.prose {
    font-family: "Inter", "Cantarell", system-ui, sans-serif;
    font-size: 17.5px;
    line-height: 1.58;
    color: var(--ao-text);
}
.editor-frame {
    padding: 6px;
}

/* ── Markdown / чтение (ao-glass-note → .markdown-preview) ──
   Просмотр заметки: чистый «бумажный» слой без рамки. Тэги блоков
   (заголовки/код/цитаты) задаются палитрой в markdown.py, здесь —
   базовая типографика и контейнер .md-read. */
.markdown {
    background-color: transparent;
    color: var(--ao-text-secondary);
    font-family: "Inter", "Cantarell", system-ui, sans-serif;
    font-size: 17.5px;
    line-height: 1.58;
    caret-color: var(--ao-accent-focus);
}
textview.markdown text {
    background-color: transparent;
}
.md-read {
    background-color: transparent;
}
.md-read .markdown {
    padding: 4px 14px;
}
.md-read--elevated {
    background-color: var(--ao-surface-glass);
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
}
.toolbar-switcher {
    background-color: var(--ao-surface-glass-soft);
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-pill);
    padding: 2px;
}
.toolbar-switcher button {
    border-radius: var(--ao-radius-pill);
    border: 1px solid transparent;
    background-color: transparent;
    color: var(--ao-text-muted);
    padding: 2px 14px;
    font-weight: 500;
    min-height: 26px;
}
.toolbar-switcher button:checked {
    background-color: var(--ao-selected);
    border-color: var(--ao-selected-border);
    color: var(--ao-accent-focus);
}
.toolbar-switcher button:hover {
    background-color: var(--ao-surface-control-hover);
}

/* ═══ Media (freakydb-media) ═══ */
.media-filter {
    border-radius: var(--ao-radius-pill);
    min-height: 28px;
    padding: 0 12px;
}
.media-filter:checked {
    background-color: rgba(167, 139, 250, 0.18);
    border-color: rgba(167, 139, 250, 0.45);
    color: #c4b5fd;
}
.media-scroller {
    padding: 6px;
}
.media-tile {
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
    padding: 12px;
    background-color: var(--ao-surface-glass);
    box-shadow: inset 0 2px 0 rgba(167, 139, 250, 0.2);
}
.media-tile:hover {
    border-color: rgba(167, 139, 250, 0.35);
    background-color: var(--ao-surface-glass-hover);
}
.media-cover {
    font-size: 2.4em;
    min-width: 68px;
    min-height: 88px;
    background-color: var(--ao-surface-inset);
    border-radius: var(--ao-radius-sm);
}
.media-cover-anime {
    background-color: rgba(167, 139, 250, 0.2);
}
.media-cover-manga {
    background-color: rgba(192, 132, 252, 0.2);
}
.media-cover-game {
    background-color: rgba(212, 165, 116, 0.2);
}
.media-cover-movie {
    background-color: rgba(126, 184, 218, 0.2);
}
.media-cover-music {
    background-color: rgba(94, 234, 212, 0.2);
}
.media-cover-book {
    background-color: rgba(212, 184, 150, 0.2);
}
.media-title {
    font-weight: 600;
    font-size: 0.98em;
    color: var(--ao-text);
}
.media-meta {
    font-size: 0.8em;
    color: var(--ao-text-muted);
}
.media-search {
    border-radius: var(--ao-radius-pill);
}

/* ═══ Настройки (tm-surfaces / settings) ═══ */
.settings-group {
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
    padding: 12px 14px;
    background-color: var(--ao-surface-glass);
    margin-bottom: 10px;
}
.settings-row {
    margin-top: 4px;
}
.settings-label {
    font-size: 0.9em;
    color: var(--ao-text-secondary);
}
.btn-sm {
    padding: 2px 10px;
    min-height: 26px;
    font-size: 0.8em;
}

/* Рабочее пространство: редактор иконок ribbon */
.ws-list {
    background-color: transparent;
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
}
.ws-row {
    background-color: transparent;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
}
.ws-row:last-child {
    border-bottom: none;
}
.ws-icon {
    font-size: 1em;
    min-width: 22px;
}
.ws-arrow {
    padding: 0;
    min-width: 26px;
    min-height: 24px;
    border-radius: var(--ao-radius-xs);
    color: var(--ao-text-muted);
}
.ws-arrow:hover {
    background-color: var(--ao-surface-control-hover);
    color: var(--ao-text);
}
.ws-kind {
    font-size: 0.72em;
    min-width: 58px;
}
.ws-frame {
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-sm);
    padding: 6px 8px;
}

/* ═══ Task Manager / Daily (tm-surfaces + tm-daily CSS канон) ═══
   Порт из .obsidian/snippets: hero-v2, today-strip, KPI чипы, day-card
   с акцент-баром слева, item'ы с баджами, RPG-бар. */

/* tm-hero-v2 — унифицирован с токенами Glass */
.tm-hero-v2 {
    background: linear-gradient(145deg, rgba(255, 255, 255, 0.07), rgba(255, 255, 255, 0.025) 55%);
    border: 1px solid rgba(255, 255, 255, 0.09);
    border-left: 3px solid rgba(100, 205, 220, 0.6);
    border-radius: var(--ao-radius-lg);
    padding: 16px 22px;
    margin: 0 14px 2px;
    box-shadow: var(--ao-shadow-card);
}
.tm-hero-v2--dash {
    border-left-color: rgba(100, 205, 220, 0.75);
    background: linear-gradient(145deg, rgba(62, 199, 214, 0.08), rgba(255, 255, 255, 0.03) 50%);
}
.tm-hero-v2--inset {
    margin: 0;
}
.tm-hero-v2__eyebrow {
    font-size: 0.66rem;
    font-weight: 700;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: rgba(100, 205, 220, 0.9);
}
.tm-hero-v2__title {
    font-size: 1.35em;
    font-weight: 750;
    letter-spacing: -0.012em;
    color: rgba(240, 244, 252, 0.98);
}
.tm-hero-v2__date {
    font-size: 0.85em;
    color: rgba(175, 186, 204, 0.72);
}

/* tm-today-strip */
.tm-today-strip {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 12px;
    padding: 10px 16px;
    margin: 0 14px 2px;
}
.tm-today-strip__label {
    font-size: 0.66rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: rgba(100, 205, 220, 0.9);
}

/* tm-kpi-chip */
.tm-kpi-chip {
    background: rgba(0, 0, 0, 0.2);
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 9px;
    padding: 8px 14px;
    min-width: 76px;
}
.tm-kpi-chip--warn {
    border-color: rgba(255, 170, 90, 0.4);
    background: rgba(255, 170, 90, 0.08);
}
.tm-kpi-chip--error {
    border-color: rgba(255, 110, 120, 0.4);
    background: rgba(255, 110, 120, 0.08);
}
.tm-kpi-chip--ok {
    border-color: rgba(120, 210, 150, 0.4);
    background: rgba(120, 210, 150, 0.08);
}
.tm-kpi-chip--run {
    border-color: rgba(130, 168, 255, 0.45);
    background: rgba(130, 168, 255, 0.08);
}
.tm-kpi-chip__value {
    font-size: 1.15em;
    font-weight: 650;
    color: rgba(240, 244, 252, 0.98);
}
.tm-kpi-chip__label {
    font-size: 0.6rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: rgba(175, 186, 204, 0.72);
}

/* tm-daily-card: акцент-бар слева + стекло — токены Glass */
.tm-daily-card {
    background: rgba(14, 18, 28, 0.55);
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: var(--ao-radius-lg);
    box-shadow: var(--ao-shadow-card);
    opacity: 1;
}
.tm-daily-card--focus {
    background: linear-gradient(145deg, rgba(130, 168, 255, 0.09), rgba(14, 18, 28, 0.6));
    box-shadow: inset 3px 0 0 rgba(130, 168, 255, 0.85);
}
.tm-daily-card--routines {
    background: linear-gradient(145deg, rgba(120, 200, 220, 0.07), rgba(14, 18, 28, 0.6));
    box-shadow: inset 3px 0 0 rgba(120, 200, 220, 0.85);
}
.tm-daily-card--done {
    background: linear-gradient(145deg, rgba(120, 210, 150, 0.08), rgba(14, 18, 28, 0.6));
    box-shadow: inset 3px 0 0 rgba(120, 210, 150, 0.85);
}
.tm-daily-card--overdue {
    background: linear-gradient(145deg, rgba(230, 175, 110, 0.1), rgba(14, 18, 28, 0.6));
    box-shadow: inset 3px 0 0 rgba(230, 175, 110, 0.9);
}
.tm-daily-card--rpg {
    background: linear-gradient(145deg, rgba(190, 165, 255, 0.13), rgba(78, 201, 216, 0.05));
    box-shadow: inset 3px 0 0 rgba(190, 165, 255, 0.9);
}
.tm-daily-card__header {
    padding: 12px 16px 8px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}
.tm-daily-card__icon {
    font-size: 1.15em;
}
.tm-daily-card__title {
    font-size: 0.78em;
    font-weight: 700;
    letter-spacing: 0.06em;
    color: rgba(228, 234, 246, 0.94);
}
.tm-daily-card__subtitle {
    font-size: 0.72em;
    color: rgba(175, 186, 204, 0.72);
}
.tm-daily-card__count {
    font-size: 0.72em;
    font-weight: 650;
    color: rgba(175, 186, 204, 0.82);
}

/* tm-daily-item */
.tm-daily-item {
    border-radius: 9px;
    padding: 9px 12px;
    margin: 0 10px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
}
.tm-daily-item:hover {
    background: rgba(255, 255, 255, 0.045);
}
.tm-daily-item__marker {
    font-size: 1.05em;
    font-weight: 700;
    min-width: 22px;
}
.tm-daily-item__title {
    font-size: 0.96em;
    font-weight: 600;
    color: rgba(228, 234, 246, 0.94);
}
.tm-daily-item__meta {
    font-size: 0.78em;
    color: rgba(175, 186, 204, 0.72);
}

/* tm-daily-badge */
.tm-daily-badge {
    border-radius: 999px;
    padding: 1px 9px;
    font-size: 0.66rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    border: 1px solid rgba(255, 255, 255, 0.1);
    color: rgba(210, 218, 232, 0.88);
    background: rgba(255, 255, 255, 0.05);
}
.tm-daily-badge--accent {
    border-color: rgba(130, 168, 255, 0.45);
    background: rgba(130, 168, 255, 0.14);
    color: #a8c8ff;
}
.tm-daily-badge--warn {
    border-color: rgba(255, 170, 90, 0.4);
    background: rgba(255, 170, 90, 0.14);
    color: #ffc69a;
}
.tm-daily-badge--date {
    border-color: rgba(120, 200, 220, 0.4);
    background: rgba(120, 200, 220, 0.12);
    color: #9adff0;
}
.tm-daily-badge--priority {
    border-color: rgba(255, 170, 90, 0.4);
    background: rgba(255, 170, 90, 0.14);
    color: #ffc69a;
}
.tm-daily-badge--type {
    border-color: rgba(190, 165, 255, 0.4);
    background: rgba(190, 165, 255, 0.12);
    color: #cdbfff;
}

/* tm-daily-rpg */
.tm-daily-rpg__stats {
    margin-bottom: 10px;
}
.tm-daily-rpg__stat--level {
    font-size: 1.6em;
    font-weight: 700;
    color: rgba(228, 234, 246, 0.94);
}
.tm-daily-rpg__stat--xp {
    font-size: 1.0em;
    font-weight: 600;
    color: rgba(235, 225, 255, 0.92);
}
.tm-daily-rpg__subline {
    font-size: 0.8em;
    color: rgba(175, 186, 204, 0.72);
}
.tm-daily-rpg__bar {
    min-height: 9px;
    border-radius: 999px;
    background: rgba(0, 0, 0, 0.35);
    border: 1px solid rgba(255, 255, 255, 0.08);
}
.tm-daily-rpg__fill {
    background: linear-gradient(90deg, rgba(130, 168, 255, 0.9), rgba(190, 165, 255, 0.9), rgba(120, 200, 220, 0.8));
    box-shadow: 0 0 14px rgba(190, 165, 255, 0.3);
    min-height: 9px;
}

/* tm-pill (в заметках TM / состоянии) */
.tm-pill {
    border-radius: 999px;
    padding: 2px 10px;
    font-size: 0.7em;
    font-weight: 600;
    letter-spacing: 0.05em;
    border: 1px solid rgba(255, 255, 255, 0.1);
    color: rgba(210, 218, 232, 0.88);
    background: rgba(255, 255, 255, 0.05);
}
.tm-pill--focus {
    border-color: rgba(130, 168, 255, 0.4);
    background: rgba(130, 168, 255, 0.15);
    color: #b8ccff;
}
.tm-pill--success {
    border-color: rgba(120, 210, 150, 0.4);
    background: rgba(120, 210, 150, 0.14);
    color: #9ce8b4;
}
.tm-pill--warn {
    border-color: rgba(230, 175, 110, 0.4);
    background: rgba(230, 175, 110, 0.14);
    color: #ffc69a;
}

/* ═══ Базовые виджеты GTK под AO Glass (ao-glass-controls) ═══ */
scrollbar {
    background-color: transparent;
}
scrollbar slider {
    background-color: rgba(255, 255, 255, 0.14);
    border-radius: var(--ao-radius-pill);
    min-width: 5px;
    min-height: 5px;
}
scrollbar slider:hover {
    background-color: rgba(255, 255, 255, 0.26);
    min-width: 7px;
    min-height: 7px;
}
scrollbar slider:active {
    background-color: rgba(130, 168, 255, 0.7);
}
scrollbar trough {
    background-color: transparent;
}

tooltip {
    background-color: rgba(10, 14, 22, 0.96);
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-sm);
    color: var(--ao-text-secondary);
}
tooltip.background {
    background-color: rgba(10, 14, 22, 0.96);
}

popover {
    background-color: var(--ao-surface-panel);
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
    box-shadow: var(--ao-shadow-lift);
}
popover > box,
popover > list {
    background-color: transparent;
}
popover list row {
    color: var(--ao-text-secondary);
    border-radius: var(--ao-radius-xs);
}
popover list row:hover {
    background-color: var(--ao-surface-row-hover);
}

entry {
    background-color: var(--ao-surface-control);
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-sm);
    color: var(--ao-text);
    caret-color: var(--ao-accent-focus);
    min-height: 32px;
    padding: 0 10px;
    transition: 120ms ease;
}
entry:hover {
    border-color: var(--ao-border-strong);
}
entry selection,
textview selection {
    background-color: var(--ao-focus-ring);
    color: var(--ao-text);
}
:focus-visible {
    outline: 2px solid rgba(130, 168, 255, 0.35);
    outline-offset: -1px;
}
button:active {
    filter: brightness(0.96);
}
entry:focus {
    border-color: var(--ao-selected-border);
    background-color: rgba(255, 255, 255, 0.07);
    box-shadow: 0 0 0 2px rgba(130, 168, 255, 0.28);
}
entry.flat {
    background-color: transparent;
}

combobox button {
    background-color: var(--ao-surface-control);
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-sm);
    color: var(--ao-text-secondary);
}
combobox button:hover {
    background-color: var(--ao-surface-control-hover);
}
combobox arrow {
    color: var(--ao-text-muted);
}

switch {
    background-color: var(--ao-surface-control-hover);
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-pill);
}
switch:checked {
    background-color: rgba(130, 168, 255, 0.32);
    border-color: var(--ao-selected-border);
}
switch slider {
    background-color: var(--ao-text-muted);
    border-radius: var(--ao-radius-pill);
    border: 1px solid var(--ao-border-strong);
}
switch:checked slider {
    background-color: var(--ao-accent-focus);
}

checkbutton check,
checkbutton radio {
    background-color: var(--ao-surface-control);
    border: 1px solid var(--ao-border-strong);
    border-radius: 4px;
}
checkbutton check:checked {
    background-color: rgba(130, 168, 255, 0.35);
    border-color: var(--ao-accent-focus);
}

spinbutton button {
    background-color: var(--ao-surface-control);
    border: 1px solid var(--ao-border-subtle);
    color: var(--ao-text-muted);
}
spinbutton button:hover {
    background-color: var(--ao-surface-control-hover);
}

/* ═══ Разное ═══ */
expander {
    color: var(--ao-text-faint);
}
expander:hover {
    color: var(--ao-text-muted);
}
expander list {
    background-color: transparent;
}
.toolbar {
    background-color: var(--ao-surface-glass-soft);
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
    padding: 6px 10px;
}
.dim-hint {
    font-size: 0.85em;
    color: var(--ao-text-muted);
}

/* GTK-класс .dim-label переопределяем на канон muted (ao-glass-tokens) */
.dim-label {
    color: var(--ao-text-muted);
}

/* ═══ Быстрый переключатель (Ctrl+P) — как команданая палитра Obsidian (.prompt) ═══ */
.qs-popover {
    background-color: var(--ao-surface-panel-elevated);
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-lg);
    box-shadow: var(--ao-shadow-lift);
}
.qs-list {
    background-color: transparent;
}
.qs-row {
    border-radius: var(--ao-radius-sm);
    padding: 4px 8px;
    color: var(--ao-text-secondary);
}
.qs-row:hover,
.qs-row:selected {
    background-color: rgba(130, 168, 255, 0.12);
    color: var(--ao-text);
}
.qs-row:selected label {
    color: var(--ao-text);
}
.qs-snippet {
    font-size: 0.85em;
    color: var(--ao-text-muted);
}
.qs-empty {
    padding: 16px 10px;
    color: var(--ao-text-muted);
}

/* ═══ Граф связей (Obsidian Graph View) ═══ */
.graph-toolbar {
    padding: 6px 10px;
}
.graph-area {
    background-color: var(--ao-bg-deep);
    background-image: radial-gradient(90% 70% at 15% -10%, rgba(130, 168, 255, 0.06), transparent 50%),
        radial-gradient(60% 50% at 95% 0%, rgba(190, 165, 255, 0.05), transparent 55%);
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
}
.graph-scroller {
    background-color: transparent;
}
.graph-stats {
    font-size: 0.78em;
    color: var(--ao-text-muted);
}

/* ═══ Canvas (Excalidraw-подобный холст) ═══ */
.canvas-area {
    background-color: var(--ao-bg-deep);
    background-image: radial-gradient(90% 70% at 15% -10%, rgba(130, 168, 255, 0.06), transparent 50%),
        radial-gradient(60% 50% at 95% 0%, rgba(190, 165, 255, 0.05), transparent 55%);
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
}
.canvas-toolbar {
    padding: 6px 10px;
}
.canvas-tools {
    padding: 6px 10px;
}
.canvas-file {
    font-size: 0.8em;
    color: var(--ao-text-muted);
}
.canvas-status {
    font-size: 0.78em;
    color: var(--ao-text-muted);
}
.canvas-hint {
    font-size: 0.75em;
    color: var(--ao-text-faint);
}
.canvas-color {
    min-width: 28px;
    min-height: 28px;
    padding: 0;
    border-radius: var(--ao-radius-xs);
}

/* ═══ Связи — бэклинки / исходящие (FilesView под редактором) ═══ */
.links-panel {
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
    background-color: var(--ao-surface-glass-soft);
    padding: 10px 14px;
    margin-top: 2px;
}
.links-title {
    font-size: 0.7em;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ao-text-muted);
}
.links-subtitle {
    font-size: 0.72em;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ao-text-muted);
}
.links-flow {
    background-color: transparent;
}
.link-chip {
    border-radius: var(--ao-radius-pill);
    padding: 2px 10px;
    font-size: 0.82em;
    font-weight: 500;
    border: 1px solid var(--ao-border-subtle);
    background-color: var(--ao-surface-control);
    color: var(--ao-text-secondary);
    min-height: 22px;
}
.link-chip:hover {
    background-color: var(--ao-surface-control-hover);
    border-color: var(--ao-border-strong);
    color: var(--ao-text);
}

/* ═══ Теги — сиреневые чипы Obsidian (FilesView под редактором) ═══ */
.tags-panel {
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
    background-color: var(--ao-surface-glass-soft);
    padding: 10px 14px;
    margin-top: 2px;
}
.tags-title {
    font-size: 0.7em;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ao-text-muted);
}
.tags-flow {
    background-color: transparent;
}
.tag-chip {
    border-radius: var(--ao-radius-pill);
    padding: 2px 10px;
    font-size: 0.82em;
    font-weight: 500;
    border: 1px solid rgba(190, 165, 255, 0.22);
    background-color: rgba(190, 165, 255, 0.12);
    color: #cdbfff;
    min-height: 22px;
}
.tag-chip:hover {
    background-color: rgba(190, 165, 255, 0.18);
    border-color: rgba(190, 165, 255, 0.38);
    color: #e0d4ff;
}
.tag-chip--active {
    background-color: rgba(190, 165, 255, 0.24);
    border-color: rgba(190, 165, 255, 0.5);
    box-shadow: 0 0 0 2px rgba(190, 165, 255, 0.18);
    color: #e8dcff;
}
.tag-clear {
    min-width: 22px;
    min-height: 22px;
    padding: 0 6px;
    border-radius: var(--ao-radius-pill);
    font-size: 0.75em;
}

/* ═══ Outline — оглавление markdown (рядом с редактором) ═══ */
.outline-panel {
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
    background-color: var(--ao-surface-glass-soft);
    padding: 8px 8px;
    min-width: 200px;
}
.outline-title {
    font-size: 0.7em;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ao-text-muted);
    padding: 2px 4px;
}
.outline-scroller {
    background-color: transparent;
}
.outline-list {
    background-color: transparent;
}
.outline-list row {
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: var(--ao-radius-sm);
    margin: 1px 2px;
    padding: 3px 6px;
    color: var(--ao-text-muted);
    transition: background var(--ao-transition-fast), color var(--ao-transition-fast);
}
.outline-list row:hover {
    background-color: var(--ao-surface-row-hover);
    color: var(--ao-text-secondary);
}
.outline-list row:selected {
    background-image: linear-gradient(135deg, rgba(130, 168, 255, 0.16), rgba(130, 168, 255, 0.06));
    border: 1px solid rgba(130, 168, 255, 0.22);
    color: var(--ao-text);
}
.outline-label {
    font-size: 0.88em;
    color: var(--ao-text-muted);
}
.outline-level-1 .outline-label {
    font-size: 0.95em;
    font-weight: 650;
    color: var(--ao-text);
}
.outline-level-2 .outline-label {
    font-size: 0.90em;
    font-weight: 600;
    color: var(--ao-text-secondary);
}
.outline-level-3 .outline-label {
    font-size: 0.85em;
    font-weight: 500;
    color: var(--ao-text-secondary);
}
.outline-level-4 .outline-label {
    font-size: 0.80em;
    color: var(--ao-text-muted);
}
.outline-level-5 .outline-label {
    font-size: 0.78em;
    color: var(--ao-text-muted);
}
.outline-level-6 .outline-label {
    font-size: 0.75em;
    color: var(--ao-text-faint);
}
.outline-empty {
    font-size: 0.82em;
    color: var(--ao-text-faint);
    padding: 8px 4px;
}

/* ═══ Kanban (Obsidian Kanban — 3 колонки + карточки + DnD) ═══ */
.kanban-toolbar {
    padding: 6px 10px;
}
.kanban-stats {
    font-size: 0.78em;
    color: var(--ao-text-muted);
}
.kanban-column {
    background-color: var(--ao-surface-glass-soft);
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
    padding: 10px;
    min-width: 240px;
}
.kanban-column--todo {
    border-top: 2px solid rgba(130, 168, 255, 0.35);
}
.kanban-column--doing {
    border-top: 2px solid rgba(230, 175, 110, 0.45);
}
.kanban-column--done {
    border-top: 2px solid rgba(120, 210, 150, 0.40);
}
.kanban-column--drag-over {
    background-color: rgba(130, 168, 255, 0.08);
    border-color: rgba(130, 168, 255, 0.38);
    box-shadow: inset 0 0 0 1px rgba(130, 168, 255, 0.22);
}
.kanban-column__head {
    padding: 2px 4px 6px;
    border-bottom: 1px solid var(--ao-border-subtle);
}
.kanban-column__icon {
    font-size: 1.05em;
}
.kanban-column__title {
    font-size: 0.82em;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ao-text);
}
.kanban-column__count {
    min-width: 24px;
}
.kanban-scroller {
    background-color: transparent;
}
.kanban-list {
    padding: 2px;
}
.kanban-card {
    background-color: var(--ao-surface-glass);
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
    padding: 10px 12px;
    box-shadow: var(--ao-shadow-card);
    transition: background var(--ao-transition-fast), border-color var(--ao-transition-fast);
}
.kanban-card:hover {
    background-color: var(--ao-surface-glass-hover);
    border-color: var(--ao-border-strong);
}
.kanban-card--dragging {
    opacity: 0.55;
    border-color: rgba(130, 168, 255, 0.45);
}
.kanban-card__title {
    font-size: 0.96em;
    font-weight: 600;
    color: var(--ao-text);
}
.kanban-card__preview {
    font-size: 0.82em;
    color: var(--ao-text-muted);
}
.kanban-card__file {
    font-size: 0.72em;
    color: var(--ao-text-faint);
}
.kanban-badge {
    border-radius: var(--ao-radius-pill);
    padding: 1px 8px;
    font-size: 0.68em;
    font-weight: 600;
    letter-spacing: 0.04em;
    border: 1px solid var(--ao-border-subtle);
    background-color: var(--ao-surface-control);
    color: var(--ao-text-muted);
}
.kanban-badge--todo {
    border-color: rgba(130, 168, 255, 0.30);
    background-color: rgba(130, 168, 255, 0.10);
    color: #a8c8ff;
}
.kanban-badge--doing {
    border-color: rgba(230, 175, 110, 0.35);
    background-color: rgba(230, 175, 110, 0.12);
    color: #ffc69a;
}
.kanban-badge--done {
    border-color: rgba(120, 210, 150, 0.30);
    background-color: rgba(120, 210, 150, 0.10);
    color: #9ce8b4;
}
.kanban-add-btn {
    border-style: dashed;
    border-color: var(--ao-border-strong);
    background-color: transparent;
    color: var(--ao-text-muted);
    font-size: 0.85em;
}
.kanban-add-btn:hover {
    background-color: var(--ao-surface-glass);
    border-color: rgba(130, 168, 255, 0.32);
    color: var(--ao-text);
}

/* ═══ Поиск с подсветкой в редакторе (TextTag + счётчик) ═══ */
.search-counter {
    font-size: 0.85em;
    color: var(--ao-text-muted);
    min-width: 42px;
    font-variant-numeric: tabular-nums;
    font-weight: 600;
}
.search-nav {
    background-color: var(--ao-surface-glass-soft);
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-pill);
    padding: 2px;
}
.search-nav-btn {
    min-width: 28px;
    min-height: 28px;
    padding: 0;
    border-radius: var(--ao-radius-pill);
}
.search-nav-btn:hover {
    background-color: var(--ao-surface-control-hover);
}
/* Дублирование подсветки для CSS-тем (основная подсветка — Gtk.TextTag background rgba(130,168,255,0.3)) */
.search-highlight {
    background-color: rgba(130, 168, 255, 0.3);
    color: var(--ao-text);
    border-radius: 3px;
}
.search-highlight-current {
    background-color: rgba(130, 168, 255, 0.55);
    color: #ffffff;
    border-radius: 3px;
    box-shadow: 0 0 0 1px rgba(130, 168, 255, 0.4);
}

/* ═══ База данных (Notion-like ColumnView) ═══ */
.database-toolbar {
    padding: 6px 10px;
}
.database-count {
    font-size: 0.78em;
    color: var(--ao-text-muted);
    min-width: 48px;
}
.database-scroller {
    background-color: transparent;
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
    margin: 0 14px 14px;
}
.database-view {
    background-color: transparent;
}
.database-view columnview {
    background-color: transparent;
}
.database-view header {
    background-color: var(--ao-surface-glass-soft);
    border-bottom: 1px solid var(--ao-border-subtle);
}
.database-view header button {
    background-color: transparent;
    border: none;
    color: var(--ao-text-muted);
    font-size: 0.72em;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
}
.database-view header button:hover {
    background-color: var(--ao-surface-control-hover);
    color: var(--ao-text);
}
.database-cell {
    background-color: transparent;
    border: none;
    color: var(--ao-text-secondary);
    font-size: 0.9em;
}
.database-cell:focus {
    background-color: rgba(130, 168, 255, 0.10);
    box-shadow: inset 0 0 0 1px rgba(130, 168, 255, 0.28);
    border-radius: var(--ao-radius-xs);
}

/* ═══ Календарь + heatmap (Obsidian Calendar) ═══ */
.cal-toolbar {
    padding: 6px 10px;
}
.cal-month {
    font-size: 1.05em;
    font-weight: 700;
    color: var(--ao-text);
    letter-spacing: 0.02em;
}
.cal-year-spin {
    min-width: 92px;
}
.cal-frame {
    padding: 4px;
}
.cal-grid {
    background: transparent;
}
.cal-weekday {
    font-size: 0.75em;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ao-text-muted);
    padding: 4px 0;
}
.cal-weekday.cal-weekend {
    color: rgba(230, 175, 110, 0.85);
}
.cal-day {
    min-width: 0;
    min-height: 0;
    /* aspect-ratio: 1; — адаптив для 360px (GTK: эмулируется min-width:0 + homogeneous Grid) */
    aspect-ratio: 1;
    border-radius: var(--ao-radius-sm);
    border: 1px solid transparent;
    background-color: var(--ao-surface-glass-soft);
    color: var(--ao-text-secondary);
    padding: 2px;
    transition: background var(--ao-transition-fast), border-color var(--ao-transition-fast), color var(--ao-transition-fast);
}
.cal-day:hover {
    background-color: var(--ao-surface-control-hover);
    border-color: var(--ao-border-strong);
    color: var(--ao-text);
}
.cal-day--outside {
    opacity: 0.42;
}
.cal-day--today {
    border-color: rgba(130, 168, 255, 0.45);
    box-shadow: inset 0 0 0 1px rgba(130, 168, 255, 0.22);
}
.cal-day--selected {
    background-color: rgba(130, 168, 255, 0.18);
    border-color: rgba(130, 168, 255, 0.42);
    color: var(--ao-text);
    box-shadow: 0 0 0 2px rgba(130, 168, 255, 0.22);
}
.cal-day--weekend.cal-day--heat0 {
    background-color: rgba(230, 175, 110, 0.06);
}
.cal-day__num {
    font-size: 0.98em;
    font-weight: 600;
    color: var(--ao-text);
}
.cal-day--outside .cal-day__num {
    color: var(--ao-text-muted);
}
.cal-day__dot {
    border-radius: 999px;
    min-width: 6px;
    min-height: 6px;
    background-color: transparent;
}
.cal-day__dot.cal-dot--1 { background-color: rgba(130, 168, 255, 0.55); }
.cal-day__dot.cal-dot--2 { background-color: rgba(120, 210, 150, 0.75); }
.cal-day__dot.cal-dot--3 { background-color: rgba(230, 175, 110, 0.85); }
.cal-day__dot.cal-dot--4 { background-color: rgba(220, 130, 145, 0.9); }

.cal-day--heat0 { background-color: var(--ao-surface-glass-soft); }
.cal-day--heat1 { background-color: rgba(130, 168, 255, 0.14); border-color: rgba(130, 168, 255, 0.18); }
.cal-day--heat2 { background-color: rgba(130, 168, 255, 0.22); border-color: rgba(130, 168, 255, 0.26); }
.cal-day--heat3 { background-color: rgba(120, 210, 150, 0.20); border-color: rgba(120, 210, 150, 0.30); }
.cal-day--heat4 { background-color: rgba(220, 130, 145, 0.22); border-color: rgba(220, 130, 145, 0.36); }

.cal-detail {
    padding: 4px 2px;
}
.cal-detail__label {
    font-size: 0.88em;
    color: var(--ao-text-muted);
}
.cal-legend {
    padding: 4px 2px;
}
.cal-legend__swatch {
    border-radius: 3px;
    border: 1px solid var(--ao-border-subtle);
}
.cal-legend__swatch.cal-day--heat0 { background-color: var(--ao-surface-glass-soft); }
.cal-legend__swatch.cal-day--heat1 { background-color: rgba(130, 168, 255, 0.14); }
.cal-legend__swatch.cal-day--heat2 { background-color: rgba(130, 168, 255, 0.22); }
.cal-legend__swatch.cal-day--heat3 { background-color: rgba(120, 210, 150, 0.20); }
.cal-legend__swatch.cal-day--heat4 { background-color: rgba(220, 130, 145, 0.22); }
.cal-legend__edge {
    font-size: 0.72em;
}
.cal-streak {
    font-size: 0.82em;
    color: rgba(230, 175, 110, 0.92);
    font-weight: 600;
}

/* ═══ Pomodoro (🍅 25/5 — AO Glass) ═══ */
.pomo-widget {
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
    background-color: var(--ao-surface-glass);
    box-shadow: inset 0 1px 0 var(--ao-highlight), 0 8px 24px rgba(0,0,0,0.14);
    padding-bottom: 2px;
}
.pomo-hero {
    background: linear-gradient(145deg, rgba(220, 130, 80, 0.09), var(--ao-surface-glass) 60%);
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
    padding: 12px 14px 10px;
}
.pomo-timer {
    font-size: 2.6em;
    font-weight: 800;
    letter-spacing: -0.02em;
    color: var(--ao-text);
    font-variant-numeric: tabular-nums;
}
.pomo-progress trough {
    min-height: 7px;
}
.pomo-progress progress {
    background: linear-gradient(90deg, rgba(220, 100, 80, 0.9), rgba(230, 175, 110, 0.9));
    border-radius: var(--ao-radius-pill);
}
.pomo-week-col {
    min-width: 28px;
}
.pomo-week-bar {
    background-color: rgba(220, 100, 80, 0.85);
    border-radius: var(--ao-radius-xs);
    border: 1px solid rgba(220, 100, 80, 0.4);
}
.pomo-week-bar--empty {
    background-color: var(--ao-surface-inset);
    border-color: var(--ao-border-subtle);
}
.pomo-week-bar--max {
    background-color: rgba(120, 210, 150, 0.85);
    border-color: rgba(120, 210, 150, 0.4);
}
.pomo-week-val {
    font-size: 0.78em;
    font-weight: 650;
    color: var(--ao-text);
    font-variant-numeric: tabular-nums;
}
.pomo-week-date {
    font-size: 0.66em;
    color: var(--ao-text-faint);
}

/* ═══ История версий (git history UI) ═══ */
.history-panel {
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
    background-color: var(--ao-surface-glass-soft);
    padding: 10px 12px;
    margin-top: 2px;
}
.history-title {
    font-size: 0.7em;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ao-text-muted);
}
.history-hint {
    font-size: 0.82em;
    color: var(--ao-text-faint);
    padding: 4px 2px;
}
.history-scroller {
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-sm);
    background-color: var(--ao-surface-inset);
    padding: 2px;
}
.history-list {
    background-color: transparent;
}
.history-list row {
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: var(--ao-radius-sm);
    margin: 1px 2px;
    padding: 1px 2px;
    color: var(--ao-text-muted);
    transition: background var(--ao-transition-fast), color var(--ao-transition-fast);
}
.history-list row:hover {
    background-color: var(--ao-surface-row-hover);
    color: var(--ao-text-secondary);
}
.history-list row:selected {
    background-image: linear-gradient(135deg, rgba(130, 168, 255, 0.16), rgba(130, 168, 255, 0.06));
    border: 1px solid rgba(130, 168, 255, 0.22);
    color: var(--ao-text);
}
.history-list row:selected label {
    color: var(--ao-text);
}
.history-subject {
    font-size: 0.88em;
    font-weight: 600;
    color: var(--ao-text-secondary);
}
.history-meta {
    font-size: 0.75em;
}
.history-diff-scroller {
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-sm);
    background-color: #0a0e16;
    padding: 2px;
}
.history-diff {
    font-family: "JetBrains Mono", monospace;
    font-size: 12px;
    color: var(--ao-text-secondary);
    background-color: #0a0e16;
}
.history-refresh {
    min-width: 28px;
    min-height: 28px;
}

/* ═══ Mobile адаптивность (<700px) — bottom bar + скрытый сайдбар ═══ */
.mobile-bottom-bar {
    background-color: rgba(10, 14, 22, 0.97);
    border-top: 1px solid var(--ao-border-subtle);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
    padding: 4px 6px;
    min-height: 52px;
}
.mobile-nav-btn {
    min-width: 44px;
    min-height: 44px;
    padding: 0;
    border-radius: var(--ao-radius-sm);
    border: 1px solid transparent;
    background-color: transparent;
    color: var(--ao-text-muted);
    font-size: 18px;
    transition: background var(--ao-transition-fast), color var(--ao-transition-fast);
}
.mobile-nav-btn:hover {
    background-color: var(--ao-surface-control-hover);
    color: var(--ao-text);
}
.mobile-nav-btn:checked {
    background-color: rgba(130, 168, 255, 0.16);
    border-color: rgba(130, 168, 255, 0.32);
    color: rgba(130, 168, 255, 1);
}
.mobile-menu-btn {
    color: var(--ao-text-secondary);
}
/* узкий сайдбар уже скрыт Breakpoint'ом, но на всякий — при narrow убираем правый бордер */
.narrow .side-column {
    border-right: none;
}
.narrow .sidebar {
    min-width: 0;
}
/* mobile-bar 52px перекрывает ScrolledWindow — отступ снизу чтобы последняя карточка не уходила под бар */
scrolledwindow {
    margin-bottom: 56px;
}
.narrow scrolledwindow {
    margin-bottom: 64px;
}

/* ═══ Light тема — токены переопределены через prefers-color-scheme ═══ */
@media (prefers-color-scheme: light) {
    window {
        --ao-bg-deep: #eef1f7;
        --ao-bg-elevated: #f4f6fb;
        --ao-bg-pane: #ffffff;
        --ao-surface-glass: rgba(255, 255, 255, 0.88);
        --ao-surface-glass-hover: rgba(244, 246, 251, 0.96);
        --ao-surface-glass-soft: rgba(0, 0, 0, 0.03);
        --ao-surface-control: rgba(0, 0, 0, 0.05);
        --ao-surface-control-hover: rgba(0, 0, 0, 0.08);
        --ao-surface-row-hover: rgba(0, 0, 0, 0.04);
        --ao-surface-panel: rgba(255, 255, 255, 0.96);
        --ao-surface-panel-elevated: rgba(255, 255, 255, 0.98);
        --ao-surface-inset: rgba(0, 0, 0, 0.04);
        --ao-highlight: rgba(255, 255, 255, 0.7);
        --ao-border-subtle: rgba(0, 0, 0, 0.08);
        --ao-border-strong: rgba(0, 0, 0, 0.14);
        --ao-text: rgba(18, 24, 38, 0.96);
        --ao-text-secondary: rgba(30, 35, 46, 0.88);
        --ao-text-muted: rgba(58, 68, 85, 0.72);
        --ao-text-faint: rgba(107, 122, 144, 0.55);
        --ao-selected: rgba(43, 94, 163, 0.14);
        --ao-selected-border: rgba(43, 94, 163, 0.28);
        --ao-focus-ring: 0 0 0 2px rgba(43, 94, 163, 0.25);
        background-color: var(--ao-bg-pane);
        color: var(--ao-text-secondary);
    }
    .fragile-shell {
        background-color: var(--ao-bg-deep);
    }
    .side-column {
        background-color: var(--ao-surface-panel);
    }
    .sb {
        background-color: var(--ao-surface-panel);
    }
}
/* Принудительные классы от theme_manager — перекрывают auto */
window.light {
    --ao-bg-deep: #eef1f7;
    --ao-bg-elevated: #f4f6fb;
    --ao-bg-pane: #ffffff;
    --ao-surface-glass: rgba(255, 255, 255, 0.88);
    --ao-surface-glass-hover: rgba(244, 246, 251, 0.96);
    --ao-surface-glass-soft: rgba(0, 0, 0, 0.03);
    --ao-surface-control: rgba(0, 0, 0, 0.05);
    --ao-surface-control-hover: rgba(0, 0, 0, 0.08);
    --ao-surface-row-hover: rgba(0, 0, 0, 0.04);
    --ao-surface-panel: rgba(255, 255, 255, 0.96);
    --ao-surface-panel-elevated: rgba(255, 255, 255, 0.98);
    --ao-surface-inset: rgba(0, 0, 0, 0.04);
    --ao-highlight: rgba(255, 255, 255, 0.7);
    --ao-border-subtle: rgba(0, 0, 0, 0.08);
    --ao-border-strong: rgba(0, 0, 0, 0.14);
    --ao-text: rgba(18, 24, 38, 0.96);
    --ao-text-secondary: rgba(30, 35, 46, 0.88);
    --ao-text-muted: rgba(58, 68, 85, 0.72);
    --ao-text-faint: rgba(107, 122, 144, 0.55);
    --ao-selected: rgba(43, 94, 163, 0.14);
    --ao-selected-border: rgba(43, 94, 163, 0.28);
    --ao-focus-ring: 0 0 0 2px rgba(43, 94, 163, 0.25);
    background-color: var(--ao-bg-pane);
    color: var(--ao-text-secondary);
}
window.dark {
    --ao-bg-deep: #06080d;
    --ao-bg-elevated: #0a0e16;
    --ao-bg-pane: #070a12;
    --ao-surface-glass: rgba(15, 19, 30, 0.84);
    --ao-surface-glass-hover: rgba(25, 31, 46, 0.97);
    --ao-surface-glass-soft: rgba(255, 255, 255, 0.035);
    --ao-surface-control: rgba(255, 255, 255, 0.05);
    --ao-surface-control-hover: rgba(255, 255, 255, 0.1);
    --ao-surface-row-hover: rgba(255, 255, 255, 0.05);
    --ao-surface-panel: rgba(10, 14, 22, 0.92);
    --ao-surface-panel-elevated: rgba(14, 18, 28, 0.96);
    --ao-surface-inset: rgba(0, 0, 0, 0.3);
    --ao-highlight: rgba(255, 255, 255, 0.045);
    --ao-border-subtle: rgba(255, 255, 255, 0.07);
    --ao-border-strong: rgba(255, 255, 255, 0.13);
    --ao-text: rgba(228, 234, 246, 0.94);
    --ao-text-secondary: rgba(210, 218, 232, 0.88);
    --ao-text-muted: rgba(175, 186, 204, 0.72);
    --ao-text-faint: rgba(140, 152, 172, 0.55);
}

/* ═══ Комментарии к строкам — gutter справа от редактора ═══ */
.comments-gutter {
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-md);
    background-color: var(--ao-surface-glass-soft);
    padding: 10px 10px;
    min-width: 220px;
}
.comments-title {
    font-size: 0.7em;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ao-text-muted);
}
.comments-subtitle {
    font-size: 0.68em;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ao-text-faint);
    margin-top: 4px;
}
.comments-line-flow {
    background-color: transparent;
}
.comments-line-btn {
    min-width: 28px;
    min-height: 24px;
    padding: 0 4px;
    border-radius: var(--ao-radius-xs);
    font-size: 0.82em;
    font-family: "JetBrains Mono", monospace;
    border: 1px solid var(--ao-border-subtle);
    background-color: var(--ao-surface-control);
    color: var(--ao-text-muted);
}
.comments-line-btn:hover {
    background-color: var(--ao-surface-control-hover);
    border-color: var(--ao-border-strong);
    color: var(--ao-text);
}
.comments-line-has {
    background-color: rgba(130, 168, 255, 0.16);
    border-color: rgba(130, 168, 255, 0.32);
    color: #a8c8ff;
    font-weight: 600;
}
.comments-line-scroller {
    background-color: transparent;
}
.comments-scroller {
    background-color: transparent;
}
.comments-list {
    background-color: transparent;
}
.comments-list row {
    background-color: var(--ao-surface-glass);
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-sm);
    margin: 2px 0;
}
.comments-list row:hover {
    background-color: var(--ao-surface-glass-hover);
    border-color: var(--ao-border-strong);
}
.comments-badge {
    min-width: 28px;
    min-height: 22px;
    padding: 0 6px;
    border-radius: var(--ao-radius-pill);
    font-size: 0.75em;
    font-weight: 600;
    font-family: "JetBrains Mono", monospace;
    background-color: rgba(130, 168, 255, 0.14);
    border: 1px solid rgba(130, 168, 255, 0.28);
    color: #a8c8ff;
}
.comments-text {
    font-size: 0.88em;
    color: var(--ao-text-secondary);
}
.comments-meta {
    font-size: 0.72em;
}
.comments-entry {
    font-family: "JetBrains Mono", monospace;
    font-size: 13px;
    background-color: var(--ao-surface-inset);
    border: 1px solid var(--ao-border-subtle);
    border-radius: var(--ao-radius-sm);
}
"""

# ── Масштабирование ────────────────────────────────────────────
# Шрифты редактора берутся в отдельный зум (меняются Ctrl+=/Ctrl+-),
# остальные font-size масштабируются общим множителем интерфейса.
_EDITOR_MONO = 13.0
# Базовый шрифт заметки 17.5px = 0.92rem от базы 19px (Obsidian baseFontSize,
# ao-glass-note §1: font-size: 0.92rem). Зум отдельный (меняется Ctrl+=/Ctrl+-).
_EDITOR_PROSE = 17.5
_SENTINEL_MONO = "@@EDITOR_MONO@@"
_SENTINEL_PROSE = "@@EDITOR_PROSE@@"
_FONT_SIZE = re.compile(r"(font-size:\s*)([\d.]+)px")
# Масштабирование «интерьера»: все px-длины в объявлениях (кроме font-size)
# умножаются на interior_fit, чтобы панели, отступы и радиусы следовали за окном.
_DECL_PX = re.compile(r"([\w-]+)\s*:\s*([^;{}]+);")
_PX_NUM = re.compile(r"([\d.]+)px")
_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _scale_lengths(css: str, fit: float) -> str:
    """Умножает все px-длины (кроме font-size) на fit. Комментарии маскируются."""
    css = _COMMENT.sub("/*c*/", css)

    def _value(m):
        name, value = m.group(1), m.group(2)
        if name == "font-size":
            return m.group(0)
        return f"{name}: {_PX_NUM.sub(lambda n: _fmt_px(float(n.group(1)) * fit), value)};"

    return _DECL_PX.sub(_value, css)


# Кэш CSS: (ui, zoom, fit) -> готовая строка. Перегенерация — дорогая
# (2 re.sub по ~1600 строк, ~120 мс), поэтому храним результаты и не
# инвалидируем при delta fit <= 0.05.
_css_cache: dict[tuple[float, float, float], str] = {}
_CACHE_MAX = 32
_CACHE_DELTA = 0.05

_provider: Gtk.CssProvider | None = None


def _fmt_px(value: float) -> str:
    value = round(value, 1)
    if abs(value - round(value)) < 0.05:
        return f"{round(value)}px"
    return f"{value:g}px"


def build_css(ui_scale: float = 1.0, editor_zoom: float = 1.0, interior_fit: float = 1.0) -> str:
    """Собирает CSS: font-size масштабируется ui_scale (+ editor_zoom для редактора).

    interior_fit — «привязка к окну»: умножает все остальные px-длины (отступы,
    панели, радиусы, обводки), чтобы внутренний интерфейс пропорционально
    следовал за внешними размерами окна. Размеры в em масштабируются сами.

    Кэшируется по (ui, zoom, fit); fit с допуском 0.05 — мелкие изменения
    окна не триггерят перегенерацию (2 re.sub по 1600 строк, ~120 мс).
    """
    ui = max(0.5, min(4.0, float(ui_scale)))
    zoom = max(0.5, min(4.0, float(editor_zoom)))
    fit = max(0.5, min(2.0, float(interior_fit)))

    # быстрый путь — точное совпадение
    key = (ui, zoom, fit)
    if key in _css_cache:
        return _css_cache[key]
    # допуск по fit: если есть кэш с тем же ui/zoom и |fit - cached_fit| <= 0.05 — reuse
    for (cu, cz, cf), cached in _css_cache.items():
        if cu == ui and cz == zoom and abs(cf - fit) <= _CACHE_DELTA:
            return cached

    css = CSS
    # GTK4 не поддерживает aspect-ratio — убираем перед загрузкой (адаптивность
    # достигается min-width:0 + homogeneous Grid, а сам токен остаётся в CSS для проверки)
    css = re.sub(r"aspect-ratio\s*:\s*[^;]+;\s*", "", css)
    css = css.replace(f"font-size: {_EDITOR_MONO:g}px;", f"font-size: {_SENTINEL_MONO};")
    css = css.replace(f"font-size: {_EDITOR_PROSE:g}px;", f"font-size: {_SENTINEL_PROSE};")

    if abs(fit - 1.0) >= 0.005:
        css = _scale_lengths(css, fit)
    css = _FONT_SIZE.sub(lambda m: f"{m.group(1)}{_fmt_px(float(m.group(2)) * ui)}", css)
    css = css.replace(_SENTINEL_MONO, _fmt_px(_EDITOR_MONO * ui * zoom))
    css = css.replace(_SENTINEL_PROSE, _fmt_px(_EDITOR_PROSE * ui * zoom))

    _css_cache[key] = css
    if len(_css_cache) > _CACHE_MAX:
        # LRU-эвикция: удаляем самый старый ключ (dict сохраняет порядок вставки)
        oldest = next(iter(_css_cache))
        del _css_cache[oldest]
    return css


def clear_css_cache() -> None:
    """Инвалидировать кэш (для тестов / смены темы)."""
    _css_cache.clear()


def apply_css(ui_scale: float = 1.0, editor_zoom: float = 1.0, interior_fit: float = 1.0) -> None:
    """(Пере)применяет CSS приложения с новым масштабом на весь display."""
    global _provider
    display = Gdk.Display.get_default()
    if display is None:
        return
    if _provider is not None:
        Gtk.StyleContext.remove_provider_for_display(display, _provider)
    provider = Gtk.CssProvider()
    provider.load_from_string(build_css(ui_scale, editor_zoom, interior_fit))
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _provider = provider


def load_css() -> None:
    """Классический вход (масштаб 1:1); окно затем применяет реальный масштаб."""
    apply_css(1.0, 1.0)


# ── Кастом CSS из vault/_System/custom.css ───────────────────────
# Отдельный провайдер поверх базового (приоритет USER > APPLICATION),
# чтобы пользователь мог переопределить токены/классы без патча исходника.

CUSTOM_CSS_REL = Path("_System") / "custom.css"
CUSTOM_CSS_REL_STR = "_System/custom.css"

_custom_provider: Gtk.CssProvider | None = None


def get_custom_css_path(settings: dict) -> Path:
    """Путь к vault/_System/custom.css."""
    vault_root = str(settings.get("vault_root") or Path.home() / "desktop")
    return Path(vault_root) / CUSTOM_CSS_REL


def custom_css_path(settings: dict) -> Path:
    """Алиас для совместимости с theme_manager."""
    return get_custom_css_path(settings)


def load_custom_css(settings: dict) -> str | None:
    """Прочитать custom.css, вернуть текст или None."""
    path = get_custom_css_path(settings)
    try:
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return None
        return text
    except OSError:
        return None


def is_custom_css_available(settings: dict) -> bool:
    try:
        return get_custom_css_path(settings).is_file()
    except Exception:
        return False


def clear_custom_css() -> None:
    """Убрать провайдер кастом CSS."""
    global _custom_provider
    if _custom_provider is None:
        return
    try:
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.remove_provider_for_display(display, _custom_provider)
    except Exception:
        pass
    finally:
        _custom_provider = None


def apply_custom_css(settings: dict) -> bool:
    """Загрузить и применить custom.css поверх базовых стилей. True если применён."""
    global _custom_provider
    css_text = load_custom_css(settings)
    if css_text is None:
        clear_custom_css()
        return False
    try:
        display = Gdk.Display.get_default()
        if display is None:
            return True
        if _custom_provider is not None:
            try:
                Gtk.StyleContext.remove_provider_for_display(display, _custom_provider)
            except Exception:
                pass
            _custom_provider = None
        provider = Gtk.CssProvider()
        provider.load_from_string(css_text)
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )
        _custom_provider = provider
        return True
    except Exception:
        return False


def reload_custom_css(settings: dict) -> bool:
    """Перезагрузить кастом CSS."""
    clear_custom_css()
    return apply_custom_css(settings)
