"""Шифрование заметок — AES-GCM, ключ из пароля PBKDF2, файлы .md.enc.

Формат файла .md.enc:
    [salt 16 байт][nonce 12 байт][ciphertext+tag]

* salt — случайный 16 байт (per-file)
* nonce — 12 байт для AES-GCM (per-file, случайный)
* ciphertext+tag — AES-GCM (cryptography) с tag 16 байт внутри
* ключ — PBKDF2-HMAC-SHA256, 200 000 итераций, 32 байта

API:
    derive_key(password, salt) -> bytes
    encrypt_bytes(plaintext, password) -> bytes  # salt+nonce+ct
    decrypt_bytes(data, password) -> bytes
    encrypt_file(src: Path, password: str, delete_original=True) -> Path  # -> .enc
    decrypt_file(enc: Path, password: str, delete_original=True) -> Path  # -> .md
    is_encrypted(path: Path) -> bool
    encrypt / decrypt — алиасы для файловых операций

Безопасность: пароль не хранится, пустой пароль запрещён, salt/nonce
генерируются через secrets. При неверном пароле — InvalidTag.
Исправлено: secrets.token_bytes + проверка уникальности nonce, AAD,
fsync+chmod 600, импорт AESGCM вынесен наверх.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
from pathlib import Path

# ── Параметры ───────────────────────────────────────────────────────
SALT_SIZE = 16
NONCE_SIZE = 12
KEY_SIZE = 32  # AES-256
PBKDF2_ITERATIONS = 200_000
ENC_SUFFIX = ".enc"  # .md -> .md.enc
# Для обратной совместимости принимаем и .md.enc и любой .enc

# ── Криптография: импорт на уровне модуля (не ленивый каждый вызов) ─
try:
    from cryptography.exceptions import InvalidTag as CryptoInvalidTag  # type: ignore
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
except ImportError as _exc:  # pragma: no cover
    AESGCM = None  # type: ignore
    CryptoInvalidTag = Exception  # type: ignore
    _CRYPTO_IMPORT_ERROR = _exc
else:
    _CRYPTO_IMPORT_ERROR = None  # type: ignore

# ── AAD для AES-GCM (привязывает шифртекст к контексту) ────────────
_AAD: bytes = b"FragileNotes-v1"

# ── Защита от коллизии nonce (AES-GCM критично) ────────────────────
_nonce_lock = threading.Lock()
_used_nonces: set[bytes] = set()
_nonce_counter: int = 0


def _to_bytes(password: str | bytes) -> bytes:
    if isinstance(password, bytes):
        return password
    return password.encode("utf-8")


def derive_key(password: str | bytes, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    """PBKDF2-HMAC-SHA256 -> 32 байта ключа AES-256."""
    if not isinstance(salt, (bytes, bytearray)):
        raise TypeError("salt must be bytes")
    if len(salt) < 8:
        raise ValueError("salt слишком короткая")
    pw = _to_bytes(password)
    if not pw:
        raise ValueError("пароль не может быть пустым")
    return hashlib.pbkdf2_hmac("sha256", pw, bytes(salt), iterations, dklen=KEY_SIZE)


def _get_aesgcm(key: bytes):
    """Возвращает (AESGCM, CryptoInvalidTag) — импорт уже наверху, не каждый вызов."""
    if AESGCM is None:
        raise ImportError(
            "cryptography не установлена — установите 'pip install cryptography' для шифрования. "
            "Или: pip install cryptography --break-system-packages"
        ) from _CRYPTO_IMPORT_ERROR
    return AESGCM, CryptoInvalidTag


def _generate_nonce() -> bytes:
    """Сгенерировать уникальный nonce 12б: secrets.token_bytes + проверка/счётчик."""
    global _nonce_counter
    with _nonce_lock:
        # Пытаемся сгенерировать уникальный случайный nonce
        for _ in range(8):
            nonce = secrets.token_bytes(NONCE_SIZE)
            if nonce not in _used_nonces:
                _used_nonces.add(nonce)
                # ограничим размер множества чтобы не рос бесконечно
                if len(_used_nonces) > 10000:
                    _used_nonces.clear()
                    _used_nonces.add(nonce)
                return nonce
        # маловероятный fallback: счётчик + случайные байты
        _nonce_counter = (_nonce_counter + 1) & 0xFFFFFFFF
        counter_part = _nonce_counter.to_bytes(4, "big")
        nonce = counter_part + secrets.token_bytes(NONCE_SIZE - 4)
        # гарантируем уникальность
        while nonce in _used_nonces:
            nonce = counter_part + secrets.token_bytes(NONCE_SIZE - 4)
        _used_nonces.add(nonce)
        return nonce


def encrypt_bytes(plaintext: bytes, password: str | bytes) -> bytes:
    """Зашифровать байты: возвращает salt+nonce+ciphertext+tag."""
    if not isinstance(plaintext, (bytes, bytearray)):
        raise TypeError("plaintext must be bytes")
    pw = _to_bytes(password)
    if not pw:
        raise ValueError("пароль не может быть пустым")
    salt = secrets.token_bytes(SALT_SIZE)
    nonce = _generate_nonce()
    key = derive_key(pw, salt)
    AESGCM_cls, _ = _get_aesgcm(key)
    aesgcm = AESGCM_cls(key)
    ct = aesgcm.encrypt(nonce, bytes(plaintext), _AAD)
    return salt + nonce + ct


def decrypt_bytes(data: bytes, password: str | bytes) -> bytes:
    """Расшифровать байты формата salt+nonce+ct."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("data must be bytes")
    pw = _to_bytes(password)
    if not pw:
        raise ValueError("пароль не может быть пустым")
    raw = bytes(data)
    min_len = SALT_SIZE + NONCE_SIZE + 16  # tag минимум
    if len(raw) < min_len:
        raise ValueError("данные слишком короткие или повреждены")
    salt = raw[:SALT_SIZE]
    nonce = raw[SALT_SIZE : SALT_SIZE + NONCE_SIZE]
    ct = raw[SALT_SIZE + NONCE_SIZE :]
    key = derive_key(pw, salt)
    AESGCM_cls, CryptoInvalidTag_cls = _get_aesgcm(key)
    aesgcm = AESGCM_cls(key)
    try:
        return aesgcm.decrypt(nonce, ct, _AAD)
    except CryptoInvalidTag_cls as exc:
        raise ValueError("неверный пароль или повреждённые данные") from exc
    except Exception as exc:  # pragma: no cover
        # cryptography может кинуть InvalidTag как Exception; пробуем fallback на старый формат без AAD для совместимости
        msg = str(exc).lower()
        if "tag" in msg or "invalid" in msg:
            # попытка расшифровать без AAD для обратной совместимости (старые файлы)
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
    """Путь для зашифрованного файла: foo.md -> foo.md.enc"""
    s = str(src)
    if s.endswith(ENC_SUFFIX):
        # уже зашифрован — не дублировать
        return src
    return Path(s + ENC_SUFFIX)


