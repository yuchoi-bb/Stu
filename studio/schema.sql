-- ToolHub Studio 데이터 모델 (설계 문서 §5, v0.10)
-- SQLite + WAL. 기존 CICD DB와 파일 분리 (studio.db)

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS users (
    user_id      TEXT PRIMARY KEY,          -- REMOTE_USER (사번/AD ID)
    ad_id        TEXT,
    display_name TEXT,
    ghe_login    TEXT,
    is_admin     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS aws_credentials (
    user_id           TEXT PRIMARY KEY REFERENCES users(user_id),
    access_key_enc    BLOB,
    secret_key_enc    BLOB,
    session_token_enc BLOB,
    expires_at        TEXT,
    refresh_token_enc BLOB,                 -- SSO OIDC access/refresh token
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ghe_credentials (
    user_id           TEXT PRIMARY KEY REFERENCES users(user_id),
    auth_type         TEXT NOT NULL DEFAULT 'oauth',   -- oauth / pat
    token_enc         BLOB,
    refresh_token_enc BLOB,
    expires_at        TEXT,
    verified_at       TEXT,
    last_ok_at        TEXT,
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_branch_config (
    user_id     TEXT PRIMARY KEY REFERENCES users(user_id),
    repo        TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
-- 가드 (§6.1): 보호 브랜치 지정 금지는 저장 시 GHE API로 검사(앱 레벨),
-- 타 사용자 중복 지정 금지는 아래 인덱스로 DB 레벨에서도 차단
CREATE UNIQUE INDEX IF NOT EXISTS idx_branch_unique
    ON user_branch_config(repo, branch_name);

CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(user_id),
    title       TEXT,
    tool_target TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    message_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL REFERENCES sessions(session_id),
    role          TEXT NOT NULL,            -- user / assistant / system
    content       TEXT NOT NULL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS attachments (
    attachment_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL REFERENCES sessions(session_id),
    filename         TEXT NOT NULL,
    mime_type        TEXT,
    storage_path     TEXT NOT NULL,
    parsed_text_path TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 확정 requirements 1건에 대한 생성~검증 반복의 단위 (1 studio_id : N builds)
CREATE TABLE IF NOT EXISTS studios (
    studio_id    TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id),
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    repo         TEXT,
    branch_name  TEXT,
    status       TEXT NOT NULL DEFAULT 'open',
                 -- open(반복 중) / done(종료·채택) / abandoned(포기 또는 재확정 대체)
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

-- build 회차: 회차마다 커밋 1개 + stage 검증 run 1개
CREATE TABLE IF NOT EXISTS builds (
    build_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    studio_id        TEXT NOT NULL REFERENCES studios(studio_id),
    attempt          INTEGER NOT NULL,      -- studio 내 회차 번호 (1,2,…)
    commit_sha       TEXT,
    run_id           INTEGER,               -- GHE Actions run (취소 전파에 필요, §6.2)
    buildid          TEXT,                  -- stage가 발급
    status           TEXT NOT NULL DEFAULT 'generating',
                     -- generating / awaiting_review / pushing / push_conflict
                     -- / ci_running / pass / fail / cancelled
    cancel_requested INTEGER NOT NULL DEFAULT 0,   -- 취소 플래그 (DB 경유, §4.3)
    fail_summary     TEXT,                  -- CI 실패 요약 → 다음 회차 컨텍스트 주입
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at     TEXT,
    UNIQUE (studio_id, attempt)
);

CREATE TABLE IF NOT EXISTS usage_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           TEXT NOT NULL,
    session_id        TEXT,
    studio_id         TEXT,
    build_id          INTEGER,
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    model_id          TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS prompts (
    prompt_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    version    INTEGER NOT NULL DEFAULT 1,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (name, version)
);
