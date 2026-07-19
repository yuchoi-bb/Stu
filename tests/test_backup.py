"""백업 테스트 (§12.4): DB 스냅샷 + 첨부 tar + 보존 정리.

실행: python3 tests/test_backup.py
"""
import os
import sqlite3
import tarfile
import tempfile
import time

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="studio-bak-")
os.environ["STUDIO_DB"] = os.path.join(TMP, "studio.db")
os.environ["STUDIO_FERNET_KEY"] = os.path.join(TMP, ".fernet.key")
os.environ["STUDIO_ATTACH_DIR"] = os.path.join(TMP, "attach")
os.environ["STUDIO_LOG_DIR"] = os.path.join(TMP, "logs")
os.environ["STUDIO_BACKUP_DIR"] = os.path.join(TMP, "backups")

from studio import config, db, backup   # noqa: E402

# DB + 첨부 준비
db.init_db()
db.execute("INSERT OR IGNORE INTO users (user_id) VALUES ('hong')")
os.makedirs(config.ATTACH_DIR, exist_ok=True)
with open(os.path.join(config.ATTACH_DIR, "a.txt"), "w") as f:
    f.write("hello")

# ---------- 백업 수행 ----------
r = backup.run_backup()
assert r["db"] and os.path.isfile(r["db"]), r
assert r["attachments"] and os.path.isfile(r["attachments"]), r
print("OK: 백업 생성 — db 스냅샷 + 첨부 tar.gz")

# DB 스냅샷이 유효한 SQLite이고 데이터 보존
c = sqlite3.connect(r["db"])
n = c.execute("SELECT COUNT(*) FROM users WHERE user_id='hong'").fetchone()[0]
c.close()
assert n == 1
print("OK: 스냅샷 무결성 — users 데이터 보존")

# 첨부 tar 내용 확인
with tarfile.open(r["attachments"]) as t:
    names = t.getnames()
assert any(x.endswith("a.txt") for x in names), names
print("OK: 첨부 tar 내용 확인")

# ---------- 보존 정리: 31일 지난 백업 삭제 ----------
old = os.path.join(config.BACKUP_DIR, "studio-20000101-000000.db")
with open(old, "w") as f:
    f.write("x")
old_time = time.time() - 31 * 86400
os.utime(old, (old_time, old_time))
r2 = backup.run_backup(retention_days=30)
assert not os.path.isfile(old), "31일 지난 백업이 정리되지 않음"
assert r2["pruned"] >= 1
print("OK: 보존 정리 — 30일 초과 백업 삭제")

# 최근 백업은 보존
assert os.path.isfile(r2["db"])
print("OK: 최근 백업은 유지")

# ---------- DB 없을 때 안전 동작 ----------
os.remove(config.DB_PATH)
r3 = backup.run_backup()
assert r3["db"] is None
print("OK: DB 부재 시에도 예외 없이 동작")

print("\nALL BACKUP TESTS PASSED")
