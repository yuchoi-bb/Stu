"""OBS 업로드 테스트: 에러 시 studio 디버그 로그를 OBS에 올린다 (미설정 시 no-op).

실행: python3 tests/test_obs.py
"""
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="studio-obs-")
os.environ["STUDIO_DB"] = os.path.join(TMP, "studio.db")
os.environ["STUDIO_FERNET_KEY"] = os.path.join(TMP, ".fernet.key")
os.environ["STUDIO_LOG_DIR"] = os.path.join(TMP, "logs")

from studio import db, logs, obs, config    # noqa: E402

db.init_db()
db.execute("INSERT INTO users (user_id) VALUES ('u')")
db.execute("INSERT INTO sessions (session_id, user_id, title) VALUES ('S','u','t')")
db.execute("INSERT INTO studios (studio_id, session_id, user_id, status) "
           "VALUES ('ST-dead1234','S','u','open')")
logs.slog("ST-dead1234", "[gen] 시작")
logs.slog("ST-dead1234", "[fail] 생성 오류: boom")

# ---------- 미설정 → no-op ----------
config.OBS_ENDPOINT = ""
assert obs.enabled() is False
assert obs.upload_studio_log("ST-dead1234", "테스트") is None
print("OK: OBS 미설정이면 no-op(None)")

# ---------- 설정 → 업로드 (S3 클라이언트 mock) ----------
config.OBS_ENDPOINT = "https://minio.intra:9000"
config.OBS_ACCESS_KEY = "ak"
config.OBS_SECRET_KEY = "sk"
config.OBS_BUCKET = "toolhub-studio-logs"
config.OBS_PREFIX = "error-logs/"
assert obs.enabled() is True

captured = {}
class FakeS3:
    def upload_file(self, path, bucket, key, ExtraArgs=None):
        captured.update(path=path, bucket=bucket, key=key, extra=ExtraArgs)

with mock.patch.object(obs, "_client", lambda: FakeS3()):
    key = obs.upload_studio_log("ST-dead1234", "생성 오류: boom")

assert key and key.startswith("error-logs/S-") and key.endswith("-dead1234.log"), key
assert captured["bucket"] == "toolhub-studio-logs"
assert captured["extra"]["Metadata"]["studio_id"] == "ST-dead1234"
assert os.path.isfile(captured["path"])       # 실제 로그 파일 경로
print("OK: 설정 시 studio 로그를 OBS error-logs/S-...-RUNID.log 로 업로드")

# ---------- 존재하지 않는 studio → None ----------
with mock.patch.object(obs, "_client", lambda: FakeS3()):
    assert obs.upload_studio_log("ST-nolog", "x") is None
print("OK: 로그 없는 studio는 업로드 생략")

# ---------- 업로드 실패해도 예외 전파 안 함 ----------
class BoomS3:
    def upload_file(self, *a, **k):
        raise RuntimeError("network down")
with mock.patch.object(obs, "_client", lambda: BoomS3()):
    assert obs.upload_studio_log("ST-dead1234", "x") is None   # 예외 삼키고 None
print("OK: OBS 업로드 실패는 흐름을 막지 않음(None)")

print("\nALL OBS TESTS PASSED")
