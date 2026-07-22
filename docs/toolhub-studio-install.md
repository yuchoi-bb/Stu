# ToolHub Studio 설치 · 이식 런북

> 대상: 신규 **Studio** 서비스를 stage/release/signtool이 **이미 동작 중인** 환경에
> 나란히 올리고, 공통 감사 규약을 기존 3서비스에 이식하는 절차.
> 사전 조직 확인은 `toolhub-studio-precheck.md`, 설치 후 운영은
> `toolhub-studio-ops.md`. 이 문서는 그 사이의 **최초 설치/이식** 단계만 다룬다.

## 0. 이식은 두 갈래다 (헷갈리지 말 것)

| 갈래 | 내용 | 코드 변경 | 위험 |
|---|---|---|---|
| **A. Studio 신규 설치** | Flask/gunicorn 서비스 1개를 새로 기동 | 없음(그대로 배포) | 격리됨 — 기존 서비스 무영향 |
| **B. 공통 감사 이식** | stage/release/signtool에 로그인+행위 이력 규약 적용 | 각 서비스에 소량 | 기존 서비스 건드림 → 저위험부터 |

**순서 권고**: A(격리 설치·검증) → B는 서비스별로 **가장 안전한 것부터**
(① Apache 접근로그 %u = 코드 0줄 → ② 앱 행위이력 record()).
A와 B는 독립이므로 A를 먼저 파일럿하고 B는 서비스팀 일정에 맞춰 분리 진행 가능.

---

## A. Studio 신규 설치

기존 stage/release/signtool과 **포트·프로세스·DB가 완전히 분리**된 별도 서비스다.
gunicorn은 `127.0.0.1:5000`만 바인딩(§3.1)하고 외부 노출은 Apache가 담당하므로,
기존 서비스의 VirtualHost와 **Location만 다르면** 같은 호스트에 공존한다.

> **작업 우선순위(모레) — SSO는 최후순위.** Knox SSO 로그인 연동과 AWS SSO(Bedrock
> device flow)는 **마지막 단계 A-8**에서 붙인다. 그 전까지(A-1~A-6)는 인프라·서비스·
> DB·GHE·백업을 먼저 올려 검증하고, UI/생성 경로 점검이 필요하면 **bring-up
> 바이패스**(로컬 `X-Remote-User` 수동 주입, 아래 A-4 주석)로 확인한다. `doctor`도
> SSO 미설정을 **WARN(후순위)**로만 표시하고 게이트를 막지 않는다.
>
> 순서: **A-1 준비 → A-2 env(SSO 제외) → A-3 키/DB/관리자 → A-4 Apache(프록시先, SSO後)
> → A-5 systemd/백업 → A-6 검증 → (GHE 스모크) → A-8 SSO 연동(마지막) → A-7 전체 1왕복**.

### A-1. 사전 준비
```bash
# 전용 계정 · 디렉터리
sudo useradd -r -s /usr/sbin/nologin toolhub
sudo mkdir -p /opt/toolhub/data /opt/toolhub/backups
sudo chown -R toolhub:toolhub /opt/toolhub

# 코드 배치 + venv (Python 3.11+)
sudo -u toolhub git clone <repo> /opt/toolhub        # 또는 릴리스 tar 전개
cd /opt/toolhub
sudo -u toolhub python3.11 -m venv venv
sudo -u toolhub venv/bin/pip install -r requirements.txt
```

### A-2. 환경변수 (systemd EnvironmentFile 권장)
`.service`에 나열하지 말고 `/opt/toolhub/data/studio.env`(600, toolhub 소유)로 분리.
`deploy/toolhub-studio.service`의 `Environment=` 대신 `EnvironmentFile=`를 쓴다.

