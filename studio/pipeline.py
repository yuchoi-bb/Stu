"""생성 파이프라인 (§7).

Step 2: requirements 초안 생성 → 사용자 승인 → studio 발급 (확정 게이트)
Step 3: 코드+testcase 생성 → 파일 파싱 → 회귀 경고 → Step 3.5 리뷰 대기
Step 4~5(push/CI)는 ghe 모듈이 담당.
"""
import re

from . import db, jobs, logs, prompts
from .bedrock import AwsNotConnected, invoke_claude, response_text

_log = logs.get("pipeline")

FILE_START_RE = re.compile(r"```file:(.+)")
PATHS_BLOCK_RE = re.compile(r"```paths\n(?P<body>.*?)```", re.DOTALL)
SHRINK_WARN_RATIO = 0.30   # 직전 회차 대비 30% 이상 감소 시 경고 (§7.1)


# ---------- Step 2: requirements ----------

def run_requirements_draft(user_id: str, session_id: str, draft_id: int) -> None:
    try:
        from . import docparse
        messages = build_history(session_id)
        attach = docparse.session_attachment_text(session_id)
        resp = invoke_claude(user_id, session_id, messages,
                             prompts.get("requirements"),
                             cached_context=attach or None)
        text = response_text(resp)
        db.execute("INSERT INTO messages (session_id, role, content) VALUES (?,?,?)",
                   (session_id, "assistant", text))
        db.execute("UPDATE requirement_drafts SET content=?, status='ready' "
                   "WHERE draft_id=?", (text, draft_id))
    except AwsNotConnected:
        _draft_failed(draft_id, session_id, "AWS 재연결 필요 (SSO 세션 만료)")
    except Exception as e:
        _draft_failed(draft_id, session_id, f"draft error: {e}")


def _draft_failed(draft_id: int, session_id: str, reason: str) -> None:
    db.execute("UPDATE requirement_drafts SET status='failed', content=? "
               "WHERE draft_id=?", (reason, draft_id))
    db.execute("INSERT INTO messages (session_id, role, content) VALUES (?,?,?)",
               (session_id, "system", f"[requirements 초안 실패] {reason}"))


# ---------- Step 3: 생성 ----------

def run_generation(user_id: str, session_id: str, studio_id: str,
                   build_id: int) -> None:
    jobs.set_build_status(build_id, "generating")
    jobs.checkpoint(build_id)
    try:
        from . import analysis, docparse
        studio = db.one("SELECT requirements, repo, branch_name FROM studios "
                        "WHERE studio_id=?", (studio_id,))

        # Step 1: 분석 md 로드 + stale 경고 (§7.1/§7.3)
        analysis_ctx, stale = analysis.load_for_session(session_id, user_id)
        if stale:
            db.execute("INSERT INTO messages (session_id, role, content) "
                       "VALUES (?,?,?)", (session_id, "system", stale))
        attach_ctx = docparse.session_attachment_text(session_id)
        cached = "\n\n".join(c for c in (analysis_ctx, attach_ctx) if c) or None

        # Step 3 pass 1: 수정 대상 파일 지목 → GHE 원문 fetch (§7.1)
        base_files, base_blobs = _fetch_originals(
            user_id, studio, analysis_ctx, session_id, studio_id, build_id)

        messages = build_history(session_id)
        messages = _inject_requirements_and_fails(messages, studio, studio_id,
                                                  base_files)
        # Step 3 pass 2: 원문 컨텍스트 포함 재호출
        resp = invoke_claude(user_id, session_id, messages,
                             prompts.get("generate"),
                             studio_id=studio_id, build_id=build_id,
                             cached_context=cached)
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
        store_build_files(build_id, studio_id, files, base_blobs, base_files)

        # 위험 패턴 정적 검사 게이트 (§12.4) — 결과 저장 + 대화 주입
        from . import scan
        import json as _json
        findings = scan.scan_files(files)
        db.execute("UPDATE builds SET scan_findings=? WHERE build_id=?",
                   (_json.dumps(findings, ensure_ascii=False), build_id))
        has_high = any(f["severity"] == "high" for f in findings)
        if findings:
            db.execute("INSERT INTO messages (session_id, role, content) "
                       "VALUES (?,?,?)", (session_id, "system", scan.summarize(findings)))

        # Step 3.5 리뷰 게이트 — 자동 승인 모드면 생략 (§7.1).
        # 단, 정적 검사 high가 있으면 auto_approve여도 사람 검토를 강제한다 (§6.4/§12.4).
        user = db.one("SELECT auto_approve FROM users WHERE user_id=?", (user_id,))
        if user and user["auto_approve"] and not has_high:
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
        _log.exception("generation failed studio=%s build=%s", studio_id, build_id)
        jobs.set_build_status(build_id, "fail", completed=True,
                              fail_summary=f"generation error: {e}")


