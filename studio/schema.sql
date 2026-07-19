-- ToolHub Studio 데이터 모델 (설계 문서 §5, v0.10)
-- SQLite + WAL. 기존 CICD DB와 파일 분리 (studio.db)

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS users (
    user_id      TEXT PRIMARY KEY,          -- REMOTE_USER (사번/AD ID)
    ad_id        TEXT,
    display_name TEXT,
    ghe_login    TEXT,
    is_admin     INTEGER NOT NULL DEFAULT 0,
    auto_approve INTEGER NOT NULL DEFAULT 0,  -- Step 3.5 승인 모드 (0=매번 확인)
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
    summary     TEXT,   -- 오래된 턴 요약 (§4.2 멀티턴 히스토리 정책)
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

-- Step 2 requirements 초안 (확정 게이트, §7.1) — 승인 시 studio 발급
CREATE TABLE IF NOT EXISTS requirement_drafts (
    draft_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    content    TEXT,
    status     TEXT NOT NULL DEFAULT 'generating',
               -- generating / ready / approved / superseded / failed
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 확정 requirements 1건에 대한 생성~검증 반복의 단위 (1 studio_id : N builds)
CREATE TABLE IF NOT EXISTS studios (
    studio_id    TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id),
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    repo         TEXT,
    branch_name  TEXT,
    requirements TEXT,   -- Step 2 승인본 (생성 근거, 브랜치에도 md로 동반 커밋)
    status       TEXT NOT NULL DEFAULT 'open',
                 -- open(반복 중) / done(종료·채택) / abandoned(포기 또는 재확정 대체)
    pr_number    INTEGER,   -- §6.6 안 B: 본인 명의로 생성한 PR (자동 merge 금지)
    pr_url       TEXT,
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
    scan_findings    TEXT,                  -- 위험 패턴 정적 검사 결과 JSON (§12.4)
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at     TEXT,
    UNIQUE (studio_id, attempt)
);

-- Step 3 생성 파일 (파일 전체 교체 방식, Step 3.5 리뷰 대상)
CREATE TABLE IF NOT EXISTS build_files (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id   INTEGER NOT NULL REFERENCES builds(build_id),
    path       TEXT NOT NULL,
    content    TEXT NOT NULL,
    line_count INTEGER NOT NULL,
    shrink_warn INTEGER NOT NULL DEFAULT 0,  -- 라인 수 급감 경고 (회귀 가드)
    base_blob_sha TEXT,   -- 원문 fetch 시점 blob SHA (§6.5 조용한 덮어쓰기 가드)
    pushed_blob_sha TEXT, -- 커밋 후 git 실제 blob SHA (다음 회차 base, 정규화 불일치 방지)
    UNIQUE (build_id, path)
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

-- tool ↔ 분석 md 매핑 (§7.1 Step 1). 분석 md는 대상 repo 내 파일 경로로 관리
CREATE TABLE IF NOT EXISTS tool_analysis (
    tool_name   TEXT PRIMARY KEY,
    repo        TEXT NOT NULL,
    md_path     TEXT NOT NULL,   -- 예: docs/analysis/parser.md
    base_sha    TEXT,            -- md 상단 기준 커밋 SHA (stale 검사, §7.3)
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 행위 로그 (§3.1.1) — 4개 서비스 공통 규약: (user_id, service, action, target,
-- result, timestamp). service는 항상 'studio'.
CREATE TABLE IF NOT EXISTS action_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT,
    service    TEXT NOT NULL DEFAULT 'studio',
    action     TEXT NOT NULL,
    target     TEXT,
    result     TEXT,
    detail     TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_action_log_time ON action_log(created_at);

CREATE TABLE IF NOT EXISTS prompts (
    prompt_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    version    INTEGER NOT NULL DEFAULT 1,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (name, version)
);
