"""로깅 설정 (§11) — 파일 로깅 + 콘솔. 운영 디버깅 가시성.

모든 모듈은 `logging.getLogger("studio.<name>")`을 쓰고, 여기서 핸들러를 붙인다.
백그라운드 스레드/ThreadPool의 예외도 파일에 남아 사후 추적이 가능하다.
"""
import logging
import logging.handlers
import os

from . import config

_configured = False


def setup() -> None:
    global _configured
    if _configured:
        return
    os.makedirs(config.LOG_DIR, exist_ok=True)
    root = logging.getLogger("studio")
    root.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s")

    fileh = logging.handlers.RotatingFileHandler(
        os.path.join(config.LOG_DIR, "studio.log"),
        maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fileh.setFormatter(fmt)
    root.addHandler(fileh)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    root.propagate = False
    _configured = True


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"studio.{name}")
