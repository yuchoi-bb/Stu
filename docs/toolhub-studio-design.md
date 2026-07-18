# ToolHub Studio 설계 문서

> 버전: v0.10 (2026-07-18) — 서비스 식별자 확정(`studio`/`stage`/`release`/`signtool`),
> `request_id` → `studio_id` 개칭, **1 studio_id : N buildid 누적 반복 모델** 도입
> (v0.9: push 충돌 정책, 2-pass, 파일럿, 성공 지표, 인수인계, 모델 버전 대응 /
> v0.8: 4개 서비스 SSO+audit, GHE OAuth App / v0.7: 개인 신원+dispatch / v0.6: Knox SSO)

## Claude Code 사용 안내

- 이 문서는 ToolHub Studio 구현의 **단일 기준 문서(source of truth)**다.
- 권장 위치: `thr` repo 루트 또는 `docs/toolhub-studio-design.md`
- 세션에서 참조: `@docs/toolhub-studio-design.md` 또는 CLAUDE.md에 아래 한 줄 추가:
  `- Studio 관련 작업 시 docs/toolhub-studio-design.md를 먼저 읽을 것 (설계 기준 문서)`
- 구현 순서는 §12 Todo의 체크박스를 위에서부터 따르고, 완료 시 이 파일의 체크박스를 갱신할 것.
- 설계 변경이 필요하면 §0 변경 이력 표에 사유와 함께 기록 후 진행할 것.

---

## 0. 주요 결정 변경 이력

| 항목 | 이전 | 현재 (v0.7) | 변경 근거 |
|---|---|---|---|
| GHE push 주체 | toolhub.sec 봇 + PAT 1개 | **사용자 개인 PAT** (7개, 암호화 보관) | 전원이 이미 repo write 권한 보유 개발자 + 엄밀한 본인 인증 요구 |
| CI 트리거 | push trigger (`studio/**` 패턴) | **workflow_dispatch** (API 명시 호출) | 자유 브랜치 허용 → 패턴 트리거 불가 |
| 브랜치 정책 | `studio/{user}/{request_id}` 자동 생성 | **개인이 설정한 자유 브랜치** | 사용자 브랜치 소유권 존중 |
| 브랜치 정리 | 7일 cron 자동 삭제 | **자동 삭제 폐지** → 관리자 조회 화면 + 수동 관리 | 사용자 소유 브랜치를 시스템이 삭제 불가 |
| request_id 전달 | 브랜치명 파싱 | **dispatch inputs 직접 전달** | 더 명시적, 파싱 불필요 |
| DB (v0.2) | PostgreSQL | SQLite + WAL | 동시 쓰기 ≤7건 |
| 작업 큐 (v0.2) | Celery+Redis | ThreadPoolExecutor(8) | 동시 작업 ≤7건 |
| 서비스 식별자 (v0.10) | 명칭 표기 혼용 (Studio/CICD dashboard/…) | **`studio`/`stage`/`release`/`signtool` 소문자 통일** (§3.1) — `studio`=생성, `stage`=기존 빌드/검증 체인 소유 | 경로/로그 표기 일관성, 짧은 식별자, 서비스 경계 명확화 |
| 작업 식별자 (v0.10) | `request_id` | **`studio_id`** (전면 개칭) | `stage`의 `buildid` 선례("소유 서비스+id")와 대칭, "request" 중의성 제거, 통합 로그에서 자기설명적 |
| 작업:검증 관계 (v0.10) | 1 request = 1 커밋 = 1 run = 1 buildid | **1 studio_id : N buildid** — 실패 시 같은 studio_id 아래 build 회차 누적, 이전 회차 코드·실패 결과를 다음 생성 컨텍스트에 주입 | 반복 개선이 Studio의 핵심 루프 — 이력이 누적되어야 LLM이 앞선 실수를 회피 |

**신원 원칙 (통일)**: AWS도 GHE도 **사용자 본인 계정**. Bedrock은 device flow,
GHE는 개인 PAT. 서버는 각 사용자의 자격증명을 암호화 대리 보관할 뿐, 모든 행위는 본인 명의.

---

## 1. 개요

### 1.1 목적

사용자가 웹(Studio)에서 도구 관련 요구조건을 입력하고 문서(정형/비정형)를 첨부하면,
Claude(AWS Bedrock)가 기존 코드 분석 자료(사전 분석 md)를 바탕으로:

1. 명확한 요구조건(requirements) 생성
2. 기존 코드에 필요한 코드 + testcase 코드 생성
3. 산출물을 사용자가 설정한 브랜치에 커밋 → ToolHub CI/CD(Zone1 Arbiter → Zone2 Build → Zone3 Test)로 race 검증
4. CI 결과를 대화에 자동 주입 → 사용자 판단 하에 수정 반복 (대화형 멀티턴)

### 1.2 핵심 설계 결정 요약

