"""Reading Time plugin — считает время чтения при открытии заметки."""

WPM = 200

def on_open(path):
    try:
        from pathlib import Path
        p = Path(str(path))
        if not p.is_file():
            return
        text = p.read_text(encoding="utf-8", errors="replace")
        words = len(text.split())
        minutes = max(1, round(words / WPM)) if words else 0
        if minutes:
            print(f"[reading_time] {p.name}: ~{minutes} мин чтения ({words} слов)")
    except Exception as e:
        print(f"[reading_time] error: {e}")

try:
    register_hook("on_open", on_open)
except NameError:
    pass
