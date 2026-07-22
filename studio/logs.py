"""로깅 설정 (§11) — 파일 로깅 + 콘솔. 운영 디버깅 가시성.

모든 모듈은 `logging.getLogger("studio.<name>")`을 쓰고, 여기서 핸들러를 붙인다.
백그라운드 스레드/ThreadPool의 예외도 파일에 남아 사후 추적이 가능하다.
"""
import datetime as dt
import logging
import logging.handlers
import os
import re
import threading

from . import config

_configured = False
_slog_lock = threading.Lock()


def studio_log_dir() -> str:
    return os.path.join(config.LOG_DIR, "studios")


def studio_log_path(studio_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(studio_id))[:80]
    return os.path.join(studio_log_dir(), safe + ".log")


def slog(studio_id: str | None, msg: str, *args) -> None:
    """studio_id별 디버그 로그 파일에 한 줄 append (logs/studios/{studio_id}.log).

    하나의 studio가 겪는 전 과정(생성/스캔/push/CI/리뷰/PR)을 한 파일에 모아
    사후 추적을 쉽게 한다. 파일 핸들러를 열어두지 않아 fd 누수가 없다.
    """
    if not studio_id:
        return
    try:
        os.makedirs(studio_log_dir(), exist_ok=True)
        line = (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " "
                + (msg % args if args else msg))
        with _slog_lock, open(studio_log_path(studio_id), "a",
                              encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        get("logs").warning("studio 로그 기록 실패: %s", studio_id)


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
