"""GHE 연동 (§3.3, §6): 사용자 본인 토큰(OAuth/PAT), push→dispatch→CI 추적, 취소 전파."""
import datetime as dt
import hashlib
import hmac
import random
import secrets
import threading
import time

import requests

from . import logs

_log = logs.get("ghe")


def _http_retry(fn, *, tries: int = 3, base: float = 2.0):
    """네트워크성 실패/5xx에 지수 백오프 재시도 (§11). 4xx는 즉시 반환."""
    delay = base
    last = None
    for attempt in range(tries):
        try:
            r = fn()
            if r.status_code < 500:
                return r
            last = RuntimeError(f"HTTP {r.status_code}")
        except requests.RequestException as e:
            last = e
        if attempt < tries - 1:
            time.sleep(delay + random.uniform(0, 0.5))
            delay = min(delay * 2, 16)
    if isinstance(last, Exception):
        raise last
    return r

from . import config, crypto, db, jobs
from .ghe_git import PushConflict, WorkflowGuardViolation, commit_and_push

_oauth_states: dict[str, str] = {}   # state -> user_id (단일 프로세스, worker 1)


class GheNotConnected(Exception):
    pass


# ---------- 토큰 (§3.3) ----------

def save_pat(user_id: str, token: str) -> str:
    """PAT 등록(폴백 2안): 유효성 검증 후 암호화 저장. 반환: ghe_login."""
    login = _verify_token(token)
    _store_token(user_id, token, auth_type="pat", login=login)
    return login


def oauth_start(user_id: str) -> str:
    """1안: 'GitHub 연결' → GHE 승인 페이지 URL."""
    state = secrets.token_urlsafe(24)
    _oauth_states[state] = user_id
    return (f"{config.GHE_BASE_URL}/login/oauth/authorize"
            f"?client_id={config.GHE_OAUTH_CLIENT_ID}"
            f"&redirect_uri={config.GHE_OAUTH_CALLBACK}"
            f"&scope=repo&state={state}")


def oauth_callback(state: str, code: str) -> str:
    user_id = _oauth_states.pop(state, None)
    if user_id is None:
        raise GheNotConnected("잘못된 OAuth state")
    r = requests.post(
        f"{config.GHE_BASE_URL}/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={"client_id": config.GHE_OAUTH_CLIENT_ID,
              "client_secret": config.GHE_OAUTH_CLIENT_SECRET,
              "code": code},
        timeout=15)
    r.raise_for_status()
    body = r.json()
    token = body["access_token"]
    login = _verify_token(token)
    _store_token(user_id, token, auth_type="oauth", login=login,
                 refresh_token=body.get("refresh_token"),
                 expires_in=body.get("expires_in"))
    return login


def _verify_token(token: str) -> str:
    r = requests.get(f"{config.GHE_API_URL}/user",
                     headers=_auth_headers(token), timeout=15)
    if r.status_code != 200:
        raise GheNotConnected(f"토큰 검증 실패 ({r.status_code})")
    return r.json()["login"]


def _store_token(user_id, token, *, auth_type, login,
                 refresh_token=None, expires_in=None) -> None:
    expires_at = None
    if expires_in:
        expires_at = (dt.datetime.now(dt.timezone.utc)
                      + dt.timedelta(seconds=int(expires_in))).isoformat()
    db.execute(
        """INSERT INTO ghe_credentials
               (user_id, auth_type, token_enc, refresh_token_enc, expires_at,
                verified_at, last_ok_at, updated_at)
           VALUES (?,?,?,?,?, datetime('now'), datetime('now'), datetime('now'))
           ON CONFLICT(user_id) DO UPDATE SET
               auth_type=excluded.auth_type, token_enc=excluded.token_enc,
               refresh_token_enc=excluded.refresh_token_enc,
               expires_at=excluded.expires_at,
               verified_at=datetime('now'), updated_at=datetime('now')""",
        (user_id, auth_type, crypto.encrypt(token),
         crypto.encrypt(refresh_token) if refresh_token else None, expires_at))
    db.execute("UPDATE users SET ghe_login=? WHERE user_id=?", (login, user_id))


def get_token(user_id: str) -> str:
    row = db.one("SELECT * FROM ghe_credentials WHERE user_id=?", (user_id,))
    if row is None or row["token_enc"] is None:
        raise GheNotConnected(user_id)
    if row["expires_at"]:
        exp = dt.datetime.fromisoformat(row["expires_at"])
        if exp <= dt.datetime.now(dt.timezone.utc):
            refreshed = _try_refresh(user_id, row)
            if refreshed is None:
                raise GheNotConnected("GHE 토큰 만료 — 재연결 필요")
            return refreshed
    return crypto.decrypt(row["token_enc"])