```ini
# ── 필수 ──
STUDIO_DB=/opt/toolhub/data/studio.db
STUDIO_FERNET_KEY=/opt/toolhub/data/.fernet.key
STUDIO_ATTACH_DIR=/opt/toolhub/data/attachments
STUDIO_LOG_DIR=/opt/toolhub/data/logs
# ── Bedrock 모델 (§3.2) ──
STUDIO_MODEL_ID=<inference-profile-or-model-id>
STUDIO_BEDROCK_REGION=<region>
# ── AWS SSO (§3.2) — 후순위(A-8)에서 채운다. A-1~A-7 동안 비워둬도 됨 ──
STUDIO_SSO_START_URL=<aws-sso-start-url>       # A-8
STUDIO_SSO_REGION=<region>                     # A-8
STUDIO_SSO_ACCOUNT_ID=<account>                # A-8
STUDIO_SSO_ROLE_NAME=<role>                    # A-8
# ── GHE (§3.3, §6) ──
STUDIO_GHE_BASE=https://github.samsungds.net
STUDIO_GHE_API=https://github.samsungds.net/api/v3
STUDIO_GHE_OWNER=<org>
STUDIO_GHE_BASE_BRANCH=main
STUDIO_GHE_OAUTH_CLIENT_ID=<id>          # OAuth 1안. 미사용 시 PAT 폴백
STUDIO_GHE_OAUTH_CLIENT_SECRET=<secret>
STUDIO_CI_WEBHOOK_SECRET=<hmac-secret>   # ci-callback 서명 검증
# ── 선택: OBS 에러로그(§2.6 ops) · 미설정이면 로컬만 ──
STUDIO_OBS_ENDPOINT=https://minio.intra:9000
STUDIO_OBS_BUCKET=toolhub-studio-logs
STUDIO_OBS_ACCESS_KEY=<ak>
STUDIO_OBS_SECRET_KEY=<sk>
```

### A-3. 키·DB·첫 관리자 부트스트랩
```bash
# Fernet 키 생성 (분실 = 저장된 모든 토큰 복호화 불가 → 안전 보관, 백업 tar 제외)
sudo -u toolhub venv/bin/python -c \
  "from cryptography.fernet import Fernet;open('/opt/toolhub/data/.fernet.key','wb').write(Fernet.generate_key())"
sudo chmod 600 /opt/toolhub/data/.fernet.key

# 스키마 생성(=마이그레이션 겸용, 멱등). 앱 최초 기동 시 자동 실행되나 명시 실행 권장
sudo -u toolhub venv/bin/python -m studio.manage health   # db: ok 확인 = 스키마 생성됨

# 최초 관리자 2인 부트스트랩(§3.1.2 — 1인 의존 금지)
sudo -u toolhub venv/bin/python -m studio.manage set-admin <user1>
sudo -u toolhub venv/bin/python -m studio.manage set-admin <user2>
```

### A-4. Apache 리버스 프록시 (SSO 인증은 A-8로 분리)
`deploy/apache-toolhub-studio.conf.reference` 사용. **지금(A-4)은 프록시 골격만** 올리고
**Knox SSO 인증 블록은 A-8에서** 켠다. 지금 확인할 것:
1. `ProxyPass /api/studio http://127.0.0.1:5000/api/studio` — 기존 서비스 Location과
   겹치지 않는 경로.
2. `RequestHeader unset X-Remote-User` — 클라이언트발 스푸핑 헤더 제거(§3.1 신뢰 경계).
   **이 unset은 SSO 유무와 무관하게 항상 켜 둔다.** SSO가 붙기 전(A-8 전)에는 그 아래
   `REQUEST_HEADER` 주입이 없으므로 인증 사용자가 없다.
3. 접근로그 `%u` LogFormat(= B의 로그인 이력 커버, 아래 B-1).

> **bring-up 바이패스(SSO 붙기 전 UI/생성 점검용, 로컬 한정)**: A-8 전에 경로를
> 검증하려면 **서버 내부에서만**(127.0.0.1) 테스트 헤더로 호출한다 —
> `curl -H 'X-Remote-User: <tester>' http://127.0.0.1:5000/api/studio/...`.
> ⚠ 외부에 열린 Apache는 위 `unset`이 이 헤더를 반드시 제거하므로 우회로가 되지 않는다.
> 이 바이패스는 로컬 스모크 전용이며 A-8에서 SSO가 붙으면 더는 필요 없다.

### A-5. systemd 등록 + 백업 타이머
```bash
sudo cp deploy/toolhub-studio.service /etc/systemd/system/     # EnvironmentFile로 수정본
sudo cp deploy/toolhub-studio-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now toolhub-studio
sudo systemctl enable --now toolhub-studio-backup.timer
```

### A-6. 검증(설치 성공 판정)
```bash
curl -s https://<host>/api/studio/health | jq       # db: ok, disk 충분, healthy: true
sudo -u toolhub venv/bin/python -m studio.manage admins   # 관리자 2인 확인
sudo -u toolhub venv/bin/python -m studio.manage users    # 매핑 확인
```
- 분석 md 등록(§7.2): `manage.py map-analysis a-tool <repo> docs/analysis/a-tool.md`

