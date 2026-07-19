"""백업 (§12.4): studio.db + 첨부 문서 일 1회, 30일 보존.

- DB는 SQLite 온라인 백업 API로 복사 — WAL 사용 중에도 torn read 없이 일관 스냅샷.
- 첨부 디렉터리는 tar.gz로 묶는다.
- 보존 기간(기본 30일) 초과분은 정리한다.

사용:
  python -m studio.manage backup [--dest DIR]     # 수동 실행
  (cron/systemd timer는 deploy/toolhub-studio-backup.* 참조)

복구(수동):
  1) 서비스 중지 → 2) 백업 db를 STUDIO_DB 위치로 복사(-wal/-shm는 삭제)
  3) 첨부 tar.gz를 STUDIO_ATTACH_DIR로 해제 → 4) 서비스 시작
"""
import datetime as dt
import os
import sqlite3
import tarfile

from . import config, logs

_log = logs.get("backup")


def _sqlite_backup(src_path: str, dst_path: str) -> None:
    """온라인 백업 API로 WAL-안전 스냅샷 생성."""
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        with dst:
            src.backup(dst)      # 잠금 없이 일관 복사
    finally:
        dst.close()
        src.close()


def _prune(dest_dir: str, retention_days: int) -> list[str]:
    """보존 기간 초과 백업 삭제. 삭제한 파일 경로 목록 반환."""
    if retention_days <= 0:
        return []
    cutoff = dt.datetime.now().timestamp() - retention_days * 86400
    removed = []
    for name in os.listdir(dest_dir):
        if not (name.startswith("studio-") or name.startswith("attachments-")):
            continue
        path = os.path.join(dest_dir, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed.append(path)
        except OSError:
            _log.warning("백업 정리 실패: %s", path)
    return removed


def run_backup(dest_dir: str | None = None,
               retention_days: int | None = None) -> dict:
    """백업 1회 수행. 반환: {db, attachments, pruned, ts}."""
    dest_dir = dest_dir or config.BACKUP_DIR
    retention_days = (config.BACKUP_RETENTION_DAYS
                      if retention_days is None else retention_days)
    os.makedirs(dest_dir, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    db_out = os.path.join(dest_dir, f"studio-{ts}.db")
    if os.path.isfile(config.DB_PATH):
        _sqlite_backup(config.DB_PATH, db_out)
    else:
        db_out = None

    att_out = None
    if os.path.isdir(config.ATTACH_DIR) and os.listdir(config.ATTACH_DIR):
        att_out = os.path.join(dest_dir, f"attachments-{ts}.tar.gz")
        with tarfile.open(att_out, "w:gz") as tar:
            tar.add(config.ATTACH_DIR, arcname="attachments")

    pruned = _prune(dest_dir, retention_days)
    result = {"db": db_out, "attachments": att_out, "pruned": len(pruned), "ts": ts}
    _log.info("backup done db=%s att=%s pruned=%d",
              os.path.basename(db_out) if db_out else "-",
              os.path.basename(att_out) if att_out else "-", len(pruned))
    return result


if __name__ == "__main__":     # 간이 실행: python -m studio.backup
    logs.setup()
    print(run_backup())
