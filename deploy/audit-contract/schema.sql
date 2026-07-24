-- ToolHub 공통 행위 로그 테이블 (studio/stage/release/signtool 공용)
-- docs/toolhub-audit-contract.md §2 스키마의 구현. 서비스가 공통 DB를 쓰면 이 테이블을
-- 함께 사용하고, 각자 DB를 쓰면 같은 컬럼으로 만들어 형식을 통일한다.
--
-- 아래는 SQLite. MySQL은 id를 BIGINT AUTO_INCREMENT, created_at을
-- DATETIME DEFAULT CURRENT_TIMESTAMP; Postgres는 BIGSERIAL / timestamptz 로 바꾼다.

CREATE TABLE IF NOT EXISTS action_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT,                       -- REMOTE_USER(사번/AD). 시스템 발생은 NULL
    service    TEXT NOT NULL,              -- studio | stage | release | signtool
    action     TEXT NOT NULL,              -- login | push_dispatch | build_trigger | deploy | sign ...
    target     TEXT,                       -- 대상 식별자 (studio_id#attempt, pkg@ver ...)
    result     TEXT,                       -- ok | fail | ci_running | new | resume ...
    detail     TEXT,                       -- 사유·SHA 등 부가정보
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_action_log_time    ON action_log(created_at);
CREATE INDEX IF NOT EXISTS idx_action_log_service ON action_log(service, created_at);
CREATE INDEX IF NOT EXISTS idx_action_log_user    ON action_log(user_id, action);