> 여기까지(A-1~A-6)가 **SSO 없이** 검증 가능한 범위다. GHE 연결/브랜치/생성/커밋/CI는
> bring-up 바이패스(A-4)로 먼저 점검해 둘 수 있다. 로그인 인증만 A-8에서 붙인다.

### A-8. SSO 연동 (후순위 — 마지막 단계)

두 갈래를 여기서 붙인다. 앞 단계가 다 초록(`doctor` FAIL 0)이 된 뒤 진행한다.

1. **Knox SSO 로그인**(웹 UI 인증) — A-4에서 올린 Apache 프록시에 인증 블록 추가:
   프로토콜에 맞춰 `mod_auth_mellon`(SAML) 또는 `mod_auth_openidc`(OIDC)로 보호하고,
   인증된 `REMOTE_USER`를 `RequestHeader set X-Remote-User %{REMOTE_USER}s`로 주입.
   **`unset`(A-4-2) 다음 줄에 set이 오도록** 순서를 지킨다(스푸핑 차단 유지).
2. **AWS SSO**(Bedrock device flow) — `studio.env`의 `STUDIO_SSO_*` 4값을 채우고
   `systemctl restart toolhub-studio`. 사용자는 UI "AWS 연결"에서 승인 링크로 1회 승인.

검증:
```bash
sudo -u toolhub .../python -m studio.manage doctor    # SSO WARN → PASS 로 바뀌는지
# 실제 SSO 로그인(브라우저)으로 접속 → 본인 ID 표시 + audit 로그인 이력 기록 확인
sudo -u toolhub .../python -m studio.manage audit --action login --limit 5
```
- 완료 후 **bring-up 바이패스는 더 쓰지 않는다**(외부 경로엔 원래 통하지도 않음).
- SSO가 늦어지면 A-1~A-6 + GHE 스모크까지는 이미 검증돼 있으니, SSO만 붙이면
  곧바로 A-7 전체 1왕복으로 넘어갈 수 있다.

### A-7. D-day 스모크 — 첫 사용자 1명 실제 1왕복 (오픈 판정)

**A-8까지 끝난 뒤** 실행한다(로그인·AWS 연결이 필요하므로). 설정이 다 맞아도 **실제
생성→CI pass가 한 번 돌아야** 이식 성공이다. 무인 자동화가 어려운 구간(AWS device flow
승인·GHE OAuth)이라, **첫 사용자 1명이 손으로** 아래를 한 번 통과시키는 걸 오픈 판정
기준으로 삼는다. 각 단계에서 실패하면 바로 옆의 로그를 본다(§C-1 매트릭스와 연결).

| # | 단계 | 성공 신호 | 실패 시 볼 곳 |
|---|---|---|---|
| 1 | UI 접속(Knox SSO, **A-8**) | 본인 ID로 로그인됨 | Apache error / `audit --action login` |
| 2 | AWS 연결(device flow 승인, **A-8**) | "AWS 연결됨" 배지 | `studio-log`·`doctor` SSO 3값 |
| 3 | GHE 연결(OAuth 또는 PAT) | `manage.py users`에 `ghe=<login>` | `doctor` GHE·`audit` |
| 4 | 작업 브랜치 설정 | 브랜치 저장됨 | `manage.py set-branch` 로 대체 확인 |
| 5 | 요구조건 1건 입력→**Step 2 승인** (검증 방식 `ci` 선택, §6.8) | studio 발급(`show-studio`) | studio-log `[step2]` |
| 6 | 생성 | 파일 생성됨(`[step3] 생성 파일`) | studio-log `[gen]`·OBS |
| 7 | 리뷰(Step 3.5)→커밋/push | 자유 브랜치에 커밋 | studio-log `push`·`audit push_dispatch` |
| 8 | **CI 트리거→pass** | build `status=pass`(`show-studio`) | stage run 로그·`failures` |
| 9 | (선택) PR 생성(§6.6 안 B) | `pr_url` 채워짐 | studio-log `[pr]`·`audit create_pr` |

```bash
# 스모크 진행 중 실시간 관찰 (관리자 터미널 2개)
watch -n2 'sudo -u toolhub .../python -m studio.manage show-studio <studio_id>'
tail -f /opt/toolhub/data/logs/studio/S-*.log      # 해당 studio 디버그 로그
```

- **판정**: 1왕복이 8번(CI pass)까지 도달하면 오픈 가능. 9번은 선택.
- (선택) **stage 전달 게이트(§6.8)도 확인**: 두 번째 요구조건을 "코드만 준비"로
  승인 → push 후 `pushed` 대기 → "stage 검증 시작" 버튼으로 전달되는지 1회.
