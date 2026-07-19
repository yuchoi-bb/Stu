"""위험 패턴 정적 검사 게이트 테스트 (§12.4).

실행: python3 tests/test_scan.py
"""
import json
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="studio-scan-")
os.environ["STUDIO_DB"] = os.path.join(TMP, "studio.db")
os.environ["STUDIO_FERNET_KEY"] = os.path.join(TMP, ".fernet.key")
os.environ["STUDIO_ATTACH_DIR"] = os.path.join(TMP, "attach")
os.environ["STUDIO_LOG_DIR"] = os.path.join(TMP, "logs")
os.environ["STUDIO_CI_WEBHOOK_SECRET"] = "s"

from studio import scan, db, pipeline, jobs   # noqa: E402
from studio.app import app                     # noqa: E402

# ---------- 순수 스캔 로직 ----------
danger = (
    'import os\n'
    'os.system("rm -rf /tmp/x")\n'          # high: os-system
    'key = "AKIAIOSFODNN7EXAMPLE"\n'         # high: aws-akid
    'subprocess.run(cmd, shell=True)\n'      # high: exec-shell-true
    'yaml.load(data)\n'                      # medium: yaml-unsafe
)
f = scan.scan_files({"bad.py": danger})
rules = {x["rule"] for x in f}
assert {"os-system", "aws-akid", "exec-shell-true"} <= rules, rules
assert "yaml-unsafe" in rules
c = scan.counts(f)
assert c["high"] >= 3 and c["medium"] >= 1, c
# high가 정렬 앞쪽
assert f[0]["severity"] == "high"
print("OK: 스캔 — high(명령실행/시크릿/shell) + medium(yaml) 탐지·정렬")

# 정상 코드는 무탐(오탐 억제 확인)
clean = 'def add(a, b):\n    return a + b\n'
assert scan.scan_files({"ok.py": clean}) == []
print("OK: 정상 코드 무탐 (오탐 억제)")

# 주석 속 위험어는 code_only 규칙에서 무탐 (auto_approve 무력화 방지)
comment_fp = (
    '# do not use eval( here\n'          # 주석 — 무탐이어야
    'x = obj.eval(y)\n'                   # 메서드 호출 — 무탐(negative lookbehind)
    'url = "http://a//b"\n'               # :// 보호 — 무탐
)
assert scan.scan_files({"c.py": comment_fp}) == [], scan.scan_files({"c.py": comment_fp})
print("OK: 주석/메서드호출/URL 오탐 억제 (code_only + lookbehind)")

# 단, 주석에 있는 시크릿은 여전히 탐지 (시크릿 규칙은 원문 검사)
sec = '# key AKIAIOSFODNN7EXAMPLE leaked\n'
assert any(x["rule"] == "aws-akid" for x in scan.scan_files({"s.py": sec}))
print("OK: 주석 속 시크릿은 여전히 탐지")

# 요약 문자열
s = scan.summarize(f)
assert "high" in s and "자동 승인을 보류" in s
print("OK: summarize — high 있을 때 자동승인 보류 문구 포함")

# ---------- 파이프라인 통합: auto_approve라도 high면 리뷰 강제 ----------
db.init_db()
db.execute("INSERT OR IGNORE INTO users (user_id, auto_approve) VALUES ('hong', 1)")
db.execute("INSERT INTO sessions (session_id, user_id, title) "
           "VALUES ('S1','hong','t')")
db.execute("INSERT INTO studios (studio_id, session_id, user_id, repo, branch_name, "
           "requirements, status) VALUES ('ST1','S1','hong','thr','feature/x','R','open')")
bid = db.execute("INSERT INTO builds (studio_id, attempt, status) "
                 "VALUES ('ST1', 1, 'generating')")

GEN = "```file:danger.py\nimport os\nos.system('id')\n```\n"
with mock.patch.object(pipeline, "invoke_claude",
                       lambda *a, **k: {"output": {"message": {"content": [
                           {"text": "```paths\n```" if "select" in a[3].lower()
                            else GEN}]}}, "usage": {}}), \
     mock.patch.object(pipeline, "_fetch_originals", lambda *a, **k: ({}, {})):
    pipeline.run_generation("hong", "S1", "ST1", bid)

row = db.one("SELECT status, scan_findings FROM builds WHERE build_id=?", (bid,))
# auto_approve=1 이지만 high 발견 → pushing이 아니라 awaiting_review
assert row["status"] == "awaiting_review", row["status"]
fnd = json.loads(row["scan_findings"])
assert any(x["rule"] == "os-system" for x in fnd), fnd
print("OK: auto_approve라도 high 발견 시 사람 검토 강제(awaiting_review)")

# ---------- 파일 엔드포인트에 scan 부착 ----------
jobs.submit = lambda fn, *a, **k: fn(*a, **k)
client = app.test_client()
r = client.get(f"/api/studio/builds/{bid}/files",
               headers={"X-Remote-User": "hong"}).get_json()
assert r[0]["path"] == "danger.py"
assert any(s["rule"] == "os-system" for s in r[0]["scan"]), r
print("OK: /builds/<id>/files 응답에 파일별 scan 부착")

# ---------- high 없으면 auto_approve 정상 진행 ----------
bid2 = db.execute("INSERT INTO builds (studio_id, attempt, status) "
                  "VALUES ('ST1', 2, 'generating')")
GEN2 = "```file:ok.py\ndef f():\n    return 1\n```\n"
with mock.patch.object(pipeline, "invoke_claude",
                       lambda *a, **k: {"output": {"message": {"content": [
                           {"text": "```paths\n```" if "select" in a[3].lower()
                            else GEN2}]}}, "usage": {}}), \
     mock.patch.object(pipeline, "_fetch_originals", lambda *a, **k: ({}, {})):
    pipeline.run_generation("hong", "S1", "ST1", bid2)
assert db.one("SELECT status FROM builds WHERE build_id=?", (bid2,))["status"] == "pushing"
print("OK: 위험 패턴 없으면 auto_approve 정상 진행(pushing)")

print("\nALL SCAN TESTS PASSED")
