"""ToolHub Studio 백엔드 통합 테스트.

실행: python3 tests/test_studio.py
LLM은 mock, git은 로컬 bare repo(file://), GHE HTTP는 mock.
"""
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="studio-test-")
os.environ["STUDIO_DB"] = os.path.join(TMP, "studio.db")
os.environ["STUDIO_FERNET_KEY"] = os.path.join(TMP, ".fernet.key")
os.environ["STUDIO_ATTACH_DIR"] = os.path.join(TMP, "attachments")
os.environ["STUDIO_CI_WEBHOOK_SECRET"] = "test-secret"

from studio.app import app                          # noqa: E402
from studio import db, ghe, jobs, pipeline          # noqa: E402
from studio.ghe_git import (PushConflict, WorkflowGuardViolation,  # noqa: E402
                            commit_and_push)

client = app.test_client()
H = {"X-Remote-User": "hong"}

# jobs.submit을 동기 실행으로 (테스트 결정성)
jobs.submit = lambda fn, *a, **k: fn(*a, **k)


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def setup_remote() -> str:
    """feature/foo 브랜치를 가진 bare repo 생성."""
    bare = os.path.join(TMP, "remote.git")
    git(None, "init", "--bare", bare)
    work = os.path.join(TMP, "seed")
    git(None, "clone", bare, work)
    with open(os.path.join(work, "README.md"), "w") as f:
        f.write("thr repo\n")
    git(work, "-c", "user.name=seed", "-c", "user.email=s@s", "add", "-A")
    git(work, "-c", "user.name=seed", "-c", "user.email=s@s",
        "commit", "-m", "init")
    git(work, "branch", "feature/foo")
    git(work, "push", "origin", "master", "feature/foo")
    return "file://" + bare


REMOTE = setup_remote()

DRAFT_TEXT = "## R1. 내림 처리\n- floor, 소수 3자리\n"
GEN_V1 = ("변경 요약: R1 반영\n"
          "```file:src/parser.c\n" + "int floor3(double v);\n" * 40 + "```\n"
          "```file:test/test_parser.c\nTEST(floor3) {}\n```\n")
GEN_V2_SHRUNK = ("요약\n```file:src/parser.c\nint x;\n```\n")


def mock_llm(text):
    resp = {"output": {"message": {"content": [{"text": text}]}},
            "usage": {"inputTokens": 100, "outputTokens": 50}}
    return mock.patch.object(pipeline, "invoke_claude",
                             lambda *a, **k: resp)


# ---------- Step 2: 확정 게이트 ----------

r = client.post("/api/studio/sessions", headers=H,
                json={"title": "parser 내림 처리", "tool_target": "parser"})
sid = r.get_json()["session_id"]

with mock_llm(DRAFT_TEXT):
    r = client.post("/api/studio/requirements/draft", headers=H,
                    json={"session_id": sid, "content": "출력을 내림 처리해줘"})
assert r.status_code == 202, r.data
draft_id = r.get_json()["draft_id"]
d = client.get(f"/api/studio/requirements/{draft_id}", headers=H).get_json()
assert d["status"] == "ready" and "R1" in d["content"], d

# 승인 전 코딩 진입 불가: /message는 studio_id 없이는 400
assert client.post("/api/studio/message", headers=H,
                   json={"session_id": sid, "content": "코딩해"}).status_code == 400

# 브랜치 설정 주입 (API는 GHE 필요 — 직접 삽입)
db.execute("INSERT INTO user_branch_config (user_id, repo, branch_name) "
           "VALUES ('hong','thr','feature/foo')")

with mock_llm(GEN_V1):
    r = client.post(f"/api/studio/requirements/{draft_id}/approve", headers=H,
                    json={})
assert r.status_code == 201, r.data
studio_id = r.get_json()["studio_id"]
build1 = r.get_json()["build_id"]

