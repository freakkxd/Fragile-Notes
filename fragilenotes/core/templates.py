"""Шаблоны: хранение в vault/_System/Templates/, переменные {{date}}, {{time}}, {{title}}, {{uuid}} + JS {{js:}} песочница.

Поддержка ``{{js: <code>}}``:
  - выполнение JS через ``js2py`` если доступен, иначе безопасный Python-exec фоллбэк
  - контекст vault: ``vault`` (root, title, listFiles/read/exists), ``title``, ``date``, ``time``, ``uuid``, ``vault_root``, ``console.log``
  - песочница: блокировка опасных паттернов, ограничение длины кода/вывода, отсутствие доступа к реальной FS вне vault
  - ``console.log('hi')`` -> подставляет ``hi`` (логи захватываются и возвращаются)
"""

from __future__ import annotations

import ast
import re
import textwrap
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

TEMPLATES_REL = "_System/Templates"
TEMPLATE_VARS = ("{{date}}", "{{time}}", "{{title}}", "{{uuid}}")

# Regex для переменных: {{date}}, {{date:FORMAT}}, {{time}}, {{time:FORMAT}}, {{title}}, {{uuid}}
_VAR_RE = re.compile(r"\{\{\s*(date|time|title|uuid)(?::([^}]+))?\s*\}\}")

# JS блоки: {{js: <code>}} — DOTALL чтобы работал многострочный JS, non-greedy до первой }}
_JS_RE = re.compile(r"\{\{\s*js\s*:\s*(.*?)\s*\}\}", re.DOTALL | re.IGNORECASE)

# Ограничения песочницы
_JS_MAX_CODE_LEN = 4000
_JS_MAX_OUTPUT_LEN = 5000
_JS_MAX_LOG_LINES = 100

# Блокируемые паттерны — попытка доступа к системе/интроспекции
_JS_BLOCKED_RE = re.compile(
    r"(__import__|__class__|__subclasses__|__mro__|__bases__|__code__|"
    r"\bimport\s+os\b|\bimport\s+sys\b|\bimport\s+subprocess\b|"
    r"\brequire\s*\(|process\s*\.\s*exit|child_process|"
    r"\bopen\s*\(|\bexec\s*\(|\bcompile\s*\(|\bglobals\s*\(|\blocals\s*\(|"
    r"\b__dict__\b|\bconstructor\b|\bprototype\b|"
    r"fs\s*\.\s*write|os\s*\.\s*system|subprocess\s*\.)",
    re.IGNORECASE,
)

# Разрешённые builtin'ы для fallback exec
_SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs,
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
    "set": set,
    "tuple": tuple,
    "range": range,
    "enumerate": enumerate,
    "sorted": sorted,
    "min": min,
    "max": max,
    "sum": sum,
    "any": any,
    "all": all,
    "round": round,
    "zip": zip,
    "map": map,
    "filter": filter,
    "reversed": reversed,
    "chr": chr,
    "ord": ord,
    "hasattr": hasattr,
    "getattr": getattr,
    "isinstance": isinstance,
    "print": print,
}


class _JSArray(list):  # type: ignore[type-arg]
    """JS-подобный массив: поддерживает .length как в JS."""

    @property
    def length(self) -> int:  # noqa: N802
        return len(self)


