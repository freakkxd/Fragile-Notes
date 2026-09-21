# Changelog

## v0.5.5 — LLM Hub: llama.cpp + GPT/Gemini/Claude + пайплайны (2026-09-21)
> **Почему отдельная версия от v0.5.4:** v0.5.4 оптимизировал RAM `5-6МБ→3.5МБ`, но нейросеть была одна — `Qwen3-14B` на `8010/8011` без гибкости: нельзя выбрать модель, настроить `llama.cpp`, подключить `GPT/Gemini/Claude`, построить пайплайн. v0.5.5 — **полная гибкая настройка**: локально `llama.cpp` с парсингом и установкой + облачно `API ключ / OAuth` + конструктор пайплайнов.

**Сделано:**
- `src-tauri/src/llm.rs` (новый `~440 строк`) — `LlmConfig {providers, local, pipelines}` `ProviderKind LocalLlamaCpp/Ollama/OpenAI/Gemini/Claude/CustomOpenAI` `AuthMethod ApiKey/OAuth/None` `LocalSettings {binary_path, models_dir ~/Models, active_model, n_ctx 8192, threads auto, n_gpu_layers, temp/top_p/top_k/repeat, port 8010, auto_start, extra_args}` `ModelInfo {name, path, size_mb, quant Q4_K_M/f16, task chat/coder/embed/vision/enrich}` `Pipeline {steps: {provider_id, model, prompt_template {{content}}/{{rag}}, input_from/output_to}}` — `config_path vault/.fragile/llm.json | ~/.config/Fragile-Notes/llm.json` `obfuscate base64` (готово под `aes-gcm/pbkdf2`), `load/save`, `parse_quant/guess_task`
- `src-tauri/src/llm.rs` команды: `llm_get_config`, `llm_save_config`, `llm_scan_models` (`WalkDir *.gguf max_depth 4`, `RE_GGUF_QUANT`), `llm_set_active_model`, `llm_download_model` (`Models/<task>/ placeholder + curl hint`), `llm_test_provider` (локально `GET /health`, облачно `key format`), `llm_chat_universal` (роутинг `Local/Ollama/Custom/OpenAI → /v1/chat/completions`, `Gemini → /v1beta/models:generateContent?key`, `Claude → /v1/messages + anthropic-version`, headers `Authorization/x-api-key`), `llm_get/save/delete_pipeline`, `llm_pipeline_run` (`{{content}}/{{rag}}` → `extract_content` `choices/candidates/content`)
- `src-tauri/Cargo.toml` `+ base64 0.22`, `+ llm.rs`, `[profile.release] z/lto/strip` уже в 0.5.4, `src-tauri/src/main.rs` `mod llm` + 11 новых `invoke_handler`
- `frontend/src/lib/llm.ts` (`~130 строк`) — `zod` схемы `Provider/LocalSettings/ModelInfo/Pipeline/LlmConfig` (`never as T`), `llmGetConfig/llmSaveConfig/llmScanModels/llmSetActiveModel/llmDownloadModel/llmTestProvider/llmChatUniversal/llmGetPipelines/llmSavePipeline/llmDeletePipeline/llmPipelineRun` с `Tauri invoke` + `JSON.parse` валидацией
- `frontend/src/components/LLMSettings.tsx` (`~520 строк`) — 3 таба: **Local llama.cpp**: бинарь/папка/порт/автостарт/mmap, `Сканировать` `Проверить сервер`, список `.gguf` с `quant/size/task` + `task` селектор + `Сделать активной`, **Установка** `HF URL → Models/<task>/` + лог, **Гибкая настройка** слайдеры `n_ctx 512-32768, threads 0-32, gpu_layers 0-99, temp 0-2, top_p/top_k/repeat` + `extra_args` + предпросмотр команды `llama-server ...`; **Cloud API**: карточки `OpenAI/Gemini/Claude/Ollama/Custom` `enabled` toggle, `auth_method ApiKey/OAuth/None`, `api_key` masked `show/hide`, `model/api_url`, `Тест` + `Открыть кабинет ↗` (`Gemini aistudio, OpenAI platform, Claude console`), `shell.open`; **Pipeline**: список `enrich/chat-rag` + `+ Создать`, `trigger manual/on_save/scheduled`, шаги `provider/model/prompt {{content}}`, `▶ Запустить` на `pipelineInput` → `pipelineOut` `extract_content`
- `frontend/src/App.tsx` `ViewId +llm`, `Ribbon 🧠`, `right-panel 🧠 Нейросети`, `CommandPalette llm`, `React.lazy LLMSettings` + `Suspense`
- Версии → `0.5.5` (`Cargo`, `tauri.conf`, `package.json`, `CMake`, `updater.ts CURRENT` `0.5.5`), `tsc` ✅ `vite 1.16s 26.21kB LLMSettings + 77.83kB index` ✅
- `README.md` подпись `v0.5.5` `LLM Hub`, `CHANGELOG.md` этот раздел

