"""§6.8 stage 전달 게이트 테스트 — 승인(push)과 stage 전달 결정의 분리.

승인 시 dispatch=false → push까지만(pushed), 별도 결정(POST /builds/<id>/dispatch)
으로 stage 검증 시작. 보류 중 취소 가능. 기본 승인은 기존대로 push+dispatch.

실행: python3 tests/test_stage_gate.py
"""
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="studio-stagegate-")
os.environ["STUDIO_DB"] = os.path.join(TMP, "studio.db")
os.environ["STUDIO_FERNET_KEY"] = os.path.join(TMP, ".fernet.key")
os.environ["STUDIO_ATTACH_DIR"] = os.path.join(TMP, "attach")
os.environ["STUDIO_LOG_DIR"] = os.path.join(TMP, "logs")

from studio.app import app                             # noqa: E402
from studio import db, logs, pipeline, jobs, ghe       # noqa: E402

jobs.submit = lambda fn, *a, **k: fn(*a, **k)
client = app.test_client()
H = {"X-Remote-User": "hong"}

db.execute("INSERT OR IGNORE INTO users (user_id) VALUES ('hong')")
db.execute("INSERT OR REPLACE INTO user_branch_config (user_id, repo, branch_name) "
           "VALUES ('hong','thr','feature/x')")

LLM = {"output": {"message": {"content": [{"text": ""}]}}, "usage": {}}
def llm(*a, **k):
    t = "```paths\n```" if "선정" in a[3] else "```file:src/a.c\nint x;\n```"
    return {"output": {"message": {"content": [{"text": t}]}}, "usage": {}}

dispatched = []       # ghe._dispatch 호출 추적

def ghe_mocks(run_id=777):
    return (mock.patch.object(ghe, "get_token", lambda uid: "tok"),
            mock.patch.object(ghe, "commit_and_push",
                              lambda *a, **k: ("cafe1234ab", {})),
            mock.patch.object(ghe, "_dispatch",
                              lambda b, t: dispatched.append(b["build_id"])),
            mock.patch.object(ghe, "_find_run_id", lambda b, t, tries=1: run_id))

# ---------- 준비: studio + 리뷰 대기 회차 ----------
with mock.patch.object(pipeline, "invoke_claude", llm):
    sid = client.post("/api/studio/sessions", headers=H,
                      json={"title": "t"}).get_json()["session_id"]
    d = client.post("/api/studio/requirements/draft", headers=H,
                    json={"session_id": sid, "content": "R"}).get_json()
    r = client.post(f"/api/studio/requirements/{d['draft_id']}/approve",
                    headers=H, json={}).get_json()
studio_id = r["studio_id"]
b1 = db.one("SELECT build_id, status FROM builds WHERE studio_id=?", (studio_id,))
assert b1["status"] == "awaiting_review", dict(b1)

# ---------- 리뷰 대기 상태에서 dispatch → 409 (게이트 순서 강제) ----------
r = client.post(f"/api/studio/builds/{b1['build_id']}/dispatch", headers=H)
assert r.status_code == 409, r.data
print("OK: awaiting_review 상태에선 stage 전달 불가(409)")

# ---------- 승인(dispatch=false) → push만, stage 보류 ----------
m1, m2, m3, m4 = ghe_mocks()
with m1, m2, m3, m4:
    r = client.post(f"/api/studio/builds/{b1['build_id']}/review", headers=H,
                    json={"action": "approve", "dispatch": False})
assert r.get_json() == {"status": "pushing", "hold_dispatch": True}, r.data
row = db.one("SELECT status, hold_dispatch, commit_sha, run_id FROM builds "
             "WHERE build_id=?", (b1["build_id"],))
assert row["status"] == "pushed", dict(row)
assert row["hold_dispatch"] == 1 and row["commit_sha"] == "cafe1234ab"
assert row["run_id"] is None and dispatched == [], "보류인데 dispatch 호출됨"
assert jobs.active_count_for_user("hong") == 1   # 보류 회차도 동시 1건에 포함
print("OK: 승인(dispatch=false) → pushed (stage 미전달, 커밋만)")

# ---------- 사용자 결정 → stage 전달 ----------
m1, m2, m3, m4 = ghe_mocks()
with m1, m2, m3, m4:
    r = client.post(f"/api/studio/builds/{b1['build_id']}/dispatch", headers=H)
assert r.get_json()["status"] == "dispatching", r.data
row = db.one("SELECT status, run_id FROM builds WHERE build_id=?",
             (b1["build_id"],))
assert row["status"] == "ci_running" and row["run_id"] == 777, dict(row)
assert dispatched == [b1["build_id"]]
# 이중 전달 차단: 이미 ci_running → 409
r = client.post(f"/api/studio/builds/{b1['build_id']}/dispatch", headers=H)
assert r.status_code == 409, r.data
print("OK: dispatch 결정 → ci_running + run_id, 이중 전달 409")

# ---------- 감사/디버그 로그 근거 ----------
acts = [a["action"] for a in db.query(
    "SELECT action FROM action_log WHERE user_id='hong' ORDER BY id")]
assert "stage_dispatch" in acts, acts
slog = open(logs.studio_log_path(studio_id)).read()
assert "stage 보류(pushed)" in slog and "[stage] 사용자 결정" in slog, slog
print("OK: stage 전달 결정이 audit + studio 디버그 로그에 기록")

# ---------- 보류 중 취소 ----------
db.execute("UPDATE builds SET status='pass', completed_at=datetime('now') "
           "WHERE build_id=?", (b1["build_id"],))   # 회차1 종결 → 다음 회차 허용