| 항목 | 결정 |
|---|---|
| 사용자 규모 | **최대 7명** (전원 대상 repo write 권한 보유 개발자) |
| Studio 로그인 | Knox SSO (Apache 레이어, REMOTE_USER 전달) — Jira/GHE 선례 동일 경로 |
| Bedrock 호출 신원 | 사용자 본인 AWS SSO (device flow 내장) → CLI와 동일 비용/신원 |
| GHE 작업 신원 | **사용자 본인 OAuth 토큰** (OAuth App "GitHub 연결", 폴백: PAT) → 본인 명의 커밋 |
| 프론트엔드 | Vanilla HTML/CSS/JS, `studio.html` (dashboard와 CSS/다크테마 공유) |
| 백엔드 | Flask + gunicorn(gthread, worker 2 × threads 8), khtoolhubw02 |
| DB | SQLite + WAL (studio.db, 기존 CICD DB와 파일 분리) |
| 장시간 작업 | ThreadPoolExecutor(8) + DB 상태 기록 + REST 폴링 |
| 브랜치 | 개인 설정 자유 브랜치 (보호 브랜치 대상 지정 금지 가드) |
| CI 트리거 | workflow_dispatch (inputs: studio_id/attempt, ref: 대상 브랜치) |
| 작업 단위 | **1 studio_id : N buildid** — 실패 시 같은 studio_id 아래 회차(build) 누적 반복 |
| CI 실패 피드백 | 자동 주입 + 사용자 판단 병행 |
| 브랜치 정리 | 관리자 수동 (Studio 관리자 화면에서 오래된 요청 브랜치 조회) |

### 1.3 Studio 산출물 정의

studio_id 1건당 산출물은 **"CI 검증 이력이 붙은 사용자 브랜치의 커밋(들)"**이다.
studio_id는 확정 requirements 1건에 대한 생성~검증 반복 전체의 단위이며,
그 아래 build 회차가 누적된다(1 studio_id : N buildid). 구성:

1. **확정 requirements 문서** (Step 2 산출, 사용자 승인본) — 브랜치에 md로 함께 커밋
2. **생성 코드** — 기존 tool에 추가/수정되는 소스 (회차마다 개선 누적)
3. **testcase 코드** — 생성 코드 검증용
4. **커밋** — 사용자 본인 명의(author=committer=사용자), 본인 설정 브랜치에 회차별 존재
5. **CI 검증 이력** — 회차별 buildid 목록, OS/arch별 build/test pass·fail (meta.json 기반)
   — 최종 회차의 pass가 채택 후보

- main 반영은 §6.6 결정에 따름 (어느 안이든 사람 리뷰 필수)
- 부수 산출물: 대화 히스토리(세션 재개), usage_log(미터링)

---

## 2. 전체 아키텍처

```
사용자(≤7, 브라우저)
  │  ① Knox SSO (Apache 레이어 → REMOTE_USER)
  ▼
Apache (khtoolhubw02)
  ├── /studio  → studio.html (Vanilla JS, 정적 서빙)
  └── /api/studio/* → reverse proxy → Flask (gunicorn gthread :5000)
                                        │
                                        ├── SQLite (WAL, studio.db)
                                        │     · 세션/메시지 (멀티턴 상태)
                                        │     · aws_credentials (device flow, 암호화)
                                        │     · ghe_credentials (개인 PAT, 암호화)
                                        │     · studios / builds (1 studio_id : N buildid)
                                        │     · usage_log (토큰 미터링)
                                        │
                                        ├── ThreadPoolExecutor(8)
                                        │     장시간 작업 → 상태를 DB 기록 → 폴링 응답
                                        │
                                        ├── ② AWS SSO device flow → 본인 임시 자격증명
                                        ├── ③ invoke_claude(user_id, ...) → Bedrock
                                        │     (본인 자격증명 + requestMetadata + caching)
                                        │
                                        └── ④ 사용자 본인 PAT
                                              · 본인 설정 브랜치에 커밋/push (author=본인)
                                              · workflow_dispatch 호출 (inputs: studio_id, attempt)
                                                → Zone1 Arbiter → Build/Test race
                                                → meta.json (studio_id, attempt)
                                                → ⑤ CI webhook → 대화 자동 주입
```

---

## 3. 인증 설계 (3계층)

### 3.1 Studio 로그인: Knox SSO (사내 AD 연계) — 4개 서비스 공통

**적용 범위: Studio / CICD dashboard / Release / SignTool** — ToolHub의 4개 기능 전체.
이 기능들은 향후 분산 배치되거나 조합될 수 있으므로 다음 원칙을 따른다:

**서비스 식별자 (확정)** — 경로/로그/행위 로그의 `service` 값은 아래 소문자 식별자로 통일한다
(짧고, URL 경로·로그 필드에 쓰기 좋음):

| service | 대상 기능 | 역할 |
|---|---|---|
| `studio` | ToolHub Studio — **이 문서의 대상 서비스** | 요구조건 → 코드/testcase **생성** (검증은 `stage` 파이프라인을 호출해 위임) |
| `stage` | 기존 CICD (dashboard 포함) | 코드 **빌드/검증** — Zone1 Arbiter → Zone2 Build → Zone3 Test (기존 역할 그대로) |
| `release` | Release | 배포 |
| `signtool` | SignTool | 서명 |

- 이하 본문에서 "CICD dashboard"로 표기된 서비스의 식별자는 `stage`다.
- **경계**: §6.2의 검증 체인(Arbiter→Build→Test)은 `stage` 소유. `studio`는
  workflow_dispatch로 이를 **호출하는 쪽**이며 체인 내부를 소유/변경하지 않는다.
  행위 로그 기준 — 커밋/push/dispatch 호출까지는 `service=studio`,
  run 실행·빌드·테스트 결과는 `service=stage`.
- 적용 지점: Apache 경로 prefix, 접근/행위 로그의 `service` 필드, 추후 통합 조회 키.

