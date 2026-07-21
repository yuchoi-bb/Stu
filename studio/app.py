"""Studio Flask 앱 (Phase 1 코어).

- 인증: Apache 레이어의 Knox SSO가 REMOTE_USER 헤더 전달 (§3.1) — 헤더만 신뢰
- 실행: gunicorn gthread, worker 1 × threads 16 고정 (§4.3), localhost 바인딩
- 상태: 모든 공유 상태는 DB 경유 (builds.cancel_requested 등)
"""
import os
import threading
import uuid
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, request
from werkzeug.exceptions import HTTPException

from . import config, crypto, db, jobs, logs, prompts
from .aws_sso import poll_device_flow, start_device_flow, start_refresh_scheduler

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = config.ATTACH_MAX_BYTES


@app.errorhandler(HTTPException)
def _json_error(e):
    """API 오류를 HTML 페이지가 아닌 JSON으로 반환 — UI가 깔끔한 메시지를 띄운다."""
    return jsonify({"error": e.description, "code": e.code}), e.code or 500

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


@app.before_request
def _csrf_guard():
    """CSRF 방지 (§3.1): 인증이 Apache/Knox SSO 쿠키 기반이라, 상태 변경 요청은
    cross-origin에서 브라우저가 쿠키를 붙여 위조될 수 있다. Origin이 있고 호스트가
    다르면 차단한다. Origin이 없는 요청(ci-callback 등 서버-서버)은 통과 —
    해당 경로는 HMAC로 별도 보호된다(§6.2). (Apache는 ProxyPreserveHost On 전제)"""
    if request.method not in _MUTATING:
        return
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc != request.host:
        abort(403, "cross-origin 요청 차단 (CSRF 방지)")

# 새 회차 제출 임계 구역 보호 (worker 1 단일 프로세스이므로 프로세스 락으로 충분).
# 동시성 한도 체크→build INSERT, draft 승인 전이가 원자적으로 직렬화되어
# TOCTOU 경합(동시 2건 생성·이중 승인)을 막는다.
_submit_lock = threading.Lock()


def _init() -> None:
    logs.setup()
    db.init_db()
    crypto.ensure_key()
    prompts.seed()
    os.makedirs(config.ATTACH_DIR, exist_ok=True)
    jobs.mark_interrupted_builds_failed()
    start_refresh_scheduler()
    from .ghe import start_ci_poller
    start_ci_poller()


_init()


def current_user() -> str:
    """Apache가 넣어주는 REMOTE_USER 헤더 (§3.1). 최초 로그인 시 users 자동 등록."""
    user_id = (request.environ.get("HTTP_X_REMOTE_USER") or request.remote_user
               or os.environ.get("STUDIO_DEV_USER"))   # 개발/테스트 전용 폴백
    if not user_id:
        abort(401, "REMOTE_USER missing — Apache SSO 경유 필요")
    db.execute("INSERT OR IGNORE INTO users (user_id, ad_id) VALUES (?,?)",
               (user_id, user_id))
    return user_id


# ---------- 연결 (AWS SSO device flow, §3.2) ----------

@app.post("/api/studio/aws/connect")
def aws_connect():
    return jsonify(start_device_flow(current_user()))


@app.get("/api/studio/aws/status")
def aws_status():
    return jsonify(poll_device_flow(current_user()))


# ---------- 연결 (GHE OAuth/PAT, §3.3) ----------

@app.post("/api/studio/ghe/pat")
def ghe_pat():
    from . import ghe
    try:
        login = ghe.save_pat(current_user(), request.get_json(force=True)["token"])
    except ghe.GheNotConnected as e:
        abort(400, str(e))
    return jsonify({"ghe_login": login})


@app.post("/api/studio/ghe/oauth/start")
def ghe_oauth_start():
    from . import ghe
    return jsonify({"authorize_url": ghe.oauth_start(current_user())})


@app.get("/api/studio/ghe/oauth/callback")
def ghe_oauth_callback():
    from . import ghe
    try:
        login = ghe.oauth_callback(request.args.get("state", ""),
                                   request.args.get("code", ""))
    except Exception as e:
        abort(400, f"GHE OAuth 실패: {e}")
    return f"GitHub 연결 완료 ({login}) — 이 창을 닫아도 됩니다."