def _try_refresh(user_id: str, row) -> str | None:
    """OAuth refresh token으로 자동 갱신 (§3.3). 실패 시 None → 재연결 배너."""
    if row["auth_type"] != "oauth" or not row["refresh_token_enc"]:
        return None
    try:
        r = requests.post(
            f"{config.GHE_BASE_URL}/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={"client_id": config.GHE_OAUTH_CLIENT_ID,
                  "client_secret": config.GHE_OAUTH_CLIENT_SECRET,
                  "grant_type": "refresh_token",
                  "refresh_token": crypto.decrypt(row["refresh_token_enc"])},
            timeout=15)
        r.raise_for_status()
        body = r.json()
        _store_token(user_id, body["access_token"], auth_type="oauth",
                     login=db.one("SELECT ghe_login FROM users WHERE user_id=?",
                                  (user_id,))["ghe_login"],
                     refresh_token=body.get("refresh_token"),
                     expires_in=body.get("expires_in"))
        return body["access_token"]
    except Exception:
        return None


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"token {token}",
            "Accept": "application/vnd.github+json"}


def remote_url(repo: str, token: str) -> str:
    """git push용 https URL (§3.3: x-access-token 형식)."""
    host = config.GHE_BASE_URL.split("://", 1)[1]
    return f"https://x-access-token:{token}@{host}/{config.GHE_OWNER}/{repo}.git"


# ---------- Step 4: push → dispatch (§6.2, §6.3) ----------

def submit_push(build_id: int) -> None:
    jobs.submit(run_push, build_id)


def run_push(build_id: int) -> None:
    b = db.one(
        """SELECT b.*, s.session_id, s.user_id, s.repo, s.branch_name,
                  s.requirements, u.ghe_login, u.ad_id
           FROM builds b JOIN studios s ON s.studio_id=b.studio_id
           JOIN users u ON u.user_id=s.user_id WHERE b.build_id=?""",
        (build_id,))
    if b is None:
        return
    session_id, studio_id = b["session_id"], b["studio_id"]
    try:
        jobs.checkpoint(build_id)
        if not b["branch_name"]:
            jobs.set_build_status(build_id, "fail", completed=True,
                                  fail_summary="작업 브랜치 미설정 (설정에서 지정)")
            return
        token = get_token(b["user_id"])

        # D: 새 회차 dispatch 직전, 이전 회차가 ci_running이면 선제 cancelled 마킹 (§6.2)
        _cancel_superseded(studio_id, b["attempt"], b["user_id"], b["repo"])

        files = {r["path"]: r["content"] for r in db.query(
            "SELECT path, content FROM build_files WHERE build_id=?", (build_id,))}
        base_blobs = {r["path"]: r["base_blob_sha"] for r in db.query(
            "SELECT path, base_blob_sha FROM build_files WHERE build_id=?",
            (build_id,))}
        # base 폴백: 2-pass fetch 기록이 없으면 같은 studio의 직전 회차가 실제로
        # push한 git blob SHA를 base로 사용 — 원시 계산 대신 git 정규화까지 반영된
        # 실제 값이라 정규화 불일치로 인한 false push_conflict가 없다 (§6.5)
        for path in files:
            if base_blobs.get(path) is None:
                prev = db.one(
                    """SELECT bf.pushed_blob_sha FROM build_files bf
                       JOIN builds x ON x.build_id=bf.build_id
                       WHERE x.studio_id=? AND bf.path=? AND x.attempt < ?
                             AND bf.pushed_blob_sha IS NOT NULL
                       ORDER BY x.attempt DESC LIMIT 1""",
                    (studio_id, path, b["attempt"]))
                if prev:
                    base_blobs[path] = prev["pushed_blob_sha"]
        # 확정 requirements md 동반 커밋 (§6.3) — studio 소유 파일이라 가드 예외
        req_path = f"docs/studio/{studio_id}-requirements.md"
        files[req_path] = b["requirements"] or ""

        message = (f"[studio] id={studio_id} attempt={b['attempt']} "
                   f"session={session_id}\n\nToolHub Studio 생성 커밋")
        author = b["ghe_login"] or b["ad_id"] or b["user_id"]
        sha, pushed = commit_and_push(
            remote_url(b["repo"], token), b["branch_name"], files, base_blobs,
            author, f"{author}@users.noreply.{config.GHE_BASE_URL.split('://')[1]}",
            message, guard_exempt={req_path})
        db.execute("UPDATE builds SET commit_sha=? WHERE build_id=?",
                   (sha, build_id))
        for path, blob in pushed.items():   # 다음 회차 base로 쓸 실제 blob SHA 기록
            db.execute("UPDATE build_files SET pushed_blob_sha=? "
                       "WHERE build_id=? AND path=?", (blob, build_id, path))

        _dispatch(b, token)
        jobs.set_build_status(build_id, "ci_running")
        _log.info("pushed+dispatched studio=%s attempt=%s commit=%s",
                  studio_id, b["attempt"], sha[:10])
        from . import audit
        audit.record(b["user_id"], "push_dispatch",
                     f"{studio_id}#{b['attempt']}", "ci_running", sha[:10])
        run_id = _find_run_id(b, token)
        if run_id:
            db.execute("UPDATE builds SET run_id=? WHERE build_id=?",
                       (run_id, build_id))
    except WorkflowGuardViolation as e:
        jobs.set_build_status(build_id, "fail", completed=True,
                              fail_summary=f"workflow 파일 수정 금지 가드: {e}")
    except PushConflict as e:
        jobs.set_build_status(build_id, "push_conflict", completed=True,
                              fail_summary=str(e))
        _inject_message(session_id,
                        f"[push_conflict] 로컬 변경과 충돌 — {e}\n"
                        "브랜치 정리 후 재시도하거나 재생성(새 회차)을 지시하세요.")
    except GheNotConnected as e:
        jobs.set_build_status(build_id, "fail", completed=True,
                              fail_summary=f"GHE 재연결 필요: {e}")
    except jobs.Cancelled:
        raise
    except Exception as e:
        _log.exception("run_push failed build=%s studio=%s", build_id, studio_id)
        jobs.set_build_status(build_id, "fail", completed=True,
                              fail_summary=f"push error: {e}")