**설계 원칙 — 인증은 서비스 밖에서.**
인증을 각 앱이 아닌 **리버스 프록시(Apache) 레이어**에서 일괄 처리하고,
모든 앱은 `REMOTE_USER` 헤더 계약만 따른다. 서비스 재배치 시 앱 코드 무변경 —
새 호스트에는 동일 Apache SSO 설정을 복제하고 Knox SP 등록에 콜백 URL만 추가.

사용자 인증 스택: PC 부팅 AD 로그인 → **Knox SSO**(사내망 서비스) → AWS SSO(Bedrock).
사내 Jira와 GHE(github.samsungds.net)가 이미 Knox SSO로 연동되어 있음
→ 표준 프로토콜 IdP + 신규 서비스 등록 절차가 존재한다는 확증.

**연동 절차:**

1. Knox SSO 운영 조직에 신규 서비스(ToolHub) 연동 신청
   — 제출 정보: 서비스 URL(khtoolhubw02), **4개 서비스 경로의 콜백 주소**, 프로토콜
2. 프로토콜에 따른 Apache 모듈 분기:
   - SAML → `mod_auth_mellon` (GHE 연동이 SAML인 경우 이쪽일 가능성 높음)
   - OIDC → `mod_auth_openidc`
   - (폴백) 연동 불가 시 AD LDAP 직접: `mod_authnz_ldap`
3. Apache 레이어 일괄 처리 → 4개 서비스 SSO 통일
4. 인증 후 `REMOTE_USER`(사번/AD ID) 헤더 → Flask/PHP는 헤더만 신뢰
5. Flask 5000 포트는 localhost 바인딩 유지 (Apache 우회 차단)

**효과**: Knox 세션이 있으면 접속 시 추가 로그인 0회 (Jira/GHE와 동일 UX).

### 3.1.1 접근/행위 이력 (Audit) — 2계층

| 계층 | 내용 | 구현 |
|---|---|---|
| **접근 로그** (4개 서비스 공통) | 누가·언제·어디에 접근 | Apache LogFormat에 `%u`(REMOTE_USER) 추가 — 앱 코드 0줄, 설정만으로 전 서비스 커버. logrotate 보존 정책 설정 |
| **행위 로그** (서비스별) | 누가·무엇을 실행 | Studio: studios·builds/usage_log로 커버(기존 설계) · CICD: 수동 트리거 등 행위 테이블 · Release: 배포 실행 기록 · **SignTool: 서명 행위 기록 — 보안 민감도 최상, 필수** |

- 행위 로그 공통 규약: `(user_id, service, action, target, result, timestamp)` 최소 필드 통일
  → 서비스가 분산/조합되어도 이력 형식 일관 유지, 추후 통합 조회 가능
- `service` 값은 §3.1 서비스 식별자(`studio`/`stage`/`release`/`signtool`) 사용

### 3.2 Bedrock: 사용자 본인 AWS SSO (device flow 내장)

CLI `aws sso login`과 동일한 **AWS SSO OIDC device authorization flow**를 백엔드가 수행.
사용자는 터미널 명령 불필요 — "AWS 연결" 버튼 → 승인 링크 클릭이 전부.

1. "AWS 연결" 클릭 → `sso-oidc:RegisterClient` → `StartDeviceAuthorization`
2. UI에 승인 링크 표시 → 브라우저 승인 (Knox/AD 페더레이션 시 클릭 1회 예상)
3. `sso-oidc:CreateToken` 폴링 → access token → `sso:GetRoleCredentials`
   → **본인 임시 AWS 자격증명** 수신
4. 암호화 보관, 백그라운드 스케줄러가 만료 30분 전 선제 refresh
5. SSO 세션 완전 만료 시에만 재승인 배너 (대화 상태는 DB 유지)

**효과**: CloudTrail 본인 명의 → CLI와 동일 비용/신원 집계.

### 3.3 GHE: 사용자 본인 신원 — OAuth App (1안) / PAT (폴백)

**전원이 repo write 권한 보유 개발자.** 봇 없이 본인 명의로 모든 GHE 작업 수행.

**왜 브라우저의 GHE SSO 세션을 재사용할 수 없나:** Knox→GHE 로그인 쿠키는 사용자
브라우저의 github.samsungds.net 도메인에만 존재. Studio 백엔드의 push/dispatch는
서버 대 서버 통신 + ThreadPool 비동기 실행(사용자가 화면을 떠난 뒤에도 동작)이므로
**서버가 사용자 명의의 API 토큰을 보유**해야 한다. 토큰 획득 방법이 아래 2가지.

**1안 — GHE OAuth App (권장): "버튼 클릭 SSO" 경험 재현**

```
1. Studio를 GHE에 OAuth App으로 1회 등록 (관리자 작업)
2. 사용자: Studio에서 "GitHub 연결" 클릭
3. → GHE 승인 페이지 (Knox/GHE 세션 있으면 승인 버튼 클릭 1회)
4. → 콜백으로 복귀, 백엔드가 사용자 명의 access token 자동 획득
5. 암호화 저장 → push/dispatch/조회에 사용 (커밋 author=본인)
   refresh token으로 자동 갱신 (GHE 만료 정책에 따름)
```

- PAT 대비: 수동 발급·복사·붙여넣기 없음, 만료 자동 갱신, UX가 AWS 연결(§3.2)과 대칭
- 신원 3종 통일: Knox / AWS / GHE 전부 "버튼 클릭 승인 → 서버가 본인 토큰 암호화 보관"
- git push는 https `x-access-token:{token}` 형식 사용

