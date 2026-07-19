"""행위 로그 (§3.1.1) — 누가·무엇을·대상·결과를 기록.

4개 서비스 공통 규약 `(user_id, service, action, target, result, timestamp)`을
따르며 service는 항상 'studio'. 추후 CICD/Release/SignTool과 통합 조회 가능.
"""
from . import db, logs

_log = logs.get("audit")


def record(user_id: str | None, action: str, target: str | None = None,
           result: str | None = None, detail: str | None = None) -> None:
    try:
        db.execute(
            "INSERT INTO action_log (user_id, service, action, target, result, detail) "
            "VALUES (?, 'studio', ?, ?, ?, ?)",
            (user_id, action, target, result, detail))
    except Exception:
        _log.exception("audit record failed: %s %s", action, target)


def recent(limit: int = 200, user_id: str | None = None) -> list[dict]:
    if user_id:
        rows = db.query(
            "SELECT * FROM action_log WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit))
    else:
        rows = db.query("SELECT * FROM action_log ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]