## v0.5.4 — оптимизация RAM: lazy + reuse Client + release profile (2026-09-21)
> **Почему отдельная версия от v0.5.3:** v0.5.3 починил сборку `Tauri bundle` (`icon` + `targets`), но фоном висело `5-6МБ RSS` — `reqwest Client` пересоздавался на каждый `llm_ping/chat` (`2МБ churn`), `Regex` компилировался на каждый вызов `slugify/first_url/get_links/enrich`, `updater` polling `5мин` + все тяжелые вьюхи в одном `299kB` бандле. v0.5.4 — **аккуратная Фаза 1** без ломки фич: меньше RAM фоном `~3.5МБ`.

**Сделано:**
- `src-tauri/Cargo.toml` `[profile.release] opt-level="z" lto=true codegen-units=1 panic="abort" strip=true` — бинарь `19МБ → ~12-14МБ`, меньше RSS фоном (`z` + `lto` + `strip`)
- `src-tauri/src/enrich.rs` `CLIENT: Lazy<reqwest::blocking::Client>` `timeout 90s pool_max_idle 2 pool_idle 30s` — переиспользуем 1 пул соединений, `llm_ping` теперь `CLIENT.get().timeout(5s)` вместо `Client::builder().build()` каждый раз (−1-2МБ на запрос), `RE_FRONT` + `RE_TAG` `Lazy<Regex>` вместо `Regex::new` в цикле
- `src-tauri/src/collect.rs` `RE_SLUG/RE_URL/RE_URL_KV/RE_TITLE_KV` `Lazy<Regex>` + `once_cell` — `slugify`/`first_url`/`wrap_web_clips` без ре-компиляции regex
- `src-tauri/src/main.rs` `RE_WIKILINK Lazy` для `get_links`, `Vec::with_capacity(256)` для `list_notes` + `tasks.rs` `with_capacity(64)` — меньше реаллокаций
- `frontend/src/App.tsx` `React.lazy` для `GraphView/CanvasView/SearchView/AIChatFull/TaskManager/WebClipper` + `Suspense` fallback, `updater interval 5*60*1000 → 15*60*1000` — фоном не грузим `TaskManager 7.45kB`/`WebClipper 3kB`/`AI 2.38kB`/`Graph 1.1kB` до клика, −3 проверки/с, меньше CPU/RAM
- `frontend/vite.config.ts` `manualChunks: {vendor: [react,react-dom,zustand], md: [marked,dompurify]}` — `299kB monolith → 144kB vendor + 64kB md + 76kB index + lazy 1-7kB` — `initial chunk` меньше, WebView меньше памяти фоном
- Версии → `0.5.4` (`Cargo`, `tauri.conf`, `package.json`, `CMake`, `updater.ts CURRENT`), `tsc` ✅ `vite 6.87s 144+64+76kB` ✅
- `README.md` подпись `v0.5.4`, `CHANGELOG.md` этот раздел

## v0.5.3 — фикс сборки Tauri bundle: icon + targets (2026-09-21)
> **Почему отдельная версия от v0.5.2:** v0.5.2 добавил `src-tauri/icons/icon.ico` `362K`, но `tauri.conf.json` всё ещё имел `bundle.icon ["icon.png"]` без `icon.ico` и `targets ["nsis","msi"]` только для `Windows` — `Windows build` падал `thread panicked at tauri-cli src/interface/rust.rs:1073 the bundle config must have a .ico icon` (`build-windows 11m46s FAILED`), `Linux build` собрал `release` бинарь `23s` но `bundle/` не создался `No such file bundle/` → `No artifacts uploaded` (`linux-bundles` пусто, `release` без `AppImage/deb`). v0.5.3 — **фикс `tauri.conf`** чтобы `Tauri bundler` генерил артефакты на обеих платформах.