**2안 — 개인 PAT (폴백, OAuth App 등록이 정책상 불가할 때)**

- 사용자가 직접 발급 후 Studio 설정 화면에 등록, 유효성 검증 후 암호화 저장
- fine-grained 지원 시: 대상 repo만 Contents(RW)+Actions(RW) 권장
- classic만 가능 시: `repo` scope — 계정 전체 repo에 미치므로 보관·통제 강화 명시

**공통 가드**: 커밋이 `.github/workflows/`를 수정하는 경우 push 거부
(classic PAT의 workflow scope 문제 + 보안 경계 이중 목적. §6.3)

**실환경 확인 필요 (12.1)**: OAuth App 등록 권한/절차(org 정책), 토큰 만료 정책 값,
OAuth 토큰의 git push 동작 검증.

---

## 4. Bedrock 호출 계층

### 4.1 `invoke_claude()` 추상화 (필수)

```python
def invoke_claude(user_id, session_id, messages, system):
    creds = get_user_aws_credentials(user_id)   # device flow 캐시 조회/갱신
    client = boto3.client("bedrock-runtime", **creds)
    resp = client.converse(
        modelId=MODEL_ID,
        messages=messages,
        system=[{"text": system, "cachePoint": ...}],   # prompt caching
        requestMetadata={"user_id": user_id, "session_id": session_id,
                         "app": "toolhub-studio"},
    )
    record_usage(user_id, session_id, resp["usage"])
    return resp
```

- 신원 방식 변경 시 이 함수 내부만 수정
- 모든 호출에 `requestMetadata` 태깅 → invocation logging 활성화 시 AWS 측 사용자별 기록

### 4.2 컨텍스트/비용 최적화

- **prompt caching**: 분석 md, 시스템 프롬프트를 캐시 블록 분리 (멀티턴 반복 비용 절감 핵심)
- 세션당 최대 턴 수 제한 + 오래된 턴 요약 (최근 N턴 원문 + 이전 요약)
- Bedrock TPM/RPM 쿼터는 계정 단위 공유 → 사용자당 동시 진행 1건 제한으로 시작
- throttling(429) 지수 백오프, 대기 상태 표기

### 4.3 장시간 작업 처리 모델

- `POST /api/studio/message` → ThreadPoolExecutor(8)에 작업 제출, `studio_id` 즉시 반환
  (신규 요구조건이면 studio_id 신규 발급, 반복이면 기존 studio_id 아래 새 build 회차 추가)
- 상태 전이는 **build 회차 단위**: `generating` → `pushing` → `ci_running` → `pass/fail/cancelled` (DB 기록)
  studio 단위 상태는 별도: `open`(반복 중) → `done`(사용자 종료/채택) / `abandoned`
- 프론트는 `GET /api/studio/status/{studio_id}` 폴링 — 최신 회차 상태 + 회차 이력 반환
- 요청 취소: `generating` 상태면 취소 플래그 → 스레드가 체크포인트마다 확인
- Celery/Redis 도입 안 함 (동시 작업 ≤7). 서버 재시작 시 진행 중 작업은
  `failed(restart)` 처리 후 재시도 안내

---

## 5. 데이터 모델 (SQLite + WAL, studio.db)

기존 CICD SQLite와 파일 분리. `PRAGMA journal_mode=WAL; busy_timeout=5000;`

```sql
users(user_id PK, ad_id, display_name, ghe_login, is_admin, created_at)

aws_credentials(user_id PK→users, access_key_enc, secret_key_enc,
                session_token_enc, expires_at, refresh_token_enc, updated_at)

ghe_credentials(user_id PK→users, auth_type,             -- oauth/pat
                token_enc, refresh_token_enc, expires_at,
                verified_at, last_ok_at, updated_at)

user_branch_config(user_id PK→users, repo, branch_name, updated_at)
                -- 개인 설정 브랜치 (보호 브랜치 지정 금지 가드)

sessions(session_id PK, user_id→users, title, tool_target,
         status, created_at, updated_at)

messages(message_id PK, session_id→sessions, role, content,
         input_tokens, output_tokens, created_at)

attachments(attachment_id PK, session_id→sessions, filename, mime_type,
            storage_path, parsed_text_path, created_at)

studios(studio_id PK, session_id→sessions, user_id, repo, branch_name,
        status,        -- open(반복 중)/done(종료·채택)/abandoned
        created_at, completed_at)
        -- 확정 requirements 1건에 대한 생성~검증 반복의 단위. 1 studio_id : N builds

builds(build_id PK, studio_id→studios, attempt,   -- studio 내 회차 번호 (1,2,…)
       commit_sha, run_id, buildid,               -- buildid는 stage가 발급
       status,        -- generating/pushing/push_conflict/ci_running/pass/fail/cancelled
       fail_summary,  -- CI 실패 요약 — 다음 회차 생성 컨텍스트로 주입 (§7.1 Step 5)
       created_at, completed_at)

usage_log(id PK, user_id, session_id, studio_id, build_id,
          input_tokens, output_tokens, cache_read_tokens, model_id, created_at)

prompts(prompt_id PK, name, version, content, created_at)
```

- 자격증명 컬럼(AWS/GHE 공통): Fernet 암호화, 키는 서버 로컬 600 권한 파일
- 확장 경로: 사용자 증가 시 스키마 그대로 PostgreSQL 이관 가능하게 유지

