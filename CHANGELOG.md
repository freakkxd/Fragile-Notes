# Changelog

## v0.4.1 — Full Tauri + C++ (2026-09-13)
> **Почему отдельная версия от v0.3.10:** v0.3.10 был последним `Python GTK` релизом с `C++ заглушками + React внутри WebView`. v0.4.1 — **полный переезд на C++/Tauri**: Python удалён из рантайма, `fragilenotes/ui` заменён на `React 18 + TypeScript + Zustand + Tauri IPC`, `cpp/` стал единственным ядром (vault, fts SQLite FTS5, crypto AES-GCM/PBKDF2, CRDT LWW/RGA, tasks, srs, link_index, dataview). Сборка теперь `CMake + Vite + Tauri bundler` вместо `PyInstaller + GTK`.

**Сделано:**
- `cpp/CMakeLists.txt` → `0.4.1`, добавлен `link_index.cpp`, `fts.hpp`, `dataview.hpp`, C++20, OpenSSL + SQLite
- `cpp/include/fragile/*`: новые модули `fts`, `dataview`, `link_index` (wikilinks `[[ ]]`, #tags, backlinks) — parity с `fragilenotes/core/*.py`
- `cpp/src/services/fts.cpp`: FTS5 `fts_init/index/search/rebuild`
- `frontend/`: полный ребилд JS→TS: `Ribbon` (Obsidian ribbon), `FileTree` (группировка по директориям), `TabBar`, `Editor`, `Preview` (marked+DOMPurify), `GraphView`, `CanvasView`, `CommandPalette` (Ctrl+P), `StatusBar`, `Zustand store`, `bridge.ts` (Tauri invoke + zod runtime validation, fallback legacy `window.fragileBridge`)
- `frontend/vite.config.ts`: `dist` → `fragilenotes/ui/react_dist` (совместимость с Python WebView пока)
- `src-tauri/`: новый Tauri 1.6 каркас: `Cargo.toml`, `tauri.conf.json`, `src/main.rs` — команды `list_notes/read_note/write_note/fts_search/get_links` с валидацией путей (no `..` traversal), `walkdir`, `rusqlite FTS5`, vault `~/Documents/FragileNotesVault`
- `pyproject.toml` → `0.4.1` (Python помечен deprecated, оставлен для совместимости, рантайм теперь Tauri)
- Сборка: `npm run typecheck` ✅, `npm run build` ✅ (274kB), `cmake --build` ✅, `fragile_tests` ✅
- Документация: обновлён `ARCHITECTURE.md` и `README.md` (стек C++/Tauri)

**Почему версия идёт отдельно:** без обратной совместимости по рантайму — старые `FragileNotes-Setup-v0.3.x.exe` (PyInstaller) не обновляются на Tauri bundle; vault остаётся совместим (`~/Documents/FragileNotesVault` — те же `.md`).

**Миграция:** просто установи `FragileNotes-Setup-v0.4.1.exe` (Tauri bundle) — vault подхватится автоматом. Python не нужен.

---

## v0.3.10 — системные папки не на рабочем столе (2026-09-12)
- Fix `vault`: системные папки не на рабочем столе + не закрывается сразу

## v0.3.0 — C++ core + React inside, notes как корень
- Первый гибрид: C++ заглушки + React WebView bridge

## v0.2.0 — successful PC port, focus shift
- Порт на ПК, Full Offline, фиксация стабильной базы перед C++ портом
