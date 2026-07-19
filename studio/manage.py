"""운영 CLI — HTTP 없이 상태 점검/관리 (7명 내부 도구, 사내 스크립트용).

사용:
  python -m studio.manage health
  python -m studio.manage set-admin <user_id> [--off]   # 마지막 관리자 강등 금지
  python -m studio.manage admins                        # 관리자 목록 + 2인 권장 경고
  python -m studio.manage backup [--dest DIR]           # DB+첨부 백업(30일 보존)
  python -m studio.manage set-branch <user_id> <repo> <branch>
  python -m studio.manage map-analysis <tool> <repo> <md_path>
  python -m studio.manage show-studio <studio_id>
  python -m studio.manage failures [--limit N]
  python -m studio.manage audit [--limit N] [--user U]
  python -m studio.manage users
"""
import argparse
import json
import sys

from . import audit, db, logs


def _init():
    logs.setup()
    db.init_db()


def cmd_health(_):
    import shutil
    import threading
    print("db:", "ok" if db.one("SELECT 1") else "error")
    alive = {t.name for t in threading.enumerate()}
    print("refresh_scheduler:", "n/a (앱 프로세스에서만 구동)")
    from . import config
    free = shutil.disk_usage(config.BASE_DIR).free
    print("disk_free_mb:", free // (1024 * 1024))
    print("open studios:",
          db.one("SELECT COUNT(*) AS n FROM studios WHERE status='open'")["n"])
    print("ci_running builds:",
          db.one("SELECT COUNT(*) AS n FROM builds WHERE status='ci_running'")["n"])


def _admin_count() -> int:
    return db.one("SELECT COUNT(*) AS n FROM users WHERE is_admin=1")["n"]


def cmd_set_admin(a):
    val = 0 if a.off else 1
    if a.off:
        # 마지막 관리자 강등 금지 (0명 락아웃 방지) — 부트스트랩 경로도 동일 가드
        cur = db.one("SELECT is_admin FROM users WHERE user_id=?", (a.user_id,))
        if cur and cur["is_admin"] and _admin_count() <= 1:
            print("마지막 관리자는 강등할 수 없습니다 (최소 1인 유지)", file=sys.stderr)
            sys.exit(1)
    db.execute("INSERT INTO users (user_id, is_admin) VALUES (?, ?) "
               "ON CONFLICT(user_id) DO UPDATE SET is_admin=?", (a.user_id, val, val))
    audit.record("cli", "grant_admin" if val else "revoke_admin", a.user_id, "ok")
    n = _admin_count()
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
    for r in audit.recent(a.limit, a.user):
        print(f"{r['created_at']} {r['user_id'] or '-'} {r['action']} "
              f"{r['target'] or ''} -> {r['result'] or ''}")


def cmd_users(_):
    for u in db.query("SELECT user_id, ghe_login, is_admin, auto_approve FROM users "
                      "ORDER BY user_id"):
        print(f"{u['user_id']:20} ghe={u['ghe_login'] or '-':15} "
              f"admin={u['is_admin']} auto_approve={u['auto_approve']}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="studio.manage")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health").set_defaults(fn=cmd_health)

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
    sp.add_argument("--user"); sp.set_defaults(fn=cmd_audit)

    sub.add_parser("users").set_defaults(fn=cmd_users)
    sub.add_parser("admins").set_defaults(fn=cmd_admins)

    sp = sub.add_parser("backup"); sp.add_argument("--dest")
    sp.set_defaults(fn=cmd_backup)

    args = p.parse_args(argv)
    _init()
    args.fn(args)


if __name__ == "__main__":
    main()
