"""생성 파이프라인 (§7) — Phase 1 골격.

Phase 1 범위: Step 1(컨텍스트 로드)~3(생성)과 상태 전이.
Step 3.5 리뷰 게이트 이후(push/dispatch/CI, §6)는 Phase 2에서 연결한다.
"""
from . import db, jobs
from .bedrock import AwsNotConnected, invoke_claude, response_text

SYSTEM_PROMPT_GENERATE = (
    "You are ToolHub Studio's code generator. "
    "분석 md와 확정 requirements를 바탕으로 코드와 testcase를 생성한다. "
    "파일 전체 교체 방식이므로 파일의 완전한 내용을 출력한다 — 생략(...) 금지."
)


def run_generation(user_id: str, session_id: str, studio_id: str,
                   build_id: int) -> None:
    jobs.set_build_status(build_id, "generating")
    jobs.checkpoint(build_id)
    try:
        messages = _build_context(session_id, studio_id)
        resp = invoke_claude(user_id, session_id, messages,
                             SYSTEM_PROMPT_GENERATE,
                             studio_id=studio_id, build_id=build_id,
                             cached_context=_load_analysis_md(session_id))
        jobs.checkpoint(build_id)
        text = response_text(resp)
        db.execute("INSERT INTO messages (session_id, role, content) VALUES (?,?,?)",
                   (session_id, "assistant", text))
        # Step 3.5 리뷰 게이트: 생성 완료 → 작업자 승인 대기 (§7.1)
        jobs.set_build_status(build_id, "awaiting_review")
    except AwsNotConnected:
        jobs.set_build_status(build_id, "fail", completed=True,
                              fail_summary="AWS 재연결 필요 (SSO 세션 만료)")
    except jobs.Cancelled:
        raise
    except Exception as e:
        jobs.set_build_status(build_id, "fail", completed=True,
                              fail_summary=f"generation error: {e}")


def _build_context(session_id: str, studio_id: str) -> list:
    """멀티턴 히스토리 (§4.2: 최근 N턴 원문) + 이전 회차 fail_summary 누적 주입 (§7.1 Step 5)."""
    from . import config
    rows = db.query(
        "SELECT role, content FROM messages WHERE session_id=? "
        "ORDER BY message_id DESC LIMIT ?",
        (session_id, config.HISTORY_RECENT_TURNS * 2))
    messages = [{"role": r["role"], "content": [{"text": r["content"]}]}
                for r in reversed(rows)]

    fails = db.query(
        "SELECT attempt, fail_summary FROM builds "
        "WHERE studio_id=? AND status='fail' AND fail_summary IS NOT NULL "
        "ORDER BY attempt", (studio_id,))
    if fails:
        history = "\n".join(f"attempt {f['attempt']}: {f['fail_summary']}"
                            for f in fails)
        messages.insert(0, {"role": "user", "content": [{
            "text": "이전 회차 실패 이력 — 같은 실수를 반복하지 말 것:\n" + history}]})
    # Converse API는 user 턴으로 시작해야 함
    if messages and messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": [{"text": "(이전 대화 계속)"}]})
    return messages


def _load_analysis_md(session_id: str) -> str | None:
    """Step 1: tool↔분석 md 매핑 로드 — 매핑 테이블/stale 검사(§7.3)는 후속 구현."""
    return None