def _cancel_superseded(studio_id: str, attempt: int, user_id: str,
                       repo: str) -> None:
    rows = db.query(
        "SELECT build_id, run_id FROM builds "
        "WHERE studio_id=? AND attempt<? AND status='ci_running'",
        (studio_id, attempt))
    for r in rows:
        jobs.set_build_status(r["build_id"], "cancelled", completed=True)
        if r["run_id"]:
            try:
                cancel_run(user_id, repo, r["run_id"])
            except Exception:
                pass   # concurrency가 어차피 취소 — 시그널은 best effort


def _dispatch(b, token: str) -> None:
    """workflow_dispatch (§6.2). inputs: studio_id/attempt/user."""
    url = (f"{config.GHE_API_URL}/repos/{config.GHE_OWNER}/{b['repo']}"
           f"/actions/workflows/{config.VERIFY_WORKFLOW}/dispatches")
    body = {"ref": b["branch_name"],
            "inputs": {"studio_id": b["studio_id"],
                       "attempt": str(b["attempt"]),
                       "user": b["ghe_login"] or b["user_id"]}}
    r = _http_retry(lambda: requests.post(url, headers=_auth_headers(token),
                                          timeout=15, json=body))
    if r.status_code >= 300:
        raise RuntimeError(f"dispatch 실패 ({r.status_code}): {r.text[:200]}")


def _find_run_id(b, token: str, tries: int = 10) -> int | None:
    """run-name 'studio-{studio_id}#{attempt}' 매칭 폴링 (§6.2 run 추적)."""
    want = f"studio-{b['studio_id']}#{b['attempt']}"
    url = (f"{config.GHE_API_URL}/repos/{config.GHE_OWNER}/{b['repo']}"
           f"/actions/runs?event=workflow_dispatch&branch={b['branch_name']}")
    for _ in range(tries):
        try:
            r = requests.get(url, headers=_auth_headers(token), timeout=15)
            for run in r.json().get("workflow_runs", []):
                if run.get("name") == want or run.get("display_title") == want:
                    return run["id"]
        except Exception:
            pass
        time.sleep(3)
    return None


# ---------- 취소 전파 (§6.2) ----------

def cancel_run(user_id: str, repo: str, run_id: int) -> None:
    """stage로 취소 시그널 — 시그널 없이는 빌드/테스트가 멈추지 않는다."""
    token = get_token(user_id)
    r = _http_retry(lambda: requests.post(
        f"{config.GHE_API_URL}/repos/{config.GHE_OWNER}/{repo}"
        f"/actions/runs/{run_id}/cancel",
        headers=_auth_headers(token), timeout=15))
    if r.status_code >= 300:
        raise RuntimeError(f"run cancel 실패 ({r.status_code})")