st = client.get(f"/api/studio/status/{studio_id}", headers=H).get_json()
assert st["studio"]["requirements"].startswith("## R1"), st["studio"]
assert st["builds"][0]["status"] == "awaiting_review", st
files = client.get(f"/api/studio/builds/{build1}/files", headers=H).get_json()
assert {f["path"] for f in files} == {"src/parser.c", "test/test_parser.c"}
print("OK: Step 2 확정 게이트 + 생성 + 파일 파싱")

# ---------- Step 3.5: 리뷰 거부 → 다음 회차 + 회귀 경고 ----------

r = client.post(f"/api/studio/builds/{build1}/review", headers=H,
                json={"action": "reject", "reason": "테스트 부족"})
assert r.get_json()["status"] == "rejected"
b = db.one("SELECT status, fail_summary FROM builds WHERE build_id=?", (build1,))
assert b["status"] == "fail" and "테스트 부족" in b["fail_summary"]

with mock_llm(GEN_V2_SHRUNK):
    r = client.post("/api/studio/message", headers=H,
                    json={"session_id": sid, "studio_id": studio_id,
                          "content": "다시"})
assert r.get_json()["attempt"] == 2
build2 = r.get_json()["build_id"]
files2 = client.get(f"/api/studio/builds/{build2}/files", headers=H).get_json()
warn = {f["path"]: f["shrink_warn"] for f in files2}
assert warn["src/parser.c"] == 1, warn   # 40줄 → 1줄 급감 경고
print("OK: 리뷰 거부 → 회차 누적 + 라인 수 급감 경고")

# ---------- Step 4: 승인 → push (로컬 bare repo) ----------

with mock_llm(GEN_V1):
    r = client.post("/api/studio/message", headers=H,
                    json={"session_id": sid, "studio_id": studio_id,
                          "content": "원래대로 복원"})
build3 = r.get_json()["build_id"]

with mock.patch.object(ghe, "get_token", lambda uid: "tok"), \
     mock.patch.object(ghe, "remote_url", lambda repo, tok: REMOTE), \
     mock.patch.object(ghe, "_dispatch", lambda b, t: None), \
     mock.patch.object(ghe, "_find_run_id", lambda b, t, tries=1: 4242):
    r = client.post(f"/api/studio/builds/{build3}/review", headers=H,
                    json={"action": "approve"})
assert r.get_json()["status"] == "pushing", r.data
b = db.one("SELECT status, run_id, commit_sha FROM builds WHERE build_id=?",
           (build3,))
assert b["status"] == "ci_running" and b["run_id"] == 4242, dict(b)

check = os.path.join(TMP, "check")
git(None, "clone", "--branch", "feature/foo", REMOTE, check)
assert os.path.isfile(os.path.join(check, "src/parser.c"))
assert os.path.isfile(os.path.join(check, f"docs/studio/{studio_id}-requirements.md"))
log = subprocess.run(["git", "log", "-1", "--format=%s|%an"], cwd=check,
                     capture_output=True, text=True).stdout.strip()
assert f"[studio] id={studio_id} attempt=3" in log, log
print("OK: 승인 → 커밋/push (메시지 규칙, requirements md 동반)")

# ---------- 취소 전파 (§6.2) ----------

cancelled_runs = []
with mock.patch.object(ghe, "cancel_run",
                       lambda u, repo, rid: cancelled_runs.append(rid)):
    r = client.post(f"/api/studio/builds/{build3}/cancel", headers=H)
assert cancelled_runs == [4242], cancelled_runs
assert db.one("SELECT status FROM builds WHERE build_id=?",
              (build3,))["status"] == "cancelled"
print("OK: 취소 전파 (run cancel 시그널 + 장부 정리)")

# ---------- blob SHA 가드 (§6.5 조용한 덮어쓰기 차단) ----------

# 사용자가 원격에서 src/parser.c를 직접 수정
user_work = os.path.join(TMP, "user")
git(None, "clone", "--branch", "feature/foo", REMOTE, user_work)
with open(os.path.join(user_work, "src/parser.c"), "a") as f:
    f.write("/* 사용자 수동 수정 */\n")
