"""Daily Quote plugin — добавляет случайную цитату при создании заметки (демо)."""

import random

QUOTES = [
    "Делай сегодня то, что другие не хотят — завтра будешь жить как другие не могут.",
    "Маленькие шаги каждый день — большой результат через год.",
    "Фокус — это умение говорить «нет».",
]

def on_new(path):
    try:
        from pathlib import Path
        p = Path(str(path))
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
            if "цитата" not in text.lower() and len(text) < 200:
                q = random.choice(QUOTES)
                print(f"[daily_quote] подсказка для {p.name}: «{q}»")
    except Exception as e:
        print(f"[daily_quote] error: {e}")

try:
    register_hook("on_new", on_new)
except NameError:
    pass