class _VaultSandbox:
    """Безопасный прокси vault для JS контекста. Только чтение внутри vault_root."""

    def __init__(self, root: Path, title: str) -> None:
        try:
            self._root = Path(root).resolve() if root else (Path.home() / "desktop").resolve()
        except Exception:
            self._root = Path.home() / "desktop"
        self.root: str = str(self._root)
        self.title: str = str(title or "")
        # alias для JS стиля
        self.vault_root: str = self.root

    def listFiles(self, pattern: str = "*.md", limit: int = 100) -> _JSArray:  # noqa: N802
        """Список файлов внутри vault (относительные пути). JS: vault.listFiles('*.md')"""
        try:
            pat = pattern or "*.md"
            lim = max(1, min(int(limit), 500))
            # защита от паттерна с ..
            if ".." in pat or pat.startswith("/"):
                return _JSArray()
            files = list(self._root.rglob(pat))[:lim]
            out = _JSArray()
            for p in files:
                try:
                    if p.is_file():
                        out.append(str(p.relative_to(self._root)))
                except Exception:
                    continue
            return out
        except Exception:
            return _JSArray()

    # pythonic alias
    list_files = listFiles

    def read(self, rel_path: str, max_len: int = 4000) -> str:
        """Безопасное чтение файла внутри vault. JS: vault.read('notes/foo.md')"""
        try:
            rel = str(rel_path or "").strip().lstrip("/")
            if not rel or ".." in rel:
                # допускаем .. только если после resolve всё ещё внутри root
                pass
            p = (self._root / rel).resolve()
            # traversal защита
            try:
                p.relative_to(self._root)
            except ValueError:
                return "[blocked: path traversal]"
            if not p.is_file():
                return ""
            text = p.read_text(encoding="utf-8")
            lim = max(1, min(int(max_len), _JS_MAX_OUTPUT_LEN))
            return text[:lim]
        except Exception as e:
            return f"[read error: {e}]"

    def exists(self, rel_path: str) -> bool:
        try:
            rel = str(rel_path or "").strip().lstrip("/")
            p = (self._root / rel).resolve()
            try:
                p.relative_to(self._root)
            except ValueError:
                return False
            return p.exists()
        except Exception:
            return False

    def __repr__(self) -> str:
        return f"<VaultSandbox root={self.root!r}>"


class _Console:
    """Перехват console.log для JS. JS: console.log('hi')"""

    def __init__(self) -> None:
        self.logs: list[str] = []

    def _to_str(self, v: Any) -> str:
        if v is None:
            return "null"
        # js2py объекты -> str
        try:
            return str(v)
        except Exception:
            return ""

    def log(self, *args: Any) -> str:
        s = " ".join(self._to_str(a) for a in args) if args else ""
        if len(self.logs) < _JS_MAX_LOG_LINES:
            self.logs.append(s)
        return s

    warn = log
    error = log
    info = log
    debug = log


def _translate_js_to_py(code: str) -> str:
    """Минимальная трансляция JS -> Python для fallback exec.

    - убирает let/const/var
    - === -> ==, !== -> !=
    - true/false/null/undefined -> True/False/None
    - .toUpperCase() -> .upper(), .toLowerCase() -> .lower(), .trim() -> .strip()
    - обрезает trailing ;
    """
    # убрать let/const/var как отдельные слова
    py_code = re.sub(r"\b(let|const|var)\b", "", code)
    # строгие сравнения
    py_code = py_code.replace("===", "==").replace("!==", "!=")
    # JS литералы -> Python (с границами слов)
    py_code = re.sub(r"\btrue\b", "True", py_code)
    py_code = re.sub(r"\bfalse\b", "False", py_code)
    py_code = re.sub(r"\bnull\b", "None", py_code)
    py_code = re.sub(r"\bundefined\b", "None", py_code)
    # частые JS методы строк
    py_code = py_code.replace(".toUpperCase()", ".upper()")
    py_code = py_code.replace(".toLowerCase()", ".lower()")
    py_code = py_code.replace(".trim()", ".strip()")
    # typeof -> type (эвристика)
    # не трогаем остальной синтаксис; console.log остаётся как вызов объекта console
    return py_code


def _is_js_blocked(code: str) -> str | None:
    """Проверить код на опасные паттерны. Вернёт причину блокировки или None если ок."""
    if len(code) > _JS_MAX_CODE_LEN:
        return f"code too long ({len(code)} > {_JS_MAX_CODE_LEN})"
    if _JS_BLOCKED_RE.search(code):
        return "unsafe pattern blocked"
    # дополнительные эвристики: импорт в любом виде
    low = code.lower()
    for bad in ("__import__", "import os", "import sys", "subprocess", "os.system", "eval("):
        if bad in low:
            # уже покрыто regex, но двойная проверка
            if _JS_BLOCKED_RE.search(code):
                return "unsafe pattern blocked"
    return None


def _build_js_context(
    title: str,
    now: datetime,
    uuid_str: str,
    settings: dict | None,
) -> dict[str, Any]:
    """Собрать контекст для JS: title, date, time, uuid, vault, vault_root."""
    d = now or datetime.now()
    uid = uuid_str if uuid_str is not None else str(uuid.uuid4())
    title_val = str(title or "")
    # vault_root из settings
    vault_root: Path
    if settings and isinstance(settings, dict) and settings.get("vault_root"):
        try:
            vault_root = Path(str(settings["vault_root"]))
        except Exception:
            vault_root = Path.home() / "desktop"
    else:
        vault_root = Path.home() / "desktop"
    date_s = d.date().isoformat()
    time_s = d.strftime("%H:%M")
    vault_obj = _VaultSandbox(vault_root, title_val)
    return {
        "title": title_val,
        "date": date_s,
        "time": time_s,
        "uuid": uid,
        "vault_root": str(vault_root),
        "vault": vault_obj,
        "datetime": d,
        "now": d,
    }


