"""장시간 작업 모델 (§4.3): ThreadPoolExecutor(8) + DB 상태 기록 + 취소 체크포인트."""
from concurrent.futures import ThreadPoolExecutor

from . import config, db, logs

log = logs.get("jobs")

_executor = ThreadPoolExecutor(max_workers=config.EXECUTOR_WORKERS,
                               thread_name_prefix="studio-job")


class Cancelled(Exception):
    pass


def submit(fn, *args, **kwargs) -> None:
    _executor.submit(_run_safely, fn, *args, **kwargs)


def _run_safely(fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Cancelled:
        pass
    except Exception:
        log.exception("studio job failed: %s", getattr(fn, "__name__", fn))


def set_build_status(build_id: int, status: str, *, fail_summary: str | None = None,
                     completed: bool = False) -> None:
    db.execute(
        "UPDATE builds SET status=?, "
        "fail_summary=COALESCE(?, fail_summary), "
        "completed_at=CASE WHEN ? THEN datetime('now') ELSE completed_at END "
        "WHERE build_id=?",
        (status, fail_summary, 1 if completed else 0, build_id))


def checkpoint(build_id: int) -> None:
    """작업 스레드가 단계마다 호출 — 취소 플래그는 DB 경유 (§4.3)."""
    row = db.one("SELECT cancel_requested FROM builds WHERE build_id=?", (build_id,))
    if row and row["cancel_requested"]:
        set_build_status(build_id, "cancelled", completed=True)
        raise Cancelled(build_id)


def request_cancel(build_id: int) -> None:
    db.execute("UPDATE builds SET cancel_requested=1 WHERE build_id=?", (build_id,))


def active_count_for_user(user_id: str) -> int:
    """사용자당 동시 진행 1건 제한 (§4.2)."""
    row = db.one(
        """SELECT COUNT(*) AS n FROM builds b
           JOIN studios s ON s.studio_id = b.studio_id
           WHERE s.user_id=? AND b.status IN
                 ('generating','awaiting_review','pushing','pushed','ci_running')""",
        (user_id,))
    return row["n"] if row else 0


def mark_interrupted_builds_failed() -> None:
    """서버 재시작 시 진행 중이던 작업 정리 (§4.3: failed(restart))."""
    db.execute(
        "UPDATE builds SET status='fail', "
        "fail_summary='server restart — 재시도 필요', "
        "completed_at=datetime('now') "
        "WHERE status IN ('generating','pushing')")
