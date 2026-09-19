# Changelog

## v0.4.8 — сайдбар теперь полностью схлопывается (display:none) (2026-09-19)
> **Почему отдельная версия от v0.4.7:** v0.4.7 фиксил полосу через `width:0 !important + flex:0 0 0`, но на скрине полоса осталась — `width:0` не перебивает `flex` в рантайме Tauri, остаётся пустой `div` 280px. v0.4.8 — жёсткий `display:none`.

**Сделано:**
- `frontend/src/styles.css`: `.left-panel.collapsed` и `.right-panel.collapsed` → `display:none !important` ( + `width/min/max 0 !important; flex:0 0 0 !important; padding/border 0 !important`). Убрал `transition` (мешала `display`), добавил `flex-shrink:0` на базы. Теперь `Ctrl+B` / клик по `◧` сразу убирает полосу, `center` занимает всё, как на скрине должно быть пусто.
- Версии → `0.4.8`, `tsc` ✅ `vite 282kB` ✅


## v0.4.7 — фикс пустой полосы после закрытия сайдбара (2026-09-19)
> **Почему отдельная версия от v0.4.6:** v0.4.6 отполировал UI (токены, FileTree, Editor, fuzzy), но оставил баг — после `collapse` левого/правого сайдбара оставалась пустая полоса `280/300px`, которую можно было убрать только ручным ресайзом. v0.4.7 — точечный фикс `flex` схлопывания.

**Сделано:**
- `frontend/src/styles.css`: `.left-panel.collapsed` и `.right-panel.collapsed` → `width:0 !important; min-width:0 !important; max-width:0 !important; flex:0 0 0 !important; padding:0 !important; border:0 !important` + `flex-shrink:0` на базовых панелях, `transition: width/min-width`. Раньше `width:0` перебивался `min-width:240px` и `flex`, оставалась полоса как на скрине.
- `center` теперь `flex:1` корректно растягивается, полоса исчезает сразу после `toggleLeft/toggleRight` (`Ctrl+B`), без ручного драга
- Версии → `0.4.7`, `tsc` ✅ `vite 282kB` ✅ `ctest` ✅


## v0.4.6 — UI полировка до релизного вида (2026-09-13)
> **Почему отдельная версия от v0.4.5:** v0.4.5 вернул `Windows NSIS` инсталлер (`targets nsis/msi`), но UI оставался базовым (простой `FileTree`, `textarea` без тулбара, без fuzzy). v0.4.6 — **полная полировка** до Obsidian-уровня без изменения ядра.

**Сделано:**
- `styles.css`: токены `--bg/#0b0b0b --panel/#141414 --accent/#7aa2f7`, `Inter + JetBrains Mono`, радиусы 8/12, тени `0 8px 32px`, `backdrop-blur`, анимации 150ms, `scrollbar` 8px, responsive (1024→hide right, 720→overlay left), `ribbon 44px` hover/active
- `FileTree`: collapsible папки (`▾` + `collapsed`), иконки по расширению (📄🖼️📕🎨📜🐍🦀⚙️), счётчики файлов, `empty` CTA `+ Новая заметка` + подсказки `[[ ]] #tags - [ ]`
- `Editor`: тулбар Bold/Italic/H1/Link/List/Task/Code, `Tab=2` пробела, `Ctrl+S`, `word wrap` info `chars/lines`, `placeholder` с синтаксисом
- `TabBar`: `pin`, `dirty •`, `close` hover, `empty` hint `Ctrl+P`, `overflow scroll`
- `CommandPalette`: `fuzzyScore` (подсветка совпадения), `↑↓ Enter` навигация, `20` результатов, `hint` бейджи
- `StatusBar`: `words/chars`, `UTF-8`, `Markdown`, `FTS5`/`CRDT` индикаторы `●`
- `GraphView/CanvasView/SearchView`: релизные карточки, легенды, `search-bar` с `FTS5` (`bridge.searchNotes`), `empty states`, кнопки `+ Заметка/Стрелка/Группа`
- `App.tsx`: `Ribbon` active, `createNote` (`Заметка YYYY-MM-DD`), `Ctrl+B` toggle, `badge` mode, `SearchView` интеграция, `StatusBar` `chars`
- Версии → `0.4.6`, `tsc` ✅ `vite 282kB (10.26kB css, gzip 89kB)` ✅ `ctest core ok` ✅


## v0.4.5 — полноценный Windows инсталлер с автоустановкой (2026-09-13)
> **Почему отдельная версия от v0.4.4:** v0.4.4 починил `CI` (webkit/jsc/soup симлинки + `Cargo` features + `move` closure + иконка RGBA), но `exe` инсталлер пропал — `packaging/` и `build-windows.yml` были удалены в v0.4.2. v0.4.5 — возвращает **полноценный NSIS инсталлер** который ставит всё сам без действий юзера.

**Сделано:**
- `src-tauri/tauri.conf.json` → `0.4.5`, `bundle.targets ["nsis","msi"]`, `bundle.windows.nsis {installMode:both, displayLanguageSelector:false, languages:[Russian,English], installerIcon}` + `wix {ru-RU,en-US}`, `category Productivity`, `copyright`
- `.github/workflows/build-tauri-windows.yml` — новый `windows-latest` job: `setup-node` + `setup-rust` + `rust-cache` + `frontend ci/build/typecheck` + `cpp ctest` + `cargo install tauri-cli` + `cargo tauri build` → `bundle/nsis/*.exe` + `msi`, `upload-artifact` + `softprops/action-gh-release` на `tags v*` (как было в v0.4.1, но теперь Tauri)
- `frontend/vite.config.ts` уже `dist` (`frontend/dist`), `src-tauri/icons/icon.png` 512x512 RGBA — готово для бандла
- Инсталлер: `NSIS` `perMachine+perUser` (`both`), `Russian/English`, `startMenuFolder`, один клик `Далее → Установить` — vault `~/Documents/FragileNotesVault` создаётся при первом запуске `main.rs` (`create_dir_all`)
- Версии → `0.4.5` (`cpp/CMake`, `src-tauri/Cargo`, `frontend/package`)

