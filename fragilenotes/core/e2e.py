"""E2E шифрование всего vault — каждый файл AES-GCM с общим ключом из пароля.

Формат:
  * Общий ключ: PBKDF2-HMAC-SHA256(password, salt_global, 200_000) -> 32 байта (AES-256).
    Salt глобальный хранится в vault/.e2e_salt (16 байт, chmod 600). Один на весь vault.
  * Каждый файл: [nonce 12 байт][ciphertext+tag 16 байт внутри] — AES-GCM.
    Salt в файл НЕ пишется (общий), nonce уникален per-file (secrets + защита от коллизий).
    AAD = b"FragileNotes-e2e-v1".
  * На диске: foo.md -> foo.md.enc (атомарная запись tmp+rename+fsync, chmod 600).
    Оригинал удаляется (delete_original=True по умолчанию).
  * Vault команды: encrypt_vault / decrypt_vault обходят vault рекурсивно.

Интеграция с settings (config.py):
  * e2e_enabled: bool — включён ли E2E режим (по умолчанию False)
  * e2e_salt_file: str — имя файла соли внутри vault (по умолчанию ".e2e_salt")
  * Пароль НЕ хранится в settings — передаётся вызовом / UI диалогом.

API:
  derive_key(password, salt) -> bytes
  encrypt_bytes(plaintext, key) -> bytes  # nonce+ct
  decrypt_bytes(data, key) -> bytes
  encrypt_file(src, key, delete_original=True) -> Path
  decrypt_file(enc, key, delete_original=True) -> Path
  is_encrypted(path) -> bool
  get_salt_path(vault_root, settings=None) -> Path
  get_or_create_salt(vault_root, settings=None) -> bytes
  load_salt(vault_root, settings=None) -> bytes | None
  encrypt_vault(vault_root, password, settings=None, delete_original=True) -> dict
  decrypt_vault(vault_root, password, settings=None, delete_original=True) -> dict
  is_vault_encrypted(vault_root, settings=None) -> bool
  encrypt / decrypt — алиасы файловых операций
  encrypt_vault / decrypt_vault — команды шифровать/дешифровать vault
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
from pathlib import Path
from typing import Any

# ── Параметры ──────────────────────────────────────────────────────────
SALT_SIZE = 16
NONCE_SIZE = 12
KEY_SIZE = 32  # AES-256
PBKDF2_ITERATIONS = 200_000
ENC_SUFFIX = ".enc"
E2E_SALT_FILE_DEFAULT = ".e2e_salt"
E2E_AAD: bytes = b"FragileNotes-e2e-v1"

# тяжёлые/скрытые папки пропускаем при обходе vault (как в vault.py)
HEAVY_DIRS = {
    "node_modules", ".git", "dist", "build", "target", ".venv", "venv",
    "__pycache__", "bin", "obj", ".cache", ".trash",
    ".obsidian", "Trash", "images", "ao-engine",
}

# ── Криптография: импорт на уровне модуля ─────────────────────────────
try:
    from cryptography.exceptions import InvalidTag as CryptoInvalidTag  # type: ignore
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
except ImportError as _exc:  # pragma: no cover
    AESGCM = None  # type: ignore
    CryptoInvalidTag = Exception  # type: ignore
    _CRYPTO_IMPORT_ERROR = _exc
else:
    _CRYPTO_IMPORT_ERROR = None  # type: ignore

# ── Защита от коллизии nonce ───────────────────────────────────────────
_nonce_lock = threading.Lock()
_used_nonces: set[bytes] = set()
_nonce_counter: int = 0


def _to_bytes(password: str | bytes) -> bytes:
    if isinstance(password, bytes):
        return password
    return password.encode("utf-8")


def derive_key(
    password: str | bytes,
    salt: bytes,
    iterations: int = PBKDF2_ITERATIONS,
) -> bytes:
    """PBKDF2-HMAC-SHA256 -> 32 байта ключа AES-256 (общий для всего vault)."""
    if not isinstance(salt, (bytes, bytearray)):
        raise TypeError("salt must be bytes")
    if len(salt) < 8:
        raise ValueError("salt слишком короткая")
    pw = _to_bytes(password)
    if not pw:
        raise ValueError("пароль не может быть пустым")
    return hashlib.pbkdf2_hmac("sha256", pw, bytes(salt), iterations, dklen=KEY_SIZE)


def _get_aesgcm(key: bytes):
    if AESGCM is None:
        raise ImportError(
            "cryptography не установлена — установите 'pip install cryptography' для шифрования. "
            "Или: pip install cryptography --break-system-packages"
        ) from _CRYPTO_IMPORT_ERROR
    return AESGCM, CryptoInvalidTag


def _generate_nonce() -> bytes:
    global _nonce_counter
    with _nonce_lock:
        for _ in range(8):
            nonce = secrets.token_bytes(NONCE_SIZE)
            if nonce not in _used_nonces:
                _used_nonces.add(nonce)
                if len(_used_nonces) > 10000:
                    _used_nonces.clear()
                    _used_nonces.add(nonce)
                return nonce
        _nonce_counter = (_nonce_counter + 1) & 0xFFFFFFFF
        counter_part = _nonce_counter.to_bytes(4, "big")
        nonce = counter_part + secrets.token_bytes(NONCE_SIZE - 4)
        while nonce in _used_nonces:
            nonce = counter_part + secrets.token_bytes(NONCE_SIZE - 4)
        _used_nonces.add(nonce)
        return nonce


# ── Salt на уровне vault ───────────────────────────────────────────────
def _salt_filename(settings: dict[str, Any] | None) -> str:
    if settings is not None:
        try:
            v = str(settings.get("e2e_salt_file") or E2E_SALT_FILE_DEFAULT).strip()
            if v:
                # только имя файла, без пути/траверса
                v = v.strip("/\\")
                v = Path(v).name or E2E_SALT_FILE_DEFAULT
                return v
        except Exception:
            pass
    return E2E_SALT_FILE_DEFAULT


def get_salt_path(
    vault_root: Path | str,
    settings: dict[str, Any] | None = None,
) -> Path:
    """Путь к файлу глобальной соли: vault/.e2e_salt."""
    return Path(vault_root) / _salt_filename(settings)


def load_salt(
    vault_root: Path | str,
    settings: dict[str, Any] | None = None,
) -> bytes | None:
    """Загрузить глобальную соль если файл существует, иначе None."""
    p = get_salt_path(vault_root, settings)
    if not p.is_file():
        return None
    try:
        data = p.read_bytes()
    except OSError:
        return None
    # файл должен содержать ровно SALT_SIZE байт (поддерживаем также hex/trim)
    if len(data) == SALT_SIZE:
        return data
    # если хранится hex или с переносами — пробуем декодировать
    stripped = data.strip()
    if len(stripped) == SALT_SIZE:
        return bytes(stripped)
    return None


def get_or_create_salt(
    vault_root: Path | str,
    settings: dict[str, Any] | None = None,
) -> bytes:
    """Вернуть существующую соль или создать новую (атомарно, 600)."""
    vault_root = Path(vault_root)
    existing = load_salt(vault_root, settings)
    if existing is not None and len(existing) == SALT_SIZE:
        return existing
    # создать новую соль
    salt = secrets.token_bytes(SALT_SIZE)
    p = get_salt_path(vault_root, settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    _secure_atomic_write(tmp, salt, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return salt


def derive_vault_key(
    password: str | bytes,
    vault_root: Path | str,
    settings: dict[str, Any] | None = None,
) -> tuple[bytes, bytes]:
    """Вывести общий ключ vault: (key, salt). Соль создаётся при отсутствии."""
    salt = get_or_create_salt(vault_root, settings)
    key = derive_key(password, salt)
    return key, salt


def derive_vault_key_strict(
    password: str | bytes,
    vault_root: Path | str,
    settings: dict[str, Any] | None = None,
) -> tuple[bytes, bytes]:
    """Вывести ключ только если соль уже существует (для расшифровки)."""
    salt = load_salt(vault_root, settings)
    if salt is None:
        raise FileNotFoundError(
            f"соль E2E не найдена: {get_salt_path(vault_root, settings)} — vault не зашифрован?"
        )
    key = derive_key(password, salt)
    return key, salt


# ── Низкоуровневые байтовые операции (общий ключ) ──────────────────────
def encrypt_bytes(plaintext: bytes, key: bytes) -> bytes:
    """Зашифровать байты общим ключом: возвращает nonce+ciphertext+tag."""
    if not isinstance(plaintext, (bytes, bytearray)):
        raise TypeError("plaintext must be bytes")
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_SIZE:
        raise ValueError(f"key должен быть {KEY_SIZE} байт")
    nonce = _generate_nonce()
    AESGCM_cls, _ = _get_aesgcm(key)
    aesgcm = AESGCM_cls(bytes(key))
    ct = aesgcm.encrypt(nonce, bytes(plaintext), E2E_AAD)
    return nonce + ct


def decrypt_bytes(data: bytes, key: bytes) -> bytes:
    """Расшифровать байты формата nonce+ct общим ключом."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("data must be bytes")
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_SIZE:
        raise ValueError(f"key должен быть {KEY_SIZE} байт")
    raw = bytes(data)
    min_len = NONCE_SIZE + 16  # tag минимум
    if len(raw) < min_len:
        raise ValueError("данные слишком короткие или повреждены")
    nonce = raw[:NONCE_SIZE]
    ct = raw[NONCE_SIZE:]
    AESGCM_cls, CryptoInvalidTag_cls = _get_aesgcm(key)
    aesgcm = AESGCM_cls(bytes(key))
    try:
        return aesgcm.decrypt(nonce, ct, E2E_AAD)
    except CryptoInvalidTag_cls as exc:
        raise ValueError("неверный пароль или повреждённые данные") from exc
    except Exception as exc:  # pragma: no cover
        msg = str(exc).lower()
        if "tag" in msg or "invalid" in msg:
            try:
                return aesgcm.decrypt(nonce, ct, None)
            except Exception:
                pass
            raise ValueError("неверный пароль или повреждённые данные") from exc
        raise


