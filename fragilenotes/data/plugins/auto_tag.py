"""Auto Tag plugin — добавляет авто-теги при сохранении заметки (демо)."""

def on_save(path, content=None):
    try:
        text = content if isinstance(content, str) else ""
        if not text and path is not None:
            from pathlib import Path
            p = Path(str(path))
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
        if not text:
            return
        lower = text.lower()
        tags = []
        if "todo" in lower or "задача" in lower:
            tags.append("#todo")
        if "идея" in lower or "idea" in lower:
            tags.append("#idea")
        if tags:
            print(f"[auto_tag] {path}: предлагаемые теги {', '.join(tags)}")
    except Exception as e:
        print(f"[auto_tag] error: {e}")

try:
    register_hook("on_save", on_save)
except NameError:
    pass
