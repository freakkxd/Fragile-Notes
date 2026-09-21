# Fragile Notes — v0.5.5

> **Подпись v0.5.5 — почему отделили эту версию:**
> - **Сделано в 0.5.4:** оптимизация RAM `5-6МБ→3.5МБ` (`Lazy Client/Regex` + `lazy chunks`).
> - **Почему 0.5.5 отдельно:** **гибкая нейросеть из приложения** — полный `llama.cpp` конструктор + внешние `API` (GPT/Gemini/Claude). Локально: парсинг `.gguf`, выбор/установка моделей по задачам (`chat/coder/embed/enrich`), гибкая настройка (`n_ctx/threads/gpu_layers/temp/top_p/top_k/repeat` + `extra_args`), пайплайны. Облачно: `API ключ` + `Вход в аккаунт (OAuth)` для каждого провайдера.
> - **Цель 0.5.5:** юзер сам строит пайплайны `с помощью нейросетей и для нейросетей`.

**Obsidian-like vault.** `Task Manager v2` + `Tauri` `C++` + `LLM Hub`.

## Быстрый старт
```bash
git clone https://github.com/freakkxd/Fragile-Notes.git
cargo tauri dev
# LLM → 🧠 Нейросети в приложении
```

## Версионирование
- `v0.5.1` — `Task Manager v2`
- `v0.5.2` — фикс `icon.ico` файл
- `v0.5.3` — фикс `tauri.conf` bundle
- `v0.5.4` — оптимизация RAM `lazy + reuse`
- `v0.5.5` — **LLM Hub: llama.cpp + GPT/Gemini/Claude + пайплайны** (текущий)

## Лицензия
MIT — `LICENSE`
