"""코드 리뷰 지적사항 수정 검증 (F1~F6 + retry guard_exempt).

실행: python3 tests/test_review_fixes.py
"""
import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="studio-fix-")
os.environ["STUDIO_DB"] = os.path.join(TMP, "studio.db")
os.environ["STUDIO_FERNET_KEY"] = os.path.join(TMP, ".fernet.key")
os.environ["STUDIO_ATTACH_DIR"] = os.path.join(TMP, "attach")
os.environ["STUDIO_CI_WEBHOOK_SECRET"] = "s"

from studio.app import app                       # noqa: E402
from studio import db, ghe, jobs, pipeline       # noqa: E402
from studio.ghe_git import commit_and_push, blob_sha_at_head  # noqa: E402

jobs.submit = lambda fn, *a, **k: fn(*a, **k)
client = app.test_client()
H = {"X-Remote-User": "hong"}


def git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


# ---------- F1: commit_and_push가 실제 blob SHA를 반환하고, CRLF 정규화 하에서도
#            다음 회차가 false push_conflict를 안 낸다 ----------

bare = os.path.join(TMP, "r.git")
git(None, "init", "--bare", bare)
work = os.path.join(TMP, "seed")
git(None, "clone", bare, work)
# .gitattributes로 정규화 강제 — 워킹트리 CRLF ≠ 저장 blob(LF)로 발산 유발
with open(os.path.join(work, ".gitattributes"), "w") as f:
    f.write("*.txt text eol=lf\n")
git(work, "-c", "user.name=s", "-c", "user.email=s@s", "add", "-A")
git(work, "-c", "user.name=s", "-c", "user.email=s@s", "commit", "-m", "init")
git(work, "branch", "feature/foo")
git(work, "push", "origin", "master", "feature/foo")
REMOTE = "file://" + bare

# attempt 1: CRLF 내용을 커밋 (git이 LF로 정규화 → 저장 blob ≠ 원시 바이트)
sha1, pushed1 = commit_and_push(
    REMOTE, "feature/foo", {"data.txt": "a\r\nb\r\nc\r\n"}, {},
    "hong", "h@h", "attempt 1")
assert isinstance(sha1, str) and "data.txt" in pushed1
# 반환된 blob SHA는 git의 실제(정규화된) HEAD blob SHA여야 한다 — 이것이 핵심 보장
clone = os.path.join(TMP, "verify")
git(None, "clone", "--branch", "feature/foo", REMOTE, clone)
real = blob_sha_at_head(clone, "data.txt")
assert pushed1["data.txt"] == real, (pushed1["data.txt"], real)

# 원시 바이트 SHA와는 다름 = 정규화가 실제로 일어남 (구버전이라면 false conflict 유발했을 값)
import hashlib  # noqa: E402
raw = "a\r\nb\r\nc\r\n".encode()
raw_sha = hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()
assert raw_sha != real, "정규화 미발생 — 테스트 전제 실패"

# attempt 2: base = attempt1의 실제 blob SHA면 가드 통과 (원시 SHA였다면 PushConflict)
sha2, _ = commit_and_push(
    REMOTE, "feature/foo", {"data.txt": "a\r\nb\r\nc\r\nd\r\n"},
    {"data.txt": pushed1["data.txt"]}, "hong", "h@h", "attempt 2")
assert isinstance(sha2, str)
print("OK: F1 — 정규화 하에서도 실제 blob SHA 기반이라 false conflict 없음")

# retry 시 guard_exempt 유지 (누락 시 requirements md가 가드에 걸림)
import inspect  # noqa: E402
src = inspect.getsource(commit_and_push)
assert "guard_exempt=guard_exempt" in src, "retry가 guard_exempt를 전달하지 않음"
print("OK: retry 재귀가 guard_exempt 유지")


# ---------- F3: apply_ci_result 원자성 — 동시 2회 호출에도 메시지 1번만 ----------

sid = client.post("/api/studio/sessions", headers=H,
                  json={"title": "t"}).get_json()["session_id"]
studio_id = "ST-atomic"
db.execute("INSERT OR IGNORE INTO users (user_id) VALUES ('hong')")
db.execute("INSERT INTO studios (studio_id, session_id, user_id) VALUES (?,?,?)",
           (studio_id, sid, "hong"))
db.execute("INSERT INTO builds (studio_id, attempt, status) VALUES (?,?,'ci_running')",
           (studio_id, 1))

results = []
barrier = threading.Barrier(2)
def worker():
    barrier.wait()
    results.append(ghe.apply_ci_result(studio_id, 1, "fail", "b1", "테스트 실패"))
ts = [threading.Thread(target=worker) for _ in range(2)]
[t.start() for t in ts]; [t.join() for t in ts]
# 두 호출 모두 True(멱등) 반환하되, CI 메시지는 정확히 1개만
n = db.one("SELECT COUNT(*) AS n FROM messages WHERE session_id=? "
           "AND role='system' AND content LIKE '%테스트 실패%'", (sid,))["n"]
assert n == 1, f"CI 메시지 {n}개 (중복 주입)"
print("OK: F3 — apply_ci_result 동시 호출에도 메시지 1회만 주입")


# ---------- F2: draft 이중 승인 — studio 1개만 생성 ----------

s2 = client.post("/api/studio/sessions", headers=H,
                 json={"title": "t2"}).get_json()["session_id"]
db.execute("INSERT INTO requirement_drafts (session_id, content, status) "
           "VALUES (?,?, 'ready')", (s2, "## R1"))
draft_id = db.one("SELECT draft_id FROM requirement_drafts WHERE session_id=?",
                  (s2,))["draft_id"]
with mock.patch.object(pipeline, "invoke_claude",
                       lambda *a, **k: {"output": {"message": {"content": [
                           {"text": "```file:a.c\nx\n```"}]}}, "usage": {}}):
    codes = []
    def approve():
        with app.test_client() as c:
            r = c.post(f"/api/studio/requirements/{draft_id}/approve",
                       headers=H, json={})
            codes.append(r.status_code)
    ts = [threading.Thread(target=approve) for _ in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
studios = db.query("SELECT studio_id FROM studios WHERE session_id=?", (s2,))
assert len(studios) == 1, f"studio {len(studios)}개 생성 (이중 승인)"
assert sorted(codes) == [201, 409], codes
print("OK: F2 — draft 이중 승인 시 studio 1개 + 나머지 409")


# ---------- F4: 동명 첨부 — 서로 덮어쓰지 않음 ----------

s3 = client.post("/api/studio/sessions", headers=H,
                 json={"title": "t3"}).get_json()["session_id"]
client.post(f"/api/studio/sessions/{s3}/attachments", headers=H,
            data={"file": (io.BytesIO(b"first content"), "spec.md")},
            content_type="multipart/form-data")
client.post(f"/api/studio/sessions/{s3}/attachments", headers=H,
            data={"file": (io.BytesIO(b"second content"), "spec.md")},
            content_type="multipart/form-data")
from studio import docparse  # noqa: E402
ctx = docparse.session_attachment_text(s3)
assert "first content" in ctx and "second content" in ctx, ctx
print("OK: F4 — 동명 첨부 2개가 모두 보존됨")


# ---------- F6: _cancel_superseded가 repo를 인자로 받음 (N+1 제거) ----------

import inspect as _ins  # noqa: E402
sig = _ins.signature(ghe._cancel_superseded)
assert "repo" in sig.parameters, "repo 인자 없음"
print("OK: F6 — _cancel_superseded(repo) 시그니처")

print("\nALL REVIEW-FIX TESTS PASSED")
