"""#2: 생성 컨텍스트 상한 — 회차 누적 폭주 방지 (§6.7).

실행: python3 tests/test_context_cap.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="studio-cap-")
os.environ["STUDIO_DB"] = os.path.join(TMP, "studio.db")
os.environ["STUDIO_FERNET_KEY"] = os.path.join(TMP, ".fernet.key")
os.environ["STUDIO_LOG_DIR"] = os.path.join(TMP, "logs")

from studio import db, config, pipeline    # noqa: E402

db.init_db()
db.execute("INSERT INTO users (user_id) VALUES ('u')")
db.execute("INSERT INTO sessions (session_id, user_id, title) VALUES ('S','u','t')")
db.execute("INSERT INTO studios (studio_id, session_id, user_id, requirements, status) "
           "VALUES ('ST','S','u','R', 'open')")

# 실패 회차 10개 (각 큰 fail_summary)
for a in range(1, 11):
    db.execute("INSERT INTO builds (studio_id, attempt, status, fail_summary) "
               "VALUES ('ST',?, 'fail', ?)", (a, f"attempt{a} " + "X" * 5000))

studio = db.one("SELECT * FROM studios WHERE studio_id='ST'")

# ---------- 실패 이력: 최근 K회만 + 각 요약 상한 ----------
msgs = pipeline._inject_requirements_and_fails([], studio, "ST")
fail_block = next(m["content"][0]["text"] for m in msgs
                  if "실패 이력" in m["content"][0]["text"])
# 최근 GEN_MAX_FAILS회만
assert f"최근 {config.GEN_MAX_FAILS}회" in fail_block, fail_block[:200]
assert f"그 외 {10 - config.GEN_MAX_FAILS}회 생략" in fail_block
# 가장 오래된 attempt1은 빠지고 최신들만
assert "attempt10" in fail_block and "attempt1:" not in fail_block
# 각 요약 길이 상한(생략 표시)
assert "생략(상한 초과)" in fail_block
print(f"OK: 실패 이력 최근 {config.GEN_MAX_FAILS}회만 + 요약 길이 상한")

# ---------- 원문(base_files) 총량 상한 ----------
big = {f"f{i}.c": "L" * 30000 for i in range(5)}   # 150k > 상한
msgs = pipeline._inject_requirements_and_fails([], studio, "ST", base_files=big)
orig_block = next(m["content"][0]["text"] for m in msgs
                  if "수정 대상 파일 원문" in m["content"][0]["text"])
assert "원문 주입 상한 도달" in orig_block
assert len(orig_block) < config.GEN_BASE_FILES_MAXCHARS + 5000, len(orig_block)
print("OK: 수정 원문 총 주입 상한(초과분 생략)")

# ---------- 히스토리 메시지 원문 상한 ----------
db.execute("INSERT INTO messages (session_id, role, content) VALUES ('S','assistant',?)",
           ("Z" * 20000,))
hist = pipeline.build_history("S")
assert all(len(m["content"][0]["text"]) <= config.HISTORY_MSG_MAXLEN + 40 for m in hist)
print("OK: 히스토리 메시지 원문 상한(생성 코드 전문 폭주 방지)")

print("\nALL CONTEXT-CAP TESTS PASSED")
