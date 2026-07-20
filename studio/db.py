"""SQLite 연결/초기화 (§5). WAL + busy_timeout, 스레드별 연결."""
import os
import sqlite3
import threading

from . import config

_local = threading.local()
_SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


# 스키마 드리프트 방지 마이그레이션 (§5): CREATE TABLE IF NOT EXISTS는 기존 DB에
# 새 컬럼을 추가하지 않으므로, 개발 중 추가된 컬럼을 idempotent ALTER로 보정한다.
# (table, column, DDL) — 이미 있으면 조용히 건너뛴다.
_MIGRATIONS = [
    ("users", "auto_approve",
     "ALTER TABLE users ADD COLUMN auto_approve INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "summary", "ALTER TABLE sessions ADD COLUMN summary TEXT"),
    ("studios", "requirements", "ALTER TABLE studios ADD COLUMN requirements TEXT"),
    ("builds", "cancel_requested",
     "ALTER TABLE builds ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"),
    ("builds", "fail_summary", "ALTER TABLE builds ADD COLUMN fail_summary TEXT"),
    ("build_files", "base_blob_sha", "ALTER TABLE build_files ADD COLUMN base_blob_sha TEXT"),
    ("build_files", "pushed_blob_sha", "ALTER TABLE build_files ADD COLUMN pushed_blob_sha TEXT"),
    ("studios", "pr_number", "ALTER TABLE studios ADD COLUMN pr_number INTEGER"),
    ("studios", "pr_url", "ALTER TABLE studios ADD COLUMN pr_url TEXT"),
    ("builds", "scan_findings", "ALTER TABLE builds ADD COLUMN scan_findings TEXT"),
    ("build_files", "base_content",
     "ALTER TABLE build_files ADD COLUMN base_content TEXT"),
]


def _columns(conn, table: str) -> set:
    try:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return set()


def init_db() -> None:
    conn = get_conn()
    with open(_SCHEMA, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    # 신규 테이블은 위 스크립트가 생성. 기존 테이블의 누락 컬럼만 ALTER로 보정.
    for table, column, ddl in _MIGRATIONS:
        cols = _columns(conn, table)
        if cols and column not in cols:
            conn.execute(ddl)
    conn.commit()


def query(sql: str, args: tuple = ()) -> list:
    return get_conn().execute(sql, args).fetchall()


def one(sql: str, args: tuple = ()):
    return get_conn().execute(sql, args).fetchone()


def execute(sql: str, args: tuple = ()) -> int:
    conn = get_conn()
    cur = conn.execute(sql, args)
    conn.commit()
    return cur.lastrowid