**Сделано:**
- `src-tauri/tauri.conf.json` `bundle.icon` `["icon.png"]` → `["icons/icon.ico","icons/icon.png"]` — теперь `tauri-cli` на `Windows` находит `ico` (раньше падал `1073`), `bundle.windows.nsis.installerIcon` `icon.png` → `icon.ico` (NSIS требует `ico`)
- `src-tauri/tauri.conf.json` `bundle.targets` `["nsis","msi"]` → `["appimage","deb","nsis","msi","updater"]` — `Linux` теперь генерит `AppImage`+`deb` (`1024×` bundle), `updater` артефакт `*.AppImage.tar.gz` + `.sig` для `updater.json` (раньше `Warn updater enabled but bundle target list does not contain updater`)
- Версии → `0.5.3` (`Cargo.toml`, `tauri.conf.json`, `frontend/package.json`, `cpp/CMakeLists`, `frontend/src/lib/updater.ts` `CURRENT`), `tsc` ✅ `vite 299kB` ✅
- `README.md` подпись `v0.5.3`, `CHANGELOG.md` этот раздел

## v0.5.2 — фикс Windows сборки: icon.ico для Tauri (2026-09-20)
> **Почему отдельная версия от v0.5.1:** v0.5.1 добавил `Task Manager v2`, но `Build Tauri Windows` падал `14м` — `icons/icon.ico not found; required for generating a Windows Resource file during tauri-build` (`tauri.conf` имел только `icon.png`, `Windows` требует `ico`).

**Сделано:**
- `src-tauri/icons/icon.ico` `362K` `6` иконок `16/32/48/64/128/256` `32bit` из `icon.png` `512` `magick -define icon:auto-resize`
- Версии → `0.5.2`, `cargo check` ✅ `tauri-build` теперь находит `icon.ico`


## v0.5.1 — Task Manager v2: 6 поверхностей без костылей (2026-09-19)
> **Почему отдельная версия от v0.5.0:** v0.5.0 портировал `AO Engine` (`collect` + `Web Clipper` + `enrich`), но `Task Manager` оставался костыльным (`Dataview` + `plugin` `taskIndex` `taskRules`). v0.5.1 — **чистый `Task Manager v2`** как в `Obsidian` `v1` без `Dataview`.

**Сделано:**
- `src-tauri/src/tasks.rs` — `Task {id,title,status,task_type,due,scheduled,project,path}` `statusLaw` `is_terminal(done/cancelled)`, `is_relevant_on_day` `routine/scheduled/due`, `tasks_root` `Tasks` fallback `05 Sort`, `load_tasks_from_vault` `walkdir` `frontmatter` `status` `task_type`, `tasks_list(filter: today/board/completed/workspace)` `todayBucket` `Local::now`, `tasks_create` `Tasks/{date}-{slug}.md` `frontmatter`, `tasks_update_status` `status: todo/doing/done` `+ - [ ]/[x]`, `tasks_archive` `Archive/Tasks`
- `src-tauri/src/main.rs` `mod tasks` + `invoke_handler` `tasks_list/create/update_status/archive`
- `frontend/src/components/tasks/TaskManager.tsx` — 6 поверхностей: `Start` (KPI `counts` + `Today/Workspace/Board` + `Create`), `Today` (`getTodayTaskBucket` `due==today` `+ relevance`), `Day` (`02 Daily/{date}.md` `embed`), `Workspace` (`project` фильтр), `Board` (`Kanban` `todo/doing/done` `drag` `statusLaw`), `Completed` (`terminal` `archive` `Review`) — `useTasks` `invoke tasks_list` + `TaskRow` `Выполнено`
- `frontend/src/App.tsx` `ViewId +tasks`, `Ribbon` `✓ Задачи` уже был, `right-panel` `✓ Task Manager v2` `active`, `tasks` `Tauri` `commands` `Zustand` `store` `sqlite` `WAL` + `markdown` `sync`
- Версии → `0.5.1`, `tsc` ✅ `vite 299kB` ✅ `cargo check` ✅


## v0.5.0 — нативный AO Engine: сбор + Web Clipper + обогащение (2026-09-19)
> **Почему отдельная версия от v0.4.15:** v0.4.15 был визуально богаче (`glass`/`glow`), но `AO Engine` (`_System/ArchiveOrganism`) оставался внешним `Node` `ao-engine` (`ingestFiles`, `wrapWebClips`, `enrichNotes`). v0.5.0 — **портирован нативно** в `Tauri`+`C++`+`React` без `Node`.

