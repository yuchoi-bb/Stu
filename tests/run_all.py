"""전체 백엔드 테스트 러너 — 모든 test_*.py를 개별 프로세스로 실행하고 요약.

각 스위트는 독립 프로세스로 돌려 전역 상태(모듈 임포트, DB 경로 env) 오염을 막는다.
UI E2E(ui_test.py)는 브라우저 필요라 기본 제외 — `--ui`로 포함.

사용:
  python3 tests/run_all.py          # 백엔드 스위트 전체
  python3 tests/run_all.py --ui     # UI E2E까지 포함
종료 코드: 하나라도 실패하면 1.
"""
import glob
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _suites(include_ui: bool) -> list[str]:
    files = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
    if include_ui:
        files.append(os.path.join(HERE, "ui_test.py"))
    return files


def main(argv: list[str]) -> int:
    include_ui = "--ui" in argv
    results = []
    for path in _suites(include_ui):
        name = os.path.basename(path)
        t0 = time.time()
        p = subprocess.run([sys.executable, path], capture_output=True, text=True)
        dt = time.time() - t0
        ok = p.returncode == 0
        last = (p.stdout.strip().splitlines() or ["(출력 없음)"])[-1]
        results.append((name, ok, dt, last, p))
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name:24} {dt:5.1f}s  {last}")
        if not ok:   # 실패 시 stderr 꼬리 노출
            tail = "\n".join((p.stderr or p.stdout).strip().splitlines()[-15:])
            print("  ── 실패 상세 ──\n" + tail)

    passed = sum(1 for _, ok, *_ in results if ok)
    total = len(results)
    print(f"\n{'='*50}\n{passed}/{total} 스위트 통과"
          + ("" if passed == total else "  ⚠ 실패 있음"))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