git(user_work, "-c", "user.name=hong", "-c", "user.email=h@h",
    "commit", "-am", "manual fix")
git(user_work, "push", "origin", "feature/foo")

with mock_llm(GEN_V1):
    r = client.post("/api/studio/message", headers=H,
                    json={"session_id": sid, "studio_id": studio_id,
                          "content": "또 다시"})
build4 = r.get_json()["build_id"]
with mock.patch.object(ghe, "get_token", lambda uid: "tok"), \
     mock.patch.object(ghe, "remote_url", lambda repo, tok: REMOTE), \
     mock.patch.object(ghe, "_dispatch", lambda b, t: None), \
     mock.patch.object(ghe, "_find_run_id", lambda b, t, tries=1: None):
    r = client.post(f"/api/studio/builds/{build4}/review", headers=H,
                    json={"action": "approve"})
b = db.one("SELECT status, fail_summary FROM builds WHERE build_id=?", (build4,))
assert b["status"] == "push_conflict", dict(b)
assert "src/parser.c" in b["fail_summary"]
print("OK: blob SHA 가드 — 사용자 수정 덮어쓰기 차단 (push_conflict)")

# ---------- workflow 가드 (§6.3) ----------

try:
    commit_and_push(REMOTE, "feature/foo",
                    {".github/workflows/evil.yml": "x"}, {}, "a", "a@a", "m")
    assert False
except WorkflowGuardViolation:
    print("OK: workflow 파일 수정 금지 가드")

# ---------- CI 콜백 (§6.2 서명 + 멱등) ----------

# build4를 ci_running으로 되돌려 콜백 테스트
db.execute("UPDATE builds SET status='ci_running', completed_at=NULL "
           "WHERE build_id=?", (build4,))
payload = json.dumps({"studio_id": studio_id, "attempt": 4, "status": "fail",
                      "buildid": "b-777",
                      "fail_summary": "test_parser 실패"}).encode()
sig = "sha256=" + hmac.new(b"test-secret", payload, hashlib.sha256).hexdigest()

assert client.post("/api/studio/ci-callback", data=payload,
                   headers={"X-Studio-Signature": "sha256=bad"},
                   content_type="application/json").status_code == 403
r = client.post("/api/studio/ci-callback", data=payload,
                headers={"X-Studio-Signature": sig},
                content_type="application/json")
assert r.status_code == 200, r.data
b = db.one("SELECT status, buildid, fail_summary FROM builds WHERE build_id=?",
           (build4,))
assert b["status"] == "fail" and b["buildid"] == "b-777"
# 멱등: 재수신해도 그대로
r = client.post("/api/studio/ci-callback", data=payload,
                headers={"X-Studio-Signature": sig},
                content_type="application/json")
assert r.status_code == 200
# CI 실패가 대화에 자동 주입되었는가 (§7.1 Step 5)
msgs = client.get(f"/api/studio/sessions/{sid}/messages", headers=H).get_json()
assert any("test_parser 실패" in m["content"] for m in msgs)
print("OK: CI 콜백 — 서명 검증, 상태 반영, 멱등, 대화 자동 주입")

# ---------- E: 재확정 = 새 studio_id + 기존 abandoned ----------

with mock_llm("## R1(개정). 올림이 아니라 내림\n"):
    r = client.post("/api/studio/requirements/draft", headers=H,
                    json={"session_id": sid, "content": "요구조건이 틀렸다, 재확정"})
draft2 = r.get_json()["draft_id"]
with mock_llm(GEN_V1):
    r = client.post(f"/api/studio/requirements/{draft2}/approve", headers=H,
                    json={})
new_studio = r.get_json()["studio_id"]
assert new_studio != studio_id
old = db.one("SELECT status FROM studios WHERE studio_id=?", (studio_id,))
assert old["status"] == "abandoned", dict(old)
print("OK: E 경로 — 재확정 시 새 studio_id 발급 + 기존 abandoned")

print("\nALL BACKEND TESTS PASSED")
