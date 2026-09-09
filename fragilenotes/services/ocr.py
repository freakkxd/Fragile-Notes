"""OCR для изображений в vault через tesseract.

Если tesseract установлен — распознаёт текст и сохраняет рядом
с изображением файл ``<image>.ocr.md``. Если не установлен —
мягко сообщает об отсутствии, не падает.

Сохранение: ``image.png`` → ``image.png.ocr.md`` (рядом, тот же каталог).
Внутри — markdown с frontmatter ``source``/``lang`` и распознанным текстом.

Без внешних зависимостей (stdlib): ``shutil.which`` + ``subprocess``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# tesseract поддерживает много форматов; берём распространённые в vault
IMAGE_EXTS: set[str] = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".tiff",
    ".tif",
    ".bmp",
    ".gif",
    ".pnm",
    ".pgm",
    ".ppm",
}

OCR_SUFFIX = ".ocr.md"
DEFAULT_LANG = "eng+rus"
# тяжёлые каталоги не сканируем — как в vault.HEAVY_DIRS
_HEAVY_DIRS = {
    "node_modules",
    ".git",
    "dist",
    "build",
    "target",
    ".venv",
    "venv",
    "__pycache__",
    "bin",
    "obj",
    ".cache",
    ".trash",
    ".obsidian",
    "Trash",
    "ao-engine",
}

@dataclass(frozen=True)
class OcrResult:
    ok: bool
    text: str
    error: str | None
    image: Path
    sidecar: Path | None = None


def is_tesseract_available() -> bool:
    """Проверка наличия бинаря tesseract в PATH."""
    return shutil.which("tesseract") is not None


# алиасы для совместимости с тестами / внешним API
tesseract_available = is_tesseract_available
is_available = is_tesseract_available


def tesseract_version() -> str | None:
    """Версия tesseract или None если не установлен."""
    bin_path = shutil.which("tesseract")
    if not bin_path:
        return None
    try:
        proc = subprocess.run(
            [bin_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        out = (proc.stdout or proc.stderr or "").strip()
        # первая строка вида "tesseract 5.3.1"
        if out:
            return out.splitlines()[0].strip()
        return None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def get_available_langs() -> list[str]:
    """Список языков tesseract (``tesseract --list-langs``). Пусто если недоступен."""
    bin_path = shutil.which("tesseract")
    if not bin_path:
        return []
    try:
        proc = subprocess.run(
            [bin_path, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        out = (proc.stdout or proc.stderr or "").strip()
        langs: list[str] = []
        for line in out.splitlines():
            line = line.strip()
            if not line or line.lower().startswith("list of"):
                continue
            langs.append(line)
        return langs
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def is_image_file(path: Path | str) -> bool:
    """Является ли путь изображением по расширению."""
    try:
        return Path(path).suffix.lower() in IMAGE_EXTS
    except Exception:
        return False


def ocr_sidecar_path(image_path: Path | str) -> Path:
    """Путь к sidecar ``.ocr.md`` рядом с изображением.

    Пример: ``photo.jpg`` → ``photo.jpg.ocr.md``.
    Не меняет исходный файл, просто добавляет суффикс.
    """
    p = Path(image_path)
    # добавление суффикса: имя файла + ".ocr.md"
    # Path("a.png").name + ".ocr.md" -> "a.png.ocr.md"
    return p.with_name(p.name + OCR_SUFFIX)


# алиас
get_ocr_path = ocr_sidecar_path
sidecar_path = ocr_sidecar_path


def _resolve_lang(lang: str | None) -> str:
    """Выбор языка с fallback если запрошенного нет."""
    if lang:
        return lang.strip() or DEFAULT_LANG
    # авто: если rus доступен — eng+rus, иначе eng
    avail = get_available_langs()
    if not avail:
        # tesseract не установлен или не отвечает — вернём дефолт,
        # проверка наличия произойдёт при вызове
        return DEFAULT_LANG
    has_eng = "eng" in avail
    has_rus = "rus" in avail
    if has_eng and has_rus:
        return "eng+rus"
    if has_eng:
        return "eng"
    if avail:
        return avail[0]
    return DEFAULT_LANG


def find_images(
    vault_root: Path | str,
    recursive: bool = True,
    settings: dict | None = None,
) -> list[Path]:
    """Найти все изображения в vault (рекурсивно, пропуская тяжёлые/скрытые).

    Args:
        vault_root: корень vault или путь. Если передан ``settings``,
            ``vault_root`` игнорируется и берётся ``resolve_paths(settings).root``.
        recursive: рекурсивный обход (иначе только верхний уровень).
        settings: опциональный словарь настроек (vault_root и т.д.).

    Returns:
        Список абсолютных Path к изображениям.
    """
    root: Path
    if settings is not None:
        try:
            from ..paths import resolve_paths

            root = resolve_paths(settings).root
        except Exception:
            root = Path(vault_root)
    else:
        root = Path(vault_root)

    if not root.is_dir():
        return []

    out: list[Path] = []
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    name = entry.name
                    if name.startswith("."):
                        continue
                    if name.endswith(OCR_SUFFIX):
                        # не считаем sidecar-ы изображениями
                        continue
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_dir:
                        if name in _HEAVY_DIRS:
                            continue
                        if recursive:
                            stack.append(Path(entry.path))
                    else:
                        try:
                            is_file = entry.is_file(follow_symlinks=False)
                        except OSError:
                            continue
                        if not is_file:
                            continue
                        suf = Path(name).suffix.lower()
                        if suf in IMAGE_EXTS:
                            out.append(Path(entry.path))
        except OSError:
            continue
    out.sort(key=lambda p: p.as_posix().lower())
    return out


def ocr_image(
    image_path: Path | str,
    lang: str | None = None,
    psm: int | None = None,
    timeout: float = 30,
) -> tuple[bool, str]:
    """Распознать текст на изображении через tesseract.

    Args:
        image_path: путь к изображению.
        lang: язык (например ``"eng+rus"``). ``None`` — авто (eng+rus если есть).
        psm: Page Segmentation Mode (0-13), None — не передавать.
        timeout: таймаут секунд.

    Returns:
        (ok, text_or_error): если ok=True, второй элемент — распознанный текст
        (может быть пустым если ничего не найдено). Если ok=False — сообщение об ошибке
        (включая "tesseract не установлен").
    """
    p = Path(image_path)
    if not p.is_file():
        return False, f"файл не найден: {p}"
    if not is_image_file(p):
        return False, f"не изображение: {p.suffix}"

    bin_path = shutil.which("tesseract")
    if not bin_path:
        return False, "tesseract не установлен (apt install tesseract-ocr / brew install tesseract)"

    chosen_lang = _resolve_lang(lang)
    cmd: list[str] = [bin_path, str(p), "stdout", "-l", chosen_lang]
    if psm is not None:
        cmd += ["--psm", str(int(psm))]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "tesseract timeout"
    except (OSError, ValueError) as exc:
        return False, str(exc)

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        # если язык не найден — пробуем fallback на eng
        if "Error opening data file" in err and chosen_lang != "eng":
            # retry once with eng
            try:
                proc2 = subprocess.run(
                    [bin_path, str(p), "stdout", "-l", "eng"] + (["--psm", str(int(psm))] if psm is not None else []),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                if proc2.returncode == 0:
                    return True, (proc2.stdout or "")
                err2 = (proc2.stderr or proc2.stdout or "").strip()
                return False, err2 or err
            except Exception:
                pass
        return False, err

    text = proc.stdout or ""
    return True, text


# алиасы
run_ocr = ocr_image
recognize = ocr_image


def read_sidecar(image_path: Path | str) -> str | None:
    """Прочитать существующий sidecar если есть, иначе None."""
    side = ocr_sidecar_path(image_path)
    if not side.is_file():
        return None
    try:
        return side.read_text(encoding="utf-8")
    except OSError:
        return None


def save_ocr_text(
    image_path: Path | str,
    text: str,
    lang: str | None = None,
    overwrite: bool = True,
) -> Path | None:
    """Сохранить распознанный текст в ``.ocr.md`` рядом с изображением.

    Формат — markdown с frontmatter для интеграции с vault.

    Args:
        image_path: исходное изображение.
        text: распознанный текст (может быть пустым).
        lang: язык распознавания (для frontmatter).
        overwrite: перезаписывать ли существующий sidecar.

    Returns:
        Путь к sidecar или None если не удалось записать.
    """
    side = ocr_sidecar_path(image_path)
    if side.exists() and not overwrite:
        return side
    p = Path(image_path)
    chosen_lang = lang or _resolve_lang(None)
    # frontmatter
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    src_name = p.name
    # безопасный yaml-фронтматтер вручную без зависимости
    fm_lines = [
        "---",
        f'source: "{src_name}"',
        f"ocr_lang: {chosen_lang}",
        "ocr_engine: tesseract",
        f"ocr_date: {ts}",
        "---",
        "",
    ]
    # текст как есть, но экранируем возможные --- в начале?
    body = (text or "").strip()
    if not body:
        body = "_пусто — tesseract не нашёл текст_"
    content = "\n".join(fm_lines) + body + "\n"
    try:
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_text(content, encoding="utf-8")
        return side
    except OSError:
        return None


def process_image(
    image_path: Path | str,
    lang: str | None = None,
    psm: int | None = None,
    overwrite: bool = False,
    timeout: float = 30,
) -> tuple[bool, str, Path | None]:
    """Распознать одно изображение и сохранить в sidecar.

    Args:
        image_path: путь к изображению.
        lang: язык.
        psm: psm.
        overwrite: перезаписывать ли существующий ``.ocr.md`` (если False
            и sidecar уже есть и новее изображения — пропуск).
        timeout: таймаут tesseract.

    Returns:
        (ok, message, sidecar_path): ok — успех распознавания,
        message — текст или ошибка, sidecar — путь к файлу если сохранён.
    """
    p = Path(image_path)
    side = ocr_sidecar_path(p)

    # пропуск если sidecar уже есть и свежий и overwrite=False
    if not overwrite and side.is_file():
        try:
            if side.stat().st_mtime >= p.stat().st_mtime:
                # уже актуален — возвращаем содержимое
                existing = read_sidecar(p)
                # извлечь тело без frontmatter для возврата?
                if existing is not None:
                    return True, existing, side
                return True, "", side
        except OSError:
            pass

    ok, out = ocr_image(p, lang=lang, psm=psm, timeout=timeout)
    if not ok:
        return False, out, None
    # out — распознанный текст
    saved = save_ocr_text(p, out, lang=lang or _resolve_lang(None), overwrite=True)
    if saved is None:
        return False, "не удалось записать .ocr.md", None
    return True, out, saved


def batch_ocr(
    vault_root: Path | str,
    settings: dict | None = None,
    lang: str | None = None,
    overwrite: bool = False,
    limit: int | None = None,
    psm: int | None = None,
    timeout: float = 30,
) -> dict:
    """Пакетный OCR всех изображений в vault.

    Args:
        vault_root: корень vault.
        settings: опционально настройки (приоритет).
        lang: язык.
        overwrite: перезаписывать ли существующие sidecar-ы.
        limit: лимит количества изображений (None — все).
        psm: Page Segmentation Mode.
        timeout: таймаут на файл.

    Returns:
        dict ``{"total": int, "ok": int, "failed": int, "skipped": int,
               "sidecars": [str], "errors": [{"image": str, "error": str}]}``.
        Если tesseract не установлен — ``{"total": 0, "ok": 0, "failed": 0,
        "skipped": 0, "error": "tesseract не установлен"}``.
    """
    images = find_images(vault_root, recursive=True, settings=settings)
    if limit is not None and limit > 0:
        images = images[: int(limit)]

    if not is_tesseract_available():
        return {
            "total": len(images),
            "ok": 0,
            "failed": 0,
            "skipped": 0,
            "sidecars": [],
            "errors": [],
            "error": "tesseract не установлен (apt install tesseract-ocr)",
        }
    if limit is not None and limit > 0:
        images = images[:limit]

    total = len(images)
    ok_cnt = 0
    failed = 0
    skipped = 0
    sidecars: list[str] = []
    errors: list[dict[str, str]] = []

    # заранее резолвим язык один раз чтобы не дергать --list-langs на каждый файл
    chosen_lang = _resolve_lang(lang)

    for img in images:
        side = ocr_sidecar_path(img)
        if not overwrite and side.is_file():
            try:
                if side.stat().st_mtime >= img.stat().st_mtime:
                    skipped += 1
                    sidecars.append(str(side))
                    continue
            except OSError:
                pass

        ok, msg, saved = process_image(img, lang=chosen_lang, psm=psm, overwrite=True, timeout=timeout)
        if ok and saved is not None:
            ok_cnt += 1
            sidecars.append(str(saved))
        else:
            failed += 1
            errors.append({"image": str(img), "error": msg})

    return {
        "total": total,
        "ok": ok_cnt,
        "failed": failed,
        "skipped": skipped,
        "sidecars": sidecars,
        "errors": errors,
    }


# Дополнительные алиасы для удобства
ocr_for_vault = batch_ocr
scan_and_ocr = batch_ocr