@app.get("/api/studio/connections")
def connections():
    user_id = current_user()
    aws = db.one("SELECT expires_at FROM aws_credentials WHERE user_id=?", (user_id,))
    ghe_row = db.one("SELECT auth_type, verified_at FROM ghe_credentials "
                     "WHERE user_id=?", (user_id,))
    user = db.one("SELECT ghe_login, auto_approve, is_admin FROM users "
                  "WHERE user_id=?", (user_id,))
    branch = db.one("SELECT repo, branch_name FROM user_branch_config "
                    "WHERE user_id=?", (user_id,))
    is_admin = bool(user["is_admin"]) if user else False
    resp = {
        "user_id": user_id,
        "aws_connected": bool(aws and aws["expires_at"]),
        "ghe_connected": bool(ghe_row),
        "ghe_auth_type": ghe_row["auth_type"] if ghe_row else None,
        "ghe_login": user["ghe_login"] if user else None,
        "auto_approve": bool(user["auto_approve"]) if user else False,
        "is_admin": is_admin,
        "branch": dict(branch) if branch else None,
    }
    if is_admin:   # 2인 체계 권장 경고 노출 (관리자에게만)
        from . import metrics
        n = metrics.admin_count()
        resp["admin_count"] = n
        resp["low_admin_warning"] = n < 2
    return jsonify(resp)


# ---------- 브랜치 설정 (§6.1) ----------

@app.post("/api/studio/branch")
def set_branch():
    """가드: 보호 브랜치 지정 불가(GHE API 확인) + 타 사용자 중복 지정 불가(DB)."""
    from . import ghe
    import sqlite3

    user_id = current_user()
    body = request.get_json(force=True)
    repo = body.get("repo") or config.DEFAULT_REPO
    branch = body["branch_name"]
    if branch in ("main", "master"):
        abort(400, "main/보호 브랜치는 대상 지정 불가")
    try:
        token = ghe.get_token(user_id)
        import requests as _rq
        r = _rq.get(f"{config.GHE_API_URL}/repos/{config.GHE_OWNER}/{repo}"
                    f"/branches/{branch}", headers={"Authorization": f"token {token}"},
                    timeout=15)
        if r.status_code == 200 and r.json().get("protected"):
            abort(400, "보호 브랜치는 대상 지정 불가")
    except ghe.GheNotConnected:
        abort(409, "GHE 연결 필요")
    # DB 예약을 먼저 — 중복 지정이면 여기서 거부되어 원격 브랜치를 만들지 않는다
    # (부작용이 가드보다 먼저 실행돼 고아 브랜치가 남는 문제 방지)
    try:
        db.execute(
            """INSERT INTO user_branch_config (user_id, repo, branch_name)
               VALUES (?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET repo=excluded.repo,
                   branch_name=excluded.branch_name, updated_at=datetime('now')""",
            (user_id, repo, branch))
    except sqlite3.IntegrityError:
        abort(409, "다른 사용자가 이미 지정한 브랜치입니다")
    created = False
    if body.get("create_if_missing"):
        try:
            created = ghe.ensure_branch(user_id, repo, branch,
                                        body.get("base", "main"))
        except Exception as e:
            abort(400, f"브랜치 생성 실패: {e}")
    return jsonify({"repo": repo, "branch_name": branch, "created": created})


# ---------- 분석 md 매핑 (관리자, §7.1 Step 1) ----------

@app.get("/api/studio/admin/tool-analysis")
def list_tool_analysis():
    _require_admin()
    rows = db.query("SELECT * FROM tool_analysis ORDER BY tool_name")
    return jsonify([dict(r) for r in rows])


@app.post("/api/studio/admin/tool-analysis")
def set_tool_analysis():
    _require_admin()
    from . import analysis
    body = request.get_json(force=True)
    analysis.set_mapping(body["tool_name"], body.get("repo") or config.DEFAULT_REPO,
                         body["md_path"])
    return jsonify({"ok": True}), 201


# ---------- CI 결과 수신 (§6.2) ----------

@app.post("/api/studio/ci-callback")
def ci_callback():
    from . import ghe
    if not ghe.verify_ci_signature(request.get_data(),
                                   request.headers.get("X-Studio-Signature", "")):
        abort(403, "서명 검증 실패")
    body = request.get_json(force=True)
    ok = ghe.apply_ci_result(body["studio_id"], int(body["attempt"]),
                             body["status"], body.get("buildid"),
                             body.get("fail_summary"))
    if not ok:
        abort(400, "적용 불가 (studio/attempt 불일치 또는 잘못된 status)")
    from . import audit
    audit.record(None, "ci_result",
                 f"{body['studio_id']}#{body['attempt']}", body["status"])
    return jsonify({"ok": True})


