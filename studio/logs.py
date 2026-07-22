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
_name_cache: dict[str, str] = {}   # studio_id -> 로그 파일명(S%y%m%d-%H%M%S)


def studio_log_dir() -> str:
    return os.path.join(config.LOG_DIR, "studio")


def _log_name_for(studio_id: str, create: bool = False) -> str | None:
    """studio 생성 시각 기준 파일명 S%y%m%d-%H%M%S. created_at을 쓰므로 재기동·
    반복 호출에도 같은 파일로 안정(캐시로 고정).

    create=False(읽기): DB에 없고 캐시에도 없으면 None(=읽을 로그 없음).
    create=True(쓰기): created_at 없으면 현재 시각 폴백으로 이름을 만든다.
    """
    cached = _name_cache.get(studio_id)
    if cached:
        return cached
    when = None
    try:
        from . import db
        row = db.one("SELECT created_at FROM studios WHERE studio_id=?", (studio_id,))
        if row and row["created_at"]:
            when = dt.datetime.fromisoformat(row["created_at"])
    except Exception:
        when = None
    if when is None and not create:
        return None
    # RUNID = studio_id 고유부(ST- 뒤). 유니크성 보장 → 같은 초 충돌 방지.
    runid = studio_id.split("-", 1)[-1] if "-" in studio_id else studio_id
    runid = re.sub(r"[^A-Za-z0-9]", "", runid)[:24] or "x"
    ts = (when or dt.datetime.now()).strftime("%y%m%d-%H%M%S")
    name = f"S-{ts}-{runid}"
    _name_cache[studio_id] = name
    return name


def studio_log_path(studio_id: str, create: bool = False) -> str | None:
    name = _log_name_for(studio_id, create=create)
    return os.path.join(studio_log_dir(), name + ".log") if name else None


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
        with _slog_lock, open(studio_log_path(studio_id, create=True), "a",
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
