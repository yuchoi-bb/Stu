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
# ── Bedrock (§3.2) ──
STUDIO_MODEL_ID=<inference-profile-or-model-id>
STUDIO_BEDROCK_REGION=<region>
STUDIO_SSO_START_URL=<aws-sso-start-url>
STUDIO_SSO_REGION=<region>
STUDIO_SSO_ACCOUNT_ID=<account>
STUDIO_SSO_ROLE_NAME=<role>
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

### A-4. Apache 리버스 프록시 + Knox SSO
`deploy/apache-toolhub-studio.conf.reference` 사용. **반드시 확인할 3가지**:
1. `RequestHeader unset X-Remote-User` — 클라이언트발 스푸핑 헤더 제거 후, SSO가
   인증한 `REMOTE_USER`만 `X-Remote-User`로 넣는다(§3.1 신뢰 경계).
2. `ProxyPass /api/studio http://127.0.0.1:5000/api/studio` — 기존 서비스 Location과
   겹치지 않는 경로.
3. 접근로그 `%u` LogFormat(= B의 로그인 이력 커버, 아래 B-1).

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
- 스모크: UI에서 AWS/GHE 연결 → 요구조건 1건 → 생성 → CI pass 1회 왕복.

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

- [ ] A-1~A-3 완료: 계정·venv·env·Fernet·DB·관리자 2인
- [ ] A-4 Apache: `X-Remote-User` unset 확인(스푸핑 차단) + Location 비충돌
- [ ] A-5 systemd + 백업 타이머 `enable --now`
- [ ] A-6 health `healthy:true` + 스모크 1왕복(생성→CI pass)
- [ ] 분석 md 등록(`map-analysis`)
- [ ] B-1 접근로그 %u (4개 서비스) — 코드 0줄
- [ ] B-2 행위이력 record() — 서비스팀 일정에 맞춰(저위험부터)
- [ ] `STUDIO_LOG_LEVEL=DEBUG` 이식 기간 → 안정화 후 INFO