# ---------- health / 디버그 조회 (운영·모니터링) ----------

@app.get("/api/studio/health")
def health():
    """DB·스케줄러·디스크 점검. 모니터링/로드밸런서용 (인증 불필요)."""
    import shutil
    checks = {}
    try:
        db.one("SELECT 1")
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"error: {e}"
    alive = {t.name for t in threading.enumerate()}
    checks["refresh_scheduler"] = "ok" if "aws-refresh" in alive else "down"
    checks["ci_poller"] = "ok" if "ci-poll" in alive else "down"
    try:
        free = shutil.disk_usage(config.BASE_DIR).free
        checks["disk_free_mb"] = free // (1024 * 1024)
        checks["disk"] = "ok" if free > 100 * 1024 * 1024 else "low"
    except Exception as e:
        checks["disk"] = f"error: {e}"
    healthy = checks["db"] == "ok" and checks.get("disk") != "low"
    return jsonify({"status": "ok" if healthy else "degraded", "checks": checks}), \
        (200 if healthy else 503)


@app.get("/api/studio/admin/builds/<int:build_id>")
def admin_build_detail(build_id):
    """build 전체 상세 — 타임라인·파일·usage·에러 추적 (관리자 디버그)."""
    _require_admin()
    b = db.one("SELECT * FROM builds WHERE build_id=?", (build_id,))
    if b is None:
        abort(404)
    files = db.query("SELECT path, line_count, shrink_warn, base_blob_sha, "
                     "pushed_blob_sha FROM build_files WHERE build_id=?", (build_id,))
    usage = db.query("SELECT input_tokens, output_tokens, cache_read_tokens, "
                     "model_id, created_at FROM usage_log WHERE build_id=?",
                     (build_id,))
    return jsonify({"build": dict(b),
                    "files": [dict(f) for f in files],
                    "usage": [dict(u) for u in usage]})


@app.get("/api/studio/admin/failures")
def admin_failures():
    """최근 실패/충돌 회차 목록 — 왜 실패했나 한눈에 (관리자 디버그)."""
    _require_admin()
    rows = db.query(
        """SELECT b.build_id, b.studio_id, b.attempt, b.status, b.fail_summary,
                  b.completed_at, s.user_id, s.repo, s.branch_name
           FROM builds b JOIN studios s ON s.studio_id=b.studio_id
           WHERE b.status IN ('fail','push_conflict')
           ORDER BY b.completed_at DESC LIMIT 100""")
    return jsonify([dict(r) for r in rows])


@app.get("/api/studio/admin/audit")
def admin_audit():
    """행위 로그 조회 (§3.1.1, 관리자)."""
    _require_admin()
    from . import audit
    return jsonify(audit.recent(int(request.args.get("limit", 200)),
                                request.args.get("user_id")))


# ---------- 세션 ----------

@app.get("/api/studio/sessions")
def list_sessions():
    rows = db.query(
        """SELECT s.session_id, s.title, s.tool_target, s.updated_at,
                  (SELECT st.status FROM studios st
                   WHERE st.session_id = s.session_id
                   ORDER BY st.created_at DESC LIMIT 1) AS latest_studio_status
           FROM sessions s WHERE s.user_id=? ORDER BY s.updated_at DESC""",
        (current_user(),))
    return jsonify([dict(r) for r in rows])


@app.post("/api/studio/sessions")
def create_session():
    body = request.get_json(force=True)
    session_id = f"S-{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO sessions (session_id, user_id, title, tool_target) "
        "VALUES (?,?,?,?)",
        (session_id, current_user(), body.get("title", "새 작업"),
         body.get("tool_target")))
    return jsonify({"session_id": session_id}), 201


