"""운영·디버깅 기능 테스트: health, audit, 디버그 조회, manage CLI.

실행: python3 tests/test_ops.py
"""
import io
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="studio-ops-")
os.environ["STUDIO_DB"] = os.path.join(TMP, "studio.db")
os.environ["STUDIO_FERNET_KEY"] = os.path.join(TMP, ".fernet.key")
os.environ["STUDIO_ATTACH_DIR"] = os.path.join(TMP, "attach")
os.environ["STUDIO_LOG_DIR"] = os.path.join(TMP, "logs")
os.environ["STUDIO_CI_WEBHOOK_SECRET"] = "s"

from studio.app import app                       # noqa: E402
from studio import db, audit, pipeline, manage   # noqa: E402
from studio import jobs                           # noqa: E402

jobs.submit = lambda fn, *a, **k: fn(*a, **k)
client = app.test_client()
H = {"X-Remote-User": "hong"}

# ---------- health ----------
r = client.get("/api/studio/health")
body = r.get_json()
assert body["checks"]["db"] == "ok", body
assert "disk_free_mb" in body["checks"], body
print("OK: health — DB/디스크 점검 (스케줄러는 test 앱이라 down 표기 가능)")

# ---------- 로그 파일 생성 ----------
assert os.path.isfile(os.path.join(TMP, "logs", "studio.log"))
print("OK: 로그 파일 생성")

# ---------- audit 기록 + 조회 ----------
# 전체 플로우를 돌려 audit 이벤트가 쌓이는지
sid = client.post("/api/studio/sessions", headers=H,
                  json={"title": "t"}).get_json()["session_id"]
db.execute("INSERT INTO user_branch_config (user_id, repo, branch_name) "
           "VALUES ('hong','thr','feature/foo')")
with mock.patch.object(pipeline, "invoke_claude",
                       lambda *a, **k: {"output": {"message": {"content": [
                           {"text": "```paths\n```" if "select" in a[3].lower()
                            else "```file:a.c\nx\ny\nz\n```"}]}}, "usage": {}}):
    d = client.post("/api/studio/requirements/draft", headers=H,
                    json={"session_id": sid, "content": "요구조건"}).get_json()
    draft_id = d["draft_id"]
    r = client.post(f"/api/studio/requirements/{draft_id}/approve", headers=H, json={})
    studio_id = r.get_json()["studio_id"]
    build_id = r.get_json()["build_id"]
# 리뷰 거부 → audit
client.post(f"/api/studio/builds/{build_id}/review", headers=H,
            json={"action": "reject", "reason": "test"})

acts = {a["action"] for a in audit.recent(50)}
assert "requirements_approve" in acts, acts
assert "review_reject" in acts, acts
print("OK: audit — 승인/거부 행위 기록됨")

# 관리자 audit 조회 (권한 가드)
assert client.get("/api/studio/admin/audit", headers=H).status_code == 403
db.execute("UPDATE users SET is_admin=1 WHERE user_id='hong'")
al = client.get("/api/studio/admin/audit", headers=H).get_json()
assert any(a["action"] == "requirements_approve" for a in al)
print("OK: audit 관리자 조회 (권한 가드 포함)")

# ---------- 디버그 조회: build 상세 + failures ----------
detail = client.get(f"/api/studio/admin/builds/{build_id}", headers=H).get_json()
assert detail["build"]["status"] == "fail"
assert any(f["path"] == "a.c" for f in detail["files"])
fails = client.get("/api/studio/admin/failures", headers=H).get_json()
assert any(f["build_id"] == build_id for f in fails)
print("OK: 디버그 조회 — build 상세(파일/usage) + 실패 목록")

# ---------- manage CLI ----------
import contextlib  # noqa: E402
def run_cli(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        manage.main(list(argv))
    return buf.getvalue()

out = run_cli("set-admin", "kim")
assert "is_admin=1" in out
assert db.one("SELECT is_admin FROM users WHERE user_id='kim'")["is_admin"] == 1
run_cli("set-branch", "kim", "thr", "feature/kim")
assert db.one("SELECT branch_name FROM user_branch_config WHERE user_id='kim'"
              )["branch_name"] == "feature/kim"
run_cli("map-analysis", "parser", "thr", "docs/analysis/parser.md")
assert db.one("SELECT md_path FROM tool_analysis WHERE tool_name='parser'"
              )["md_path"] == "docs/analysis/parser.md"
out = run_cli("show-studio", studio_id)
assert studio_id in out and "#1" in out
out = run_cli("failures", "--limit", "10")
assert studio_id in out
out = run_cli("users")
assert "hong" in out and "kim" in out
out = run_cli("health")
assert "db: ok" in out
print("OK: manage CLI — set-admin/set-branch/map-analysis/show-studio/failures/users/health")

print("\nALL OPS TESTS PASSED")
