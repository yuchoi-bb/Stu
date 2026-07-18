"""Studio Flask 앱 (Phase 1 코어).

- 인증: Apache 레이어의 Knox SSO가 REMOTE_USER 헤더 전달 (§3.1) — 헤더만 신뢰
- 실행: gunicorn gthread, worker 1 × threads 16 고정 (§4.3), localhost 바인딩
- 상태: 모든 공유 상태는 DB 경유 (builds.cancel_requested 등)
"""
import os
import uuid

from flask import Flask, abort, jsonify, request

from . import config, crypto, db, jobs, prompts
from .aws_sso import poll_device_flow, start_device_flow, start_refresh_scheduler

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = config.ATTACH_MAX_BYTES


def _init() -> None:
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
    user_id = request.environ.get("HTTP_X_REMOTE_USER") or request.remote_user
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
    return jsonify({
        "user_id": user_id,
        "aws_connected": bool(aws and aws["expires_at"]),
        "ghe_connected": bool(ghe_row),
        "ghe_auth_type": ghe_row["auth_type"] if ghe_row else None,
        "ghe_login": user["ghe_login"] if user else None,
        "auto_approve": bool(user["auto_approve"]) if user else False,
        "is_admin": bool(user["is_admin"]) if user else False,
        "branch": dict(branch) if branch else None,
    })


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
    try:
        db.execute(
            """INSERT INTO user_branch_config (user_id, repo, branch_name)
               VALUES (?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET repo=excluded.repo,
                   branch_name=excluded.branch_name, updated_at=datetime('now')""",
            (user_id, repo, branch))
    except sqlite3.IntegrityError:
        abort(409, "다른 사용자가 이미 지정한 브랜치입니다")
    return jsonify({"repo": repo, "branch_name": branch})


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
    return jsonify({"ok": True})


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
    if jobs.active_count_for_user(user_id) >= config.MAX_CONCURRENT_PER_USER:
        abort(409, "동시 진행 작업 한도 초과 (사용자당 1건)")

    session_id = draft["session_id"]
    requirements = body.get("content") or draft["content"]   # 사용자 수정본 우선

    db.execute("UPDATE studios SET status='abandoned', "
               "completed_at=datetime('now') "
               "WHERE session_id=? AND status='open'", (session_id,))
    db.execute("UPDATE requirement_drafts SET status='approved', content=? "
               "WHERE draft_id=?", (requirements, draft_id))

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

    if jobs.active_count_for_user(user_id) >= config.MAX_CONCURRENT_PER_USER:
        abort(409, "동시 진행 작업 한도 초과 (사용자당 1건)")

    db.execute("INSERT INTO messages (session_id, role, content) VALUES (?,?,?)",
               (session_id, "user", body["content"]))
    row = db.one("SELECT MAX(attempt) AS a FROM builds WHERE studio_id=?",
                 (studio_id,))
    attempt = (row["a"] or 0) + 1
    build_id = db.execute("INSERT INTO builds (studio_id, attempt) VALUES (?,?)",
                          (studio_id, attempt))

    from .pipeline import run_generation
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
    return jsonify({"studio": dict(studio), "builds": [dict(b) for b in builds]})


# ---------- Step 3.5: 코드 리뷰 게이트 (§7.1, §6.4 주 방어선) ----------

@app.get("/api/studio/builds/<int:build_id>/files")
def build_files(build_id):
    _own_build(build_id)
    rows = db.query(
        "SELECT path, content, line_count, shrink_warn FROM build_files "
        "WHERE build_id=? ORDER BY path", (build_id,))
    return jsonify([dict(r) for r in rows])


@app.post("/api/studio/builds/<int:build_id>/review")
def review_build(build_id):
    """승인 → push 단계 진입 / 거부 → fail(사유 기록, 다음 회차 컨텍스트로 주입)."""
    row = _own_build(build_id)
    if row["status"] != "awaiting_review":
        abort(409, f"리뷰 대기 상태가 아님: {row['status']}")
    body = request.get_json(force=True)
    action = body.get("action")
    if action == "approve":
        jobs.set_build_status(build_id, "pushing")
        from .ghe import submit_push
        submit_push(build_id)
        return jsonify({"status": "pushing"})
    if action == "reject":
        reason = body.get("reason", "작업자 리뷰 거부")
        jobs.set_build_status(build_id, "fail", completed=True,
                              fail_summary=f"리뷰 거부: {reason}")
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
    """studio 종결: done(채택) 또는 abandoned(포기)."""
    row = db.one("SELECT user_id, status FROM studios WHERE studio_id=?", (studio_id,))
    if row is None or row["user_id"] != current_user():
        abort(404)
    status = (request.get_json(force=True) or {}).get("status", "done")
    if status not in ("done", "abandoned"):
        abort(400)
    db.execute("UPDATE studios SET status=?, completed_at=datetime('now') "
               "WHERE studio_id=?", (status, studio_id))
    return jsonify({"status": status})


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
            pass   # 폴링 fallback이 최종 상태를 보정
        jobs.set_build_status(build_id, "cancelled", completed=True)
    return jsonify({"status": "cancel_requested"})


# ---------- 첨부 (§8) ----------

@app.post("/api/studio/sessions/<session_id>/attachments")
def upload_attachment(session_id):
    _own_session(session_id)
    f = request.files["file"]
    dest = os.path.join(config.ATTACH_DIR, session_id)
    os.makedirs(dest, exist_ok=True)
    safe_name = os.path.basename(f.filename or "unnamed")
    path = os.path.join(dest, safe_name)
    f.save(path)
    attachment_id = db.execute(
        "INSERT INTO attachments (session_id, filename, mime_type, storage_path) "
        "VALUES (?,?,?,?)",
        (session_id, safe_name, f.mimetype, path))
    return jsonify({"attachment_id": attachment_id, "filename": safe_name}), 201


def _own_session(session_id: str):
    row = db.one("SELECT user_id FROM sessions WHERE session_id=?", (session_id,))
    if row is None or row["user_id"] != current_user():
        abort(404)
    return row


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