def _eval_js_with_js2py(code: str, ctx: dict[str, Any], console: _Console) -> str | None:
    """Попытаться выполнить через js2py. Вернёт str результат или None если js2py недоступен/ошибка."""
    try:
        import js2py  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        # js2py EvalJs контекст
        # Передаём vault как dict, title/date etc как примитивы
        # console.log -> python функция
        logs: list[str] = console.logs

        def _py_log(*args: Any) -> str:  # type: ignore[no-untyped-def]
            # js2py передаёт JsObject, конвертим через str
            parts: list[str] = []
            for a in args:
                try:
                    # js2py base pystr
                    import js2py.base as _base  # type: ignore

                    parts.append(str(_base.pystr(a)))
                except Exception:
                    parts.append(str(a))
            s = " ".join(parts)
            if len(logs) < _JS_MAX_LOG_LINES:
                logs.append(s)
            return s

        # Подготовим JS прелюдию
        # Экранируем значения через json для безопасной вставки
        import json as _json

        vault_dict = {"root": ctx["vault_root"], "title": ctx["title"]}
        prelude = (
            f"var title = {_json.dumps(ctx['title'])};\n"
            f"var date = {_json.dumps(ctx['date'])};\n"
            f"var time = {_json.dumps(ctx['time'])};\n"
            f"var uuid = {_json.dumps(ctx['uuid'])};\n"
            f"var vault_root = {_json.dumps(ctx['vault_root'])};\n"
            f"var vault = {_json.dumps(vault_dict)};\n"
        )
        # js2py не поддержит прямую привязку python функции через EvalJs, сделаем через set
        js_ctx = js2py.EvalJs({})
        # внедряем console как объект с log
        # js2py: можно выполнить JS чтобы создать console, затем перезаписать log python функцией через JsObject
        js_ctx.execute(prelude + "var console = {log: function(){}, warn: function(){}, error: function(){}, info: function(){}};")
        # теперь подменим log на python функцию
        try:
            js_ctx.console.log = _py_log  # type: ignore[attr-defined]
            js_ctx.console.warn = _py_log  # type: ignore[attr-defined]
            js_ctx.console.error = _py_log  # type: ignore[attr-defined]
            js_ctx.console.info = _py_log  # type: ignore[attr-defined]
        except Exception:
            pass

        # Выполнить пользовательский код
        # Если код — выражение, попробуем вернуть значение через eval
        stripped = code.strip()
        # Убираем trailing ;
        if stripped.endswith(";"):
            stripped = stripped[:-1]
        result: Any = None
        # Попытка: если код содержит return или ; считаем как statements
        has_return = "return" in stripped
        is_multi = "\n" in stripped or ";" in stripped or has_return
        try:
            if not is_multi:
                # попробовать как выражение
                js_ctx.execute(f"var _js_result = ({stripped});")
                try:
                    result = js_ctx._js_result  # type: ignore[attr-defined]
                except Exception:
                    result = None
            else:
                # оборачиваем в функцию для поддержки return
                if has_return:
                    wrapped = "var _js_result = (function(){ " + stripped + " })();"
                else:
                    # исполняем как statements, захватываем последнее выражение если есть
                    # эвристика: добавим _js_result = eval последнего?
                    wrapped = stripped + "\nvar _js_result = (typeof _js_last !== 'undefined' ? _js_last : undefined);"
                    # более простой: просто исполним и попробуем взять _js_result если был присвоен
                    # fallback: исполним code напрямую и _js_result останется undefined
                    # поэтому делаем: var _js_result; <code>; try{_js_result = eval('...')} catch(e){}
                    # упростим: исполним code, затем если _js_result undefined — вернём logs
                    stripped_exec = stripped
                    # если последнее выражение — не присваивание, сохраним его
                    # не усложняем, просто exec
                    js_ctx.execute(stripped_exec)
                    try:
                        result = js_ctx._js_result  # type: ignore[attr-defined]
                    except Exception:
                        result = None
                    # если result ещё None и есть логи — вернём логи позже
                    if has_return:
                        pass
                    else:
                        # попытка достать результат уже выполнена
                        pass
                    # для multi без return уже выполнили, вернём логи/result
                    if logs:
                        return "\n".join(logs) if result is None or str(result) in ("undefined", "None") else str(result)
                    if result is not None:
                        try:
                            import js2py.base as _base  # type: ignore

                            s = str(_base.pystr(result))
                            return "" if s == "undefined" else s
                        except Exception:
                            return str(result) if str(result) != "undefined" else ""
                    return "\n".join(logs) if logs else ""
                js_ctx.execute(wrapped)
                try:
                    result = js_ctx._js_result  # type: ignore[attr-defined]
                except Exception:
                    result = None
        except Exception as e:
            return f"[JS error: {e}]"

        # Интерпретация результата
        if logs:
            # если был console.log — приоритет логам, если result пустой/undefined
            try:
                import js2py.base as _base  # type: ignore

                r_str = str(_base.pystr(result)) if result is not None else "undefined"
            except Exception:
                r_str = str(result) if result is not None else "undefined"
            if r_str in ("undefined", "None", ""):
                return "\n".join(logs)
            # если есть и логи и результат — склеим (логи первыми как side-effect)
            # но для {{js: console.log('hi')}} ожидаем "hi", а не "hi\nhi"
            # поэтому если результат == логу, вернём только лог
            if r_str in logs:
                return "\n".join(logs)
            # иначе вернём результат, а логи игнорируем? Выберем логи если они есть и result undefined
            # уже обработано выше; здесь вернём результат
            return r_str
        if result is None:
            return ""
        try:
            import js2py.base as _base  # type: ignore

            s = str(_base.pystr(result))
            return "" if s == "undefined" else s
        except Exception:
            return str(result)
    except Exception as e:
        # js2py ошибка — fallback к python exec вернёт своё
        return f"[JS error: {e}]"


