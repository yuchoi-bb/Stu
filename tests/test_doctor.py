"""manage.py doctor — 이식/설치 종합 진단 테스트.

미설정이면 FAIL로 exit 1(게이트), 필수를 채우면 exit 0.
실행: python3 tests/test_doctor.py
"""
import io
import contextlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="studio-doctor-")
os.environ["STUDIO_DB"] = os.path.join(TMP, "studio.db")
os.environ["STUDIO_FERNET_KEY"] = os.path.join(TMP, ".fernet.key")
os.environ["STUDIO_LOG_DIR"] = os.path.join(TMP, "logs")
os.environ["STUDIO_ATTACH_DIR"] = os.path.join(TMP, "attach")
os.environ["STUDIO_BACKUP_DIR"] = os.path.join(TMP, "bak")

from studio import db, manage, config    # noqa: E402


def run_doctor(net=False):
    """doctor를 돌리고 (exit_code, stdout) 반환."""
    buf = io.StringIO()
    code = 0
    ns = type("A", (), {"net": net})()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            manage._init()
            manage.cmd_doctor(ns)
    except SystemExit as e:
        code = e.code or 0
    return code, buf.getvalue()


# ---------- 미설정 → FAIL 다수, exit 1 ----------
code, out = run_doctor()
assert code == 1, out
assert "[FAIL] STUDIO_SSO_START_URL" in out
assert "CI_WEBHOOK_SECRET" in out
assert "관리자 0명" in out
assert "요약: FAIL" in out
print("OK: 미설정 환경은 FAIL + exit 1 (이식 게이트)")

# ---------- 필수 채우기 → exit 0 ----------
from cryptography.fernet import Fernet    # noqa: E402
with open(config.FERNET_KEY_PATH, "wb") as f:
    f.write(Fernet.generate_key())
os.chmod(config.FERNET_KEY_PATH, 0o600)
config.SSO_START_URL = "https://sso.intra/start"
config.SSO_ACCOUNT_ID = "123456789012"
config.SSO_ROLE_NAME = "ToolhubBedrock"
config.CI_WEBHOOK_SECRET = "s3cret"
config.GHE_OWNER = "realorg"
config.GHE_OAUTH_CLIENT_ID = "cid"
config.GHE_OAUTH_CLIENT_SECRET = "csec"
db.execute("INSERT INTO users (user_id, is_admin) VALUES ('a',1)")
db.execute("INSERT INTO users (user_id, is_admin) VALUES ('b',1)")

code, out = run_doctor()
assert code == 0, out
assert "[FAIL]" not in out, out
assert "필수 항목 통과" in out
print("OK: 필수 항목을 채우면 FAIL 0 + exit 0")

# ---------- 스키마 자기치유 (init_db 멱등) ----------
db.execute("DROP TABLE action_log")
# doctor는 _init()에서 init_db를 다시 부르므로 CREATE TABLE IF NOT EXISTS로 복구된다
code, out = run_doctor()
assert code == 0, out
assert "스키마 누락" not in out
print("OK: init_db 멱등 재생성으로 스키마 자기치유")

print("\nALL DOCTOR TESTS PASSED")