@app.get("/api/studio/sessions/<session_id>")
def session_detail(session_id):
    """UI 구동용: 세션 + 열린 studio + 최신 draft를 한 번에."""
    _own_session(session_id)
    session = db.one("SELECT * FROM sessions WHERE session_id=?", (session_id,))
    studio = db.one(
        "SELECT * FROM studios WHERE session_id=? AND status='open' "
        "ORDER BY created_at DESC LIMIT 1", (session_id,))
    draft = db.one(
        "SELECT * FROM requirement_drafts WHERE session_id=? "
        "AND status IN ('generating','ready','failed') "
        "ORDER BY draft_id DESC LIMIT 1", (session_id,))
    builds = []
    can_pr = False
    if studio:
        builds = [dict(b) for b in db.query(
            "SELECT build_id, attempt, status, buildid, run_id, fail_summary, "
            "created_at, completed_at FROM builds WHERE studio_id=? ORDER BY attempt",
            (studio["studio_id"],))]
        can_pr = _can_pr(studio, builds)
    attachments = [dict(a) for a in db.query(
        "SELECT attachment_id, filename FROM attachments "
        "WHERE session_id=? ORDER BY attachment_id", (session_id,))]
    from . import analysis
    return jsonify({"session": dict(session),
                    "studio": dict(studio) if studio else None,
                    "builds": builds,
                    "can_pr": can_pr,
                    "attachments": attachments,
                    # ① 회차 소프트 캡 경고, ② 분석 md 미매핑 경고 (§6.7)
                    "attempt_warn": len(builds) >= config.MAX_ATTEMPTS_SOFT,
                    "analysis_warn": analysis.status_warning(session_id),
                    "draft": dict(draft) if draft else None})


@app.get("/api/studio/tools")
def list_tools():
    """세션 생성 시 대상 tool 선택용 — 등록된 분석 md 매핑 목록."""
    current_user()
    return jsonify([dict(r) for r in db.query(
        "SELECT tool_name, repo FROM tool_analysis ORDER BY tool_name")])


@app.get("/studio")
def studio_page():
    from flask import send_from_directory
    return send_from_directory(app.static_folder, "studio.html")


@app.get("/api/studio/sessions/<session_id>/messages")
def list_messages(session_id):
    _own_session(session_id)
    rows = db.query(
        "SELECT role, content, created_at FROM messages "
        "WHERE session_id=? ORDER BY message_id", (session_id,))
    return jsonify([dict(r) for r in rows])


# ---------- Step 2: requirements 확정 게이트 (§7.1) ----------

@app.post("/api/studio/requirements/draft")
def create_requirements_draft():
    """요구조건 입력 → Claude가 초안 생성. 승인 전에는 코딩 단계 진입 불가."""
    user_id = current_user()
    body = request.get_json(force=True)
    session_id = body["session_id"]
    _own_session(session_id)

    db.execute("INSERT INTO messages (session_id, role, content) VALUES (?,?,?)",
               (session_id, "user", body["content"]))
    db.execute("UPDATE requirement_drafts SET status='superseded' "
               "WHERE session_id=? AND status IN ('generating','ready')",
               (session_id,))
    draft_id = db.execute(
        "INSERT INTO requirement_drafts (session_id) VALUES (?)", (session_id,))

    from .pipeline import run_requirements_draft
    jobs.submit(run_requirements_draft, user_id, session_id, draft_id)
    return jsonify({"draft_id": draft_id}), 202


@app.get("/api/studio/requirements/<int:draft_id>")
def get_requirements_draft(draft_id):
    row = db.one(
        """SELECT d.* FROM requirement_drafts d
           JOIN sessions s ON s.session_id = d.session_id
           WHERE d.draft_id=? AND s.user_id=?""", (draft_id, current_user()))
    if row is None:
        abort(404)
    return jsonify(dict(row))


