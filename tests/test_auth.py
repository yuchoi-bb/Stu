"""인증 헤더 계약 테스트 (§3.1).

앱은 X-Remote-User(=Apache가 REMOTE_USER로 설정)를 신원 소스로 신뢰한다.
따라서 Apache가 클라이언트 제공 X-Remote-User를 반드시 제거 후 재설정해야
스푸핑이 차단된다(deploy/apache-toolhub-studio.conf.reference). 여기서는
앱 측 계약만 검증: 헤더 없으면 401, 있으면 그 값이 신원이 된다.

실행: python3 tests/test_auth.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="studio-auth-")
os.environ["STUDIO_DB"] = os.path.join(TMP, "studio.db")
os.environ["STUDIO_FERNET_KEY"] = os.path.join(TMP, ".fernet.key")
os.environ["STUDIO_ATTACH_DIR"] = os.path.join(TMP, "attach")
os.environ["STUDIO_LOG_DIR"] = os.path.join(TMP, "logs")
os.environ.pop("STUDIO_DEV_USER", None)   # 개발 폴백 비활성 — 운영과 동일 조건

from studio.app import app   # noqa: E402
from studio import db        # noqa: E402

client = app.test_client()

# ---------- 헤더 없으면 401 ----------
r = client.get("/api/studio/connections")
assert r.status_code == 401, r.status_code
print("OK: X-Remote-User 없으면 401 (SSO 경유 강제)")

# ---------- 헤더 값이 신원이 된다 ----------
r = client.get("/api/studio/connections", headers={"X-Remote-User": "hong"})
assert r.status_code == 200 and r.get_json()["user_id"] == "hong"
assert db.one("SELECT 1 FROM users WHERE user_id='hong'")  # 최초 로그인 자동 등록
print("OK: X-Remote-User 값이 신원 + 최초 로그인 자동 등록")

# ---------- 서로 다른 헤더 값은 서로 다른 신원 ----------
r = client.get("/api/studio/connections", headers={"X-Remote-User": "kim"})
assert r.get_json()["user_id"] == "kim"
print("OK: 헤더 값에 따라 신원 분리 (Apache가 이 값을 통제해야 함)")

# ---------- CSRF: cross-origin 상태변경 요청 차단 ----------
H = {"X-Remote-User": "hong"}
# 같은 오리진(test_client host=localhost)은 통과(차단 아님)
r = client.post("/api/studio/sessions",
                headers={**H, "Origin": "http://localhost"}, json={"title": "t"})
assert r.status_code != 403, r.status_code
# Origin 없는 요청(서버-서버)도 통과 (ci-callback 등, HMAC 별도 보호)
r = client.post("/api/studio/sessions", headers=H, json={"title": "t"})
assert r.status_code != 403, r.status_code
# cross-origin은 403 (핸들러 도달 전 차단)
r = client.post("/api/studio/sessions",
                headers={**H, "Origin": "https://evil.example"}, json={"title": "t"})
assert r.status_code == 403, r.status_code
# GET은 CSRF 대상 아님 — cross-origin이어도 통과
r = client.get("/api/studio/connections",
               headers={**H, "Origin": "https://evil.example"})
assert r.status_code == 200, r.status_code
print("OK: CSRF — cross-origin 상태변경 403, same/no-origin·GET 통과")

print("\nALL AUTH TESTS PASSED")
