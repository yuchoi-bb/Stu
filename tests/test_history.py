"""로그인 + PUSH 감사 이력 테스트 (§3.1.1).

실행: python3 tests/test_history.py
"""
import os
import sys
import tempfile
import time
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="studio-hist-")
os.environ["STUDIO_DB"] = os.path.join(TMP, "studio.db")
os.environ["STUDIO_FERNET_KEY"] = os.path.join(TMP, ".fernet.key")
os.environ["STUDIO_ATTACH_DIR"] = os.path.join(TMP, "attach")
os.environ["STUDIO_LOG_DIR"] = os.path.join(TMP, "logs")

import studio.app as appmod           # noqa: E402
from studio import db, audit, ghe     # noqa: E402

client = appmod.app.test_client()

# ---------- 로그인 이력 ----------
# 최초 접근 → login(new)
client.get("/api/studio/connections", headers={"X-Remote-User": "hong"})
logins = audit.recent(50, action="login")
assert any(r["user_id"] == "hong" and r["result"] == "new" for r in logins), logins
# 스로틀: 같은 창 내 재요청은 추가 기록 없음
before = len(audit.recent(200, action="login"))
for _ in range(5):
    client.get("/api/studio/connections", headers={"X-Remote-User": "hong"})
assert len(audit.recent(200, action="login")) == before, "스로틀 실패(로그인 폭주)"
print("OK: 로그인 이력 기록(new) + 스로틀(창 내 재요청 미기록)")

# 스로틀 창 지난 것처럼 강제 → resume 기록
appmod._last_login_log["hong"] = time.monotonic() - 10**6
client.get("/api/studio/connections", headers={"X-Remote-User": "hong"})
assert any(r["result"] == "resume" for r in audit.recent(50, action="login"))
print("OK: 창 경과 후 재접근 → resume 이력")

# 다른 사용자 → 별도 login
client.get("/api/studio/connections", headers={"X-Remote-User": "kim"})
assert any(r["user_id"] == "kim" for r in audit.recent(50, action="login"))
print("OK: 사용자별 로그인 이력 분리")

# ---------- PUSH 이력 (성공/실패 모두) ----------
db.execute("INSERT OR IGNORE INTO users (user_id) VALUES ('hong')")
db.execute("INSERT INTO sessions (session_id, user_id, title) VALUES ('S','hong','t')")
db.execute("INSERT INTO studios (studio_id, session_id, user_id, repo, branch_name, "
           "requirements, status) VALUES ('ST','S','hong','thr','feature/x','R','open')")
bid = db.execute("INSERT INTO builds (studio_id, attempt, status) VALUES ('ST',1,'pushing')")

# push 실패(GHE 미연결) → push_dispatch(fail) 이력
with mock.patch.object(ghe, "get_token",
                       side_effect=ghe.GheNotConnected("no token")):
    ghe.run_push(bid)
pushes = audit.recent(50, action="push_dispatch")
assert any(r["target"] == "ST#1" and r["result"] == "fail" for r in pushes), pushes
print("OK: push 실패도 push_dispatch 이력에 남음")

# push 성공 → push_dispatch(ci_running) 이력
bid2 = db.execute("INSERT INTO builds (studio_id, attempt, status) VALUES ('ST',2,'pushing')")
with mock.patch.object(ghe, "get_token", lambda u: "tok"), \
     mock.patch.object(ghe, "commit_and_push", lambda *a, **k: ("abc123", {})), \
     mock.patch.object(ghe, "_dispatch", lambda b, t: None), \
     mock.patch.object(ghe, "_find_run_id", lambda b, t: 1), \
     mock.patch.object(ghe, "_cancel_superseded", lambda *a, **k: None):
    ghe.run_push(bid2)
assert any(r["target"] == "ST#2" and r["result"] == "ci_running"
           for r in audit.recent(50, action="push_dispatch"))
print("OK: push 성공 push_dispatch(ci_running) 이력")

# ---------- 관리자 조회: action 필터 ----------
db.execute("UPDATE users SET is_admin=1 WHERE user_id='hong'")
appmod._last_login_log["hong"] = time.monotonic() - 10**6   # 관리자 조회도 login 남을 수 있음
al = client.get("/api/studio/admin/audit?action=push_dispatch",
                headers={"X-Remote-User": "hong"}).get_json()
assert al and all(r["action"] == "push_dispatch" for r in al), al
print("OK: 관리자 audit 조회 action=push_dispatch 필터")

print("\nALL HISTORY TESTS PASSED")