@app.post("/api/studio/requirements/<int:draft_id>/approve")
def approve_requirements(draft_id):
    """승인 → studio_id 발급 + 첫 회차 생성 시작.
    같은 세션에 open studio가 있으면 abandoned 처리 (E: 재확정 = 새 studio_id)."""
    user_id = current_user()
    body = request.get_json(force=True) or {}
    draft = db.one(
        """SELECT d.* FROM requirement_drafts d
           JOIN sessions s ON s.session_id = d.session_id
           WHERE d.draft_id=? AND s.user_id=?""", (draft_id, user_id))
    if draft is None:
        abort(404)
    if draft["status"] != "ready":
        abort(409, f"승인 불가 상태: {draft['status']}")

    session_id = draft["session_id"]
    requirements = body.get("content") or draft["content"]   # 사용자 수정본 우선

    with _submit_lock:
        if jobs.active_count_for_user(user_id) >= config.MAX_CONCURRENT_PER_USER:
            abort(409, "동시 진행 작업 한도 초과 (사용자당 1건)")
        # 원자적 승인 전이 — 동시 이중 승인 시 한 번만 통과 (ready→approved)
        conn = db.get_conn()
        cur = conn.execute(
            "UPDATE requirement_drafts SET status='approved', content=? "
            "WHERE draft_id=? AND status='ready'", (requirements, draft_id))
        conn.commit()
        if cur.rowcount != 1:
            abort(409, "이미 처리된 draft")
        # 재확정(E): 기존 open studio를 abandoned 처리하기 전에, 그 진행 중 회차와
        # stage CI를 취소한다 (§6.2 — 죽은 studio의 고아 CI 방지).
        from . import ghe
        for old in db.query("SELECT studio_id, repo FROM studios "
                            "WHERE session_id=? AND status='open'", (session_id,)):
            ghe.cancel_studio_inflight(old["studio_id"], user_id, old["repo"])
        db.execute("UPDATE studios SET status='abandoned', "
                   "completed_at=datetime('now') "
                   "WHERE session_id=? AND status='open'", (session_id,))
        branch = db.one("SELECT repo, branch_name FROM user_branch_config "
                        "WHERE user_id=?", (user_id,))
        studio_id = f"ST-{uuid.uuid4().hex[:8]}"
        db.execute(
            "INSERT INTO studios (studio_id, session_id, user_id, repo, branch_name, "
            "requirements) VALUES (?,?,?,?,?,?)",
            (studio_id, session_id, user_id,
             branch["repo"] if branch else config.DEFAULT_REPO,
             branch["branch_name"] if branch else None, requirements))
        build_id = db.execute("INSERT INTO builds (studio_id, attempt) VALUES (?,1)",
                              (studio_id,))

    from . import audit
    audit.record(user_id, "requirements_approve", studio_id, "ok")
    from .pipeline import run_generation
    jobs.submit(run_generation, user_id, session_id, studio_id, build_id)
    return jsonify({"studio_id": studio_id, "build_id": build_id, "attempt": 1}), 201


# ---------- Step 3 반복 (§4.3, §7.1 Step 5 루프) ----------

@app.post("/api/studio/message")
def post_message():
    """기존 studio에 새 회차 지시 (studio_id 필수 — 신규는 requirements 승인 경유)."""
    user_id = current_user()
    body = request.get_json(force=True)
    session_id = body["session_id"]
    _own_session(session_id)

    studio_id = body.get("studio_id")
    if not studio_id:
        abort(400, "studio_id 필요 — 신규 작업은 requirements 승인(Step 2)을 거칠 것")
    studio = db.one("SELECT status, user_id FROM studios WHERE studio_id=?",
                    (studio_id,))
    if studio is None or studio["user_id"] != user_id:
        abort(404)
    if studio["status"] != "open":
        abort(409, f"studio가 {studio['status']} 상태 — 재확정(Step 2)으로 새 작업 시작")

    # 새 회차가 이전 회차를 대체: 리뷰 대기는 cancelled, 생성 중은 취소 플래그 (§6.2 D)
    for prev in db.query(
            "SELECT build_id, status FROM builds WHERE studio_id=? "
            "AND status IN ('generating','awaiting_review')", (studio_id,)):
        if prev["status"] == "awaiting_review":
            jobs.set_build_status(prev["build_id"], "cancelled", completed=True)
        else:
            jobs.request_cancel(prev["build_id"])

    with _submit_lock:
        if jobs.active_count_for_user(user_id) >= config.MAX_CONCURRENT_PER_USER:
            abort(409, "동시 진행 작업 한도 초과 (사용자당 1건)")
        db.execute("INSERT INTO messages (session_id, role, content) VALUES (?,?,?)",
                   (session_id, "user", body["content"]))
        row = db.one("SELECT MAX(attempt) AS a FROM builds WHERE studio_id=?",
                     (studio_id,))
        attempt = (row["a"] or 0) + 1
        build_id = db.execute("INSERT INTO builds (studio_id, attempt) VALUES (?,?)",
                              (studio_id, attempt))

    from . import metrics
    from .pipeline import run_generation
    jobs.submit(metrics.maybe_summarize, user_id, session_id)  # §4.2, 베스트에포트
    jobs.submit(run_generation, user_id, session_id, studio_id, build_id)
    return jsonify({"studio_id": studio_id, "build_id": build_id,
                    "attempt": attempt}), 202


