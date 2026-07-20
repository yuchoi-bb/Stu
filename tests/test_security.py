"""보안 회귀 테스트: LLM 생성 경로의 절대경로/상위 탈출 차단.

fetch_file(읽기)·commit_and_push(쓰기) 모두 repo 밖 경로를 거부해야
임의 파일 읽기(예: .fernet.key 노출)·쓰기를 막을 수 있다.

실행: python3 tests/test_security.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from studio import ghe_git   # noqa: E402

# ---------- 경로 판정 ----------
unsafe = [
    "/etc/passwd", "/opt/toolhub/data/.fernet.key",   # 절대경로
    "../../../etc/shadow", "..", "a/../../b",          # 상위 탈출
    "", ".",                                            # 빈/루트
]
for p in unsafe:
    assert ghe_git.is_unsafe_path(p), f"unsafe로 판정돼야: {p!r}"

safe = ["src/parser.c", "test/x.c", "a/b/c.py", "deep/../deep/f.txt"]
for p in safe:
    assert not ghe_git.is_unsafe_path(p), f"safe여야: {p!r}"
print("OK: is_unsafe_path — 절대경로/상위탈출 차단, 정상 상대경로 허용")

# ---------- fetch_file: 절대경로면 clone 전에 거부 (임의 파일 읽기 차단) ----------
# 실제 민감 파일을 만들어 두고, 절대경로로 읽으려는 시도가 UnsafePath로 막히는지
secret = tempfile.NamedTemporaryFile("w", delete=False, suffix=".key")
secret.write("SUPER-SECRET-KEY"); secret.close()
raised = False
try:
    ghe_git.fetch_file("http://example.invalid/x.git", "main", secret.name)
except ghe_git.UnsafePath:
    raised = True
assert raised, "절대경로 fetch가 UnsafePath로 막히지 않음"
# 상위 탈출도
try:
    ghe_git.fetch_file("http://example.invalid/x.git", "main", "../../../etc/passwd")
    assert False
except ghe_git.UnsafePath:
    pass
os.unlink(secret.name)
print("OK: fetch_file — 절대경로/상위탈출은 clone 전에 UnsafePath (임의 읽기 차단)")

# ---------- commit_and_push: 절대경로 파일은 UnsafePath ----------
try:
    ghe_git.commit_and_push(
        "http://example.invalid/x.git", "main",
        {"/etc/cron.d/evil": "x"}, {}, "n", "e@x", "m")
    assert False, "절대경로 쓰기가 막히지 않음"
except ghe_git.UnsafePath:
    pass
# 상위 탈출도
try:
    ghe_git.commit_and_push(
        "http://example.invalid/x.git", "main",
        {"../../evil.sh": "x"}, {}, "n", "e@x", "m")
    assert False
except ghe_git.UnsafePath:
    pass
print("OK: commit_and_push — 절대경로/상위탈출 파일은 UnsafePath (임의 쓰기 차단)")

# ---------- 토큰 노출 차단: git 오류 메시지에서 자격증명 마스킹 ----------
tokened = ("git clone --branch main "
           "https://x-access-token:ghs_SECRET123@github.example/toolhub/x.git /tmp/y: "
           "fatal: could not read Username")
red = ghe_git._redact(tokened)
assert "ghs_SECRET123" not in red and "x-access-token" not in red, red
assert "***@github.example" in red, red
# clone 실패가 실제로 마스킹된 RuntimeError를 던지는지 (존재하지 않는 remote)
try:
    ghe_git._git(None, "clone", "--branch", "main",
                 "https://x-access-token:ghs_TOPSECRET@127.0.0.1:1/x.git",
                 tempfile.mkdtemp())
    assert False, "clone이 실패해야"
except RuntimeError as e:
    assert "ghs_TOPSECRET" not in str(e), f"토큰 노출: {e}"
print("OK: git 오류 메시지에서 토큰 마스킹 (fail_summary→DB·대화·Bedrock 유출 차단)")

# ---------- git 서브프로세스 타임아웃 (executor 스레드 hang 방지) ----------
import subprocess as _sp    # noqa: E402
def _boom(*a, **k):
    raise _sp.TimeoutExpired(cmd="git", timeout=ghe_git.GIT_TIMEOUT)
try:
    import unittest.mock as _mock
    with _mock.patch("studio.ghe_git.subprocess.run", _boom):
        ghe_git._run_git(["clone", "x"], check=True)
    assert False, "타임아웃이 RuntimeError로 변환되지 않음"
except RuntimeError as e:
    assert "타임아웃" in str(e), e
print("OK: git 타임아웃 → RuntimeError (executor 스레드 무한 hang 방지)")

print("\nALL SECURITY TESTS PASSED")