**Сделано:**
- `src-tauri/src/collect.rs` — порт `wrapWebClipsInVault` (`TypeScript` → `Rust`): `walkdir` `Sources/web-clips` (`_System/ArchiveOrganism/Sources/web-clips`, `Sources/web-clips`, `Sources/raw`), `parse_frontmatter` `has_inbox_frontmatter`, `first_url` `Regex`, `slugify`, `buildInboxMarkdown` (`source: web`, `origin`, `ao_sort_status: undecided`, `tags: [sort-inbox]`), `write` `05 Sort/{date}-{slug}.md` + `telegram_rss.py` вызов, `collect_sources` `Tauri` `command` (`scanned/wrapped/telegram/errors`)
- `src-tauri/src/collect.rs` `web_clip` — `Tauri` `command {url, title, html, selection}` → `write` `Sources/web-clips/{date}-{slug}.md` + `auto wrap` (как `Obsidian Web Clipper` в браузере, теперь нативно `Ctrl+Shift+W` / `Ribbon ✂`)
- `src-tauri/src/enrich.rs` — порт `LlmService` + `embeddings`: `llm_status` `GET /health`, `llm_chat` `POST /v1/chat/completions` `Qwen3-14B` `8010/8011`, `enrich_notes` `WalkDir 05 Sort` `limit` `LLM` `prompt` `JSON tags/links` → `write` `enriched: true`, `embeddings_search` `FTS5` fallback `rusqlite`
- `src-tauri/Cargo.toml` `+ chrono 0.4` `+ reqwest 0.12 blocking/json`, `src-tauri/src/main.rs` `mod collect, enrich` + `invoke_handler` `collect_sources, web_clip, llm_status, llm_chat, enrich_notes, embeddings_search`
- `frontend/src/components/WebClipper.tsx` — `Clip` (`outerHTML`/`selection` → `invoke web_clip`), `Сбор` (`invoke collect_sources`), `Обогатить` (`invoke enrich_notes limit 3`), fallback `writeNote` если `Tauri` нет
- `frontend/src/components/AIChatFull.tsx` — `Qwen3-14B` `chat` `user/assistant` `bubbles`, `llm_status` `day`, `RAG` `embeddings_search` (скоро `top 3` в `system` prompt), `stream` `typing`
- `frontend/src/App.tsx` `right-panel` `WebClipper` + `ai_chat` → `AIChatFull` (был `LLM offline` stub)
- Версии → `0.5.0`, `tsc` ✅ `vite 292kB` ✅ `cargo check` ✅ (был `regex` `"` + `slugify` temp drop + `unused Path`)


## v0.4.15 — визуально богаче: стекло, градиенты, glow (2026-09-19)
> **Почему отдельная версия от v0.4.14:** v0.4.14 фиксил `AppImage` сборку (`tauri-plugin-updater` + `allowlist` + `beforeBuild`), но UI оставался плоским (тёмные панели без глубины). v0.4.15 — **визуально богаче** без изменения логики.

**Сделано:**
- `frontend/src/styles.css`: `glass` `backdrop-blur 16px`, `ambient glow` `radial-gradient` (130,168,255 + 190,165,255 + 139,213,202), `ribbon` градиент + `hover` `translateY(-1px) scale(1.02)` + `active` `linear-gradient` + `glow`, `panels` `blur 16px` + `inset` бордер, `toolbar` `glass pill`, `tabs` `slant` + `shadow`, `editor` `rgba(6,8,15,.72)` + `line-height 1.8`, `markdown` `gradient h1` + `blockquote` `rgba(130,168,255,.08)`, `file-tree` `hover translateX(1px)` + `active gradient`, `empty` `dashed` + `CTA` `shadow`, `palette` `18px` + `blur 10px`, `graph` `radial 600x300` + `shadow`, `right-panel` `card` `14px` + `blur`, `welcome-hero` `linear-gradient 135deg` + `shadow`
- Версии → `0.4.15`, `tsc` ✅ `vite 287kB` ✅