@app.get("/api/studio/status/<studio_id>")
def studio_status(studio_id):
    studio = db.one("SELECT * FROM studios WHERE studio_id=?", (studio_id,))
    if studio is None or studio["user_id"] != current_user():
        abort(404)
    builds = db.query(
        "SELECT build_id, attempt, status, buildid, run_id, fail_summary, "
        "created_at, completed_at FROM builds WHERE studio_id=? ORDER BY attempt",
        (studio_id,))
    can_pr = _can_pr(studio, builds)
    return jsonify({"studio": dict(studio), "builds": [dict(b) for b in builds],
                    "can_pr": can_pr})


# ---------- Step 3.5: 코드 리뷰 게이트 (§7.1, §6.4 주 방어선) ----------

@app.get("/api/studio/builds/<int:build_id>/files")
def build_files(build_id):
    b = _own_build(build_id)
    rows = db.query(
        "SELECT path, content, line_count, shrink_warn, base_content FROM build_files "
        "WHERE build_id=? ORDER BY path", (build_id,))
    # 위험 패턴 정적 검사 결과(§12.4)를 파일별로 붙여 리뷰 카드에 노출
    import json as _json
    by_path = {}
    try:
        for f in _json.loads(b["scan_findings"] or "[]"):
            by_path.setdefault(f["path"], []).append(f)
    except (ValueError, TypeError):
        pass
    out = []
    for r in rows:
        d = dict(r)
        d["scan"] = by_path.get(r["path"], [])
        out.append(d)
    return jsonify(out)


@app.post("/api/studio/builds/<int:build_id>/review")
def review_build(build_id):
    """승인 → push 단계 진입 / 거부 → fail(사유 기록, 다음 회차 컨텍스트로 주입)."""
    row = _own_build(build_id)
    if row["status"] != "awaiting_review":
        abort(409, f"리뷰 대기 상태가 아님: {row['status']}")
    body = request.get_json(force=True)
    action = body.get("action")
    from . import audit
    if action == "approve":
        jobs.set_build_status(build_id, "pushing")
        audit.record(current_user(), "review_approve", f"build={build_id}", "ok")
        from .ghe import submit_push
        submit_push(build_id)
        return jsonify({"status": "pushing"})
    if action == "reject":
        reason = body.get("reason", "작업자 리뷰 거부")
        jobs.set_build_status(build_id, "fail", completed=True,
                              fail_summary=f"리뷰 거부: {reason}")
        audit.record(current_user(), "review_reject", f"build={build_id}",
                     "rejected", reason)
        return jsonify({"status": "rejected"})
    abort(400, "action은 approve 또는 reject")


@app.post("/api/studio/settings/approval-mode")
def set_approval_mode():
    """Step 3.5 승인 모드: 매번 확인(기본) / 자동 승인 (§6.4 주의 문구는 UI 담당)."""
    body = request.get_json(force=True)
    db.execute("UPDATE users SET auto_approve=? WHERE user_id=?",
               (1 if body.get("auto_approve") else 0, current_user()))
    return jsonify({"auto_approve": bool(body.get("auto_approve"))})


@app.post("/api/studio/studios/<studio_id>/close")
def close_studio(studio_id):
    """studio 종결: done(채택) 또는 abandoned(포기). 진행 중 회차는 취소(§6.2)."""
    row = db.one("SELECT user_id, status, repo FROM studios WHERE studio_id=?",
                 (studio_id,))
    if row is None or row["user_id"] != current_user():
        abort(404)
    status = (request.get_json(force=True) or {}).get("status", "done")
    if status not in ("done", "abandoned"):
        abort(400)
    from . import ghe
    cancelled = ghe.cancel_studio_inflight(studio_id, row["user_id"], row["repo"])
    db.execute("UPDATE studios SET status=?, completed_at=datetime('now') "
               "WHERE studio_id=?", (status, studio_id))
    return jsonify({"status": status, "cancelled_builds": cancelled})


