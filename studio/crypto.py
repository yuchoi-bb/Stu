"""자격증명 암호화 (§5): Fernet, 키는 서버 로컬 600 권한 파일."""
import os

from cryptography.fernet import Fernet

from . import config

_fernet = None


def _load() -> Fernet:
    global _fernet
    if _fernet is None:
        path = config.FERNET_KEY_PATH
        if not os.path.exists(path):
            key = Fernet.generate_key()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(key)
        else:
            if os.stat(path).st_mode & 0o077:
                raise PermissionError(f"{path} must be mode 600")
            with open(path, "rb") as f:
                key = f.read()
        _fernet = Fernet(key)
    return _fernet


def ensure_key() -> None:
    """앱 기동 시 호출 — 키 생성/권한(600) 검증을 지연 없이 수행."""
    _load()


def encrypt(plain: str) -> bytes:
    return _load().encrypt(plain.encode())


def decrypt(token: bytes) -> str:
    return _load().decrypt(bytes(token)).decode()
