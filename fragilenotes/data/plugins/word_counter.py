"""Word Counter plugin — считает слова при сохранении заметки."""

def on_save(path, content=None):
    try:
        text = content if isinstance(content, str) else ""
        if not text and path is not None:
            from pathlib import Path
            p = Path(str(path))
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
        words = len(text.split()) if text else 0
        print(f"[word_counter] {path}: {words} слов")
    except Exception as e:
        print(f"[word_counter] error: {e}")

try:
    register_hook("on_save", on_save)
except NameError:
    pass