@app.post("/api/studio/studios/<studio_id>/create-pr")
def create_pr(studio_id):
    """§6.6 안 B: CI 통과 후 본인 명의 PR 생성. main 반영은 사람 리뷰 필수 —
    자동 merge는 하지 않는다. 이미 열린 PR이 있으면 그것을 반환(멱등)."""
    s = db.one("SELECT * FROM studios WHERE studio_id=?", (studio_id,))
    if s is None or s["user_id"] != current_user():
        abort(404)
    if not s["branch_name"]:
        abort(409, "작업 브랜치 미설정")
    base = config.GHE_DEFAULT_BASE_BRANCH
    if s["branch_name"] == base:
        abort(409, f"작업 브랜치가 base({base})와 동일 — PR 대상 아님")
    passed = db.one(
        "SELECT attempt, commit_sha FROM builds "
        "WHERE studio_id=? AND status='pass' ORDER BY attempt DESC LIMIT 1",
        (studio_id,))
    if passed is None:
        abort(409, "CI 통과 회차가 없음 — 검증 통과 후 PR 생성 가능")
    body_in = request.get_json(silent=True) or {}
    req = (s["requirements"] or "").strip()
    title = body_in.get("title") or (
        req.splitlines()[0][:72] if req else f"[Studio] {studio_id}")
    pr_body = body_in.get("body") or (
        f"ToolHub Studio 생성 (studio `{studio_id}`, attempt {passed['attempt']}, "
        f"commit `{(passed['commit_sha'] or '')[:10]}`).\n\n"
        f"## 확정 요구조건\n{req}\n\n"
        f"> main 반영은 사람 리뷰 후 수동 merge (자동 merge 금지, §6.6)")
    from . import ghe, audit
    try:
        number, url, existing = ghe.create_pull_request(
            s["user_id"], s["repo"], s["branch_name"], base, title, pr_body)
    except ghe.GheNotConnected as e:
        abort(409, f"GHE 재연결 필요: {e}")
    except ghe.GheError as e:
        # 422 = 반영할 변경 없음/이미 처리됨 등 사용자 대응 가능 상황 → 409
        if getattr(e, "status", None) == 422:
            abort(409, "PR 생성 불가: 브랜치에 main 대비 반영할 변경이 없거나 "
                       "이미 처리된 상태입니다.")
        abort(502, str(e))
    db.execute("UPDATE studios SET pr_number=?, pr_url=? WHERE studio_id=?",
               (number, url, studio_id))
    audit.record(current_user(), "create_pr", studio_id,
                 "existing" if existing else "created", url)
    return jsonify({"pr_number": number, "pr_url": url, "existing": existing})


def _can_pr(studio, builds) -> bool:
    """§6.6 안 B: CI 통과 회차가 있고 아직 PR이 없으면 PR 생성 버튼 노출 조건."""
    return bool(studio and any(b["status"] == "pass" for b in builds)
                and studio["branch_name"]
                and studio["branch_name"] != config.GHE_DEFAULT_BASE_BRANCH
                and not studio["pr_url"])


def _own_build(build_id: int):
    row = db.one(
        """SELECT b.*, s.user_id FROM builds b
           JOIN studios s ON s.studio_id=b.studio_id WHERE b.build_id=?""",
        (build_id,))
    if row is None or row["user_id"] != current_user():
        abort(404)
    return row


@app.post("/api/studio/builds/<int:build_id>/cancel")
def cancel_build(build_id):
    row = db.one(
        """SELECT b.status, b.run_id, s.user_id, s.repo, s.studio_id
           FROM builds b JOIN studios s ON s.studio_id=b.studio_id
           WHERE b.build_id=?""",
        (build_id,))
    if row is None or row["user_id"] != current_user():
        abort(404)
    jobs.request_cancel(build_id)
    if row["status"] == "ci_running" and row["run_id"]:
        # 취소 전파 (§6.2): stage는 시그널 없이는 멈추지 않는다
        from . import ghe
        try:
            ghe.cancel_run(row["user_id"], row["repo"], row["run_id"])
        except Exception:
            logs.get("app").warning("cancel signal failed run=%s", row["run_id"])
        jobs.set_build_status(build_id, "cancelled", completed=True)
    from . import audit
    audit.record(current_user(), "cancel", f"build={build_id}", "ok")
    return jsonify({"status": "cancel_requested"})


# ---------- 관리자: 미터링/지표 (§10, is_admin 한정) ----------

@app.get("/api/studio/admin/metrics")
def admin_metrics():
    _require_admin()
    from . import metrics
    return jsonify({"overall": metrics.overall_metrics(),
                    "users": metrics.user_metrics()})