---

## 6. GHE 연동 (개인 PAT & 자유 브랜치)

### 6.1 브랜치 정책

- 사용자가 Studio 설정에서 **자신의 작업 브랜치를 지정** (`user_branch_config`)
- **가드 (필수)**: `main` 및 보호 브랜치는 대상 지정 불가 — 저장 시 GHE API로
  protection 여부 확인 후 거부
- 브랜치 존재하지 않으면 지정 base(기본 main)에서 생성 제안
- 관리 주체는 **관리자**: Studio 관리자 화면에서 요청 이력이 있는 브랜치 목록
  (마지막 커밋일, CI 상태 포함) 조회 → 정리 판단은 사람이. **자동 삭제 없음**

### 6.2 CI/CD 트리거: workflow_dispatch

자유 브랜치는 push 패턴 트리거가 불가능하므로 **명시 호출** 방식.

```yaml
# .github/workflows/studio-verify.yml
on:
  workflow_dispatch:
    inputs:
      studio_id: {required: true}
      attempt:   {required: true}      # studio 내 회차 번호
      user:      {required: true}
run-name: "studio-${{ inputs.studio_id }}#${{ inputs.attempt }} (${{ inputs.user }})"
concurrency:
  group: studio-${{ github.ref }}      # 같은 브랜치 재실행 시
  cancel-in-progress: true             # 이전 회차 run 자동 취소 (새 회차가 대체)
```

**트리거 체인:**

```
Flask(ThreadPool)
 → 사용자 PAT로 커밋 생성(author=본인) + push (대상: 본인 설정 브랜치)
 → 사용자 PAT로 POST /repos/{owner}/{repo}/actions/workflows/studio-verify.yml/dispatches
    body: { ref: {branch}, inputs: { studio_id, attempt, user } }
 → run 시작 → Zone1 Arbiter (frozen matrix, terminal-state gating 재사용)
 → studio_id/attempt는 inputs로 직접 전달 → meta.json에 기록 (브랜치명 파싱 불필요)
 → Zone2 Build / Zone3 Test (격리 runner 그룹, §6.4)
 → 완료 webhook → POST /api/studio/ci-callback
 → builds.status 갱신 + CI 로그 요약을 세션 대화에 자동 주입 (fail_summary 저장)
```

**run 추적 (dispatch API는 run id를 반환하지 않음):**

- `run-name`에 studio_id#attempt 포함 (회차까지 있어야 재시도 간 유일) → dispatch 직후
  runs API를 짧게 폴링하여 run-name 매칭으로 run_id 확보 → `builds.run_id` 저장
- webhook 유실 대비: `ci_running` 건은 run_id 기준 주기 폴링 fallback

### 6.3 커밋 규칙

- author = committer = 사용자 본인 (PAT 소유자, `users.ghe_login` 기준)
- 커밋 메시지: `[studio] id={studio_id} attempt={n} session={session_id}` + 요약
- 확정 requirements md 동반 커밋 (생성 근거 보존)
- **workflow 파일 수정 금지 가드**: 생성 결과에 `.github/workflows/` 변경이 포함되면
  push 거부 + 사용자 안내 (PAT scope 문제 + 보안 경계 이중 목적)

### 6.4 생성 코드 실행 보안 경계 ⚠️

Claude 생성 코드가 self-hosted runner에서 실행됨 → 첨부 문서 내 악성 지시(prompt
injection) 경유 임의 코드 실행 위험. 대책:

- studio-verify.yml은 **secrets 미사용** (environment 분리)
- Studio 검증 전용 **격리 runner 그룹** (릴리즈 runner와 분리 — 경합 완화 겸용)
- push 전 정적 검사 게이트: 네트워크 호출/자격증명 접근/시스템 명령 등 위험 패턴 스캔 후 경고
- workflow 파일 수정 금지 가드 (§6.3)
- runner egress는 기존 인트라넷 프록시 정책 유지

### 6.5 push 충돌 정책 (자유 브랜치의 필연 시나리오)

사용자 설정 브랜치는 로컬 작업에도 쓰일 수 있어 **non-fast-forward 충돌**이 발생한다.

- Studio는 push 직전 **원격 HEAD를 fetch → 그 위에 커밋 생성** (stale base 방지)
- 그래도 거부되면(push 사이에 사용자가 먼저 push): 1회 재시도(재fetch 후 재생성)
- 동일 파일 충돌 시: **강제 push 절대 금지** → 상태 `push_conflict`로 전이 +
  "로컬 변경과 충돌" 안내, 사용자가 브랜치 정리 후 재시도 판단
- 상태 전이 확장: `generating → pushing → (push_conflict) → ci_running → ...`

### 6.6 검증 통과 이후 워크플로우 (미정 — Todo)

CI pass 산출물의 main 반영 방식 결정 필요. 브랜치가 본인 소유이므로 자연스러운 흐름은:

- 안 A: 사용자가 본인 브랜치에서 직접 PR 생성 (기존 개발 플로우 그대로, MVP 권장)
- 안 B: Studio "PR 생성" 버튼 → 본인 PAT로 PR 자동 생성 (본인 명의)
- 어느 쪽이든 **main merge는 사람 리뷰 필수** (자동 merge 금지)

---

## 7. 생성 파이프라인 (Step 정의)

