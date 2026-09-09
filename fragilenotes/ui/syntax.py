"""Лёгкая подсветка синтаксиса для code-блоков предпросмотра (без GTK).

Возвращает сегменты (текст, тег|None), которые рендерер markdown
применяет поверх базового тега codeblock.
"""

from __future__ import annotations

import re

_PY_KEYWORDS = frozenset(
    """async await def class return if elif else for while import from as with try except
    finally raise assert break continue pass yield lambda global nonlocal in not and or is
    None True False del match case self""".split()
)
_BASH_KEYWORDS = frozenset(
    """if then else elif fi for while do done case esac function in select until break
    continue return local exit unset export declare eval source shopt set""".split()
)

_RT = ("'''", '"""')
_LINE_TOK = re.compile(
    r"""
      (?P<comment>\#[^\n]*)
    | (?P<dstr>"[^"\n]*")
    | (?P<sstr>'[^'\n]*')
    | (?P<var>\$\{?[A-Za-z_][A-Za-z0-9_]*\}?)
    | (?P<num>\b\d+(?:\.\d+)?\b)
    | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<op>[^A-Za-z0-9_'"#\s$]+)
    """,
    re.VERBOSE,
)


def _mode(lang: str) -> str | None:
    lang = (lang or "").strip().lower()
    if lang in ("py", "python", "jupyter", "ipython"):
        return "python"
    if lang in ("js", "javascript", "ts", "typescript", "jsx", "tsx"):
        return "python"
    if lang in ("json", "jsonc"):
        return "json"
    if lang in ("bash", "sh", "zsh", "shell", "console"):
        return "bash"
    return None


def highlight(code: str, lang: str = "") -> list[tuple[str, str | None]]:
    """Разбивает блок кода на сегменты (текст, тег подсветки|None)."""
    mode = _mode(lang)
    if mode is None or not code:
        return [(code, None)] if code else []

    out: list[tuple[str, str | None]] = []
    triple: str | None = None
    for line in code.split("\n"):
        if triple:
            idx = line.find(triple)
            if idx == -1:
                out.append((line, "syn_str"))
                out.append(("\n", None))
                continue
            out.append((line[: idx + 3], "syn_str"))
            line = line[idx + 3 :]
            triple = None
        segs, triple = _scan(line, mode)
        out.extend(segs)
        out.append(("\n", None))
    if out and out[-1][0] == "\n":
        out.pop()
    return out


def _scan(line: str, mode: str) -> tuple[list[tuple[str, str | None]], str | None]:
    """Токенизирует строку; возвращает (сегменты, открытая мультистрока)."""
    marker: str | None = None
    mpos: int | None = None
    for mrk in _RT:
        p = line.find(mrk)
        if p != -1 and (mpos is None or p < mpos):
            marker, mpos = mrk, p
    if marker is not None:
        close = line.find(marker, mpos + 3)
        if close != -1:
            out, _ = _scan(line[:mpos], mode)
            out.append((line[mpos : close + 3], "syn_str"))
            tail, _ = _scan(line[close + 3 :], mode)
            out.extend(tail)
            return out, None
        head, _ = _scan(line[:mpos], mode)
        head.append((line[mpos:], "syn_str"))
        return head, marker

    kw = _PY_KEYWORDS if mode in ("python", "json") else _BASH_KEYWORDS
    tokens: list[tuple[str, str | None]] = []
    pos = 0
    for m in _LINE_TOK.finditer(line):
        if m.start() > pos:
            tokens.append((line[pos : m.start()], None))
        text = m.group(0)
        pos = m.end()
        kind = m.lastgroup
        if kind == "comment" and mode in ("python", "bash"):
            tokens.append((text, "syn_com"))
        elif kind in ("dstr", "sstr"):
            tokens.append((text, "syn_str"))
        elif kind == "num":
            tokens.append((text, "syn_num"))
        elif kind == "var" and mode == "bash":
            tokens.append((text, "syn_var"))
        elif kind == "word":
            if text in kw:
                tokens.append((text, "syn_kw"))
            elif mode == "python" and _followed_by_call(line, m.end()):
                tokens.append((text, "syn_func"))
            else:
                tokens.append((text, None))
        elif kind == "op":
            tokens.append((text, None))
        else:
            tokens.append((text, None))
    tail = line[pos:]
    if tail:
        tokens.append((tail, None))

    if mode == "json":
        for i in range(len(tokens) - 1):
            if tokens[i][1] == "syn_str" and tokens[i + 1][0].lstrip().startswith(":"):
                tokens[i] = (tokens[i][0], "syn_key")
    return tokens, None


def _followed_by_call(line: str, idx: int) -> bool:
    return bool(re.match(r"\s*\(", line[idx:]))
