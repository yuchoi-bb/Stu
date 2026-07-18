"""생성 파이프라인 (§7).

Step 2: requirements 초안 생성 → 사용자 승인 → studio 발급 (확정 게이트)
Step 3: 코드+testcase 생성 → 파일 파싱 → 회귀 경고 → Step 3.5 리뷰 대기
Step 4~5(push/CI)는 ghe 모듈이 담당.
"""
import re

from . import db, jobs, prompts
from .bedrock import AwsNotConnected, invoke_claude, response_text

FILE_BLOCK_RE = re.compile(r"```file:(?P<path>[^\n]+)\n(?P<body>.*?)```", re.DOTALL)
SHRINK_WARN_RATIO = 0.30   # 직전 회차 대비 30% 이상 감소 시 경고 (§7.1)


# ---------- Step 2: requirements ----------

def run_requirements_draft(user_id: str, session_id: str, draft_id: int) -> None:
    try:
        messages = build_history(session_id)
        resp = invoke_claude(user_id, session_id, messages,
                             prompts.get("requirements"))
        text = response_text(resp)
        db.execute("INSERT INTO messages (session_id, role, content) VALUES (?,?,?)",
                   (session_id, "assistant", text))
        db.execute("UPDATE requirement_drafts SET content=?, status='ready' "
                   "WHERE draft_id=?", (text, draft_id))
    except AwsNotConnected:
        db.execute("UPDATE requirement_drafts SET status='failed', "
                   "content='AWS 재연결 필요 (SSO 세션 만료)' WHERE draft_id=?",
                   (draft_id,))
    except Exception as e:
        db.execute("UPDATE requirement_drafts SET status='failed', content=? "
                   "WHERE draft_id=?", (f"draft error: {e}", draft_id))


# ---------- Step 3: 생성 ----------

def run_generation(user_id: str, session_id: str, studio_id: str,
                   build_id: int) -> None:
    jobs.set_build_status(build_id, "generating")
    jobs.checkpoint(build_id)
    try:
        studio = db.one("SELECT requirements FROM studios WHERE studio_id=?",
                        (studio_id,))
        messages = build_history(session_id)
        messages = _inject_requirements_and_fails(messages, studio, studio_id)
        resp = invoke_claude(user_id, session_id, messages,
                             prompts.get("generate"),
                             studio_id=studio_id, build_id=build_id,
                             cached_context=_load_analysis_md(session_id))
        jobs.checkpoint(build_id)
        text = response_text(resp)
        db.execute("INSERT INTO messages (session_id, role, content) VALUES (?,?,?)",
                   (session_id, "assistant", text))

        files = parse_file_blocks(text)
        if not files:
            jobs.set_build_status(build_id, "fail", completed=True,
                                  fail_summary="생성 결과에 파일 블록 없음 "
                                               "(```file:path 형식 미준수)")
            return
        store_build_files(build_id, studio_id, files)

        # Step 3.5 리뷰 게이트 — 자동 승인 모드면 생략 (§7.1)
        user = db.one("SELECT auto_approve FROM users WHERE user_id=?", (user_id,))
        if user and user["auto_approve"]:
            jobs.set_build_status(build_id, "pushing")
            # push/dispatch는 ghe 모듈이 이어받음 (Phase 2 연결 지점)
        else:
            jobs.set_build_status(build_id, "awaiting_review")
    except AwsNotConnected:
        jobs.set_build_status(build_id, "fail", completed=True,
                              fail_summary="AWS 재연결 필요 (SSO 세션 만료)")
    except jobs.Cancelled:
        raise
    except Exception as e:
        jobs.set_build_status(build_id, "fail", completed=True,
                              fail_summary=f"generation error: {e}")


def parse_file_blocks(text: str) -> dict[str, str]:
    """```file:path ...``` 블록 추출 (프롬프트 규격, prompt_files/generate.md)."""
    files = {}
    for m in FILE_BLOCK_RE.finditer(text):
        path = m.group("path").strip()
        files[path] = m.group("body")
    return files


def store_build_files(build_id: int, studio_id: str, files: dict[str, str]) -> None:
    """파일 저장 + 라인 수 급감 경고 (직전 회차 동일 경로 대비, §7.1 회귀 가드)."""
    for path, content in files.items():
        lines = content.count("\n") + 1
        prev = db.one(
            """SELECT bf.line_count FROM build_files bf
               JOIN builds b ON b.build_id = bf.build_id
               WHERE b.studio_id=? AND bf.path=? AND bf.build_id != ?
               ORDER BY b.attempt DESC LIMIT 1""",
            (studio_id, path, build_id))
        warn = 1 if (prev and lines < prev["line_count"] * (1 - SHRINK_WARN_RATIO)) else 0
        db.execute(
            "INSERT INTO build_files (build_id, path, content, line_count, shrink_warn) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(build_id, path) DO UPDATE SET "
            "content=excluded.content, line_count=excluded.line_count, "
            "shrink_warn=excluded.shrink_warn",
            (build_id, path, content, lines, warn))


# ---------- 컨텍스트 조립 ----------

def build_history(session_id: str) -> list:
    """멀티턴 히스토리 (§4.2: 최근 N턴 원문 + 이전 요약)."""
    from . import config
    row = db.one("SELECT summary FROM sessions WHERE session_id=?", (session_id,))
    rows = db.query(
        "SELECT role, content FROM messages WHERE session_id=? "
        "ORDER BY message_id DESC LIMIT ?",
        (session_id, config.HISTORY_RECENT_TURNS * 2))
    messages = [{"role": r["role"], "content": [{"text": r["content"]}]}
                for r in reversed(rows)]
    if row and row["summary"]:
        messages.insert(0, {"role": "user", "content": [{
            "text": "이전 대화 요약:\n" + row["summary"]}]})
    if not messages or messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": [{"text": "(이전 대화 계속)"}]})
    return messages


def _inject_requirements_and_fails(messages: list, studio, studio_id: str) -> list:
    blocks = []
    if studio and studio["requirements"]:
        blocks.append("확정 requirements (이 문서가 유일한 기준):\n"
                      + studio["requirements"])
    fails = db.query(
        "SELECT attempt, fail_summary FROM builds "
        "WHERE studio_id=? AND status='fail' AND fail_summary IS NOT NULL "
        "ORDER BY attempt", (studio_id,))
    if fails:
        blocks.append("이전 회차 실패 이력 — 같은 실수를 반복하지 말 것:\n"
                      + "\n".join(f"attempt {f['attempt']}: {f['fail_summary']}"
                                  for f in fails))
    for text in reversed(blocks):
        messages.insert(0, {"role": "user", "content": [{"text": text}]})
    return messages


def _load_analysis_md(session_id: str) -> str | None:
    """Step 1: tool↔분석 md 매핑 로드 — 매핑 테이블/stale 검사(§7.3)는 후속 구현."""
    return None
