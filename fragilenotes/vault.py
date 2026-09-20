"""Доступ к vault: чтение/запись markdown-файлов, frontmatter, ссылки, обход файлов."""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path

import yaml

FILE_EMOJI = {
    ".md": "📄",
    ".txt": "📝",
    ".html": "🌐",
    ".htm": "🌐",
    ".css": "🎨",
    ".js": "📜",
    ".ts": "📜",
    ".json": "🧩",
    ".yaml": "🧩",
    ".yml": "🧩",
    ".toml": "🧩",
    ".ini": "🧩",
    ".cfg": "🧩",
    ".py": "🐍",
    ".cpp": "⚙️",
    ".h": "⚙️",
    ".hpp": "⚙️",
    ".c": "⚙️",
    ".rs": "🦀",
    ".go": "🐹",
    ".java": "☕",
    ".sh": "💻",
    ".xml": "🗂️",
    ".svg": "🖼️",
    ".csv": "📊",
    ".log": "📝",
    ".enc": "🔒",
    ".pdf": "📕",
}

ALLOWED_EXTS = {".md", ".txt", ".html", ".htm", ".css", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".py", ".cpp", ".h", ".hpp", ".c", ".rs", ".go", ".java", ".sh", ".xml", ".svg", ".csv", ".log", ".enc", ".pdf", ".mdx"}

TEXT_FALLBACK_EXTS = {".env", ".gitignore", ".prettierrc", ".eslintrc"}


BINARY_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mkv", ".webm", ".mp3", ".wav", ".flac", ".zip", ".tar", ".gz", ".rar", ".7z", ".exe", ".dll", ".so", ".dylib"}


def is_text_file(path: Path) -> bool:
    ext = path.suffix.lower()
    if ext in ALLOWED_EXTS:
        return True
    if ext in BINARY_EXTS:
        return False
    if path.name in TEXT_FALLBACK_EXTS or path.name.startswith("."):
        try:
            with open(path, "rb") as f:
                chunk = f.read(2048)
            if b"\x00" in chunk:
                return False
            chunk.decode("utf-8")
            return True
        except Exception:
            return False
    try:
        with open(path, "rb") as f:
            chunk = f.read(1024)
        if not chunk:
            return True
        if b"\x00" in chunk:
            return False
        chunk.decode("utf-8")
        return True
    except Exception:
        return False

HEAVY_DIRS = {
    "node_modules", ".git", "dist", "build", "target", ".venv", "venv",
    "__pycache__", "bin", "obj", ".cache", ".trash",
    ".obsidian", "Trash", "images", "ao-engine",
}

# Дата-подобные строки (due_date, created …) должны оставаться строками,
# а не превращаться PyYAML в date-объекты.
class _FrontMatterLoader(yaml.SafeLoader):
    pass


_FrontMatterLoader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}

FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter dict, body without frontmatter)."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        data = yaml.load(m.group(1), Loader=_FrontMatterLoader)
    except yaml.YAMLError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    body = text[m.end() :]
    return data, body


def serialize_frontmatter(fm: dict) -> str:
    if not fm:
        return ""
    # safe_dump keeps ordering, wraps long values
    block = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, width=1000)
    return f"---\n{block}---\n"


def read_md(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    return parse_frontmatter(text)


def write_md(path: Path, fm: dict, body: str) -> None:
    content = serialize_frontmatter(fm) + body.lstrip("\n")
    path.write_text(content, encoding="utf-8")


def update_frontmatter(path: Path, updates: dict) -> dict:
    """Merge updates into existing frontmatter, preserving body. Returns new fm."""
    fm, body = read_md(path)
    fm.update(updates)
    write_md(path, fm, body)
    return fm


def list_markdown(root: Path, recursive: bool = True) -> list[Path]:
    it = root.rglob("*.md") if recursive else root.glob("*.md")
    return [p for p in it if p.is_file()]


def scan_vault(root: Path) -> list[tuple[Path, bool]]:
    """Обход vault вне GUI-потока: (абсолютный путь, is_dir), DFS-преордер.

    На каждом уровне папки идут раньше файлов, всё отсортировано по имени;
    скрытые и «тяжёлые» папки пропускаются, симлинки на папки не обходятся.
    """
    out: list[tuple[Path, bool]] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(
                os.scandir(directory),
                key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()),
            )
        except OSError:
            continue
        pending_dirs: list[Path] = []
        for entry in entries:
            if entry.name.startswith(".") or entry.name in HEAVY_DIRS:
                continue
            path = Path(entry.path)
            is_dir = entry.is_dir(follow_symlinks=False)
            if is_dir:
                out.append((path, True))
                pending_dirs.append(path)
            elif (
                entry.is_file(follow_symlinks=False)
                and entry.name.lower().endswith(tuple(ALLOWED_EXTS))
            ):
                out.append((path, False))
        for path in reversed(pending_dirs):
            stack.append(path)
    return out


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    v = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(v)
    except ValueError:
        return None


def iso_date(d: date | None) -> str | None:
    return d.isoformat() if d else None


def wikilinks(body: str) -> list[str]:
    return list(dict.fromkeys(WIKILINK_RE.findall(body)))
