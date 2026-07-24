"""B: 회차 소프트 캡 경고 + 분석 md 미매핑 경고 테스트 (§6.7).

실행: python3 tests/test_warnings.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="studio-warn-")
os.environ["STUDIO_DB"] = os.path.join(TMP, "studio.db")
os.environ["STUDIO_FERNET_KEY"] = os.path.join(TMP, ".fernet.key")
os.environ["STUDIO_ATTACH_DIR"] = os.path.join(TMP, "attach")
os.environ["STUDIO_LOG_DIR"] = os.path.join(TMP, "logs")

from studio.app import app                    # noqa: E402
from studio import db, config, analysis       # noqa: E402

client = app.test_client()
H = {"X-Remote-User": "hong"}


# ---------- ② 분석 md 미매핑 경고 ----------
# tool 미지정 세션
s1 = client.post("/api/studio/sessions", headers=H, json={"title": "t"}).get_json()
w = analysis.status_warning(s1["session_id"])
assert w and "미지정" in w, w
d1 = client.get(f"/api/studio/sessions/{s1['session_id']}", headers=H).get_json()
assert d1["analysis_warn"] and "미지정" in d1["analysis_warn"]
print("OK: tool 미지정 → 분석 md 경고 (session_detail 노출)")

# tool 지정했으나 매핑 없음
s2 = client.post("/api/studio/sessions", headers=H,
                 json={"title": "t", "tool_target": "parser"}).get_json()
w = analysis.status_warning(s2["session_id"])
assert w and "미등록" in w, w
print("OK: tool 지정+매핑 없음 → '미등록' 경고")

# 매핑 등록 후엔 경고 없음
db.execute("INSERT INTO tool_analysis (tool_name, repo, md_path) "
           "VALUES ('parser','thr','docs/analysis/parser.md')")
assert analysis.status_warning(s2["session_id"]) is None
print("OK: 매핑 등록 후 경고 해제")

# /tools 엔드포인트
tools = client.get("/api/studio/tools", headers=H).get_json()
assert any(t["tool_name"] == "parser" for t in tools), tools
print("OK: /tools 등록된 tool 목록 반환")

# ---------- ① 회차 소프트 캡 경고 ----------
db.execute("INSERT INTO studios (studio_id, session_id, user_id, repo, "
           "branch_name, requirements, status) VALUES "
           "('ST-w','%s','hong','thr','feature/x','R','open')" % s2["session_id"])
# 캡 미만
for a in range(1, config.MAX_ATTEMPTS_SOFT):
    db.execute("INSERT INTO builds (studio_id, attempt, status) VALUES ('ST-w',?, 'fail')",
               (a,))
d = client.get(f"/api/studio/sessions/{s2['session_id']}", headers=H).get_json()
assert d["attempt_warn"] is False, d["attempt_warn"]
# 캡 도달
db.execute("INSERT INTO builds (studio_id, attempt, status) VALUES ('ST-w',?, 'fail')",
           (config.MAX_ATTEMPTS_SOFT,))
d = client.get(f"/api/studio/sessions/{s2['session_id']}", headers=H).get_json()
assert d["attempt_warn"] is True, d
print(f"OK: 회차 {config.MAX_ATTEMPTS_SOFT}회 도달 → 재확정 권장 경고")

print("\nALL WARNING TESTS PASSED")