- 첫 왕복은 `STUDIO_LOG_LEVEL=DEBUG`로 두고 전 구간 로그를 남겨 기준선(baseline)을
  확보한다. 이후 실사용 문제를 이 기준선과 비교해 빠르게 좁힌다.
- CI가 fail이면 코드/분석 md/프롬프트 중 어디 문제인지 `fail_summary`(+OBS 로그)로
  판별 → 분석 md stale이면 갱신(§7.3), 프롬프트 회귀면 prompts 버전 분기(§11).

---

## B. 공통 감사 규약 이식 (stage / release / signtool)

참조 구현·규약: `deploy/audit-contract/`(README·audit.py·audit.php·schema.sql) +
`docs/toolhub-audit-contract.md`. Studio는 이미 이 규약의 참조 구현(자체 audit).

### B-1. 로그인/접근 이력 — **앱 코드 0줄**
각 서비스 Apache VirtualHost에 `%u`(REMOTE_USER) LogFormat + `service=` 태그만 추가
(`deploy/audit-contract/apache-access-log.conf.reference`). SSO가 이미 붙어 있으므로
로그인 이력은 이걸로 4개 서비스 전부 커버된다. **가장 먼저, 가장 안전.**

### B-2. 행위 이력 — 서비스 언어에 맞게 택1
상태 변경(버튼→서버 처리) 지점마다 `record(user, action, target, result)` 1줄.
- PHP: `audit.php` → `Audit::record(...)`
- Python: `audit.py` → `record(...)`
- 그 외: `schema.sql` 테이블에 같은 컬럼으로 INSERT.
- 서비스별 action 예: stage=`build_trigger`/`build_result` · release=`deploy` ·
  signtool=`sign`.

통합 조회는 공통 테이블을 함께 쓰거나, 각자 DB에 같은 형식으로 남기고 TIG/ELK에서
`service` 필드로 필터.

---

## C. 이식/배포 중 디버그 로그 — "어디를 보나"

> **이식 당일 첫 명령**: `manage.py doctor [--net]` — 필수 경로·Fernet 키·DB 스키마·
> Bedrock/GHE/CI 설정·관리자 수·(옵션)네트워크 도달성을 한 번에 점검하고, 각 실패에
> **바로 실행할 조치**를 붙여 출력한다. **FAIL이 하나라도 있으면 exit 1** → 이식 진행
> 게이트로 쓴다. 아래 표는 doctor가 통과한 뒤 런타임에서 문제가 날 때의 지도다.

설치·이식 **전용** 로그 파일이 따로 있지는 않다. 대신 단계별로 봐야 할 곳이 정해져 있다.

| 무엇을 볼 때 | 어디 | 명령 |
|---|---|---|
| 서비스 기동 실패 / 부팅 에러 | systemd 저널 | `journalctl -u toolhub-studio -n 200 -f` |
| 앱 런타임(요청·예외·백그라운드 스레드) | 앱 로그 | `tail -f /opt/toolhub/data/logs/studio.log` (10MB×5 회전) |
| 설치 성공 판정 | health | `curl .../api/studio/health \| jq` (`db`/`disk`/`healthy`) |
| **특정 studio 전 과정**(생성/스캔/push/CI/리뷰/PR) | studio별 디버그 로그 | `manage.py studio-log <id>` → `logs/studio/S-YYMMDD-HHMMSS.log` |
| **에러 사후분석**(생성오류·push실패·CI fail) | OBS 자동 업로드 | `{OBS_PREFIX}S-YYMMDD-HHMMSS.log` (설정 시), Metadata에 reason |
| 접근/행위 이력(로그인·PUSH·PR·관리자 지정) | audit | `manage.py audit --limit 50 [--user <id>]` |
| build가 ci_running에서 멈춤 등 | studio 상세 | `manage.py show-studio <id>` / `manage.py failures` |

즉 **기동/설치 문제 = journalctl + studio.log**, **동작/생성 문제 = studio-log(+OBS)**,
**누가 무엇을 했나 = audit**. 로그 레벨은 `STUDIO_LOG_LEVEL=DEBUG`로 올려 이식 기간
동안 상세히 남기고, 안정화 후 INFO로 되돌린다.

### C-1. 이식 당일 빠른 대응 매트릭스 (증상 → 로그 → 원인 → 조치)