원시 코드 전체가 아닌 **사전 분석 md**를 컨텍스트로 사용.
토큰 효율(prompt caching)과 생성 품질 모두 원시 코드 주입보다 우수.

### 7.1 Step 흐름

```
Step 1. 컨텍스트 로드
  · 대상 tool 선택 → tool↔분석md 매핑 테이블로 자동 로드
  · md 기준 SHA vs repo HEAD 비교 → diff 크면 "md 오래됨" 경고 배지
  · md는 prompt caching 블록으로 고정

Step 2. 요구조건 정제 (확정 게이트) ★
  · 사용자 입력 + 첨부 문서 + 분석 md → Claude가 requirements 초안 생성
  · 사용자 검토·수정·승인 — 승인 전 코딩 단계 진입 불가
  · 목적: 흔들리는 요구조건으로 코딩 → CI 실패 루프 → 토큰 낭비 방지

Step 3. 코드 + testcase 생성 — 신규/수정 구분
  · 신규 파일 생성: 분석 md + 확정 requirements만으로 가능
  · 기존 파일 수정: 원문 필수 → **2-pass 구조**
     pass 1: Claude가 md 기반으로 수정 대상 파일 지목
     pass 2: Studio가 GHE에서 해당 원문 fetch → 컨텍스트 추가 → 재호출
  · 쓰기 전략: **파일 전체 교체** (diff/patch 적용은 어긋남 위험 → 미채택)
  · 필요 시 사용자가 직접 파일 지정 추가 주입 (옵션)

Step 4. 본인 브랜치 커밋/push → dispatch → CI race  (§6.2)

Step 5. CI 결과 자동 주입 → Step 3 루프 (사용자 판단 병행) — 누적 반복
  · 실패 시 같은 studio_id 아래 새 build 회차(attempt+1)로 반복
  · 이전 회차들의 생성 코드 + CI 실패 요약(fail_summary)을 컨텍스트에 누적 주입
    → LLM이 앞선 실수를 회피, 회차가 갈수록 결과 개선
  · 회차 이력은 builds 테이블에 보존 — 사용자는 회차별 diff/CI 결과 열람 가능
```

### 7.2 분석 md 규격 (ANALYSIS.md 필수 항목)

```markdown
# {tool_name} 분석
- 기준 커밋: {SHA}  /  최종 갱신: {date}
## 1. 파일 구조 (경로 트리 + 각 파일 역할)
## 2. 공개 인터페이스 (함수/클래스 시그니처, 파라미터 타입, 반환값)
## 3. 핵심 데이터 구조
## 4. 빌드/실행 방법 (CI가 실제 사용하는 명령 기준)
## 5. 코딩 컨벤션 (네이밍, 에러 처리, 로깅 규칙)
## 6. testcase 작성 규칙 (프레임워크, 파일 위치, 네이밍, 실행 방법)
## 7. 주의사항 (알려진 제약, 건드리면 안 되는 영역)
```

### 7.3 md 신선도(stale) 관리

- md 상단 기준 커밋 SHA 필수 → Step 1에서 HEAD와 자동 비교, 임계 초과 시 경고
- 장기: main 머지 시 분석 md 자동 재생성 파이프라인 (추후 확장)
- 저장 위치: 대상 repo 내 (`docs/analysis/{tool}.md`, 버전관리 대상)

---

## 8. 문서 입력 파이프라인

- 업로드: 멀티파트, 파일당 20MB 제한(초안), 세션 소유자만 접근
- 파싱: PDF/DOCX/TXT/MD → 텍스트 추출 후 저장, Bedrock에는 텍스트 전달. 대용량 청킹
- **HWP 지원 여부 확인 필요** (지원 시 별도 추출 도구 과제)
- 저장소: 서버 로컬로 시작, 용량 증가 시 기존 OBS(MinIO) 이관

---

## 9. 프론트엔드 (studio.html)

- 기존 dashboard.html과 동일 스택 (Vanilla, 빌드도구 없음), CSS/다크테마 공유
- 구성: 요구조건 입력 + 첨부 / 멀티턴 채팅 뷰 / Step 2 승인 UI /
  AWS 연결 배지 + device flow 승인 / **GHE PAT 등록 + 브랜치 설정 화면** /
  studio별 CI 상태 카드(회차별 buildid 이력, run/buildid 링크) / 요청 취소 버튼 /
  관리자: 브랜치 조회 화면
- 통신: REST 폴링 (MVP). SSE 스트리밍은 2단계

---

## 10. 비용/사용량

- AWS 신원이 본인이므로 CLI와 동일 집계 (사내 정산 방식 무관하게 대응 가능)
- 앱 레벨 미터링(usage_log)은 무조건 구현 — 운영 가시성/정책 근거
- 시작 정책: 무제한 + 사용자당 동시 1건 → 미터링 관찰 후 조정

### 10.1 성공 지표 (도입 효과 측정)

- **CI pass 도달률** (studio_id 기준 — 최종 회차 pass 비율) /
  **studio_id당 평균 build 회차 수** (Step 3↔5 루프 수렴 속도)
- **채택률**: 생성물이 실제 main에 merge된 비율 — 최종 가치 지표
- 사용자별 토큰 소비 대비 채택 건수 (비용 효율)
- 수집: studios/builds + usage_log 집계, 미터링 화면에 함께 표시

---

## 11. 운영

