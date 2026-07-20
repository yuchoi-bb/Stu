"""미터링/성공 지표 (§10) + 멀티턴 요약 (§4.2)."""
from . import config, db


def admin_count() -> int:
    """현재 관리자 수 (§3.1.2). 락아웃/2인 권장 판단의 단일 소스."""
    return db.one("SELECT COUNT(*) AS n FROM users WHERE is_admin=1")["n"]


def user_metrics() -> list[dict]:
    """사용자별 토큰 소비 + §10.1 지표."""
    rows = db.query(
        """SELECT u.user_id,
                  COALESCE(SUM(l.input_tokens),0)  AS input_tokens,
                  COALESCE(SUM(l.output_tokens),0) AS output_tokens,
                  COALESCE(SUM(l.cache_read_tokens),0) AS cache_read_tokens
           FROM users u LEFT JOIN usage_log l ON l.user_id=u.user_id
           GROUP BY u.user_id ORDER BY u.user_id""")
    per_user = {r["user_id"]: dict(r) for r in rows}
    for r in db.query(
            """SELECT s.user_id,
                      COUNT(*) AS studios,
                      SUM(CASE WHEN EXISTS (SELECT 1 FROM builds b
                          WHERE b.studio_id=s.studio_id AND b.status='pass')
                          THEN 1 ELSE 0 END) AS studios_passed,
                      SUM(CASE WHEN s.status='done' THEN 1 ELSE 0 END) AS adopted
               FROM studios s GROUP BY s.user_id"""):
        per_user.setdefault(r["user_id"], {"user_id": r["user_id"]}).update(dict(r))
    return list(per_user.values())


def overall_metrics() -> dict:
    """§10.1: CI pass 도달률(studio 기준), studio당 평균 회차 수, 채택률."""
    total = db.one("SELECT COUNT(*) AS n FROM studios")["n"]
    passed = db.one(
        """SELECT COUNT(DISTINCT studio_id) AS n FROM builds
           WHERE status='pass'""")["n"]
    adopted = db.one("SELECT COUNT(*) AS n FROM studios WHERE status='done'")["n"]
    avg_attempts = db.one(
        """SELECT AVG(a) AS v FROM
           (SELECT COUNT(*) AS a FROM builds GROUP BY studio_id)""")["v"]
    tokens = db.one(
        """SELECT COALESCE(SUM(input_tokens),0) AS input_tokens,
                  COALESCE(SUM(output_tokens),0) AS output_tokens,
                  COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens
           FROM usage_log""")
    return {
        "studios_total": total,
        "ci_pass_rate": (passed / total) if total else None,
        "adoption_rate": (adopted / total) if total else None,
        "avg_attempts_per_studio": avg_attempts,
        **dict(tokens),
    }


# ---------- 멀티턴 요약 (§4.2: 최근 N턴 원문 + 이전 요약) ----------

SUMMARY_TRIGGER = config.HISTORY_RECENT_TURNS * 4   # 이 개수 초과 시 요약 갱신

SUMMARIZE_PROMPT = (
    "다음 대화를 이후 코드 생성에 필요한 사실 위주로 요약하라. "
    "확정된 요구조건, 시도한 접근, 실패 원인, 사용자 결정을 남기고 "
    "잡담은 버린다. 500자 이내 한국어.")


def maybe_summarize(user_id: str, session_id: str) -> bool:
    """오래된 턴을 요약으로 치환. 호출 시점: 새 회차 시작 전 (베스트에포트)."""
    n = db.one("SELECT COUNT(*) AS n FROM messages WHERE session_id=?",
               (session_id,))["n"]
    if n <= SUMMARY_TRIGGER:
        return False
    old = db.query(
        "SELECT role, content FROM messages WHERE session_id=? "
        "ORDER BY message_id LIMIT ?", (session_id, n - config.HISTORY_RECENT_TURNS * 2))
    prev = db.one("SELECT summary FROM sessions WHERE session_id=?", (session_id,))
    text = ""
    if prev and prev["summary"]:
        text += "기존 요약:\n" + prev["summary"] + "\n\n"
    text += "\n".join(f"[{r['role']}] {r['content'][:2000]}" for r in old)

    from .bedrock import AwsNotConnected, invoke_claude, response_text
    try:
        resp = invoke_claude(user_id, session_id,
                             [{"role": "user", "content": [{"text": text}]}],
                             SUMMARIZE_PROMPT)
        summary = response_text(resp)
    except AwsNotConnected:
        return False
    except Exception:
        return False
    db.execute("UPDATE sessions SET summary=? WHERE session_id=?",
               (summary, session_id))
    return True
