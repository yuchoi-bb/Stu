"""엔드투엔드 데모 스모크 — scripts/demo.py가 전체 흐름을 완주하는지 확인.

실행: python3 tests/test_demo.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
demo = os.path.join(ROOT, "scripts", "demo.py")

p = subprocess.run([sys.executable, demo], capture_output=True, text=True,
                   cwd=ROOT)
out = p.stdout
assert p.returncode == 0, p.stderr[-800:]
assert "엔드투엔드 데모 완료" in out, out[-800:]
# 핵심 단계가 실제로 지나갔는지 (흐름 회귀 방지)
for marker in ("studio 발급", "회차 #2 생성", "#2:pass", "can_pr = True",
               "pr_number", "채택률 1.0"):
    assert marker in out, f"데모에 '{marker}' 없음\n{out[-800:]}"
print("OK: 데모 엔드투엔드 스모크 — Step2→생성→리뷰→CI실패→회차2→통과→PR→지표")
print("\nALL DEMO TESTS PASSED")