- 에러 처리: Bedrock 429 백오프 / push·dispatch 실패 재시도(3회) / webhook 유실 폴링 fallback
- 백업: studio.db + 첨부문서 일 1회, 보존 30일 (초안)
- 로그 보존: 대화·usage_log 180일 (초안)
- 프롬프트 버전관리: prompts 테이블 + repo 파일 이중 관리
- 모델 정책: MODEL_ID 고정(설정 파일), cross-region inference profile 사용 여부 명시
- 모니터링: TIG 스택에 Studio 지표 추가 (요청 수, 실패율, 생성 시간, 토큰)
- **운영 인수인계(bus factor)**: 관리자 2인 체계 + 운영 문서(장애 대응, Knox/GHE/AWS 토큰
  문제 해결, 백업 복구 절차) 작성 — 1인 의존 탈피
- **모델 버전 대응**: MODEL_ID 교체 시 대표 요구조건 3~5건으로 스모크 테스트(생성→CI pass
  확인) 후 전환. 프롬프트 회귀 발견 시 prompts 테이블에서 버전 분기

---

## 12. Todo

### 12.1 사전 확인 (구현 전 질문)

- [ ] Knox SSO 운영 조직 확인 + 신규 서비스(ToolHub) 연동 신청 절차·리드타임 파악 — **4개 서비스(Studio/CICD/Release/SignTool) 콜백 포함** **(Phase 1 전 필수)**
- [ ] Knox SSO 프로토콜 확인 (SAML → `mod_auth_mellon` / OIDC → `mod_auth_openidc`) — GHE/Jira 연동 방식 참고
- [ ] **GHE OAuth App 등록 권한/절차 확인 (org 정책, 관리자 승인 여부)** **(Phase 2 전 필수)**
- [ ] GHE OAuth 토큰 만료 정책 값 + OAuth 토큰 git push 동작 검증 (실환경 테스트)
- [ ] (폴백 대비) GHE fine-grained PAT 지원 여부 확인
- [ ] SignTool의 행위 로그 요구 수준 확인 (서명 기록 보존 기간, 감사 대상 범위)
- [ ] AWS SSO 세션 길이 확인 → 재승인 UX 빈도 예측 **(Phase 1 전 필수)**
- [ ] AWS SSO 승인이 Knox/AD에 페더레이션되어 있는지 확인 (승인 클릭 횟수 예측)
- [ ] 첨부 문서 포맷 범위 확정 — **HWP 포함 여부** (공수 크게 좌우)
- [ ] 첨부 문서의 사내 데이터 반출 정책 확인 — Bedrock 전송 가능 등급인지
- [ ] 사내 CLI 사용자별 비용 집계 방식 확인 → 정산 리포트 소스 결정
- [ ] 사내 보안/AI 심의 필요 여부 확인 — 코드의 Bedrock 전송은 CLI 선례 있으나 "공식 서비스"화 시 별도 심의 대상 가능
- [ ] 관리자 롤 지정 (users.is_admin) — **2인 체계** 권장, 브랜치 관리/미터링 열람 권한 범위
- [ ] 사전 분석 md 관리 주체/갱신 주기 결정
- [ ] 검증 통과 후 워크플로우 결정: 수동 PR(안 A) vs Studio PR 버튼(안 B) — §6.6

### 12.2 Phase 1 — 코어

- [ ] studio.db 스키마 생성 (WAL, busy_timeout, ghe_credentials/user_branch_config 포함) + Fernet 키 관리(600)
- [ ] AWS SSO device flow 구현 (RegisterClient → StartDeviceAuthorization → CreateToken 폴링 → GetRoleCredentials)
- [ ] AWS 자격증명 캐시/갱신 + 백그라운드 선제 refresh 스케줄러(만료 30분 전) + 재승인 배너
- [ ] AWS 연결 온보딩 UI 플로우 (최초 접속 안내 → 승인 링크 → 완료 확인)
- [ ] `invoke_claude()` 추상화 + requestMetadata + usage 기록
- [ ] prompt caching 적용 (시스템 프롬프트/분석 md 캐시 블록)
- [ ] ThreadPoolExecutor 작업 모델 + 상태 전이 + 취소 플래그
- [ ] REST API: POST /message, GET /status/{id}, 세션 목록/재개, 첨부 업로드
- [ ] 문서 파싱 파이프라인 (PDF/DOCX/TXT/MD, 20MB, 청킹)
- [ ] 멀티턴 히스토리 정책 (최근 N턴 + 요약)
- [ ] tool ↔ 분석 md 매핑 테이블 + Step 1 자동 로드
- [ ] md 기준 SHA vs HEAD 비교 → stale 경고 배지
- [ ] Step 2 확정 게이트: requirements 초안 → 검토/수정/승인 UI + 상태 전이
- [ ] ANALYSIS.md 규격 템플릿 + 대상 tool 최소 1개 분석 md 작성
- [ ] studio.html: 입력/채팅/승인/AWS 배지/취소 (dashboard CSS 공유)
- [ ] 시스템 프롬프트 초안 (요구조건 생성용 / 코드+testcase 생성용) + prompts 테이블
- [ ] gunicorn gthread(worker 2 × threads 8) + systemd 반영

### 12.3 Phase 2 — GHE/CI 연동