def ensure_branch(user_id: str, repo: str, branch: str, base: str = "main") -> bool:
    """미존재 브랜치를 base에서 생성 (§6.1). 이미 있으면 False, 생성하면 True."""
    token = get_token(user_id)
    exists = requests.get(
        f"{config.GHE_API_URL}/repos/{config.GHE_OWNER}/{repo}/branches/{branch}",
        headers=_auth_headers(token), timeout=15)
    if exists.status_code == 200:
        return False
    base_ref = requests.get(
        f"{config.GHE_API_URL}/repos/{config.GHE_OWNER}/{repo}/git/refs/heads/{base}",
        headers=_auth_headers(token), timeout=15)
    if base_ref.status_code != 200:
        raise RuntimeError(f"base 브랜치 {base} 조회 실패")
    sha = base_ref.json()["object"]["sha"]
    r = _http_retry(lambda: requests.post(
        f"{config.GHE_API_URL}/repos/{config.GHE_OWNER}/{repo}/git/refs",
        headers=_auth_headers(token), timeout=15,
        json={"ref": f"refs/heads/{branch}", "sha": sha}))
    if r.status_code >= 300:
        raise RuntimeError(f"브랜치 생성 실패 ({r.status_code})")
    return True


# ---------- CI 결과 수신 (§6.2) ----------

def verify_ci_signature(raw_body: bytes, signature: str) -> bool:
    if not config.CI_WEBHOOK_SECRET:
        return False
    mac = hmac.new(config.CI_WEBHOOK_SECRET.encode(), raw_body,
                   hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + mac, signature or "")

def apply_ci_result(studio_id: str, attempt: int, status: str,
                    buildid: str | None, fail_summary: str | None) -> bool:
    """webhook/폴링 공용 — 멱등 처리 (§6.2)."""
    b = db.one("SELECT build_id, status FROM builds WHERE studio_id=? AND attempt=?",
               (studio_id, attempt))
    if b is None:
        return False
    if b["status"] in ("pass", "fail", "cancelled", "push_conflict"):
        return True   # 이미 종결 — 멱등
    if status not in ("pass", "fail", "cancelled"):
        return False
    # 원자적 전이: ci_running일 때만 UPDATE — webhook과 폴러가 동시에 처리해도
    # 정확히 한 번만 성공해 메시지 중복 주입을 막는다 (§6.2)
    conn = db.get_conn()
    cur = conn.execute(
        "UPDATE builds SET status=?, buildid=?, "
        "fail_summary=COALESCE(?, fail_summary), "
        "completed_at=datetime('now') "
        "WHERE build_id=? AND status='ci_running'",
        (status, buildid, fail_summary, b["build_id"]))
    conn.commit()
    if cur.rowcount != 1:
        return True   # 다른 경로가 먼저 종결 — 멱등, 중복 주입 안 함
    s = db.one("SELECT session_id FROM studios WHERE studio_id=?", (studio_id,))
    if s:
        text = (f"[CI] {studio_id} attempt {attempt}: {status}"
                + (f" (buildid {buildid})" if buildid else ""))
        if fail_summary:
            text += "\n실패 요약:\n" + fail_summary
        _inject_message(s["session_id"], text)   # CI 결과 자동 주입 (§7.1 Step 5)
    return True


def _inject_message(session_id: str, text: str) -> None:
    db.execute("INSERT INTO messages (session_id, role, content) VALUES (?,?,?)",
               (session_id, "system", text))


# ---------- webhook 유실 대비 폴링 fallback (§6.2) ----------

def _poll_loop() -> None:
    while True:
        try:
            rows = db.query(
                """SELECT b.build_id, b.studio_id, b.attempt, b.run_id,
                          s.repo, s.user_id
                   FROM builds b JOIN studios s ON s.studio_id=b.studio_id
                   WHERE b.status='ci_running' AND b.run_id IS NOT NULL""")
            for r in rows:
                try:
                    token = get_token(r["user_id"])
                    resp = requests.get(
                        f"{config.GHE_API_URL}/repos/{config.GHE_OWNER}"
                        f"/{r['repo']}/actions/runs/{r['run_id']}",
                        headers=_auth_headers(token), timeout=15).json()
                    if resp.get("status") == "completed":
                        concl = resp.get("conclusion")
                        status = {"success": "pass", "cancelled": "cancelled"}.get(
                            concl, "fail")
                        if apply_ci_result(r["studio_id"], r["attempt"], status,
                                           None, None if status == "pass"
                                           else f"CI conclusion: {concl}"):
                            _log.info("ci poll resolved studio=%s attempt=%s -> %s",
                                      r["studio_id"], r["attempt"], status)
                except Exception:
                    _log.warning("ci poll error run=%s", r["run_id"])
        except Exception:
            _log.exception("ci poll loop error")
        time.sleep(config.CI_POLL_INTERVAL_SEC)


def start_ci_poller() -> None:
    threading.Thread(target=_poll_loop, daemon=True, name="ci-poll").start()
