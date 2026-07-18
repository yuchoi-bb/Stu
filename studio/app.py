"""Studio Flask 앱 (Phase 1 코어).

- 인증: Apache 레이어의 Knox SSO가 REMOTE_USER 헤더 전달 (§3.1) — 헤더만 신뢰
- 실행: gunicorn gthread, worker 1 × threads 16 고정 (§4.3), localhost 바인딩
- 상태: 모든 공유 상태는 DB 경유 (builds.cancel_requested 등)
"""
import os
import uuid

from flask import Flask, abort, jsonify, request

from . import config, crypto, db, jobs
from .aws_sso import poll_device_flow, start_device_flow, start_refresh_scheduler

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = config.ATTACH_MAX_BYTES


def _init() -> None:
    db.init_db()
    crypto.ensure_key()
    os.makedirs(config.ATTACH_DIR, exist_ok=True)
    jobs.mark_interrupted_builds_failed()
    start_refresh_scheduler()


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


# ---------- 메시지/작업 (§4.3) ----------

@app.post("/api/studio/message")
def post_message():
    user_id = current_user()
    body = request.get_json(force=True)
    session_id = body["session_id"]
    _own_session(session_id)

    if jobs.active_count_for_user(user_id) >= config.MAX_CONCURRENT_PER_USER:
        abort(409, "동시 진행 작업 한도 초과 (사용자당 1건)")

    db.execute("INSERT INTO messages (session_id, role, content) VALUES (?,?,?)",
               (session_id, "user", body["content"]))

    studio_id = body.get("studio_id")
    if studio_id is None:
        studio_id = f"ST-{uuid.uuid4().hex[:8]}"
        db.execute(
            "INSERT INTO studios (studio_id, session_id, user_id) VALUES (?,?,?)",
            (studio_id, session_id, user_id))
        attempt = 1
    else:
        row = db.one("SELECT MAX(attempt) AS a FROM builds WHERE studio_id=?",
                     (studio_id,))
        attempt = (row["a"] or 0) + 1

    build_id = db.execute(
        "INSERT INTO builds (studio_id, attempt) VALUES (?,?)",
        (studio_id, attempt))

    from .pipeline import run_generation   # Phase 1: 생성까지, push/CI는 Phase 2
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


@app.post("/api/studio/builds/<int:build_id>/cancel")
def cancel_build(build_id):
    row = db.one(
        """SELECT b.status, s.user_id FROM builds b
           JOIN studios s ON s.studio_id=b.studio_id WHERE b.build_id=?""",
        (build_id,))
    if row is None or row["user_id"] != current_user():
        abort(404)
    jobs.request_cancel(build_id)
    # ci_running이면 stage로 취소 시그널(run cancel API) 전송 — Phase 2 (§6.2 취소 전파)
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
