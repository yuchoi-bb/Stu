"""§6.6 안 B: CI 통과 후 본인 명의 PR 생성 버튼 테스트.

실행: python3 tests/test_pr.py
"""
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="studio-pr-")
os.environ["STUDIO_DB"] = os.path.join(TMP, "studio.db")
os.environ["STUDIO_FERNET_KEY"] = os.path.join(TMP, ".fernet.key")
os.environ["STUDIO_ATTACH_DIR"] = os.path.join(TMP, "attach")
os.environ["STUDIO_LOG_DIR"] = os.path.join(TMP, "logs")
os.environ["STUDIO_CI_WEBHOOK_SECRET"] = "s"

from studio.app import app                       # noqa: E402
from studio import db, audit, pipeline, ghe, jobs  # noqa: E402

jobs.submit = lambda fn, *a, **k: fn(*a, **k)
client = app.test_client()
H = {"X-Remote-User": "hong"}


def make_passed_studio(branch="feature/foo"):
    """요구조건 승인 → studio/build 발급 → build를 pass로 강제."""
    db.execute("INSERT OR IGNORE INTO users (user_id) VALUES ('hong')")
    db.execute("INSERT OR REPLACE INTO user_branch_config (user_id, repo, branch_name) "
               "VALUES ('hong','thr',?)", (branch,))
    sid = client.post("/api/studio/sessions", headers=H,
                      json={"title": "t"}).get_json()["session_id"]
    with mock.patch.object(pipeline, "invoke_claude",
                           lambda *a, **k: {"output": {"message": {"content": [
                               {"text": "```paths\n```" if "select" in a[3].lower()
                                else "```file:a.c\nx\n```"}]}}, "usage": {}}):
        d = client.post("/api/studio/requirements/draft", headers=H,
                        json={"session_id": sid, "content": "요구조건 1줄"}).get_json()
        r = client.post(f"/api/studio/requirements/{d['draft_id']}/approve",
                        headers=H, json={}).get_json()
    studio_id, build_id = r["studio_id"], r["build_id"]
    db.execute("UPDATE builds SET status='pass', commit_sha='abc123def456' "
               "WHERE build_id=?", (build_id,))
    return studio_id


# ---------- 전제 가드: CI 통과 회차 없으면 409 ----------
sid0 = client.post("/api/studio/sessions", headers=H,
                   json={"title": "t0"}).get_json()["session_id"]
db.execute("INSERT OR REPLACE INTO user_branch_config (user_id, repo, branch_name) "
           "VALUES ('hong','thr','feature/foo')")
with mock.patch.object(pipeline, "invoke_claude",
                       lambda *a, **k: {"output": {"message": {"content": [
                           {"text": "```paths\n```" if "select" in a[3].lower()
                            else "```file:a.c\nx\n```"}]}}, "usage": {}}):
    d0 = client.post("/api/studio/requirements/draft", headers=H,
                     json={"session_id": sid0, "content": "R0"}).get_json()
    r0 = client.post(f"/api/studio/requirements/{d0['draft_id']}/approve",
                     headers=H, json={}).get_json()
resp = client.post(f"/api/studio/studios/{r0['studio_id']}/create-pr", headers=H, json={})
assert resp.status_code == 409, resp.status_code
print("OK: CI 통과 회차 없으면 PR 생성 거부 (409)")
# 이 build를 종결시켜 동시 진행 한도(1건)를 해제
db.execute("UPDATE builds SET status='cancelled', completed_at=datetime('now') "
           "WHERE build_id=?", (r0["build_id"],))

# ---------- 정상 생성 ----------
studio_id = make_passed_studio()
# status 응답에 can_pr True 노출
stt = client.get(f"/api/studio/status/{studio_id}", headers=H).get_json()
assert stt["can_pr"] is True, stt

created = {"n": 0}
def get_no_pr(url, **k):
    class R:
        status_code = 200
        def json(self): return []          # 열린 PR 없음
    return R()
def post_create(url, **k):
    created["n"] += 1
    assert url.endswith("/repos/toolhub/thr/pulls"), url
    body = k.get("json", {})
    assert body["head"] == "feature/foo" and body["base"] == "main", body
    assert "요구조건" in body["body"]        # 확정 요구조건 포함
    class R:
        status_code = 201
        def json(self): return {"number": 42,
                                "html_url": "https://ghe/toolhub/thr/pull/42"}
    return R()
with mock.patch.object(ghe, "get_token", lambda u: "tok"), \
     mock.patch("studio.ghe.requests.get", get_no_pr), \
     mock.patch("studio.ghe.requests.post", post_create):
    r = client.post(f"/api/studio/studios/{studio_id}/create-pr",
                    headers=H, json={}).get_json()
assert r["pr_number"] == 42 and r["existing"] is False and created["n"] == 1, r
row = db.one("SELECT pr_number, pr_url FROM studios WHERE studio_id=?", (studio_id,))
assert row["pr_number"] == 42 and "pull/42" in row["pr_url"]
assert any(a["action"] == "create_pr" for a in audit.recent(20))
print("OK: PR 생성 — 본인 명의 POST, studio에 pr 저장, audit 기록")

# PR 저장 후 can_pr False
stt2 = client.get(f"/api/studio/status/{studio_id}", headers=H).get_json()
assert stt2["can_pr"] is False and stt2["studio"]["pr_url"], stt2
print("OK: PR 생성 후 can_pr=False + pr_url 노출")

# ---------- 멱등: 이미 열린 PR이면 재사용(중복 POST 없음) ----------
studio_id2 = make_passed_studio(branch="feature/bar")
def get_has_pr(url, **k):
    class R:
        status_code = 200
        def json(self): return [{"number": 7,
                                 "html_url": "https://ghe/toolhub/thr/pull/7"}]
    return R()
posts = {"n": 0}
def post_boom(url, **k):
    posts["n"] += 1
    raise AssertionError("이미 열린 PR이 있으면 POST 하면 안 됨")
with mock.patch.object(ghe, "get_token", lambda u: "tok"), \
     mock.patch("studio.ghe.requests.get", get_has_pr), \
     mock.patch("studio.ghe.requests.post", post_boom):
    r = client.post(f"/api/studio/studios/{studio_id2}/create-pr",
                    headers=H, json={}).get_json()
assert r["pr_number"] == 7 and r["existing"] is True and posts["n"] == 0, r
print("OK: 멱등 — 열린 PR 재사용, 중복 생성 안 함")

# ---------- 브랜치가 base와 동일하면 거부 ----------
studio_id3 = make_passed_studio(branch="main")
resp = client.post(f"/api/studio/studios/{studio_id3}/create-pr", headers=H, json={})
assert resp.status_code == 409, resp.status_code
print("OK: 작업 브랜치==base(main)면 PR 거부 (409)")

# ---------- 소유자 아니면 404 ----------
resp = client.post(f"/api/studio/studios/{studio_id}/create-pr",
                   headers={"X-Remote-User": "kim"}, json={})
assert resp.status_code == 404, resp.status_code
print("OK: 타 사용자 PR 생성 차단 (404)")

print("\nALL PR TESTS PASSED")
