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


def init_db() -> None:
    with open(_SCHEMA, encoding="utf-8") as f:
        get_conn().executescript(f.read())
    get_conn().commit()


def query(sql: str, args: tuple = ()) -> list:
    return get_conn().execute(sql, args).fetchall()


def one(sql: str, args: tuple = ()):
    return get_conn().execute(sql, args).fetchone()


def execute(sql: str, args: tuple = ()) -> int:
    conn = get_conn()
    cur = conn.execute(sql, args)
    conn.commit()
    return cur.lastrowid
