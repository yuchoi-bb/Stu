"""운영 CLI — HTTP 없이 상태 점검/관리 (7명 내부 도구, 사내 스크립트용).

사용:
  python -m studio.manage health
  python -m studio.manage doctor [--net]                # 이식/설치 종합 진단(게이트)
  python -m studio.manage set-admin <user_id> [--off]   # 마지막 관리자 강등 금지
  python -m studio.manage admins                        # 관리자 목록 + 2인 권장 경고
  python -m studio.manage backup [--dest DIR]           # DB+첨부 백업(30일 보존)
  python -m studio.manage set-branch <user_id> <repo> <branch>
  python -m studio.manage map-analysis <tool> <repo> <md_path>
  python -m studio.manage show-studio <studio_id>
  python -m studio.manage failures [--limit N]
  python -m studio.manage audit [--limit N] [--user U] [--action login|push_dispatch]
  python -m studio.manage studio-log <studio_id> [--tail N]  # studio별 디버그 로그
  python -m studio.manage users
"""
import argparse
import json
import os
import sys

from . import audit, db, logs


def _init():
    logs.setup()
    db.init_db()


def cmd_health(_):
    import shutil
    print("db:", "ok" if db.one("SELECT 1") else "error")
    print("refresh_scheduler:", "n/a (앱 프로세스에서만 구동)")
    from . import config
    free = shutil.disk_usage(config.BASE_DIR).free
    print("disk_free_mb:", free // (1024 * 1024))
    print("open studios:",
          db.one("SELECT COUNT(*) AS n FROM studios WHERE status='open'")["n"])
    print("ci_running builds:",
          db.one("SELECT COUNT(*) AS n FROM builds WHERE status='ci_running'")["n"])


# ── 이식/설치 당일 종합 진단 (§install 런북) ───────────────────────────────────
# 필수 경로·키·설정·스키마·관리자·(선택)네트워크를 한 번에 점검하고 각 실패에
# 바로 실행할 조치를 붙여 출력한다. FAIL이 하나라도 있으면 exit 1(이식 게이트).
_EXPECTED_TABLES = {
    "users", "sessions", "studios", "builds", "build_files", "messages",
    "requirement_drafts", "prompts", "attachments", "tool_analysis",
    "user_branch_config", "aws_credentials", "ghe_credentials",
    "usage_log", "action_log",
}


class _Doctor:
    def __init__(self):
        self.fail = 0
        self.warn = 0

    def ok(self, label, detail=""):
        print(f"  [PASS] {label}" + (f" — {detail}" if detail else ""))

    def w(self, label, fix):
        self.warn += 1
        print(f"  [WARN] {label} — {fix}")

    def f(self, label, fix):
        self.fail += 1
        print(f"  [FAIL] {label} — {fix}")


def _check_writable(d, path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".doctor-write-test")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
        d.ok(f"쓰기 가능: {path}")
    except Exception as e:
        d.f(f"쓰기 불가: {path}", f"디렉터리 권한/소유(toolhub) 확인 — {e}")


def cmd_doctor(a):
    from . import config, metrics
    d = _Doctor()

    print("── 1. 런타임/파일시스템 ──")
    try:
        db.one("SELECT 1")
        have = {r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = _EXPECTED_TABLES - have
        if missing:
            d.f(f"스키마 누락 테이블: {', '.join(sorted(missing))}",
                "manage.py health 로 init_db 재실행(멱등) 후 재확인")
        else:
            d.ok("DB 연결 + 스키마", f"{len(_EXPECTED_TABLES)}개 테이블")
    except Exception as e:
        d.f("DB 연결 실패", f"STUDIO_DB 경로/권한 확인 — {e}")

    key = config.FERNET_KEY_PATH
    if not os.path.isfile(key):
        d.f(f"Fernet 키 없음: {key}",
            "생성: python -c \"from cryptography.fernet import Fernet;"
            "open('<path>','wb').write(Fernet.generate_key())\" 후 chmod 600")
    else:
        try:
            from cryptography.fernet import Fernet
            Fernet(open(key, "rb").read())
            mode = oct(os.stat(key).st_mode & 0o777)
            if mode == "0o600":
                d.ok(f"Fernet 키 유효 (권한 {mode})")
            else:
                d.w(f"Fernet 키 유효하나 권한 {mode}", "chmod 600 " + key)
        except Exception as e:
            d.f("Fernet 키 손상/무효", f"백업 키로 복구 필요 — {e}")

    import shutil
    free_mb = shutil.disk_usage(config.BASE_DIR).free // (1024 * 1024)
    (d.ok if free_mb >= 500 else d.w)(
        f"디스크 여유 {free_mb}MB",
        "백업/로그/첨부 정리 — ops §2.4" if free_mb < 500 else "")
    for path in (config.ATTACH_DIR, config.LOG_DIR, config.BACKUP_DIR,
                 logs.studio_log_dir()):
        _check_writable(d, path)

    print("── 2. Bedrock / AWS SSO 설정 (§3.2, 후순위) ──")
    # SSO는 이식 최후순위(마지막 단계 A-8). 미설정이어도 게이트를 막지 않도록 WARN 처리.
    for name, val in (("STUDIO_SSO_START_URL", config.SSO_START_URL),
                      ("STUDIO_SSO_ACCOUNT_ID", config.SSO_ACCOUNT_ID),
                      ("STUDIO_SSO_ROLE_NAME", config.SSO_ROLE_NAME)):
        (d.ok if val else d.w)(
            name, "" if val else "후순위(SSO 단계 A-8)에서 설정 — 그 전엔 미설정 정상")
    d.ok("MODEL_ID", config.MODEL_ID)
    d.ok("BEDROCK_REGION", config.BEDROCK_REGION)

    print("── 3. GHE 설정 (§3.3, §6) ──")
    d.ok("GHE_API_URL", config.GHE_API_URL)
    if config.GHE_OWNER in ("", "toolhub"):
        d.w(f"GHE_OWNER={config.GHE_OWNER!r}", "기본값으로 보임 — 실제 org로 STUDIO_GHE_OWNER 설정")
    else:
        d.ok("GHE_OWNER", config.GHE_OWNER)
    if config.GHE_OAUTH_CLIENT_ID and config.GHE_OAUTH_CLIENT_SECRET:
        d.ok("GHE OAuth", "client id/secret 설정됨(1안)")
    else:
        d.w("GHE OAuth 미설정", "OAuth 미사용 시 사용자 PAT 폴백만 가능 — 의도했는지 확인")
    (d.ok if config.CI_WEBHOOK_SECRET else d.f)(
        "CI_WEBHOOK_SECRET",
        "" if config.CI_WEBHOOK_SECRET
        else "미설정 시 ci-callback HMAC 검증 불가(위조 콜백 위험) — 반드시 설정")

    print("── 4. OBS 에러로그 (선택, §2.6) ──")
    if not config.OBS_ENDPOINT:
        print("  [INFO] OBS 미설정 — 로컬 로그만(에러 자동 업로드 비활성)")
    else:
        parts = [config.OBS_BUCKET, config.OBS_ACCESS_KEY, config.OBS_SECRET_KEY]
        (d.ok if all(parts) else d.f)(
            "OBS 설정", "완전"
            if all(parts) else "부분 설정 — BUCKET/ACCESS_KEY/SECRET_KEY 모두 필요")

    print("── 5. 운영 전제 ──")
    # 인증 우회 폴백이 프로덕션에 켜져 있으면 SSO를 우회한다 — 반드시 제거.
    if os.environ.get("STUDIO_DEV_USER"):
        d.f("STUDIO_DEV_USER 설정됨",
            "개발 전용 인증 우회 — 프로덕션에서 제거(SSO 우회 위험)")
    else:
        d.ok("인증 우회 폴백 없음 (STUDIO_DEV_USER 미설정)")
    n_admin = metrics.admin_count()
    if n_admin == 0:
        d.f("관리자 0명", "부트스트랩: manage.py set-admin <user> (2인 권장)")
    elif n_admin == 1:
        d.w("관리자 1명", "락아웃 대비 2인 체계 권장 — set-admin 1명 추가")
    else:
        d.ok(f"관리자 {n_admin}명")
    if config.LOG_LEVEL.upper() != "DEBUG":
        d.w(f"LOG_LEVEL={config.LOG_LEVEL}",
            "이식 기간엔 STUDIO_LOG_LEVEL=DEBUG 권장(안정화 후 INFO 복귀)")
    else:
        d.ok("LOG_LEVEL=DEBUG (이식 기간 적합)")

    if a.net:
        print("── 6. 네트워크 도달성 (--net) ──")
        for name, url in (("GHE", config.GHE_API_URL),
                          ("OBS", config.OBS_ENDPOINT)):
            if not url:
                continue
            try:
                import requests
                r = requests.get(url, timeout=5, verify=True)
                d.ok(f"{name} 도달 ({url})", f"HTTP {r.status_code}")
            except Exception as e:
                d.f(f"{name} 도달 실패 ({url})",
                    f"방화벽/프록시/DNS 확인 — {type(e).__name__}")

    print(f"\n요약: FAIL {d.fail} · WARN {d.warn}")
    if d.fail:
        print("→ FAIL 항목을 조치한 뒤 재실행하세요. 이식 진행 보류 권장.", file=sys.stderr)
        sys.exit(1)
    print("→ 필수 항목 통과. WARN은 검토 후 진행 가능.")


def cmd_set_admin(a):
    from . import metrics
    val = 0 if a.off else 1
    if a.off:
        # 마지막 관리자 강등 금지 (0명 락아웃 방지) — 부트스트랩 경로도 동일 가드
        cur = db.one("SELECT is_admin FROM users WHERE user_id=?", (a.user_id,))
        if cur and cur["is_admin"] and metrics.admin_count() <= 1:
            print("마지막 관리자는 강등할 수 없습니다 (최소 1인 유지)", file=sys.stderr)
            sys.exit(1)
    db.execute("INSERT INTO users (user_id, is_admin) VALUES (?, ?) "
               "ON CONFLICT(user_id) DO UPDATE SET is_admin=?", (a.user_id, val, val))
    audit.record("cli", "grant_admin" if val else "revoke_admin", a.user_id, "ok")
    n = metrics.admin_count()
    warn = "  ⚠ 관리자 1명 — 2인 체계 권장" if n < 2 else ""
    print(f"{a.user_id} is_admin={val} (총 관리자 {n}명){warn}")


def cmd_backup(a):
    from . import backup
    r = backup.run_backup(a.dest)
    print(f"db: {r['db'] or '-'}")
    print(f"attachments: {r['attachments'] or '-'}")
    print(f"pruned(30일 초과): {r['pruned']}건")


def cmd_admins(_):
    rows = db.query("SELECT user_id, ghe_login FROM users WHERE is_admin=1 "
                    "ORDER BY user_id")
    for r in rows:
        print(f"{r['user_id']:20} ghe={r['ghe_login'] or '-'}")
    n = len(rows)
    print(f"— 총 {n}명" + ("  ⚠ 2인 체계 권장 (현재 부족)" if n < 2 else ""))


def cmd_set_branch(a):
    db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (a.user_id,))
    try:
        db.execute(
            "INSERT INTO user_branch_config (user_id, repo, branch_name) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET repo=?, branch_name=?, "
            "updated_at=datetime('now')",
            (a.user_id, a.repo, a.branch, a.repo, a.branch))
    except Exception as e:
        print("실패:", e, file=sys.stderr)
        sys.exit(1)
    print(f"{a.user_id} → {a.repo}/{a.branch}")


def cmd_map_analysis(a):
    from . import analysis
    analysis.set_mapping(a.tool, a.repo, a.md_path)
    print(f"{a.tool} → {a.repo}:{a.md_path}")


def cmd_show_studio(a):
    s = db.one("SELECT * FROM studios WHERE studio_id=?", (a.studio_id,))
    if not s:
        print("없음", file=sys.stderr); sys.exit(1)
    print(json.dumps(dict(s), ensure_ascii=False, indent=2))
    for b in db.query("SELECT build_id, attempt, status, buildid, run_id, "
                      "fail_summary, completed_at FROM builds WHERE studio_id=? "
                      "ORDER BY attempt", (a.studio_id,)):
        print(f"  #{b['attempt']} {b['status']} "
              f"build={b['buildid'] or '-'} run={b['run_id'] or '-'}"
              + (f" | {b['fail_summary']}" if b['fail_summary'] else ""))


def cmd_failures(a):
    for r in db.query(
            "SELECT b.studio_id, b.attempt, b.status, b.fail_summary, s.user_id "
            "FROM builds b JOIN studios s ON s.studio_id=b.studio_id "
            "WHERE b.status IN ('fail','push_conflict') "
            "ORDER BY b.completed_at DESC LIMIT ?", (a.limit,)):
        print(f"{r['studio_id']}#{r['attempt']} [{r['status']}] "
              f"{r['user_id']}: {r['fail_summary']}")


def cmd_audit(a):
    for r in audit.recent(a.limit, a.user, a.action):
        print(f"{r['created_at']} {r['user_id'] or '-'} {r['action']} "
              f"{r['target'] or ''} -> {r['result'] or ''}")


def cmd_studio_log(a):
    from . import logs
    path = logs.studio_log_path(a.studio_id)
    if not path or not os.path.isfile(path):
        print(f"로그 없음: {a.studio_id}", file=sys.stderr); sys.exit(1)
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    for ln in (lines[-a.tail:] if a.tail else lines):
        print(ln.rstrip())


def cmd_users(_):
    for u in db.query("SELECT user_id, ghe_login, is_admin, auto_approve FROM users "
                      "ORDER BY user_id"):
        print(f"{u['user_id']:20} ghe={u['ghe_login'] or '-':15} "
              f"admin={u['is_admin']} auto_approve={u['auto_approve']}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="studio.manage")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health").set_defaults(fn=cmd_health)

    sp = sub.add_parser("doctor")
    sp.add_argument("--net", action="store_true", help="GHE/OBS 도달성까지 점검")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("set-admin"); sp.add_argument("user_id")
    sp.add_argument("--off", action="store_true"); sp.set_defaults(fn=cmd_set_admin)

    sp = sub.add_parser("set-branch")
    sp.add_argument("user_id"); sp.add_argument("repo"); sp.add_argument("branch")
    sp.set_defaults(fn=cmd_set_branch)

    sp = sub.add_parser("map-analysis")
    sp.add_argument("tool"); sp.add_argument("repo"); sp.add_argument("md_path")
    sp.set_defaults(fn=cmd_map_analysis)

    sp = sub.add_parser("show-studio"); sp.add_argument("studio_id")
    sp.set_defaults(fn=cmd_show_studio)

    sp = sub.add_parser("failures"); sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(fn=cmd_failures)

    sp = sub.add_parser("audit"); sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--user")
    sp.add_argument("--action", help="예: login / push_dispatch")
    sp.set_defaults(fn=cmd_audit)

    sp = sub.add_parser("studio-log"); sp.add_argument("studio_id")
    sp.add_argument("--tail", type=int, help="마지막 N줄만")
    sp.set_defaults(fn=cmd_studio_log)

    sub.add_parser("users").set_defaults(fn=cmd_users)
    sub.add_parser("admins").set_defaults(fn=cmd_admins)

    sp = sub.add_parser("backup"); sp.add_argument("--dest")
    sp.set_defaults(fn=cmd_backup)

    args = p.parse_args(argv)
    _init()
    args.fn(args)


if __name__ == "__main__":
    main()
