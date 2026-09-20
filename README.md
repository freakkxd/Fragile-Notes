# Fragile Notes — v0.5.1

> **Подпись v0.5.1 — почему отделили эту версию:**
> - **Сделано в 0.5.0:** нативный `AO Engine` (`collect` + `Web Clipper` + `enrich` `Qwen3`).
> - **Почему 0.5.1 отдельно:** **Task Manager v2** — `6` поверхностей (`Start/Today/Day/Workspace/Board/Completed`) без `Dataview` костылей, `statusLaw` + `todayBucket` + `Kanban` + `Review` как в `Obsidian` `v1`.
> - **Цель 0.5.1:** `Tasks` релизно, без `Dataview`.

**Obsidian-like vault.** `Task Manager v2` `Tauri` `C++` `React`.

![CI](https://github.com/freakkxd/Fragile-Notes/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/freakkxd/Fragile-Notes)
![License](https://img.shields.io/github/license/freakkxd/Fragile-Notes)

## Task Manager v2

| Поверхность | Когда | Что |
|-------------|-------|-----|
| `Start` | Старт сессии | `KPI` `counts` → `Today` |
| `Today` | Делать сегодня | `focus` `Выполнено` |
| `Day` | Прожить день | `02 Daily/{date}.md` `embed` |
| `Workspace` | Списки | `project` фильтр |
| `Board` | Канбан | `todo/doing/done` `drag` |
| `Completed` | Ретро | `done/cancelled` + `Archive` |

`store` `SQLite` `WAL` + `Tasks/*.md` `frontmatter` `sync`, `statusLaw` `terminal`, `todayBucket`.

## Быстрый старт

```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
npm --prefix frontend install && npm --prefix frontend run build
cargo tauri dev    # Task Manager: Ribbon ✓
```

## Версионирование
- `v0.5.0` — `AO` нативно
- `v0.5.1` — **Task Manager v2** (текущий)

## Лицензия
MIT — `LICENSE`
