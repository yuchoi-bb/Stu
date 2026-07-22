"""studio_id별 디버그 로그 파일 테스트 (§11).

실행: python3 tests/test_studio_log.py
"""
import io
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="studio-slog-")
os.environ["STUDIO_DB"] = os.path.join(TMP, "studio.db")
os.environ["STUDIO_FERNET_KEY"] = os.path.join(TMP, ".fernet.key")
os.environ["STUDIO_ATTACH_DIR"] = os.path.join(TMP, "attach")
os.environ["STUDIO_LOG_DIR"] = os.path.join(TMP, "logs")

from studio.app import app                       # noqa: E402
from studio import db, logs, pipeline, jobs, manage   # noqa: E402

jobs.submit = lambda fn, *a, **k: fn(*a, **k)
client = app.test_client()
H = {"X-Remote-User": "hong"}

# ---------- slog: 파일 경로/append/파일명 살균 ----------
logs.slog("ST-abc123", "hello %s", "world")
p = logs.studio_log_path("ST-abc123")
assert os.path.isfile(p) and "hello world" in open(p).read()
# 위험 문자 살균 (경로 탈출 방지): 슬래시가 제거돼 studios 디렉터리를 벗어나지 못함
bad = logs.studio_log_path("../../etc/passwd")
assert os.path.dirname(bad) == logs.studio_log_dir(), bad
assert "/" not in os.path.basename(bad)
print("OK: slog 파일 append + 파일명 살균(경로 탈출 차단)")

# ---------- 생성 흐름이 studio 파일에 남는지 (E2E) ----------
db.execute("INSERT OR IGNORE INTO users (user_id) VALUES ('hong')")
db.execute("INSERT OR REPLACE INTO user_branch_config (user_id, repo, branch_name) "
           "VALUES ('hong','thr','feature/x')")
with mock.patch.object(pipeline, "invoke_claude",
                       lambda *a, **k: {"output": {"message": {"content": [
                           {"text": "```paths\n```" if "선정" in a[3]
                            else "```file:a.c\nx\n```"}]}}, "usage": {}}):
    sid = client.post("/api/studio/sessions", headers=H,
                      json={"title": "t"}).get_json()["session_id"]
    d = client.post("/api/studio/requirements/draft", headers=H,
                    json={"session_id": sid, "content": "R"}).get_json()
    r = client.post(f"/api/studio/requirements/{d['draft_id']}/approve",
                    headers=H, json={}).get_json()
studio_id = r["studio_id"]
content = open(logs.studio_log_path(studio_id)).read()
for marker in ("[step2] 요구조건 승인", "[gen] 시작", "[step3] 생성 파일",
               "[scan]", "[step3.5]"):
    assert marker in content, f"'{marker}' 없음\n{content}"
print("OK: 한 studio의 생성 전 과정이 studio_id 파일에 기록됨")

# ---------- 관리자 조회 엔드포인트 ----------
db.execute("UPDATE users SET is_admin=1 WHERE user_id='hong'")
resp = client.get(f"/api/studio/admin/studios/{studio_id}/log", headers=H).get_json()
assert resp["exists"] and "[gen] 시작" in resp["log"]
# 없는 studio → exists False
n = client.get("/api/studio/admin/studios/ST-none/log", headers=H).get_json()
assert n["exists"] is False
print("OK: 관리자 studio 로그 조회 엔드포인트")

# ---------- manage CLI ----------
buf = io.StringIO()
import contextlib  # noqa: E402
with contextlib.redirect_stdout(buf):
    manage.main(["studio-log", studio_id])
assert "[gen] 시작" in buf.getvalue()
print("OK: manage studio-log CLI")

print("\nALL STUDIO-LOG TESTS PASSED")
