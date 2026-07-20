"""린트 게이트 — ruff check가 깨끗한지 회귀로 고정 (설정: ruff.toml).

실행: python3 tests/test_lint.py
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if shutil.which("ruff") is None:
    print("SKIP: ruff 미설치 (pip install -r requirements-dev.txt)")
    sys.exit(0)

p = subprocess.run(["ruff", "check", "."], cwd=ROOT,
                   capture_output=True, text=True)
assert p.returncode == 0, "린트 실패:\n" + (p.stdout or p.stderr)
print("OK: ruff check . 통과 (0 findings)")
print("\nALL LINT TESTS PASSED")
