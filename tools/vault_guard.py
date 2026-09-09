#!/usr/bin/env python3
"""vault_guard: блокирует коммит личных заметок в GitHub.

Проверяет staged файлы и падает если находит:
- файлы внутри vault-папок (01 Home, 02 Daily и т.д.)
- личные .md кроме README.md
- settings.json с путём к волту
- .enc / .e2e_salt
"""
import subprocess
import sys
from pathlib import Path

# Паттерны которые никогда не должны коммититься
BLOCKED_DIRS = {
    "01 Home",
    "02 Daily",
    "03 Projects",
    "04 FreakyWiki",
    "05 Sort",
    "06 Media",
    "_System",
    "Vault",
    "vault",
    "desktop",
    "Desktop",
    "Secret",
    "Private",
    "private",
}

BLOCKED_EXTS = {".enc"}
BLOCKED_FILES = {".e2e_salt", "settings.json"}

ALLOWED_MD = {"README.md", "CONTRIBUTING.md", "CHANGELOG.md"}

def get_staged() -> list[str]:
    try:
        out = subprocess.check_output(["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"], text=True)
        return [l.strip() for l in out.splitlines() if l.strip()]
    except subprocess.CalledProcessError:
        return []

def is_blocked(path: str) -> str | None:
    p = Path(path)
    parts = set(p.parts)

    # Проверка директорий волта
    for d in BLOCKED_DIRS:
        if d in parts or path.startswith(d + "/") or f"/{d}/" in path:
            return f"vault dir '{d}' in path '{path}'"

    # Проверка расширений
    if p.suffix in BLOCKED_EXTS:
        return f"blocked ext '{p.suffix}' in '{path}'"

    # Проверка файлов
    if p.name in BLOCKED_FILES:
        return f"blocked file '{p.name}' in '{path}'"

    # Проверка .md - разрешаем только README
    if p.suffix == ".md":
        if p.name not in ALLOWED_MD and not path.startswith("docs/"):
            # Разрешаем README в любой подпапке: */README.md
            if p.name == "README.md":
                return None
            return f"personal .md blocked '{path}' (разрешён только README.md)"

    # Проверка settings.json в любом месте
    if "settings.json" in path:
        return f"settings.json with vault path blocked '{path}'"

    return None

def main() -> int:
    staged = get_staged()
    blocked = []
    for f in staged:
        reason = is_blocked(f)
        if reason:
            blocked.append((f, reason))

    if blocked:
        print("❌ VAULT PROTECTION: попытка закоммитить личные данные!", file=sys.stderr)
        print("Заблокированные файлы:", file=sys.stderr)
        for f, reason in blocked:
            print(f"  - {f}: {reason}", file=sys.stderr)
        print("\nЕсли это ошибка - добавь файл в .gitignore или в ALLOWED_MD", file=sys.stderr)
        print("Волт находится в ~/desktop и никогда не должен быть внутри ~/Проекты/FragileNotes", file=sys.stderr)
        return 1

    # Дополнительная проверка: нет ли волта скопированного внутрь репо как папки
    repo_root = Path(__file__).resolve().parent.parent
    for d in BLOCKED_DIRS:
        suspect = repo_root / d
        if suspect.exists() and suspect.is_dir():
            # Если папка существует но не в git (из-за .gitignore) - это ок, но предупредим
            # Если она staged - уже поймали выше. Здесь просто предупреждение
            pass

    return 0

if __name__ == "__main__":
    sys.exit(main())
