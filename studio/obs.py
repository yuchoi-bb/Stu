"""OBS(MinIO, S3 호환) 업로드 — 에러 발생 시 studio 디버그 로그를 올려 사후 분석.

에러가 나면 해당 studio의 로그 파일(logs/studio/S-YYMMDD-HHMMSS-RUNID.log)을
OBS의 {OBS_PREFIX}{파일명} 키로 업로드한다. 운영자는 OBS에서 그 키에 접근해
ERROR 발생 원인을 분석한다. OBS 미설정/실패는 본 흐름을 막지 않는다(best-effort).
"""
import os

from . import config, logs

_log = logs.get("obs")


def enabled() -> bool:
    return bool(config.OBS_ENDPOINT and config.OBS_ACCESS_KEY
               and config.OBS_SECRET_KEY)


def _client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=config.OBS_ENDPOINT,
        aws_access_key_id=config.OBS_ACCESS_KEY,
        aws_secret_access_key=config.OBS_SECRET_KEY,
        region_name=config.OBS_REGION,
    )


def upload_studio_log(studio_id: str, reason: str | None = None) -> str | None:
    """studio 로그 파일을 OBS에 업로드하고 키를 반환. 미설정/실패 시 None.

    키 = OBS_PREFIX + S-YYMMDD-HHMMSS-RUNID.log (studio별 고유). 재업로드는 갱신.
    """
    if not enabled():
        return None
    path = logs.studio_log_path(studio_id)   # 읽기 경로(존재하는 로그만)
    if not path or not os.path.isfile(path):
        return None
    key = config.OBS_PREFIX + os.path.basename(path)
    try:
        _client().upload_file(
            path, config.OBS_BUCKET, key,
            ExtraArgs={"ContentType": "text/plain; charset=utf-8",
                       "Metadata": {"studio_id": studio_id,
                                    "reason": (reason or "")[:256]}})
        _log.info("studio 로그 OBS 업로드 studio=%s key=%s", studio_id, key)
        logs.slog(studio_id, "[obs] 에러 로그 업로드 → %s/%s (원인: %s)",
                  config.OBS_BUCKET, key, reason or "-")
        return key
    except Exception:
        _log.exception("OBS 업로드 실패 studio=%s", studio_id)
        logs.slog(studio_id, "[obs] 업로드 실패 (원인: %s)", reason or "-")
        return None
