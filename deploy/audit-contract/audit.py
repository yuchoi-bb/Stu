"""ToolHub 공통 행위 로그 — Python 참조 구현 (studio 외 Python 서비스용).

studio 앱에 의존하지 않는 독립 모듈. 공통 규약(docs/toolhub-audit-contract.md)을
그대로 따른다. 서비스는 SERVICE_NAME만 자기 것으로 바꾸면 된다.

사용:
    import audit
    audit.SERVICE_NAME = "release"          # 또는 환경변수 TOOLHUB_SERVICE
    audit.log_login(user_id)                # SSO 접근 시(세션 스로틀 내장)
    audit.record(user_id, "deploy", target="pkg@1.2.3", result="ok")
    for r in audit.recent(action="login"):  ...
"""
import os
import sqlite3
import time

DB_PATH = os.environ.get("TOOLHUB_AUDIT_DB", "/opt/toolhub/data/action_log.db")
SERVICE_NAME = os.environ.get("TOOLHUB_SERVICE", "release")
LOGIN_THROTTLE_SEC = int(os.environ.get("TOOLHUB_LOGIN_THROTTLE_MIN", "30")) * 60

_last_login: dict[str, float] = {}


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS action_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, service TEXT NOT NULL,
        action TEXT NOT NULL, target TEXT, result TEXT, detail TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    return c


def record(user_id, action, target=None, result=None, detail=None):
    """행위 1건 기록. 실패해도 서비스 흐름을 막지 않는다."""
    try:
        c = _conn()
        c.execute("INSERT INTO action_log (user_id, service, action, target, result, "
                  "detail) VALUES (?,?,?,?,?,?)",
                  (user_id, SERVICE_NAME, action, target, result, detail))
        c.commit(); c.close()
    except Exception:
        pass


def log_login(user_id):
    """SSO 접근 시 호출. 세션 창(기본 30분) 내 재호출은 기록하지 않는다."""
    now = time.monotonic()
    last = _last_login.get(user_id)
    if last is not None and now - last < LOGIN_THROTTLE_SEC:
        return
    _last_login[user_id] = now
    record(user_id, "login", result="resume" if last is not None else "new")


def recent(limit=200, user_id=None, action=None):
    clauses, args = [], []
    if user_id:
        clauses.append("user_id=?"); args.append(user_id)
    if action:
        clauses.append("action=?"); args.append(action)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    args.append(limit)
    c = _conn()
    c.row_factory = sqlite3.Row
    rows = c.execute(f"SELECT * FROM action_log{where} ORDER BY id DESC LIMIT ?",
                     tuple(args)).fetchall()
    c.close()
    return [dict(r) for r in rows]
