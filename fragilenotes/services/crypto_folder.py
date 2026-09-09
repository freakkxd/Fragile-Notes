"""Крипто-папка авто-шифрования FragileNotes.

Папка vault/Secret/ (настраивается через settings: crypto_folder, auto_encrypt)
автоматически шифрует все .md в .md.enc при сохранении (AES-GCM через core.crypto)
и дешифрует при открытии.

Настройки:
    crypto_folder: str — относительный путь от vault_root, по умолчанию "Secret"
    auto_encrypt: bool — включить авто-шифрование, по умолчанию True
    crypto_password: str | None — опционально кэшированный пароль (не рекомендуется
        хранить в settings.json; используется in-memory кэш)

API:
    get_crypto_folder(settings) -> Path          # абсолютный путь Secret/
    get_crypto_folder_relative(settings) -> str  # относительный как в settings
    is_auto_enabled(settings) -> bool
    is_in_crypto_folder(path, settings) -> bool
    should_auto_encrypt(path, settings) -> bool  # .md внутри Secret + auto_encrypt
    should_auto_decrypt(path, settings) -> bool  # .md.enc внутри Secret
    is_encrypted_path(path) -> bool              # прокси core.crypto.is_encrypted
    ensure_crypto_folder(settings) -> Path       # mkdir -p vault/Secret
    encrypt_file_auto(src, password, delete_original=True) -> Path
    decrypt_file_auto(enc, password, delete_original=True) -> Path
    encrypt_text(plaintext, password) -> bytes   # salt+nonce+ct
    decrypt_to_text(data, password) -> str
    read_encrypted_text(path, password) -> str   # decrypt file -> str
    write_encrypted_text(path, text, password) -> Path  # encrypt text -> .enc file
    handle_open(path, password, settings) -> str | None  # auto decrypt if needed else None
    handle_save(path, text, password, settings) -> Path  # auto encrypt if needed else write plain

In-memory пароль:
    set_cached_password(pwd) / get_cached_password() / clear_cached_password()

Класс CryptoFolderService — обёртка с settings для удобства в UI.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# ── core.crypto (AES-GCM) ────────────────────────────────────────────
try:
    from ..core import crypto as _crypto
except Exception:  # pragma: no cover
    _crypto = None  # type: ignore

# ── Константы ────────────────────────────────────────────────────────
CRYPTO_FOLDER_DEFAULT: str = "Secret"
AUTO_ENCRYPT_DEFAULT: bool = True
ENC_SUFFIX: str = ".enc"

# ── In-memory кэш пароля ─────────────────────────────────────────────
_cached_password: str | None = None


def set_cached_password(pwd: str | None) -> None:
    global _cached_password
    _cached_password = str(pwd) if pwd else None


def get_cached_password() -> str | None:
    # приоритет in-memory, затем settings fallback делает вызывающий код
    return _cached_password


def clear_cached_password() -> None:
    global _cached_password
    _cached_password = None


def _get_password_from_settings(settings: dict[str, Any]) -> str | None:
    try:
        pwd = settings.get("crypto_password")
        if isinstance(pwd, str) and pwd:
            return pwd
        if isinstance(pwd, bytes) and pwd:
            return pwd.decode("utf-8", errors="ignore")
    except Exception:
        pass
    return None


def resolve_password(settings: dict[str, Any] | None = None) -> str | None:
    """Вернуть пароль из кэша или settings, если есть."""
    if _cached_password:
        return _cached_password
    if settings is not None:
        p = _get_password_from_settings(settings)
        if p:
            return p
    return None


# ── Пути и проверки ──────────────────────────────────────────────────
def get_crypto_folder_relative(settings: dict[str, Any]) -> str:
    try:
        v = str(settings.get("crypto_folder") or CRYPTO_FOLDER_DEFAULT).strip()
        # нормализуем: убираем ведущие ./ и слэши
        v = v.strip("/\\").strip()
        if not v:
            return CRYPTO_FOLDER_DEFAULT
        # защита от абсолютных путей / traversal
        # оставляем как относительный
        p = Path(v)
        if p.is_absolute():
            # берём имя
            return p.name or CRYPTO_FOLDER_DEFAULT
        return v
    except Exception:
        return CRYPTO_FOLDER_DEFAULT


def get_crypto_folder(settings: dict[str, Any]) -> Path:
    """Абсолютный путь крипто-папки: vault_root / crypto_folder."""
    root = Path(str(settings.get("vault_root") or Path.home()))
    rel = get_crypto_folder_relative(settings)
    # rel может содержать подпапки "Secret/Notes"
    return (root / rel).resolve(strict=False)


def is_auto_enabled(settings: dict[str, Any]) -> bool:
    try:
        v = settings.get("auto_encrypt")
        if v is None:
            return AUTO_ENCRYPT_DEFAULT
        return bool(v)
    except Exception:
        return AUTO_ENCRYPT_DEFAULT


def is_encrypted_path(path: Path | str) -> bool:
    try:
        if _crypto is not None and hasattr(_crypto, "is_encrypted"):
            return bool(_crypto.is_encrypted(Path(path)))
    except Exception:
        pass
    s = str(path)
    return s.endswith(ENC_SUFFIX) or ".enc" in Path(s).suffixes


def is_in_crypto_folder(path: Path | str, settings: dict[str, Any]) -> bool:
    """Проверить, лежит ли path внутри vault/Secret/.

    Учитывает как plain .md так и .md.enc (снимает .enc для проверки папки).
    Сравнивает через resolve(strict=False) и relative_to с fallback на строковый префикс.
    """
    try:
        p = Path(path).resolve(strict=False)
        folder = get_crypto_folder(settings).resolve(strict=False)
        # если путь — это .md.enc, проверяем также без суффикса .enc
        # но relative_to всё равно сработает и для .enc пути, т.к. папка та же
        # пробуем прямое relative_to
        try:
            p.relative_to(folder)
            return True
        except ValueError:
            pass
        # если p это файл .md.enc, попробуем без .enc суффикса (на случай симлинков)
        # также проверяем parent
        try:
            # для файла внутри Secret: parent должен быть внутри folder
            p.parent.resolve(strict=False).relative_to(folder)
            # но если файл вне folder а parent совпадает частично? relative_to уже проверил
            # дополнительная проверка что p находится внутри folder как строка
            s_p = str(p)
            s_f = str(folder)
            # нормализуем с завершающим разделителем
            if s_p == s_f:
                return True
            if s_p.startswith(s_f + os.sep):
                return True
            return False
        except ValueError:
            # fallback строковый префикс
            s_p = str(p)
            s_f = str(folder)
            if s_p == s_f or s_p.startswith(s_f + os.sep):
                return True
            return False
    except Exception:
        return False


def should_auto_encrypt(path: Path | str, settings: dict[str, Any]) -> bool:
    """Нужно ли авто-шифровать при сохранении: auto_encrypt + внутри Secret + .md plain."""
    if not is_auto_enabled(settings):
        return False
    if not is_in_crypto_folder(path, settings):
        return False
    # уже зашифрован — не шифруем повторно, а перешифруем через handle_save
    if is_encrypted_path(path):
        return False
    # только .md / .txt ? по ТЗ — все .md; расширяем на .md и .txt как в vault
    p = Path(path)
    # проверяем суффикс без .enc
    suffix = p.suffix.lower()
    # .md или .txt внутри Secret
    if suffix in (".md", ".txt", ".json", ".yaml", ".yml"):
        return True
    # если без расширения но имя .md ?
    if p.name.lower().endswith(".md"):
        return True
    return False


def should_auto_decrypt(path: Path | str, settings: dict[str, Any]) -> bool:
    """Нужно ли авто-дешифровать при открытии: внутри Secret + .enc."""
    # дешифруем если файл внутри Secret и зашифрован, независимо от auto_encrypt?
    # но по ТЗ — при auto_encrypt, поэтому проверяем флаг
    # для прозрачности: если файл .enc внутри Secret — всегда пытаемся дешифровать
    if not is_in_crypto_folder(path, settings):
        return False
    return is_encrypted_path(path)


# Convenience aliases (как указано в ТЗ)
def should_encrypt(path: Path | str, settings: dict[str, Any]) -> bool:
    return should_auto_encrypt(path, settings)


def should_decrypt(path: Path | str, settings: dict[str, Any]) -> bool:
    return should_auto_decrypt(path, settings)


# ── Обеспечение папки ────────────────────────────────────────────────
def ensure_crypto_folder(settings: dict[str, Any]) -> Path:
    folder = get_crypto_folder(settings)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return folder


# ── Низкоуровневые шифр/дешифр текста ────────────────────────────────
def encrypt_text(plaintext: str | bytes, password: str | bytes) -> bytes:
    if _crypto is None:
        raise ImportError("cryptography не установлена — pip install cryptography")
    if isinstance(plaintext, str):
        data = plaintext.encode("utf-8")
    else:
        data = bytes(plaintext)
    return _crypto.encrypt_bytes(data, password)  # type: ignore[union-attr]


def decrypt_to_text(data: bytes, password: str | bytes) -> str:
    if _crypto is None:
        raise ImportError("cryptography не установлена — pip install cryptography")
    plain = _crypto.decrypt_bytes(data, password)  # type: ignore[union-attr]
    return plain.decode("utf-8")


def read_encrypted_text(path: Path | str, password: str | bytes) -> str:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"файл не найден: {p}")
    data = p.read_bytes()
    return decrypt_to_text(data, password)


def write_encrypted_text(
    path: Path | str,
    text: str,
    password: str | bytes,
    *,
    delete_original: bool = False,
) -> Path:
    """Зашифровать text и записать в path (.md -> .md.enc если нужно).

    Если path — plain .md внутри Secret, создаёт .md.enc рядом и при
    delete_original=True удаляет исходник. Если path уже .enc — перезаписывает его.
    Возвращает путь к созданному .enc файлу.
    """
    if _crypto is None:
        raise ImportError("cryptography не установлена — pip install cryptography")
    p = Path(path)
    data = encrypt_text(text, password)
    # определеяем целевой .enc путь
    if is_encrypted_path(p):
        dst = p
        # если dst совпадает с исходником plain? нет
        delete_original = False
    else:
        dst = Path(str(p) + ENC_SUFFIX)
    # атомарная запись как в core.crypto._secure_atomic_write
    # используем тот же механизм если доступен
    if hasattr(_crypto, "_secure_atomic_write"):
        tmp = dst.with_name(dst.name + ".tmp")
        _crypto._secure_atomic_write(tmp, data, dst)  # type: ignore[attr-defined]
        try:
            os.chmod(dst, 0o600)
        except OSError:
            pass
    else:
        tmp = dst.with_name(dst.name + ".tmp")
        tmp.write_bytes(data)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(dst)
    if delete_original and p != dst and p.exists():
        try:
            p.unlink()
        except OSError:
            pass
    return dst


# ── Файловые операции (прокси core.crypto) ───────────────────────────
def encrypt_file_auto(
    src: Path | str,
    password: str | bytes,
    *,
    delete_original: bool = True,
) -> Path:
    if _crypto is None:
        raise ImportError("cryptography не установлена — pip install cryptography")
    return _crypto.encrypt_file(src, password, delete_original=delete_original)  # type: ignore[union-attr]


def decrypt_file_auto(
    enc: Path | str,
    password: str | bytes,
    *,
    delete_original: bool = True,
) -> Path:
    if _crypto is None:
        raise ImportError("cryptography не установлена — pip install cryptography")
    return _crypto.decrypt_file(enc, password, delete_original=delete_original)  # type: ignore[union-attr]


# Алиасы encrypt/decrypt для совместимости с ТЗ
def encrypt(src: Path | str, password: str | bytes, *, delete_original: bool = True) -> Path:
    return encrypt_file_auto(src, password, delete_original=delete_original)


def decrypt(enc: Path | str, password: str | bytes, *, delete_original: bool = True) -> Path:
    return decrypt_file_auto(enc, password, delete_original=delete_original)


# ── High-level для files_view ────────────────────────────────────────
def handle_open(
    path: Path | str,
    password: str | bytes | None,
    settings: dict[str, Any],
) -> str | None:
    """Если path требует авто-дешифрации — вернуть расшифрованный текст, иначе None.

    Вызывающий код: if handle_open(...) is not None: use decrypted else read plain.
    """
    if not should_auto_decrypt(path, settings):
        return None
    pwd = password if password is not None else resolve_password(settings)
    if not pwd:
        return None
    try:
        return read_encrypted_text(path, pwd)
    except Exception:
        return None


def handle_save(
    path: Path | str,
    text: str,
    password: str | bytes | None,
    settings: dict[str, Any],
) -> Path | None:
    """Если path требует авто-шифрования — зашифровать и вернуть путь к .enc, иначе None.

    Для plain .md внутри Secret создаёт .md.enc и удаляет исходник (если delete_original).
    Для уже .enc внутри Secret перезаписывает .enc шифртекстом.
    """
    p = Path(path)
    # случай 1: plain .md внутри Secret -> конвертировать в .enc
    if should_auto_encrypt(p, settings):
        pwd = password if password is not None else resolve_password(settings)
        if not pwd:
            return None
        return write_encrypted_text(p, text, pwd, delete_original=True)
    # случай 2: уже .enc внутри Secret -> перешифровать (overwrite)
    if is_encrypted_path(p) and is_in_crypto_folder(p, settings) and is_auto_enabled(settings):
        pwd = password if password is not None else resolve_password(settings)
        if not pwd:
            return None
        # перезапись того же .enc
        return write_encrypted_text(p, text, pwd, delete_original=False)
    return None


# ── Сервис-класс ─────────────────────────────────────────────────────
class CryptoFolderService:
    """Удобная обёртка над функциями модуля с хранением settings."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings: dict[str, Any] = dict(settings) if settings is not None else {}

    def update_settings(self, settings: dict[str, Any]) -> None:
        self.settings = dict(settings) if settings is not None else {}

    @property
    def folder(self) -> Path:
        return get_crypto_folder(self.settings)

    @property
    def relative(self) -> str:
        return get_crypto_folder_relative(self.settings)

    def is_enabled(self) -> bool:
        return is_auto_enabled(self.settings)

    def is_in_folder(self, path: Path | str) -> bool:
        return is_in_crypto_folder(path, self.settings)

    def should_encrypt(self, path: Path | str) -> bool:
        return should_auto_encrypt(path, self.settings)

    def should_decrypt(self, path: Path | str) -> bool:
        return should_auto_decrypt(path, self.settings)

    def ensure_folder(self) -> Path:
        return ensure_crypto_folder(self.settings)

    def encrypt_text(self, text: str, password: str | bytes) -> bytes:
        return encrypt_text(text, password)

    def decrypt_to_text(self, data: bytes, password: str | bytes) -> str:
        return decrypt_to_text(data, password)

    def handle_open(self, path: Path | str, password: str | bytes | None = None) -> str | None:
        pwd = password if password is not None else resolve_password(self.settings)
        return handle_open(path, pwd, self.settings)

    def handle_save(self, path: Path | str, text: str, password: str | bytes | None = None) -> Path | None:
        pwd = password if password is not None else resolve_password(self.settings)
        return handle_save(path, text, pwd, self.settings)


__all__ = [
    "CRYPTO_FOLDER_DEFAULT",
    "AUTO_ENCRYPT_DEFAULT",
    "ENC_SUFFIX",
    "set_cached_password",
    "get_cached_password",
    "clear_cached_password",
    "resolve_password",
    "get_crypto_folder",
    "get_crypto_folder_relative",
    "is_auto_enabled",
    "is_encrypted_path",
    "is_in_crypto_folder",
    "should_auto_encrypt",
    "should_auto_decrypt",
    "should_encrypt",
    "should_decrypt",
    "ensure_crypto_folder",
    "encrypt_text",
    "decrypt_to_text",
    "read_encrypted_text",
    "write_encrypted_text",
    "encrypt_file_auto",
    "decrypt_file_auto",
    "encrypt",
    "decrypt",
    "handle_open",
    "handle_save",
    "CryptoFolderService",
]