def _eval_js_fallback(code: str, ctx: dict[str, Any], console: _Console) -> str:
    """Безопасный Python-exec фоллбэк для JS. Возвращает строку для подстановки."""
    py_code = _translate_js_to_py(code)
    # блокировка уже проверена выше, но перепроверим на py_code
    blocked = _is_js_blocked(py_code)
    if blocked:
        return f"[JS blocked: {blocked}]"

    # Контекст переменных
    vault_obj: _VaultSandbox = ctx["vault"]
    local_vars: dict[str, Any] = {
        "console": console,
        "vault": vault_obj,
        "title": ctx["title"],
        "date": ctx["date"],
        "time": ctx["time"],
        "uuid": ctx["uuid"],
        "vault_root": ctx["vault_root"],
        "vaultRoot": ctx["vault_root"],
        # дополнительные алиасы
        "now": ctx.get("now"),
        "datetime": ctx.get("datetime"),
    }
    # также дать доступ к SimpleNamespace для vault как dict
    # vault уже объект

    globals_map: dict[str, Any] = {"__builtins__": _SAFE_BUILTINS}

    # Попытка выполнить как выражение (eval) — самый частый случай: {{js: 2+2}} или {{js: title.toUpperCase()}} -> в fallback title это python str
    # Для совместимости добавим обработку JS строковых методов: если код содержит .toUpperCase etc — попробуем заменить
    # простая эвристика: title.toUpperCase() -> title.upper()
    # но не усложняем, оставим как есть; пользователь может писать Python-совместимый код в fallback режиме

    # Обработка return-обёртки
    stripped = py_code.strip()
    has_return = bool(re.search(r"\breturn\b", stripped))
    try:
        if has_return:
            # Обернуть в функцию чтобы поддержать return
            # textwrap.indent для корректных отступов
            func_body = textwrap.indent(stripped, "    ")
            wrapped = f"def _js_fn():\n{func_body}\n_result = _js_fn()"
            exec(wrapped, globals_map, local_vars)  # noqa: S102
            result = local_vars.get("_result")
            if result is not None:
                s = str(result)
                return s[:_JS_MAX_OUTPUT_LEN]
            if console.logs:
                return "\n".join(console.logs)[:_JS_MAX_OUTPUT_LEN]
            return ""
        # Попытка eval как выражения
        try:
            tree = ast.parse(stripped, mode="eval")
            # ast.parse eval успешен -> это выражение
            result = eval(stripped, globals_map, local_vars)  # noqa: S307
            if result is not None:
                return str(result)[:_JS_MAX_OUTPUT_LEN]
            if console.logs:
                return "\n".join(console.logs)[:_JS_MAX_OUTPUT_LEN]
            return ""
        except SyntaxError:
            pass

        # Exec режим с захватом последнего выражения
        try:
            tree = ast.parse(stripped, mode="exec")  # type: ignore[assignment]
        except SyntaxError as e:
            return f"[JS error: {e}]"

        if tree.body and isinstance(tree.body[-1], ast.Expr):  # type: ignore
            last_expr_node = tree.body[-1].value  # type: ignore
            # собрать код без последнего выражения для exec
            if len(tree.body) > 1:  # type: ignore
                mod = ast.Module(tree.body[:-1], type_ignores=[])  # type: ignore
                exec(compile(mod, "<js>", "exec"), globals_map, local_vars)  # noqa: S102
            # теперь eval последнего выражения
            last_code = ast.unparse(last_expr_node) if hasattr(ast, "unparse") else None
            if last_code is not None:
                try:
                    result = eval(last_code, globals_map, local_vars)  # noqa: S307
                    # если есть логи — приоритет логам (console.log)
                    if console.logs:
                        # если логи есть, вернуть их (для {{js: console.log('a'); console.log('b')}} -> "a\nb")
                        # если result отличен от логов и не пустой — можно добавить, но базовый случай — только логи
                        # если последнее выражение вернуло None/пусто — точно логи
                        if result is None or str(result) == "":
                            return "\n".join(console.logs)[:_JS_MAX_OUTPUT_LEN]
                        # если result есть и совпадает с последним логом — вернуть все логи
                        # иначе вернуть логи (чтобы не дублировать)
                        # эвристика: если console.log использовался, возвращаем логи
                        return "\n".join(console.logs)[:_JS_MAX_OUTPUT_LEN]
                    if result is not None:
                        s = str(result)
                        if not s and console.logs:
                            return "\n".join(console.logs)[:_JS_MAX_OUTPUT_LEN]
                        return s[:_JS_MAX_OUTPUT_LEN]
                except Exception as e:
                    return f"[JS error: {e}]"
            else:
                # fallback: просто exec всё
                exec(stripped, globals_map, local_vars)  # noqa: S102
        else:
            # нет финального выражения — просто exec
            exec(stripped, globals_map, local_vars)  # noqa: S102

        if console.logs:
            return "\n".join(console.logs)[:_JS_MAX_OUTPUT_LEN]
        # проверить _result если был присвоен внутри
        if "_result" in local_vars and local_vars["_result"] is not None:
            return str(local_vars["_result"])[:_JS_MAX_OUTPUT_LEN]
        # проверить result переменную если пользователь её создал
        if "result" in local_vars and local_vars["result"] is not None:
            return str(local_vars["result"])[:_JS_MAX_OUTPUT_LEN]
        return ""
    except Exception as e:
        return f"[JS error: {e}]"