def parse_file_blocks(text: str) -> dict[str, str]:
    """```file:path ...``` 블록 추출 (프롬프트 규격, prompt_files/generate.md).

    라인 단위 파서 — 블록은 **단독 ``` 라인**에서만 닫는다. 파일 내용에 인라인
    ```가 있어도(예: `s = "```"`, docstring) 잘리지 않는다. (정규식 non-greedy는
    첫 ```에서 잘려 파일이 조용히 손상되던 버그를 대체.)
    """
    files = {}
    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        m = FILE_START_RE.match(lines[i])
        if not m:
            i += 1
            continue
        path = m.group(1).strip()
        i += 1
        body = []
        while i < n and lines[i].rstrip() != "```":
            body.append(lines[i])
            i += 1
        # 파일은 관례상 개행으로 끝난다 — 닫는 ``` 앞의 개행을 복원
        files[path] = ("\n".join(body) + "\n") if body else ""
        i += 1   # 닫는 ``` 라인 건너뜀
    return files


def store_build_files(build_id: int, studio_id: str, files: dict[str, str],
                      base_blobs: dict[str, str | None] | None = None,
                      base_files: dict[str, str] | None = None) -> None:
    """파일 저장 + 라인 수 급감 경고 + base blob SHA/원문 기록 (§7.1 회귀 가드, §6.5).

    base_files: 2-pass에서 fetch한 수정 파일 원문 {path: content} — 리뷰 diff 뷰용.
    """
    base_blobs = base_blobs or {}
    base_files = base_files or {}
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
            "INSERT INTO build_files (build_id, path, content, line_count, "
            "shrink_warn, base_blob_sha, base_content) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(build_id, path) DO UPDATE SET "
            "content=excluded.content, line_count=excluded.line_count, "
            "shrink_warn=excluded.shrink_warn, base_blob_sha=excluded.base_blob_sha, "
            "base_content=excluded.base_content",
            (build_id, path, content, lines, warn, base_blobs.get(path),
             base_files.get(path)))


def _fetch_originals(user_id, studio, analysis_ctx, session_id, studio_id,
                     build_id):
    """pass 1: 수정 대상 파일 지목 → GHE에서 원문+blob SHA fetch (§7.1, §6.5).

    반환: (원문 dict, blob SHA dict). GHE 미연결/브랜치 미설정이면 빈 dict
    (신규 파일만 생성하거나, blob 가드가 폴백 경로로 동작).
    """
    if not studio or not studio["repo"] or not studio["branch_name"]:
        return {}, {}
    try:
        from . import ghe, ghe_git
        select_msgs = [{"role": "user", "content": [{"text":
            "확정 requirements:\n" + (studio["requirements"] or "")}]}]
        if analysis_ctx:
            select_msgs[0]["content"][0]["text"] += "\n\n" + analysis_ctx
        resp = invoke_claude(user_id, session_id, select_msgs,
                             prompts.get("select_files"))
        paths = parse_paths_block(response_text(resp))
        if not paths:
            return {}, {}   # 전부 신규 생성 — 원문 fetch 불필요
        token = ghe.get_token(user_id)
        remote = ghe.remote_url(studio["repo"], token)
        originals, blobs = {}, {}
        for path in paths:
            try:
                got = ghe_git.fetch_file(remote, studio["branch_name"], path)
            except ghe_git.UnsafePath:
                _log.warning("unsafe pass-1 경로 무시: %s", path)   # 임의 파일 읽기 차단
                continue
            if got:
                originals[path], blobs[path] = got
        return originals, blobs
    except Exception:
        return {}, {}


def parse_paths_block(text: str) -> list[str]:
    m = PATHS_BLOCK_RE.search(text)
    if not m:
        return []
    return [ln.strip() for ln in m.group("body").splitlines() if ln.strip()]


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


def _inject_requirements_and_fails(messages: list, studio, studio_id: str,
                                   base_files: dict[str, str] | None = None) -> list:
    blocks = []
    if studio and studio["requirements"]:
        blocks.append("확정 requirements (이 문서가 유일한 기준):\n"
                      + studio["requirements"])
    if base_files:
        parts = ["수정 대상 파일 원문 (전체 교체 시 아래를 기준으로):"]
        for path, content in base_files.items():
            parts.append(f"### {path}\n```\n{content}\n```")
        blocks.append("\n\n".join(parts))
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