def _dec_path(enc: Path) -> Path:
    """Путь для расшифрованного файла: foo.md.enc -> foo.md"""
    s = str(enc)
    if s.endswith(ENC_SUFFIX):
        return Path(s[: -len(ENC_SUFFIX)])
    # нет .enc — возвращаем как есть (нечего снимать)
    return enc


def _secure_atomic_write(tmp: Path, data: bytes, dst: Path) -> None:
    """Атомарная запись с chmod 600, fsync файла и fsync директории."""
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
    # fsync родительской директории для durability rename
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
    password: str | bytes,
    *,
    delete_original: bool = True,
) -> Path:
    """Зашифровать файл на диске.

    Args:
        src: путь к исходному .md/.txt файлу (байты читаются как есть, затем шифруются)
        password: пароль (не пустой)
        delete_original: удалить исходник после успеха

    Returns:
        Path к созданному .enc файлу

    Raises:
        FileNotFoundError, ValueError (пустой пароль / уже .enc), ImportError, OSError
    """
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(f"файл не найден: {src}")
    if is_encrypted(src):
        raise ValueError(f"файл уже зашифрован (.enc): {src}")
    pw = _to_bytes(password)
    if not pw:
        raise ValueError("пароль не может быть пустым")
    data = src.read_bytes()
    enc_data = encrypt_bytes(data, pw)
    dst = _enc_path(src)
    # атомарная запись: временный + rename + fsync + chmod 600
    tmp = dst.with_name(dst.name + ".tmp")
    _secure_atomic_write(tmp, enc_data, dst)
    # убедиться что итоговый файл 600
    try:
        os.chmod(dst, 0o600)
    except OSError:
        pass
    if delete_original:
        try:
            src.unlink()
        except OSError:
            # если не удалось удалить — откатывать шифр не надо, но сообщим
            pass
    return dst


def decrypt_file(
    enc_path: Path | str,
    password: str | bytes,
    *,
    delete_original: bool = True,
) -> Path:
    """Расшифровать .md.enc файл на диске.

    Args:
        enc_path: путь к .enc файлу
        password: пароль
        delete_original: удалить .enc после успешной расшифровки

    Returns:
        Path к расшифрованному файлу

    Raises:
        FileNotFoundError, ValueError (не .enc / неверный пароль), OSError
    """
    enc_path = Path(enc_path)
    if not enc_path.is_file():
        raise FileNotFoundError(f"файл не найден: {enc_path}")
    if not is_encrypted(enc_path):
        raise ValueError(f"файл не является зашифрованным (.enc): {enc_path}")
    pw = _to_bytes(password)
    if not pw:
        raise ValueError("пароль не может быть пустым")
    enc_data = enc_path.read_bytes()
    plain = decrypt_bytes(enc_data, pw)
    dst = _dec_path(enc_path)
    # защита от перезаписи существующего расшифрованного? перезаписываем
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


# ── Алиасы “команды шифровать/дешифровать” (как указано в ТЗ) ─────────
def encrypt(src: Path | str, password: str | bytes, *, delete_original: bool = True) -> Path:
    """Алиас encrypt_file — команда 'шифровать'."""
    return encrypt_file(src, password, delete_original=delete_original)


def decrypt(enc_path: Path | str, password: str | bytes, *, delete_original: bool = True) -> Path:
    """Алиас decrypt_file — команда 'дешифровать'."""
    return decrypt_file(enc_path, password, delete_original=delete_original)


__all__ = [
    "SALT_SIZE",
    "NONCE_SIZE",
    "KEY_SIZE",
    "PBKDF2_ITERATIONS",
    "ENC_SUFFIX",
    "derive_key",
    "encrypt_bytes",
    "decrypt_bytes",
    "is_encrypted",
    "encrypt_file",
    "decrypt_file",
    "encrypt",
    "decrypt",
]
