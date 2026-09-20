"""Hello World plugin for FragileNotes — пример хуков on_open/on_save/on_new."""

def on_open(path):
    print(f"[hello_world] opened {path}")

def on_save(path, content=None):
    print(f"[hello_world] saved {path}")

def on_new(path):
    print(f"[hello_world] new note {path}")

# Регистрация через API (альтернатива авто-регистрации по имени функции)
try:
    register_hook("on_open", on_open)
    register_hook("on_save", on_save)
    register_hook("on_new", on_new)
except NameError:
    pass
