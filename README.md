# Fragile Notes — v0.5.0

> **Подпись v0.5.0 — почему отделили эту версию:**
> - **Сделано в 0.4.15:** визуально богаче (`glass`/`glow`).
> - **Почему 0.5.0 отдельно:** **нативный `AO Engine`** — `сбор` (`wrapWebClips` + `telegram`) + `Web Clipper` (`selection`/`html` → `Sources/web-clips` → `05 Sort`) + `обогащение` (`Qwen3-14B` `enrich_notes`) портированы из `Node` `ao-engine` в `Tauri`+`C++`+`React` без `Node`.
> - **Цель 0.5.0:** `Obsidian` `Vault` `AO` фичи нативно в `Fragile Notes`.

**Obsidian-like vault.** `AO` нативно `Tauri` `C++` `React`.

![CI](https://github.com/freakkxd/Fragile-Notes/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/freakkxd/Fragile-Notes)
![License](https://img.shields.io/github/license/freakkxd/Fragile-Notes)

## AO Нативно

| Фича `Obsidian` | Нативно в `Fragile Notes` |
|---|---|
| `Сбор` `Sources` (`telegram`, `web-clips`, `raw`) | `collect_sources` `Rust` `walkdir` → `05 Sort` `ao_sort_status: undecided` |
| `Web Clipper` (браузер `Obsidian`) | `WebClipper.tsx` `✂ Clip` (`selection`/`html` → `Sources/web-clips` → `auto wrap`) `Ctrl+Shift+W` |
| `Обогащение` `enrichNotes` (`LLM`) | `enrich_notes` `Qwen3-14B` `8010` `JSON tags/links` → `enriched: true` |

`Vault` `~/Documents/FragileNotesVault` (`_System/ArchiveOrganism` + `05 Sort`).

## Быстрый старт

```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
npm --prefix frontend install && npm --prefix frontend run build
cargo tauri dev    # Clip: выдели текст в Tauri WebView → ✂ Clip в правом сайдбаре
cargo tauri build  # -> AppImage
```

## Версионирование
- `v0.4.15` — визуально богаче
- `v0.5.0` — **нативный `AO`** (текущий)

## Лицензия
MIT — `LICENSE`