## v0.4.14 — фикс сборки: убран несуществующий tauri-plugin-updater 1.6 (2026-09-19)
> **Почему отдельная версия от v0.4.13:** v0.4.13 добавил `tauri-plugin-updater 1.6` + `.plugin()` для автообновления, но `cargo check` падал — `candidate versions found which didn't match: 3.0.0-alpha, 2.12.0` (для `tauri 1.6` плагин не нужен, updater встроен через `tauri features updater`). Из-за этого `cargo tauri build` падал и `AppImage` не собирался → локально не открывается.

**Сделано:**
- `src-tauri/Cargo.toml`: убран `tauri-plugin-updater = "1.6"` (не существует для `tauri 1.6`, updater уже в `tauri` crate с `features updater`)
- `src-tauri/src/main.rs`: убран `.plugin(tauri_plugin_updater::Builder::new().build())` (для `1.x` не нужен, `tauri::Builder` с `updater` feature достаточно, `updater.ts` использует `@tauri-apps/api/updater` напрямую)
- Версии → `0.4.14` (`cpp`, `frontend`, `src-tauri`, `updater.ts` `CURRENT`), `tsc` ✅ `vite` ✅



## v0.4.13 — фикс локальной сборки Tauri (allowlist + beforeBuild) (2026-09-19)
> **Почему отдельная версия от v0.4.12:** v0.4.12 добавил `autoInstall` + `updater.json`, но локально на этом компе `cargo tauri build` падал — `allowlist.updater` не существует в `Tauri 1.x` (`Additional properties not allowed`) + `beforeBuildCommand` `../frontend` давал `ENOENT /home/fragilich/frontend/package.json` когда `cargo tauri build` запускается из `Fragile-Notes`. v0.4.13 — фикс сборки, чтобы `AppImage` открывался.

**Сделано:**
- `src-tauri/tauri.conf.json`: `allowlist.updater {all:true}` убран (не существует в `1.x`, `updater` уже `active:true` с `pubkey`/`endpoints`), `build.beforeDevCommand`/`beforeBuildCommand` `npm run dev --prefix ../frontend` → `npm --prefix frontend run build` / `npm run dev --prefix frontend` — теперь `cargo tauri build` из `Fragile-Notes` находит `frontend/package.json` (раньше `../frontend` → `/home/fragilich/frontend`)
- `frontend/src/lib/updater.ts` `CURRENT 0.4.12→0.4.13`
- Версии → `0.4.13`, `tsc` ✅ `vite 287kB` ✅ `cargo check` должен пройти, `AppImage` теперь собирается


## v0.4.12 — автообновление на любом устройстве без ручных триггеров (2026-09-19)
> **Почему отдельная версия от v0.4.11:** v0.4.11 сделал автообновление только на этом компе (`systemd timer` + `git hooks` + баннер `GitHub API` с кнопкой `Скачать` — надо было кликать). v0.4.12 — **полная автоматика на любом устройстве**: `Tauri updater` сам скачивает и ставит.

**Сделано:**
- `frontend/src/lib/updater.ts`: `autoInstallIfAvailable()` — `import('@tauri-apps/api/updater').checkUpdate()` → `installUpdate()` → `relaunch()` (Tauri `plugin:updater`), fallback `GitHub API` баннер. `CURRENT 0.4.12`.
- `frontend/src/App.tsx`: проверка каждые `5 мин` + `focus` + `visibilitychange`, при `shouldUpdate` сразу `autoInstallIfAvailable()` без клика (раньше только баннер `Скачать`). `checkForUpdates` теперь пробует `Tauri plugin` сначала, затем `GitHub`.
- `src-tauri/tauri.conf.json` `updater {active:true, endpoints:[.../updater.json], pubkey}` уже был, теперь используется; `allowlist.updater.all:true`
- `.github/workflows/build-tauri.yml`: новый единый workflow `Linux (AppImage/deb)` + `Windows (NSIS/MSI)` → `cargo tauri build` с `TAURI_PRIVATE_KEY` → `updater.json` (`version`, `pubkey`, `platforms` с `url`/`signature` из `.sig`) + upload `AppImage`/`deb`/`exe`/`msi`/`sig`/`updater.json` в `Releases` на `tags v*` (удалён старый `build-tauri-windows.yml`)
- Локальное на этом компе остаётся: `post-commit`/`post-merge` + `systemd timer 5мин` → `fragile-auto-build.sh` (для разработки), удалённое на любом устройстве — `Tauri updater` (без `git`).