- [ ] GHE OAuth App 등록 (관리자 1회) + "GitHub 연결" 버튼 → 승인 → 콜백 → 토큰 획득 플로우 구현
- [ ] GHE 토큰 암호화 저장 + refresh 자동 갱신 + 401/만료 감지 → 재연결 배너
- [ ] (폴백) PAT 등록 화면 + 발급 가이드 (OAuth App 불가 시에만 활성화)
- [ ] 브랜치 설정 화면 (user_branch_config) + **보호 브랜치 지정 금지 가드** + 미존재 시 생성 제안
- [ ] 커밋 생성 로직: author=본인, 메시지 규칙, requirements md 동반 커밋
- [ ] **workflow 파일 수정 금지 가드** (생성 결과에 .github/workflows 변경 시 push 거부)
- [ ] `studio-verify.yml` 작성: workflow_dispatch(inputs: studio_id/attempt/user) + run-name + concurrency 취소 — **stage 측 파일** (변경 조율 필요)
- [ ] dispatch 호출 + run-name(studio_id#attempt) 매칭으로 run_id 확보 → builds.run_id 저장
- [ ] secrets 미사용 environment 분리 + **격리 runner 그룹** 지정
- [ ] meta.json에 studio_id/attempt 필드 추가 — **stage 측(Arbiter) 수정** (변경 조율 필요, inputs 경유)
- [ ] build 회차 모델 구현: 실패 시 attempt+1 생성 + 이전 회차 fail_summary 컨텍스트 누적 주입 (§7.1 Step 5)
- [ ] CI 완료 webhook `/api/studio/ci-callback` + 서명 검증
- [ ] webhook 유실 대비 run_id 기준 폴링 fallback
- [ ] CI 로그 요약 → 대화 자동 주입 (실패 로그 추출 규칙)
- [ ] push 충돌 처리 (§6.5): 원격 HEAD 기반 커밋 + 1회 재시도 + push_conflict 상태/안내 UI
- [ ] Step 3 2-pass 구현: 수정 대상 파일 지목 → GHE 원문 fetch → 재호출 (파일 전체 교체 방식)
- [ ] Bedrock 429 백오프 / push·dispatch 재시도(3회) 에러 처리

### 12.3.5 Phase 2.5 — 파일럿 (7명 오픈 전 필수)

- [ ] 본인 + 1명으로 1~2주 파일럿 운영
- [ ] 확인 항목: CI 루프가 실제 수렴하는가(반복 횟수), 프롬프트 품질, 분석 md 충분성, push 충돌 빈도
- [ ] 성공 지표(§10.1) 첫 수집 → 프롬프트/md 규격 개선 반영 후 확대 결정

### 12.4 Phase 3 — 오픈 준비

- [ ] Knox SSO 기반 로그인 연동 (프로토콜은 12.1 확인 결과에 따름):
  - [ ] Knox SSO 서비스 등록 완료 (신청은 12.1에서 선행)
  - [ ] Apache 인증 모듈 설치·설정 (`mod_auth_mellon` 또는 `mod_auth_openidc`, 폴백 `mod_authnz_ldap`)
  - [ ] REMOTE_USER → Flask 전달 검증 + users 자동 등록(최초 로그인)
  - [ ] **4개 서비스(Studio/CICD dashboard/Release/SignTool) SSO 통일 적용**
  - [ ] 테스트 계정 로그인/권한 검증
- [ ] Apache LogFormat에 `%u`(REMOTE_USER) 추가 — 4개 서비스 접근 로그 + logrotate 보존 정책
- [ ] 행위 로그 공통 규약 적용: CICD/Release/SignTool에 행위 테이블 추가 `(user_id, service, action, target, result, timestamp)`
- [ ] Flask 5000 localhost 바인딩 + Apache 우회 차단 확인
- [ ] 위험 패턴 정적 검사 게이트 (push 전 스캔 + 경고)
- [ ] 관리자 브랜치 조회 화면 (요청 이력 브랜치 + 마지막 커밋일 + CI 상태)
- [ ] 사용자별 미터링 조회 화면 (usage_log 집계 + §10.1 성공 지표 표시)
- [ ] 운영 문서 작성 (장애 대응, 토큰 문제 해결, 백업 복구) + 관리자 2인 인수인계
- [ ] studio.db + 첨부문서 백업 cron (일 1회, 보존 30일)
- [ ] TIG 지표 연동 (요청 수/실패율/생성 시간/토큰)
- [ ] 사용자당 동시 진행 1건 제한 구현
- [ ] AWS SSO 사용 가이드 (최초 연결 스크린샷, 재승인, 트러블슈팅)
- [ ] GHE 연결 가이드 (OAuth 승인 절차, 재연결 방법; 폴백 시 PAT 발급 절차)
- [ ] 7명 대상 Studio 사용 가이드 (요구조건 입력 → 확정 → 검증 흐름)

### 12.5 추후 / 확장

- [ ] SSE 스트리밍 (gunicorn worker class 변경 + Apache 버퍼링 해제)
- [ ] HWP 파싱 지원 (12.1 확인 결과에 따라)
- [ ] Studio PR 생성 버튼 (안 B 채택 시, 본인 PAT로 생성)
- [ ] 사용자 증가 시 PostgreSQL 이관 (스키마 호환 유지)
- [ ] 첨부문서 저장소 OBS(MinIO) 이관
- [ ] 일일 토큰 쿼터 정책 (미터링 데이터 기반)
- [ ] 세션당 턴 수 상한 조정 / 요약 품질 개선
- [ ] main 머지 시 분석 md 자동 재생성 파이프라인 (stale 근본 해결)
- [ ] Step 3에 특정 파일 원문 추가 주입 옵션 UI
