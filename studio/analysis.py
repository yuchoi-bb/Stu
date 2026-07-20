"""분석 md 매핑/로드/stale 검사 (§7.1 Step 1, §7.3).

- tool_analysis 테이블로 tool→md 경로 매핑.
- Step 1: 대상 tool의 분석 md를 GHE에서 fetch → cached_context로 주입.
- stale: md 상단 '기준 커밋: {SHA}' vs repo HEAD 비교 → 다르면 경고.
"""
import re

from . import config, db

BASE_SHA_RE = re.compile(r"기준 커밋:\s*([0-9a-f]{7,40})")


def set_mapping(tool_name: str, repo: str, md_path: str) -> None:
    db.execute(
        """INSERT INTO tool_analysis (tool_name, repo, md_path) VALUES (?,?,?)
           ON CONFLICT(tool_name) DO UPDATE SET repo=excluded.repo,
               md_path=excluded.md_path, updated_at=datetime('now')""",
        (tool_name, repo, md_path))


def get_mapping(tool_name: str):
    return db.one("SELECT * FROM tool_analysis WHERE tool_name=?", (tool_name,))


def load_for_session(session_id: str, user_id: str) -> tuple[str | None, str | None]:
    """Step 1: 세션의 tool_target 분석 md 로드 → (컨텍스트, stale경고).

    반환 컨텍스트는 invoke_claude(cached_context=...)로 전달.
    stale 경고는 대화에 주입할 안내 문자열 또는 None.
    """
    sess = db.one("SELECT tool_target FROM sessions WHERE session_id=?", (session_id,))
    if not sess or not sess["tool_target"]:
        return None, None
    mapping = get_mapping(sess["tool_target"])
    if mapping is None:
        return None, None

    content, head_sha = _fetch_md_and_head(user_id, mapping)
    if content is None:
        return None, f"[분석 md 로드 실패: {mapping['tool_name']} ({mapping['md_path']})]"

    warn = _stale_warning(content, head_sha)
    context = f"# {mapping['tool_name']} 분석 (기준 문서)\n{content}"
    return context, warn


def status_warning(session_id: str) -> str | None:
    """Step 2 사전 안내(§6.7): 분석 md가 로드될 수 없는 상태면 품질 저하 경고.

    - 대상 tool 미지정: 분석 md 없이 생성 → 품질 저하.
    - tool 지정됐으나 매핑 없음: 관리자 매핑 필요.
    정상(매핑 존재)이면 None. stale 경고는 생성 시점(load_for_session)에서 별도 처리.
    """
    sess = db.one("SELECT tool_target FROM sessions WHERE session_id=?", (session_id,))
    if not sess or not sess["tool_target"]:
        return "대상 tool 미지정 — 분석 md 없이 생성되어 코드 품질이 낮을 수 있습니다."
    if get_mapping(sess["tool_target"]) is None:
        return (f"tool '{sess['tool_target']}' 분석 md 미등록 — "
                "관리자에게 매핑 요청(품질 저하 가능).")
    return None


def _fetch_md_and_head(user_id: str, mapping):
    """GHE에서 분석 md 원문 + repo HEAD SHA 조회. GHE 미연결/오류 시 (None, None)."""
    try:
        from . import ghe
        token = ghe.get_token(user_id)
        content, _blob = _get_file(token, mapping["repo"], mapping["md_path"])
        head = _get_head_sha(token, mapping["repo"])
        return content, head
    except Exception:
        return None, None


def _get_file(token, repo, path):
    import requests
    r = requests.get(
        f"{config.GHE_API_URL}/repos/{config.GHE_OWNER}/{repo}/contents/{path}",
        headers={"Authorization": f"token {token}",
                 "Accept": "application/vnd.github.raw"}, timeout=15)
    if r.status_code != 200:
        return None, None
    return r.text, None


def _get_head_sha(token, repo, branch="main"):
    import requests
    r = requests.get(
        f"{config.GHE_API_URL}/repos/{config.GHE_OWNER}/{repo}/commits/{branch}",
        headers={"Authorization": f"token {token}"}, timeout=15)
    return r.json().get("sha") if r.status_code == 200 else None


def _stale_warning(content: str, head_sha: str | None) -> str | None:
    m = BASE_SHA_RE.search(content or "")
    if not m or not head_sha:
        return None
    base = m.group(1)
    if not head_sha.startswith(base) and not base.startswith(head_sha[:len(base)]):
        return (f"[분석 md 오래됨 경고] 기준 커밋 {base} ≠ 현재 HEAD {head_sha[:10]} "
                "— 생성 품질이 떨어질 수 있으니 분석 md 갱신을 권장합니다.")
    return None