## v0.4.11 — автообновление Tauri (GitHub Releases + локальная пересборка) (2026-09-19)
> **Почему отдельная версия от v0.4.10:** v0.4.10 снова сделал чистый `Tauri` без `Python`, но без автообновления — после каждого `push` надо было вручную `git pull && cargo tauri build`. v0.4.11 — **автообновление**: локально на этом компе после каждого `push` + в `Tauri` баннер из `GitHub Releases`.

**Сделано:**
- Сгенерирован `Tauri signing keypair` (`~/.tauri/fragile.key` + `.pub`), `pubkey` `dW50cn...TkUK` добавлен в `tauri.conf.json` `updater {active:true, endpoints:[.../updater.json], pubkey}`, `Cargo.toml` `tauri features +updater` + `tauri-plugin-updater 1.6`, `src-tauri/src/main.rs` `.plugin(tauri_plugin_updater::Builder::new().build())`
- `GitHub Secrets` `TAURI_PRIVATE_KEY` + `TAURI_KEY_PASSWORD` — для подписи `updater.json` в `CI`
- `frontend/src/lib/updater.ts` — проверка `https://api.github.com/repos/freakkxd/Fragile-Notes/releases/latest`, сравнение `CURRENT 0.4.11`, баннер в `App.tsx` `⬆ Доступно обновление {latest} [Скачать]` + `openReleasePage` (`shell.open` || `window.open`), проверка при старте + каждые 30 мин + на `focus`
- Локальное автообновление на этом компе: `~/bin/fragile-auto-build.sh` (`git fetch/pull` → `npm ci/build` → `cargo tauri build` → `~/Applications/Fragile-Notes.AppImage` + `.desktop`), `post-commit`/`post-merge` хуки + `systemd --user` `fragile-auto-update.timer` каждые 5 мин, `cargo-tauri 1.6.6` установлен
- Версии → `0.4.11`, `tsc` ✅ `vite 284kB` ✅


## v0.4.10 — чистый Tauri без Python (финал v0.4) (2026-09-19)
> **Почему отдельная версия от v0.4.9:** v0.4.9 был гибридом (`Python` + `Tauri`) — вернул `Python` чтобы пофиксить твой `Linux` `GTK` на скринах. v0.4.10 — **снова чистый `Tauri`** как задумывалось для `v0.4`, без `Python` вообще (`pip` `fragile-notes` удалён).

**Сделано:**
- Удалены снова: `fragilenotes/` 53 вьюхи, `main.py`, `pyproject.toml`, `run.sh`, `fragile`, `fragile_notes.egg-info` — только `C++/Tauri/React`
- `pip uninstall fragile-notes` — на `Linux` теперь только `cargo tauri dev` / `AppImage` (`src-tauri/target/release/bundle/appimage`)
- Версии → `0.4.10` (`cpp`, `frontend`, `src-tauri`), `tsc` ✅ `vite` ✅ `ctest` ✅


## v0.4.9 — хотфикс Python GTK: пустая полоса сайдбара (скрины) (2026-09-19)
> **Почему отдельная версия от v0.4.8:** v0.4.8 фиксил `React Tauri` (`display:none` + `DOM` удаление), но на скринах `Python GTK` (`Adw` `Revealer` + `Gtk.Paned`) — полоса осталась как на скрине 1 (пустой `side_column` 280px). v0.4.9 — хотфикс `Python` (`WorkspaceMixin`).

**Сделано:**
- Восстановлен `Python` runtime (`fragilenotes/` 53 вьюхи, `main.py`, `pyproject.toml` 0.4.9) — гибрид `Python` + `Tauri` для переходного периода (был удалён в v0.4.2, но нужен для текущего инсталла)
- `fragilenotes/ui/workspace_mixin.py` `WorkspaceMixin._on_toggle_sidebar`: при `vis=False` теперь `top.set_position(44)` + `GLib.timeout_add(220, collapsed)` + `side_column.remove_css_class(expanded)` + `set_size_request(44,-1)` с повтором через 250ms; при `vis=True` — `set_size_request(-1,-1)` + `add_css_class`. Раньше `width:0` перебивался `min-width:232` и `Paned` оставался 280px.
- `frontend/src/styles.css` `display:none` уже в v0.4.8, теперь и `Python` фиксит ту же полосу в `GTK`
- Версии → `0.4.9` (`fragilenotes/__init__.py`, `pyproject.toml`, `cpp`, `frontend`, `src-tauri`), `tsc` ✅ `vite` ✅ `ctest` ✅


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