def _eval_js(code: str, ctx: dict[str, Any]) -> str:
    """Единая точка выполнения JS блока с песочницей."""
    raw = code.strip()
    if not raw:
        return ""
    # Удаляем возможные trailing ; для единообразия
    # но сохраняем ; внутри
    blocked = _is_js_blocked(raw)
    if blocked:
        return f"[JS blocked: {blocked}]"
    # Логи всегда через консоль
    console = _Console()
    # Попытка js2py
    js2py_res = _eval_js_with_js2py(raw, ctx, console)
    if js2py_res is not None:
        # js2py доступен — если он вернул строку (даже с ошибкой) — используем её
        # Но если js2py вернул None из-за ImportError — уже вернули None выше, здесь будет не None
        # Определяем: если js2py_res был получен и не является индикатором отсутствия js2py — вернём
        # _eval_js_with_js2py возвращает None только при ImportError, иначе строку
        # Поэтому если вернулась строка — используем её, даже если это "[JS error...]"
        # Однако если js2py вернул "[JS error...]" из-за синтаксиса, фоллбэк может быть лучше — оставим js2py результат
        # Для консистентности обрежем
        if isinstance(js2py_res, str):
            # если js2py вернул результат и он не пустой — отдать его
            # если же js2py вернул пусто а код был console.log — консоль уже захвачена, js2py_res уже содержит логи
            return js2py_res[:_JS_MAX_OUTPUT_LEN]
    # Фоллбэк Python exec
    # Нужен новый console если js2py уже использовал свой лог — создадим свежий если предыдущий был использован
    # Но _eval_js_with_js2py уже использовал переданный console, поэтому логи уже там; если мы идём в fallback — сбросим
    if js2py_res is not None:
        # js2py был доступен, не идём в fallback
        return js2py_res[:_JS_MAX_OUTPUT_LEN] if isinstance(js2py_res, str) else ""
    # js2py недоступен — чистый fallback
    console2 = _Console()
    return _eval_js_fallback(raw, ctx, console2)[:_JS_MAX_OUTPUT_LEN]


