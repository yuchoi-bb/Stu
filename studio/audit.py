"""행위 로그 (§3.1.1) — 누가·무엇을·대상·결과를 기록.

4개 서비스 공통 규약 `(user_id, service, action, target, result, timestamp)`을
따르며 service는 항상 'studio'. 추후 CICD/Release/SignTool과 통합 조회 가능.
"""
from . import config, db, logs

_log = logs.get("audit")


def record(user_id: str | None, action: str, target: str | None = None,
           result: str | None = None, detail: str | None = None) -> None:
    """공통 규약 (user_id, service, action, target, result, detail, timestamp).
    service는 config.SERVICE_NAME — 다른 서비스가 이 규약을 재사용해 통합 조회 가능."""
    try:
        db.execute(
            "INSERT INTO action_log (user_id, service, action, target, result, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, config.SERVICE_NAME, action, target, result, detail))
    except Exception:
        _log.exception("audit record failed: %s %s", action, target)


def recent(limit: int = 200, user_id: str | None = None,
           action: str | None = None) -> list[dict]:
    clauses, args = [], []
    if user_id:
        clauses.append("user_id=?"); args.append(user_id)
    if action:
        clauses.append("action=?"); args.append(action)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    args.append(limit)
    rows = db.query(f"SELECT * FROM action_log{where} ORDER BY id DESC LIMIT ?",
                    tuple(args))
    return [dict(r) for r in rows]