with mock.patch.object(pipeline, "invoke_claude", llm):
    r = client.post("/api/studio/message", headers=H,
                    json={"session_id": sid, "studio_id": studio_id,
                          "content": "수정"})
b2 = r.get_json()["build_id"]
dispatched.clear()
m1, m2, m3, m4 = ghe_mocks()
with m1, m2, m3, m4:
    client.post(f"/api/studio/builds/{b2}/review", headers=H,
                json={"action": "approve", "dispatch": False})
assert db.one("SELECT status FROM builds WHERE build_id=?", (b2,))["status"] == "pushed"
client.post(f"/api/studio/builds/{b2}/cancel", headers=H)
row = db.one("SELECT status, completed_at FROM builds WHERE build_id=?", (b2,))
assert row["status"] == "cancelled" and row["completed_at"], dict(row)
assert dispatched == [], "취소된 보류 회차가 dispatch됨"
print("OK: 보류(pushed) 회차 취소 → cancelled (stage 미전달)")

# ---------- 기본 승인은 기존대로 push+dispatch (회귀 방지) ----------
with mock.patch.object(pipeline, "invoke_claude", llm):
    r = client.post("/api/studio/message", headers=H,
                    json={"session_id": sid, "studio_id": studio_id,
                          "content": "다시"})
b3 = r.get_json()["build_id"]
m1, m2, m3, m4 = ghe_mocks(run_id=888)
with m1, m2, m3, m4:
    r = client.post(f"/api/studio/builds/{b3}/review", headers=H,
                    json={"action": "approve"})
assert r.get_json()["hold_dispatch"] is False
row = db.one("SELECT status, run_id, hold_dispatch FROM builds WHERE build_id=?",
             (b3,))
assert row["status"] == "ci_running" and row["run_id"] == 888, dict(row)
assert row["hold_dispatch"] == 0 and dispatched == [b3]
print("OK: 기본 승인은 기존대로 push+검증 한 번에 (회귀 없음)")

# ---------- Step 2에서 검증 방식 결정: code_only (코드만 준비) ----------
db.execute("UPDATE builds SET status='pass', completed_at=datetime('now') "
           "WHERE build_id=?", (b3,))   # 이전 회차 종결
with mock.patch.object(pipeline, "invoke_claude", llm):
    sid2 = client.post("/api/studio/sessions", headers=H,
                       json={"title": "t2"}).get_json()["session_id"]
    d2 = client.post("/api/studio/requirements/draft", headers=H,
                     json={"session_id": sid2, "content": "R2"}).get_json()
    # 잘못된 verify_mode → 400
    r = client.post(f"/api/studio/requirements/{d2['draft_id']}/approve",
                    headers=H, json={"verify_mode": "yolo"})
    assert r.status_code == 400, r.data
    r = client.post(f"/api/studio/requirements/{d2['draft_id']}/approve",
                    headers=H, json={"verify_mode": "code_only"}).get_json()
studio2 = r["studio_id"]
assert db.one("SELECT verify_mode FROM studios WHERE studio_id=?",
              (studio2,))["verify_mode"] == "code_only"
c1 = db.one("SELECT build_id, status, hold_dispatch FROM builds "
            "WHERE studio_id=?", (studio2,))
assert c1["status"] == "awaiting_review" and c1["hold_dispatch"] == 1, dict(c1)
print("OK: Step 2에서 code_only 결정 → 회차가 stage 보류 기본값으로 생성 (잘못된 값 400)")

# ---------- code_only: 평범한 승인(dispatch 미지정) → pushed ----------
dispatched.clear()
m1, m2, m3, m4 = ghe_mocks()
with m1, m2, m3, m4:
    r = client.post(f"/api/studio/builds/{c1['build_id']}/review", headers=H,
                    json={"action": "approve"})
assert r.get_json()["hold_dispatch"] is True, r.data
assert db.one("SELECT status FROM builds WHERE build_id=?",
              (c1["build_id"],))["status"] == "pushed"
assert dispatched == [], "code_only인데 stage로 전달됨"
# 이후 마음이 바뀌면 §6.8 결정으로 전달 가능
m1, m2, m3, m4 = ghe_mocks(run_id=999)
with m1, m2, m3, m4:
    client.post(f"/api/studio/builds/{c1['build_id']}/dispatch", headers=H)
assert db.one("SELECT status, run_id FROM builds WHERE build_id=?",
              (c1["build_id"],))["status"] == "ci_running"
print("OK: code_only studio — 승인은 push까지만, 이후 명시 결정으로만 stage 전달")

# ---------- code_only: 새 회차도 보류 상속 + 이번만 검증 override ----------
db.execute("UPDATE builds SET status='pass', completed_at=datetime('now') "
           "WHERE build_id=?", (c1["build_id"],))
with mock.patch.object(pipeline, "invoke_claude", llm):
    r = client.post("/api/studio/message", headers=H,
                    json={"session_id": sid2, "studio_id": studio2,
                          "content": "수정"})
c2 = r.get_json()["build_id"]
assert db.one("SELECT hold_dispatch FROM builds WHERE build_id=?",
              (c2,))["hold_dispatch"] == 1   # 상속
dispatched.clear()
m1, m2, m3, m4 = ghe_mocks(run_id=1000)
with m1, m2, m3, m4:
    r = client.post(f"/api/studio/builds/{c2}/review", headers=H,
                    json={"action": "approve", "dispatch": True})   # 이번만 검증
assert r.get_json()["hold_dispatch"] is False
assert db.one("SELECT status FROM builds WHERE build_id=?",
              (c2,))["status"] == "ci_running"
assert dispatched == [c2]
print("OK: code_only 새 회차 보류 상속 + dispatch=true로 이번만 즉시 검증")

print("\nALL STAGE-GATE TESTS PASSED")