def _render_js_blocks(content: str, ctx: dict[str, Any]) -> str:
    """Заменить все {{js: ...}} на результат выполнения."""

    def _repl(m: re.Match) -> str:
        code = m.group(1) or ""
        # Имя переменной для отладки в ошибках не нужно
        try:
            return _eval_js(code, ctx)
        except Exception as e:
            return f"[JS error: {e}]"

    return _JS_RE.sub(_repl, content)


def get_templates_dir(settings: dict) -> Path:
    """Путь к vault/_System/Templates/ по настройкам."""
    root = Path(settings.get("vault_root") or Path.home() / "desktop")
    return root / TEMPLATES_REL


def ensure_templates_dir(settings: dict) -> Path:
    """Создать директорию шаблонов если отсутствует, вернуть путь."""
    p = get_templates_dir(settings)
    p.mkdir(parents=True, exist_ok=True)
    return p


def list_templates(settings: dict) -> list[Path]:
    """Отсортированный список .md шаблонов."""
    d = get_templates_dir(settings)
    if not d.is_dir():
        return []
    out: list[Path] = []
    for e in d.iterdir():
        if e.is_file() and e.suffix.lower() == ".md":
            out.append(e)
    out.sort(key=lambda p: p.name.lower())
    return out


def list_template_names(settings: dict) -> list[str]:
    return [p.stem for p in list_templates(settings)]


