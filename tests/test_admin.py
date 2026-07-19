"""관리자 2인 체계 정책 테스트: 승격/강등, 마지막 관리자 강등 금지, 2인 경고.

실행: python3 tests/test_admin.py
"""
import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="studio-admin-")
os.environ["STUDIO_DB"] = os.path.join(TMP, "studio.db")
os.environ["STUDIO_FERNET_KEY"] = os.path.join(TMP, ".fernet.key")
os.environ["STUDIO_ATTACH_DIR"] = os.path.join(TMP, "attach")
os.environ["STUDIO_LOG_DIR"] = os.path.join(TMP, "logs")
os.environ["STUDIO_CI_WEBHOOK_SECRET"] = "s"

from studio.app import app                  # noqa: E402
from studio import db, audit, manage        # noqa: E402

client = app.test_client()
HONG = {"X-Remote-User": "hong"}
KIM = {"X-Remote-User": "kim"}

# 사용자 등록(로그인 흉내) — connections 호출로 users 자동 생성
client.get("/api/studio/connections", headers=HONG)
client.get("/api/studio/connections", headers=KIM)

# ---------- 비관리자 차단 ----------
assert client.get("/api/studio/admin/admins", headers=HONG).status_code == 403
print("OK: 비관리자 /admin/admins 접근 차단 (403)")

# 부트스트랩: hong을 관리자로 (CLI 경로 시뮬레이션)
db.execute("UPDATE users SET is_admin=1 WHERE user_id='hong'")

# ---------- 목록 + 2인 권장 경고 ----------
al = client.get("/api/studio/admin/admins", headers=HONG).get_json()
assert al["count"] == 1 and al["low"] is True and al["recommend"] == 2, al
conn = client.get("/api/studio/connections", headers=HONG).get_json()
assert conn["admin_count"] == 1 and conn["low_admin_warning"] is True, conn
print("OK: 관리자 1명 → low=True, connections에 2인 권장 경고")

# ---------- 승격 ----------
r = client.post("/api/studio/admin/admins", headers=HONG,
                json={"user_id": "kim", "is_admin": True}).get_json()
assert r["admin_count"] == 2 and r["is_admin"] is True, r
al = client.get("/api/studio/admin/admins", headers=HONG).get_json()
assert al["count"] == 2 and al["low"] is False, al
# 이제 kim도 관리자 → kim이 조회 가능
assert client.get("/api/studio/admin/admins", headers=KIM).status_code == 200
print("OK: 승격 → 관리자 2명, low=False, 대상도 관리자 기능 사용 가능")

# ---------- 존재하지 않는 사용자 승격 거부 ----------
assert client.post("/api/studio/admin/admins", headers=HONG,
                   json={"user_id": "ghost", "is_admin": True}).status_code == 404
print("OK: 로그인 이력 없는 사용자 승격 거부 (404)")

# ---------- 강등 ----------
r = client.post("/api/studio/admin/admins", headers=HONG,
                json={"user_id": "kim", "is_admin": False}).get_json()
assert r["admin_count"] == 1 and r["is_admin"] is False, r
print("OK: 강등 → 관리자 1명")

# ---------- 마지막 관리자 강등 금지 ----------
# 이제 hong 1명뿐 — 자기 자신 강등 시도
resp = client.post("/api/studio/admin/admins", headers=HONG,
                   json={"user_id": "hong", "is_admin": False})
assert resp.status_code == 409, resp.status_code
assert db.one("SELECT is_admin FROM users WHERE user_id='hong'")["is_admin"] == 1
print("OK: 마지막 관리자 강등 금지 (409) — 락아웃 방지")

# ---------- audit 기록 ----------
acts = {a["action"] for a in audit.recent(30)}
assert "grant_admin" in acts and "revoke_admin" in acts, acts
print("OK: 승격/강등 audit 기록 (grant_admin/revoke_admin)")

# ---------- manage CLI ----------
def run_cli(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        manage.main(list(argv))
    return buf.getvalue()

out = run_cli("admins")
assert "hong" in out and "2인 체계 권장" in out, out
# CLI로 kim 다시 승격 후 hong --off 시도 → 정상 강등 (2명이므로 허용)
run_cli("set-admin", "kim")
assert db.one("SELECT is_admin FROM users WHERE user_id='kim'")["is_admin"] == 1
# 마지막 관리자 강등 금지: kim --off → hong 남음(허용), 이어서 hong --off → 금지
run_cli("set-admin", "kim", "--off")
try:
    run_cli("set-admin", "hong", "--off")
    raised = False
except SystemExit:
    raised = True
assert raised, "마지막 관리자 CLI 강등이 막히지 않음"
assert db.one("SELECT is_admin FROM users WHERE user_id='hong'")["is_admin"] == 1
print("OK: manage CLI — admins 목록 + 마지막 관리자 강등 금지 가드")

print("\nALL ADMIN TESTS PASSED")
