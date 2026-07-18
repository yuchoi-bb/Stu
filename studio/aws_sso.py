"""AWS SSO device flow (§3.2) — CLI `aws sso login`과 동일한 OIDC 흐름을 백엔드가 수행.

흐름: RegisterClient → StartDeviceAuthorization → (사용자 브라우저 승인)
      → CreateToken 폴링 → GetRoleCredentials → 임시 자격증명 암호화 보관.
refresh 스케줄러는 앱 내 백그라운드 스레드 1개 (worker 1 고정, §4.3).
"""
import datetime as dt
import threading
import time

import boto3

from . import config, crypto, db

_lock = threading.Lock()
_pending = {}   # user_id -> device flow 진행 상태 (승인 폴링용)


def start_device_flow(user_id: str) -> dict:
    """'AWS 연결' 클릭 → 승인 URL 반환. UI는 링크를 표시하고 폴링한다."""
    oidc = boto3.client("sso-oidc", region_name=config.SSO_REGION)
    reg = oidc.register_client(clientName=f"toolhub-studio-{user_id}",
                               clientType="public")
    auth = oidc.start_device_authorization(
        clientId=reg["clientId"],
        clientSecret=reg["clientSecret"],
        startUrl=config.SSO_START_URL,
    )
    with _lock:
        _pending[user_id] = {
            "client_id": reg["clientId"],
            "client_secret": reg["clientSecret"],
            "device_code": auth["deviceCode"],
            "interval": auth.get("interval", 5),
            "expires_at": time.time() + auth["expiresIn"],
        }
    return {
        "verification_uri": auth["verificationUriComplete"],
        "user_code": auth["userCode"],
        "expires_in": auth["expiresIn"],
    }


def poll_device_flow(user_id: str) -> dict:
    """UI 폴링 엔드포인트가 호출. 승인 완료 시 자격증명 발급·저장."""
    with _lock:
        flow = _pending.get(user_id)
    if flow is None:
        return {"status": "not_started"}
    if time.time() > flow["expires_at"]:
        with _lock:
            _pending.pop(user_id, None)
        return {"status": "expired"}

    oidc = boto3.client("sso-oidc", region_name=config.SSO_REGION)
    try:
        token = oidc.create_token(
            clientId=flow["client_id"],
            clientSecret=flow["client_secret"],
            grantType="urn:ietf:params:oauth:grant-type:device_code",
            deviceCode=flow["device_code"],
        )
    except oidc.exceptions.AuthorizationPendingException:
        return {"status": "pending"}
    except oidc.exceptions.SlowDownException:
        return {"status": "pending"}

    _issue_role_credentials(user_id, token["accessToken"])
    with _lock:
        _pending.pop(user_id, None)
    return {"status": "connected"}


def _issue_role_credentials(user_id: str, sso_access_token: str) -> None:
    sso = boto3.client("sso", region_name=config.SSO_REGION)
    creds = sso.get_role_credentials(
        roleName=config.SSO_ROLE_NAME,
        accountId=config.SSO_ACCOUNT_ID,
        accessToken=sso_access_token,
    )["roleCredentials"]
    expires = dt.datetime.fromtimestamp(creds["expiration"] / 1000,
                                        tz=dt.timezone.utc)
    db.execute(
        """INSERT INTO aws_credentials
               (user_id, access_key_enc, secret_key_enc, session_token_enc,
                expires_at, refresh_token_enc, updated_at)
           VALUES (?,?,?,?,?,?, datetime('now'))
           ON CONFLICT(user_id) DO UPDATE SET
               access_key_enc=excluded.access_key_enc,
               secret_key_enc=excluded.secret_key_enc,
               session_token_enc=excluded.session_token_enc,
               expires_at=excluded.expires_at,
               refresh_token_enc=excluded.refresh_token_enc,
               updated_at=datetime('now')""",
        (user_id,
         crypto.encrypt(creds["accessKeyId"]),
         crypto.encrypt(creds["secretAccessKey"]),
         crypto.encrypt(creds["sessionToken"]),
         expires.isoformat(),
         crypto.encrypt(sso_access_token)),
    )


def get_user_aws_credentials(user_id: str) -> dict | None:
    """invoke_claude()가 호출 (§4.1). 만료면 None → 재승인 배너."""
    row = db.one("SELECT * FROM aws_credentials WHERE user_id=?", (user_id,))
    if row is None or row["expires_at"] is None:
        return None
    if dt.datetime.fromisoformat(row["expires_at"]) <= dt.datetime.now(dt.timezone.utc):
        return None
    return {
        "aws_access_key_id": crypto.decrypt(row["access_key_enc"]),
        "aws_secret_access_key": crypto.decrypt(row["secret_key_enc"]),
        "aws_session_token": crypto.decrypt(row["session_token_enc"]),
        "region_name": config.BEDROCK_REGION,
    }


def _refresh_loop() -> None:
    """만료 30분 전 선제 refresh (§3.2). SSO access token이 살아 있는 동안만 가능."""
    margin = dt.timedelta(minutes=config.SSO_REFRESH_MARGIN_MIN)
    while True:
        try:
            rows = db.query("SELECT user_id, refresh_token_enc, expires_at "
                            "FROM aws_credentials WHERE expires_at IS NOT NULL")
            now = dt.datetime.now(dt.timezone.utc)
            for row in rows:
                exp = dt.datetime.fromisoformat(row["expires_at"])
                if now < exp <= now + margin and row["refresh_token_enc"]:
                    try:
                        _issue_role_credentials(
                            row["user_id"], crypto.decrypt(row["refresh_token_enc"]))
                    except Exception:
                        pass   # SSO 세션 만료 → 사용자에게 재승인 배너 (get이 None 반환)
        except Exception:
            pass
        time.sleep(300)


def start_refresh_scheduler() -> None:
    threading.Thread(target=_refresh_loop, daemon=True,
                     name="aws-refresh").start()
