"""studio 종결 시 진행 중 회차·CI 취소 테스트 (§6.2 — 고아 CI 방지).

실행: python3 tests/test_abandon.py
"""
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="studio-ab-")
os.environ["STUDIO_DB"] = os.path.join(TMP, "studio.db")
os.environ["STUDIO_FERNET_KEY"] = os.path.join(TMP, ".fernet.key")
os.environ["STUDIO_ATTACH_DIR"] = os.path.join(TMP, "attach")
os.environ["STUDIO_LOG_DIR"] = os.path.join(TMP, "logs")
os.environ["STUDIO_CI_WEBHOOK_SECRET"] = "s"

from studio.app import app          # noqa: E402
from studio import db, ghe, jobs    # noqa: E402

jobs.submit = lambda fn, *a, **k: fn(*a, **k)
client = app.test_client()
H = {"X-Remote-User": "hong"}


def _studio_with_running(sid_val, attempt=1, run_id=555):
    db.execute("INSERT OR IGNORE INTO users (user_id) VALUES ('hong')")
    db.execute("INSERT INTO sessions (session_id, user_id, title) VALUES (?, 'hong','t')",
               (sid_val,))
    st = f"ST-{sid_val}"
    db.execute("INSERT INTO studios (studio_id, session_id, user_id, repo, "
               "branch_name, requirements, status) VALUES (?,?,?,?,?,?, 'open')",
               (st, sid_val, "hong", "thr", "feature/x", "R"))
    db.execute("INSERT INTO builds (studio_id, attempt, status, run_id) "
               "VALUES (?,?, 'ci_running', ?)", (st, attempt, run_id))
    return st


# ---------- close(abandoned)가 진행 회차 + stage CI 취소 ----------
st1 = _studio_with_running("S1", run_id=777)
called = {}
with mock.patch.object(ghe, "cancel_run",
                       lambda u, repo, rid: called.setdefault("rid", rid)):
    r = client.post(f"/api/studio/studios/{st1}/close", headers=H,
                    json={"status": "abandoned"}).get_json()
assert r["cancelled_builds"] == 1, r
assert called.get("rid") == 777, called          # stage 취소 시그널 전송
b = db.one("SELECT status FROM builds WHERE studio_id=?", (st1,))
assert b["status"] == "cancelled", b["status"]
s = db.one("SELECT status FROM studios WHERE studio_id=?", (st1,))
assert s["status"] == "abandoned"
print("OK: close(abandoned) → 진행 회차 cancelled + stage 취소 시그널")

# ---------- done(채택)도 진행 회차 취소 ----------
st2 = _studio_with_running("S2", run_id=888)
with mock.patch.object(ghe, "cancel_run", lambda *a, **k: None):
    r = client.post(f"/api/studio/studios/{st2}/close", headers=H,
                    json={"status": "done"}).get_json()
assert r["cancelled_builds"] == 1
assert db.one("SELECT status FROM builds WHERE studio_id=?", (st2,))["status"] == "cancelled"
print("OK: done(채택)도 진행 회차 취소 (runner 낭비 방지)")

# ---------- CI 콜백이 취소된(죽은) 회차에 적용되지 않음 (멱등) ----------
ok = ghe.apply_ci_result(st1, 1, "pass", "b1", None)
assert ok is True   # 멱등 True지만
assert db.one("SELECT status FROM builds WHERE studio_id=?", (st1,))["status"] == "cancelled"
print("OK: 종결 후 도착한 CI 결과는 죽은 회차에 미적용 (멱등)")

print("\nALL ABANDON TESTS PASSED")
