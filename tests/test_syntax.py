"""Тесты лёгкой подсветки синтаксиса (чистая логика без GTK)."""

from __future__ import annotations

from fragilenotes.ui.syntax import highlight


def _tagged(code: str, lang: str = "") -> list[tuple[str, str | None]]:
    return [(t, tg) for t, tg in highlight(code, lang) if tg]


def test_python_keywords():
    out = _tagged("def f(x):\n    return x\n", "python")
    tags = [tg for _, tg in out]
    assert "syn_kw" in tags
    assert "syn_func" in tags


def test_python_comment_and_string():
    out = _tagged('# коммент\nx = "текст"\n', "py")
    assert ("# коммент", "syn_com") in out
    assert any(t == "syn_str" for _, t in out)


def test_number():
    out = _tagged("n = 42.5\n", "python")
    assert any(t == "syn_num" for _, t in out)


def test_json_keys():
    out = _tagged('{"a": 1, "b": "x"}\n', "json")
    assert any(t == "syn_key" for _, t in out)


def test_bash_var_and_kw():
    out = _tagged("if [ -f $file ]; then echo ok; fi\n", "bash")
    tags = [tg for _, tg in out]
    assert "syn_kw" in tags and "syn_var" in tags


def test_unknown_lang_plain():
    out = highlight("def x(): pass", "toml")
    assert out == [("def x(): pass", None)]


def test_triple_string_spans_lines():
    out = _tagged('"""многострочная\nстрока"""\nx = 1\n', "python")
    assert all(t == "syn_str" for _, t in out[:2])
    assert any(t == "syn_num" for _, t in out)
