"""Bedrock 호출 계층 (§4). invoke_claude() 추상화 — 신원 방식 변경 시 이 파일만 수정."""
import random
import time

import boto3
import botocore.exceptions

from . import config, db
from .aws_sso import get_user_aws_credentials


class AwsNotConnected(Exception):
    """자격증명 없음/만료 — UI는 재승인 배너를 띄운다."""


def invoke_claude(user_id: str, session_id: str, messages: list, system: str,
                  *, studio_id: str | None = None, build_id: int | None = None,
                  cached_context: str | None = None) -> dict:
    """§4.1. 본인 자격증명 + requestMetadata 태깅 + prompt caching + usage 기록.

    system: 시스템 프롬프트 (캐시 블록으로 고정)
    cached_context: 분석 md 등 대용량 고정 컨텍스트 (별도 캐시 블록, §4.2)
    """
    creds = get_user_aws_credentials(user_id)
    if creds is None:
        raise AwsNotConnected(user_id)
    client = boto3.client("bedrock-runtime", **creds)

    system_blocks = [{"text": system}, {"cachePoint": {"type": "default"}}]
    if cached_context:
        system_blocks += [{"text": cached_context},
                          {"cachePoint": {"type": "default"}}]

    resp = _converse_with_backoff(
        client,
        modelId=config.MODEL_ID,
        messages=messages,
        system=system_blocks,
        requestMetadata={"user_id": user_id, "session_id": session_id,
                         "app": "toolhub-studio"},
    )
    _record_usage(user_id, session_id, studio_id, build_id, resp.get("usage", {}))
    return resp


def _converse_with_backoff(client, **kwargs) -> dict:
    """throttling(429) 지수 백오프 (§4.2)."""
    delay = 2.0
    for attempt in range(6):
        try:
            return client.converse(**kwargs)
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code != "ThrottlingException" or attempt == 5:
                raise
            time.sleep(delay + random.uniform(0, 1))
            delay = min(delay * 2, 60)
    raise RuntimeError("unreachable")


def _record_usage(user_id, session_id, studio_id, build_id, usage: dict) -> None:
    db.execute(
        """INSERT INTO usage_log (user_id, session_id, studio_id, build_id,
                                  input_tokens, output_tokens,
                                  cache_read_tokens, model_id)
           VALUES (?,?,?,?,?,?,?,?)""",
        (user_id, session_id, studio_id, build_id,
         usage.get("inputTokens", 0), usage.get("outputTokens", 0),
         usage.get("cacheReadInputTokens", 0), config.MODEL_ID),
    )


def response_text(resp: dict) -> str:
    parts = resp.get("output", {}).get("message", {}).get("content", [])
    return "".join(p.get("text", "") for p in parts if "text" in p)