def is_encrypted(path: Path | str) -> bool:
    """Проверка по расширению .enc (включая .md.enc)."""
    p = Path(path)
    return p.suffix == ENC_SUFFIX or str(p).endswith(ENC_SUFFIX) or p.name.endswith(ENC_SUFFIX)


def _enc_path(src: Path) -> Path:
    s = str(src)
    if s.endswith(ENC_SUFFIX):
        return src
    return Path(s + ENC_SUFFIX)


def _dec_path(enc: Path) -> Path:
    s = str(enc)
    if s.endswith(ENC_SUFFIX):
        return Path(s[: -len(ENC_SUFFIX)])
    return enc


def _secure_atomic_write(tmp: Path, data: bytes, dst: Path) -> None:
    """Атомарная запись с chmod 600, fsync файла и директории."""
    tmp.write_bytes(data)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    try:
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
    except OSError:
        pass
    tmp.replace(dst)
    try:
        dir_fd = os.open(str(dst.parent), os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def encrypt_file(
    src: Path | str,
    key: bytes,
    *,
    delete_original: bool = True,
) -> Path:
    """Зашифровать файл общим ключом E2E.

    Args:
        src: путь к исходному файлу
        key: 32 байта общего ключа vault (derive_key)
        delete_original: удалить исходник после успеха

    Returns:
        Path к созданному .enc файлу
    """
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(f"файл не найден: {src}")
    if is_encrypted(src):
        raise ValueError(f"файл уже зашифрован (.enc): {src}")
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_SIZE:
        raise ValueError(f"key должен быть {KEY_SIZE} байт")
    data = src.read_bytes()
    enc_data = encrypt_bytes(data, bytes(key))
    dst = _enc_path(src)
    tmp = dst.with_name(dst.name + ".tmp")
    _secure_atomic_write(tmp, enc_data, dst)
    try:
        os.chmod(dst, 0o600)
    except OSError:
        pass
    if delete_original:
        try:
            src.unlink()
        except OSError:
            pass
    return dst


def decrypt_file(
    enc_path: Path | str,
    key: bytes,
    *,
    delete_original: bool = True,
) -> Path:
    """Расшифровать .enc файл общим ключом E2E."""
    enc_path = Path(enc_path)
    if not enc_path.is_file():
        raise FileNotFoundError(f"файл не найден: {enc_path}")
    if not is_encrypted(enc_path):
        raise ValueError(f"файл не является зашифрованным (.enc): {enc_path}")
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_SIZE:
        raise ValueError(f"key должен быть {KEY_SIZE} байт")
    enc_data = enc_path.read_bytes()
    plain = decrypt_bytes(enc_data, bytes(key))
    dst = _dec_path(enc_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    _secure_atomic_write(tmp, plain, dst)
    try:
        os.chmod(dst, 0o600)
    except OSError:
        pass
    if delete_original:
        try:
            enc_path.unlink()
        except OSError:
            pass
    return dst


# Алиасы “команды шифровать/дешифровать” (файловый уровень)
def encrypt(src: Path | str, key: bytes, *, delete_original: bool = True) -> Path:
    """Алиас encrypt_file — команда 'шифровать' (общий ключ)."""
    return encrypt_file(src, key, delete_original=delete_original)


def decrypt(enc_path: Path | str, key: bytes, *, delete_original: bool = True) -> Path:
    """Алиас decrypt_file — команда 'дешифровать' (общий ключ)."""
    return decrypt_file(enc_path, key, delete_original=delete_original)


# ── Обход vault ────────────────────────────────────────────────────────
def _should_skip_dir(name: str) -> bool:
    return name.startswith(".") and name not in (".e2e_salt",) or name in HEAVY_DIRS


def _collect_plain_files(vault_root: Path, settings: dict[str, Any] | None = None) -> list[Path]:
    """Собрать все незашифрованные файлы vault для encrypt_vault."""
    salt_name = _salt_filename(settings)
    out: list[Path] = []
    stack = [Path(vault_root)]
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
            # пропускаем соль и уже зашифрованные
            if entry.name == salt_name:
                continue
            if entry.name.endswith(ENC_SUFFIX):
                continue
            if entry.name.startswith(".") or entry.name in HEAVY_DIRS:
                continue
            path = Path(entry.path)
            is_dir = entry.is_dir(follow_symlinks=False)
            if is_dir:
                # не обходим симлинки-папки
                pending_dirs.append(path)
            elif entry.is_file(follow_symlinks=False):
                out.append(path)
        for d in reversed(pending_dirs):
            stack.append(d)
    return out


def _collect_enc_files(vault_root: Path, settings: dict[str, Any] | None = None) -> list[Path]:
    """Собрать все .enc файлы vault для decrypt_vault."""
    out: list[Path] = []
    stack = [Path(vault_root)]
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
            if entry.name in HEAVY_DIRS:
                continue
            # скрытые папки пропускаем, но .enc файлы внутри них? — пропускаем скрытые
            if entry.name.startswith(".") and not entry.name.endswith(ENC_SUFFIX):
                # но если файл .enc скрытый — всё равно пропустим? да, скрытые пропускаем
                # кроме salt (не .enc)
                continue
            path = Path(entry.path)
            is_dir = entry.is_dir(follow_symlinks=False)
            if is_dir:
                pending_dirs.append(path)
            elif entry.is_file(follow_symlinks=False) and entry.name.endswith(ENC_SUFFIX):
                out.append(path)
        for d in reversed(pending_dirs):
            stack.append(d)
    return out


# ── Команды vault ──────────────────────────────────────────────────────
def encrypt_vault(
    vault_root: Path | str,
    password: str | bytes,
    settings: dict[str, Any] | None = None,
    *,
    delete_original: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Зашифровать весь vault общим ключом из пароля.

    Args:
        vault_root: корень vault
        password: пароль (не пустой)
        settings: опционально dict настроек (для имени salt-файла)
        delete_original: удалять исходники после успеха
        dry_run: только подсчитать без шифрования

    Returns:
        dict {ok, encrypted, skipped, errors, salt_path, key_derived}
    """
    vault_root = Path(vault_root)
    if not vault_root.is_dir():
        raise FileNotFoundError(f"vault не найден: {vault_root}")
    pw = _to_bytes(password)
    if not pw:
        raise ValueError("пароль не может быть пустым")

    # проверка импорта заранее
    _get_aesgcm(b"\x00" * KEY_SIZE)

    key, salt = derive_vault_key(password, vault_root, settings)
    salt_path = get_salt_path(vault_root, settings)

    plain_files = _collect_plain_files(vault_root, settings)

    encrypted = 0
    skipped = 0
    errors: list[str] = []

    if dry_run:
        return {
            "ok": True,
            "encrypted": 0,
            "would_encrypt": len(plain_files),
            "skipped": 0,
            "errors": [],
            "salt_path": str(salt_path),
            "key_derived": True,
        }

    for src in plain_files:
        # пропускаем сам salt файл (уже исключён) и пустые? шифруем всё
        try:
            encrypt_file(src, key, delete_original=delete_original)
            encrypted += 1
        except Exception as exc:
            # если уже .enc или ошибка чтения — считаем skipped/error
            if "уже зашифрован" in str(exc):
                skipped += 1
            else:
                errors.append(f"{src}: {exc}")

    return {
        "ok": len(errors) == 0,
        "encrypted": encrypted,
        "skipped": skipped,
        "errors": errors,
        "salt_path": str(salt_path),
        "key_derived": True,
    }


def decrypt_vault(
    vault_root: Path | str,
    password: str | bytes,
    settings: dict[str, Any] | None = None,
    *,
    delete_original: bool = True,
    dry_run: bool = False,
    remove_salt: bool = True,
) -> dict[str, Any]:
    """Расшифровать весь vault общим ключом из пароля.

    Args:
        vault_root: корень vault
        password: пароль
        settings: опционально dict настроек
        delete_original: удалять .enc после успеха
        dry_run: только подсчитать
        remove_salt: удалить .e2e_salt после успешной расшифровки всего vault

    Returns:
        dict {ok, decrypted, skipped, errors, salt_path}
    """
    vault_root = Path(vault_root)
    if not vault_root.is_dir():
        raise FileNotFoundError(f"vault не найден: {vault_root}")
    pw = _to_bytes(password)
    if not pw:
        raise ValueError("пароль не может быть пустым")

    _get_aesgcm(b"\x00" * KEY_SIZE)

    salt = load_salt(vault_root, settings)
    if salt is None:
        raise FileNotFoundError(
            f"соль E2E не найдена: {get_salt_path(vault_root, settings)} — vault не зашифрован?"
        )
    key = derive_key(pw, salt)
    salt_path = get_salt_path(vault_root, settings)

    enc_files = _collect_enc_files(vault_root, settings)

    if dry_run:
        return {
            "ok": True,
            "decrypted": 0,
            "would_decrypt": len(enc_files),
            "skipped": 0,
            "errors": [],
            "salt_path": str(salt_path),
        }

    decrypted = 0
    skipped = 0
    errors: list[str] = []

    for enc in enc_files:
        try:
            decrypt_file(enc, key, delete_original=delete_original)
            decrypted += 1
        except ValueError as exc:
            # неверный пароль/повреждённые данные — критично
            errors.append(f"{enc}: {exc}")
        except Exception as exc:
            errors.append(f"{enc}: {exc}")

    # если всё расшифровано без ошибок и remove_salt — удаляем соль
    if remove_salt and not errors:
        # проверяем остались ли .enc файлы
        remaining = _collect_enc_files(vault_root, settings)
        if not remaining:
            try:
                salt_path.unlink()
            except OSError:
                pass

    return {
        "ok": len(errors) == 0,
        "decrypted": decrypted,
        "skipped": skipped,
        "errors": errors,
        "salt_path": str(salt_path),
    }


def is_vault_encrypted(
    vault_root: Path | str,
    settings: dict[str, Any] | None = None,
) -> bool:
    """Проверяет, зашифрован ли vault (есть соль и хотя бы один .enc)."""
    vault_root = Path(vault_root)
    if not vault_root.is_dir():
        return False
    salt = load_salt(vault_root, settings)
    if salt is None:
        return False
    enc_files = _collect_enc_files(vault_root, settings)
    return len(enc_files) > 0


# ── Интеграция с settings (helpers) ───────────────────────────────────
def is_e2e_enabled(settings: dict[str, Any]) -> bool:
    """Проверка флага e2e_enabled в settings."""
    try:
        return bool(settings.get("e2e_enabled", False))
    except Exception:
        return False


def get_e2e_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Вернуть E2E-подмножество настроек (для UI)."""
    return {
        "e2e_enabled": bool(settings.get("e2e_enabled", False)),
        "e2e_salt_file": str(settings.get("e2e_salt_file", E2E_SALT_FILE_DEFAULT)),
    }


__all__ = [
    "SALT_SIZE",
    "NONCE_SIZE",
    "KEY_SIZE",
    "PBKDF2_ITERATIONS",
    "ENC_SUFFIX",
    "E2E_SALT_FILE_DEFAULT",
    "E2E_AAD",
    "derive_key",
    "derive_vault_key",
    "derive_vault_key_strict",
    "get_salt_path",
    "load_salt",
    "get_or_create_salt",
    "encrypt_bytes",
    "decrypt_bytes",
    "is_encrypted",
    "encrypt_file",
    "decrypt_file",
    "encrypt",
    "decrypt",
    "encrypt_vault",
    "decrypt_vault",
    "is_vault_encrypted",
    "is_e2e_enabled",
    "get_e2e_settings",
]