@app.get("/api/studio/admin/branches")
def admin_branches():
    """관리자 브랜치 조회 (§6.1): 요청 이력 브랜치 + 최근 상태. 자동 삭제 없음."""
    _require_admin()
    rows = db.query(
        """SELECT s.repo, s.branch_name, s.user_id,
                  MAX(s.created_at) AS last_studio_at,
                  COUNT(DISTINCT s.studio_id) AS studios,
                  (SELECT b.status FROM builds b
                   JOIN studios s2 ON s2.studio_id=b.studio_id
                   WHERE s2.branch_name = s.branch_name
                   ORDER BY b.created_at DESC LIMIT 1) AS last_build_status
           FROM studios s WHERE s.branch_name IS NOT NULL
           GROUP BY s.repo, s.branch_name, s.user_id
           ORDER BY last_studio_at DESC""")
    return jsonify([dict(r) for r in rows])


def _require_admin():
    row = db.one("SELECT is_admin FROM users WHERE user_id=?", (current_user(),))
    if not row or not row["is_admin"]:
        abort(403, "관리자 전용")


# ---------- 관리자 롤 (2인 체계 권장, §12.1) ----------

@app.get("/api/studio/admin/admins")
def list_admins():
    """현재 관리자 목록 + 2인 권장 경고 플래그."""
    _require_admin()
    rows = db.query("SELECT user_id, ghe_login, display_name FROM users "
                    "WHERE is_admin=1 ORDER BY user_id")
    return jsonify({"admins": [dict(r) for r in rows], "count": len(rows),
                    "recommend": 2, "low": len(rows) < 2})


@app.post("/api/studio/admin/admins")
def set_admin_role():
    """관리자 승격/강등 (관리자만). 마지막 관리자 강등은 금지 — 0명 락아웃 방지.
    관리자 권한은 조회 + 운영쓰기(analysis 매핑/관리자 지정)까지이며, 타 사용자
    브랜치·작업은 건드리지 않는다(소유권 존중)."""
    _require_admin()
    body = request.get_json(force=True)
    target = (body.get("user_id") or "").strip()
    make_admin = bool(body.get("is_admin"))
    if not target:
        abort(400, "user_id 필요")
    from . import audit, metrics
    # 마지막 관리자 강등 가드는 count 확인과 UPDATE가 원자적이어야 한다 —
    # 서로 다른 관리자를 동시에 강등하는 경합(둘 다 count>1 통과 → 0명)을 막기 위해
    # 직렬화한다(_submit_lock 재사용).
    with _submit_lock:
        row = db.one("SELECT is_admin FROM users WHERE user_id=?", (target,))
        if row is None:
            abort(404, "해당 사용자가 아직 로그인한 적 없음 (users 미등록)")
        if not make_admin and row["is_admin"] and metrics.admin_count() <= 1:
            abort(409, "마지막 관리자는 강등할 수 없습니다 (최소 1인 유지)")
        db.execute("UPDATE users SET is_admin=? WHERE user_id=?",
                   (1 if make_admin else 0, target))
        count = metrics.admin_count()
    audit.record(current_user(),
                 "grant_admin" if make_admin else "revoke_admin", target, "ok")
    return jsonify({"user_id": target, "is_admin": make_admin,
                    "admin_count": count})


# ---------- 첨부 (§8) ----------

@app.post("/api/studio/sessions/<session_id>/attachments")
def upload_attachment(session_id):
    _own_session(session_id)
    f = request.files["file"]
    dest = os.path.join(config.ATTACH_DIR, session_id)
    os.makedirs(dest, exist_ok=True)
    safe_name = os.path.basename(f.filename or "unnamed")
    # attachment_id를 저장 파일명에 접두 — 동명 첨부가 서로 덮어쓰지 않도록
    attachment_id = db.execute(
        "INSERT INTO attachments (session_id, filename, mime_type, storage_path) "
        "VALUES (?,?,?,?)",
        (session_id, safe_name, f.mimetype, ""))
    path = os.path.join(dest, f"{attachment_id}_{safe_name}")
    f.save(path)
    db.execute("UPDATE attachments SET storage_path=? WHERE attachment_id=?",
               (path, attachment_id))
    from . import docparse
    jobs.submit(docparse.process_attachment, attachment_id)   # 비동기 텍스트 추출
    return jsonify({"attachment_id": attachment_id, "filename": safe_name}), 201


def _own_session(session_id: str):
    row = db.one("SELECT user_id FROM sessions WHERE session_id=?", (session_id,))
    if row is None or row["user_id"] != current_user():
        abort(404)
    return row


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