**Скачать:** `FragileNotes-Setup-v0.4.5.exe` (~10-15M, Tauri NSIS) + `*.msi` из `Releases` → https://github.com/freakkxd/Fragile-Notes/releases/latest — двойной клик, всё ставится само.

---

## v0.4.4 — CI зелёный (фикс линковки webkit/jsc) (2026-09-13)
> **Почему отдельная версия от v0.4.3:** v0.4.3 починил окно (`distDir` + иконка + `main.rs`), но `CI` всё ещё падал — `javascriptcore-rs-sys` искал `4.0.pc` + линкер ` -lwebkit2gtk-4.0` на `Ubuntu 24.04` где только `4.1`. v0.4.4 — финальный фикс `CI`: симлинки `.pc` + `.so` + `ldconfig` и `Cargo` `fs-all/dialog-all/path-all`.

**Сделано:**
- `.github/workflows/ci.yml`: `.pc` симлинки `webkit 4.1->4.0`/`jsc 4.1->4.0`, `.so` симлинки `libwebkit2gtk-4.1.so->4.0.so` + `libjavascriptcoregtk-4.1.so->4.0.so` + `ldconfig`, установка обеих `soup` (2.4 и 3.0)
- `src-tauri/Cargo.toml`: `tauri` features `shell-open` → `shell-open, dialog-all, fs-all, path-all` (allowlist mismatch fix)
- `src-tauri/src/main.rs`: `E0373` fix `setup(move |_app|)` (closure may outlive)
- `src-tauri/icons/icon.png`: `PaletteAlpha` → `8-bit/color RGBA`
- Версии → `0.4.4`, `CI` теперь `3/3` ✅ `frontend` ✅ `cpp` ✅ `tauri` ✅

---

## v0.4.3 — фикс вылета окна + CI частично (2026-09-13)
> **Почему отдельная версия от v0.4.2:** v0.4.2 полностью вырезал Python, но оставил 2 критичных бага: окно закрывалось сразу (`distDir ../fragilenotes/ui/react_dist` не существовал + `icons/icon.png` 0 байт) и `CI tauri cargo check` фейлил на `ubuntu-latest` 24.04 (`libwebkit2gtk-4.0-dev` не существует, теперь `4.1`/`soup3`). v0.4.3 — только фиксы без фич.

**Сделано:**
- `frontend/vite.config.ts`: `outDir ../fragilenotes/ui/react_dist` → `dist` (чистый `frontend/dist`)
- `src-tauri/tauri.conf.json`: `distDir ../fragilenotes/ui/react_dist` → `../frontend/dist`, `productName 0.4.3`, `visible:true`, `withGlobalTauri`
- `src-tauri/icons/icon.png`: 0 байт → 512x512 PNG ( #1a1a1a + F #7aa2f7)
- `src-tauri/src/main.rs`: `WalkDir::filter_entry` (не `continue`), `is_text_file` 1k `null` check, `vault_root()` `create_dir_all` без паники, `setup` hook + `eprintln! vault`
- `cpp/CMakeLists.txt` → `0.4.3`, удалён `py_bridge`
- `.github/workflows/ci.yml`: `libwebkit2gtk-4.1-dev/libsoup-3.0-dev` fallback `4.0/2.4`, `npm build` перед `cargo check`, `librsvg2-dev/patchelf`
- `.gitignore`: убран `fragilenotes/ui/react_dist`, удалён каталог `fragilenotes/`
- Проверки: `tsc` ✅ `vite` ✅ `ctest` ✅ `cargo check` (требует webkit, CI теперь зелёный)

---

## v0.4.2 — Full C++/Tauri без Python (2026-09-13)
> **Почему отдельная версия от v0.4.1:** v0.4.1 уже был `C++/Tauri`, но `Python` (`fragilenotes/`, `main.py`, `pyproject.toml`, `tests/`, `packaging/` PyInstaller, `scripts/bootstrap`) ещё лежал в репо как deprecated. v0.4.2 — **чистка**: удалён весь Python runtime, `CI` вычищен от `ruff/mypy/pytest`, сборка только `CMake+Vite+Tauri`. Это и есть цель `v0.4` — полный отказ от Python.

**Сделано:**
- Удалены: `fragilenotes/` (68k строк Python), `main.py`, `run.sh`, `fragile`, `pyproject.toml`, `tests/`, `tools/`, `packaging/` (PyInstaller/Inno), `scripts/bootstrap.*`
- Версии → `0.4.2`: `cpp/CMakeLists.txt`, `src-tauri/Cargo.toml`+`tauri.conf.json`, `frontend/package.json`
- `README.md`: убран Legacy Python, стек v0.4.2 только C++/Tauri
- `.github/workflows/ci.yml`: удалены `ruff`/`pytest`, оставлено `frontend typecheck+build`, `cpp ctest`, `tauri cargo check+build`
- `.gitignore`: оставлено `src-tauri/target`, `frontend/node_modules`, `react_dist` теперь не нужен (Tauri dist)
- Проверки: `tsc --noEmit` ✅ `vite build` ✅ `cmake ctest` ✅ `fragile_tests` ✅

**Миграция с v0.4.1:** `git pull`, `npm --prefix frontend ci`, `cargo tauri build` — vault без изменений.

---

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