| 증상 | 먼저 볼 로그 | 흔한 원인 | 즉시 조치 |
|---|---|---|---|
| 서비스가 안 뜸 / 계속 재시작 | `journalctl -u toolhub-studio -n50` | env 누락·Fernet 키 권한·포트 점유 | `manage.py doctor` → FAIL 항목 조치 |
| health가 `db: error` | studio.log | STUDIO_DB 경로 권한(toolhub 소유 아님) | `chown toolhub` + `manage.py health` |
| 로그인은 되는데 500 | studio.log + Apache error | `X-Remote-User` 미주입/스푸핑 필터 순서 | Apache `RequestHeader` 블록 점검(§A-4) |
| AWS 연결 클릭 시 실패 | studio-log `<id>` | SSO_START_URL/ACCOUNT/ROLE 오설정 | `doctor`로 3개 값 확인 후 재연결 |
| push가 "GHE 재연결" | studio-log + audit `push_dispatch` | OAuth 미설정/토큰 만료/OWNER 오설정 | `doctor` GHE 항목 → OWNER·OAuth 확인 |
| CI 콜백이 무시됨 | studio.log | CI_WEBHOOK_SECRET 불일치(HMAC 실패) | 양쪽 시크릿 일치 확인, 재설정 |
| 생성은 됐는데 CI fail 반복 | studio-log `<id>`(+OBS) | 분석 md stale·프롬프트·실제 코드 이슈 | `show-studio` + OBS 로그로 fail_summary 분석 |
| build가 `ci_running` 멈춤 | `show-studio <id>` | webhook 유실·run_id 미확보 | 사용자 취소→재생성(ops §2.2) |
| 원인 불명 에러 사후분석 | **OBS** `error-logs/S-*.log` | — | 에러 시 자동 업로드분 다운로드, reason 메타 확인 |

**대응 루프(3단계)**: ① `doctor`로 설정/전제 확인 → ② 증상별 위 표의 로그 확인 →
③ 못 잡으면 `STUDIO_LOG_LEVEL=DEBUG` 재기동 후 재현. 에러는 OBS에 자동 축적되므로
사후에도 같은 키로 재분석 가능하다.

---

## D. 롤백 / 안전장치

- **A(Studio)**: 격리 서비스라 롤백이 단순 — `systemctl stop toolhub-studio` +
  Apache Location 제거. 기존 stage/release/signtool 무영향. 데이터는
  `toolhub-studio-ops.md §4.2` 복구 절차.
- **B(감사 이식)**: 로그인 이력(B-1)은 Apache 설정만이라 되돌리기 쉽다. 행위이력(B-2)은
  **best-effort로 넣어 실패해도 본 흐름을 막지 않게**(예외 삼킴) 붙이는 게 원칙 —
  기존 서비스 동작에 리스크를 주지 않는다.
- 공통: `.fernet.key`는 별도 안전 보관본 확보 후 진행(분실 시 토큰 전원 재연결).

## E. D-day 체크리스트

> 순서대로. **SSO(A-8)는 최후순위** — 위 GHE 스모크까지 끝낸 뒤 붙인다.

- [ ] **`bash scripts/preflight.sh` PASS** (배포 전 코드 준비도 — 의존성·부팅·테스트·E2E·린트)
- [ ] **`manage.py doctor` FAIL 0** (SSO는 WARN 허용 — 게이트 통과, 최우선)
- [ ] A-1~A-3 완료: 계정·venv·env(SSO 제외)·Fernet·DB·관리자 2인
- [ ] A-4 Apache 프록시: `X-Remote-User` unset(스푸핑 차단) + Location 비충돌
- [ ] A-5 systemd + 백업 타이머 `enable --now`
- [ ] A-6 health `healthy:true` + 분석 md 등록(`map-analysis`)
- [ ] (SSO 전) GHE 연결·브랜치·생성·커밋·CI를 bring-up 바이패스로 선점검
- [ ] B-1 접근로그 %u (4개 서비스) — 코드 0줄
- [ ] B-2 행위이력 record() — 서비스팀 일정에 맞춰(저위험부터)
- [ ] **A-8 SSO 연동(마지막)**: Knox 로그인 + AWS SSO → `doctor` SSO PASS 확인
- [ ] **A-7 스모크 1왕복**: 첫 사용자 로그인→AWS/GHE→생성→**CI pass**(오픈 판정)
- [ ] `STUDIO_LOG_LEVEL=DEBUG` 이식 기간 → 안정화 후 INFO