def load_template(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def save_template(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # ensure trailing newline like write_md without frontmatter wrap
    path.write_text(content, encoding="utf-8")


def delete_template(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _format_date(d: datetime, fmt: str | None) -> str:
    """Поддержка форматов: YYYY-MM-DD, YYYY, MM, DD, HH, mm, ss, а также dddd, MMMM и т.д.
    Если fmt is None -> YYYY-MM-DD.
    """
    if fmt is None or not fmt.strip():
        return d.date().isoformat()
    raw = fmt.strip()
    # совместимость с Daily view: {{date:YYYY-MM-DD}}, {{date:YYYY}}, {{date:MM}} etc.
    # Заменяем токены в порядке убывания длины чтобы избежать конфликтов.
    # Поддерживаем:
    # YYYY -> 2026, YY -> 26, MM -> 09, M -> 9, DD -> 04, D -> 4,
    # HH -> 14, mm -> 05, ss -> 09,
    # dddd -> день недели ru, MMMM -> месяц ru
    days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    months_ru = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    # Расширенные шаблоны типа "dddd, D MMMM" -> "четверг, 4 сентября"
    # Сначала заменим dddd и MMMM
    out = raw
    if "dddd" in out:
        out = out.replace("dddd", days_ru[d.weekday()])
    if "MMMM" in out:
        out = out.replace("MMMM", months_ru[d.month - 1])
    # затем числовые токены
    replacements = [
        ("YYYY", f"{d.year:04d}"),
        ("YY", f"{d.year % 100:02d}"),
        ("MM", f"{d.month:02d}"),
        ("DD", f"{d.day:02d}"),
        ("HH", f"{d.hour:02d}"),
        ("mm", f"{d.minute:02d}"),
        ("ss", f"{d.second:02d}"),
    ]
    for token, val in replacements:
        out = out.replace(token, val)
    # одиночные M/D без ведущего нуля (если остались)
    # избегаем порчи уже заменённых — только если формат содержит одиночные и не была двойной
    # просто поддержка M и D как чисел без нуля (редкий случай)
    # Если в формате есть отдельный "M" или "D" не в составе YYYY/MM/DD etc — заменим
    # Для простоты: если строкa содержит " M " или ", M" - уже обработано через MMMM, иначе ignore
    return out


def _format_time(d: datetime, fmt: str | None) -> str:
    if fmt is None or not fmt.strip():
        return d.strftime("%H:%M")
    raw = fmt.strip()
    out = raw
    out = out.replace("HH", f"{d.hour:02d}")
    out = out.replace("mm", f"{d.minute:02d}")
    out = out.replace("ss", f"{d.second:02d}")
    # также YYYY etc не логично для time, но поддержим
    out = out.replace("YYYY", f"{d.year:04d}")
    out = out.replace("MM", f"{d.month:02d}")
    out = out.replace("DD", f"{d.day:02d}")
    return out


def render_template(
    content: str,
    title: str = "",
    *,
    now: datetime | None = None,
    uuid_str: str | None = None,
    settings: dict | None = None,
    vault_root: str | Path | None = None,
    extra_context: dict[str, Any] | None = None,
) -> str:
    """Подставить переменные в шаблон.

    Args:
        content: сырой текст шаблона.
        title: заголовок заметки (для {{title}}).
        now: фиксированное время (для тестов), иначе datetime.now().
        uuid_str: фиксированный uuid (для тестов), иначе uuid4.
        settings: словарь настроек с ``vault_root`` для JS контекста vault.
        vault_root: альтернатива settings — прямой путь к vault.
        extra_context: дополнительные переменные для JS (необязательно).

    Заменяет {{date}}, {{time}}, {{title}}, {{uuid}} и их форматные варианты,
    а также выполняет {{js: <code>}} в песочнице (js2py или Python fallback).
    Неизвестные форматы фоллбэкаются на iso date/time.
    """
    if content is None:
        return ""
    d = now or datetime.now()
    uid = uuid_str if uuid_str is not None else str(uuid.uuid4())
    title_val = str(title or "")

    # Собрать JS контекст
    js_settings: dict | None = None
    if settings is not None:
        js_settings = dict(settings) if isinstance(settings, dict) else {}
    elif vault_root is not None:
        js_settings = {"vault_root": str(vault_root)}
    else:
        js_settings = {}
    # extra_context может переопределить
    if extra_context:
        # не мутируем ctx напрямую, но можно добавить в settings
        pass

    js_ctx = _build_js_context(title_val, d, uid, js_settings)
    if extra_context:
        # добавить extra в контекст как доступные переменные для fallback
        for k, v in extra_context.items():
            if isinstance(k, str) and k.isidentifier():
                js_ctx[k] = v

    # 1) JS блоки — первыми, чтобы их результат мог содержать переменные? нет, наоборот: JS имеет доступ к title/date напрямую
    # но если JS вернул текст с {{date}} — второй проход подставит и его (опционально)
    content_with_js = _render_js_blocks(content, js_ctx)

    # 2) Переменные {{date}} etc
    def repl(m: re.Match) -> str:
        kind = m.group(1)
        fmt = m.group(2)
        if kind == "date":
            return _format_date(d, fmt)
        if kind == "time":
            return _format_time(d, fmt)
        if kind == "title":
            return title_val
        if kind == "uuid":
            return uid
        return m.group(0)

    return _VAR_RE.sub(repl, content_with_js)


def render_for_new_note(
    settings: dict,
    template_name: str | None,
    title: str,
    *,
    now: datetime | None = None,
    uuid_str: str | None = None,
) -> str:
    """Удобный хелпер для создания заметки из шаблона.

    template_name — stem или имя файла (с или без .md). Если None/пусто/не найден — вернёт "".
    """
    if not template_name:
        return ""
    d = get_templates_dir(settings)
    candidates: list[Path] = []
    name = template_name.strip()
    if not name:
        return ""
    # если указано с расширением
    p1 = d / name
    if p1.is_file():
        candidates.append(p1)
    # stem -> .md
    if not name.lower().endswith(".md"):
        p2 = d / (name + ".md")
        if p2.is_file():
            candidates.append(p2)
    if not candidates:
        # поиск по stem без учета регистра
        low = name.lower().removesuffix(".md")
        for p in list_templates(settings):
            if p.stem.lower() == low:
                candidates.append(p)
                break
    if not candidates:
        return ""
    try:
        raw = candidates[0].read_text(encoding="utf-8")
    except OSError:
        return ""
    return render_template(raw, title, now=now, uuid_str=uuid_str, settings=settings)


__all__ = [
    "TEMPLATES_REL",
    "TEMPLATE_VARS",
    "get_templates_dir",
    "ensure_templates_dir",
    "list_templates",
    "list_template_names",
    "load_template",
    "save_template",
    "delete_template",
    "render_template",
    "render_for_new_note",
]
