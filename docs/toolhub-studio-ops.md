# ToolHub Studio 운영 런북

> 대상: 관리자(2인 체계 권장, §3.1.2). 장애 대응·토큰 문제·백업 복구를 1인 의존 없이
> 처리하기 위한 절차. 경로/환경변수는 `deploy/toolhub-studio.service` 기준
> (`/opt/toolhub`, 데이터 `/opt/toolhub/data`, 백업 `/opt/toolhub/backups`).
>
> **최초 설치·이식**(신규 배포, 공통 감사 규약 이식)은 `toolhub-studio-install.md`.
> 이 문서는 설치 이후의 운영(day-2)만 다룬다.

## 0. 빠른 점검 (헬스체크)

```bash
# HTTP (Apache 경유)
curl -s https://<host>/api/studio/health | jq
# 프로세스 직접 (서버 안에서, HTTP 없이)
sudo -u toolhub /opt/toolhub/venv/bin/python -m studio.manage health
```

- `db: ok` / `disk_free_mb` 충분 / `ci_running builds` 수 확인.
- 서비스 상태: `systemctl status toolhub-studio`
- 로그: `journalctl -u toolhub-studio -n 200` + 앱 로그 `/opt/toolhub/data/logs/studio.log`
  (RotatingFileHandler 10MB×5).

## 1. 서비스 기동/재시작

```bash
sudo systemctl restart toolhub-studio      # 무중단 아님 — worker 1이라 순단 발생
sudo systemctl status toolhub-studio
```

- gunicorn `worker 1 × threads 16` 고정(§4.3): 단일 프로세스라 취소/스케줄러 중복이 없다.
  worker 수를 늘리지 말 것 — 취소 플래그·CI 폴러 전제가 깨진다.

## 2. 장애 유형별 대응

### 2.1 “관리자 0명” 락아웃
- 원인: 마지막 관리자 강등은 API/CLI가 막지만, DB 직접 수정 등으로 발생 가능.
- 복구: `sudo -u toolhub .../python -m studio.manage set-admin <user>` 로 부트스트랩.

### 2.2 build가 `ci_running`에서 멈춤
- webhook 유실 가능. CI 폴러(run_id 기준)가 백업 경로지만, run_id 미확보면 못 잡음.
- 확인: `manage.py show-studio <studio_id>` 로 회차/상태/run 확인.
- 조치: 사용자가 해당 build 취소(취소 전파로 stage run도 cancel) 후 재생성 지시.

### 2.3 push_conflict 빈발
- 자유 브랜치에 사용자의 로컬 수정이 겹칠 때 발생(§6.5 blob SHA 가드). 정상 방어 동작.
- 안내: 브랜치 정리 후 재시도 또는 재생성(새 회차). 조용한 덮어쓰기는 하지 않는다.

### 2.4 디스크 부족
- 첨부/백업/로그 누적. `du -sh /opt/toolhub/data/* /opt/toolhub/backups | sort -h`.
- 백업 보존 축소: `STUDIO_BACKUP_RETENTION_DAYS` 조정. 로그는 자동 회전.

### 2.5 실패 급증 파악
- `manage.py failures --limit 30` 또는 `GET /api/studio/admin/failures` — 최근 실패 회차와
  fail_summary. 특정 tool/사용자 편중이면 분석 md 신선도(§7.3)·프롬프트 점검.

### 2.6 에러 원인 분석 — studio 디버그 로그 (로컬 / OBS)
- studio 하나의 전 과정: `manage.py studio-log <studio_id>` 또는
  `GET /api/studio/admin/studios/<studio_id>/log`. 로컬 파일은
  `logs/studio/S-YYMMDD-HHMMSS.log` (studio 생성 시각 기준).
- **에러(생성 오류·push 실패·CI fail) 발생 시** 그 로그가 **OBS(MinIO)** 로 자동
  업로드된다(설정 시). OBS에서 `{OBS_PREFIX}S-YYMMDD-HHMMSS.log` 키로 접근해
  원인 분석. 객체 Metadata에 `studio_id`·`reason` 포함.
  - 설정: `STUDIO_OBS_ENDPOINT`(MinIO), `STUDIO_OBS_BUCKET`, `STUDIO_OBS_ACCESS_KEY`,
    `STUDIO_OBS_SECRET_KEY`, `STUDIO_OBS_PREFIX`(기본 `error-logs/`). 미설정이면 로컬만.
  - 조회 예: `aws --endpoint-url $OBS s3 ls s3://$BUCKET/error-logs/` 후 해당 키 get.

## 3. 토큰/자격증명 문제

### 3.1 AWS SSO (Bedrock)
- 증상: 생성이 “AWS 재연결 필요(SSO 세션 만료)”로 fail.
- 조치: 사용자가 UI “AWS 연결” 재클릭 → 승인 링크. 만료 30분 전 선제 refresh 스케줄러가
  있으나 세션 자체 만료 시 재승인 필요.

### 3.2 GHE (OAuth/PAT)
- 증상: push가 “GHE 재연결 필요”. OAuth는 refresh 자동 갱신, 실패 시 재연결 배너.
- PAT 폴백: 사용자가 UI에서 PAT 재등록(유효성 검증 후 암호화 저장).
- 확인: `manage.py users` 로 `ghe=<login>` 매핑 여부.

### 3.3 Fernet 키
- 자격증명은 Fernet 암호화, 키는 `/opt/toolhub/data/.fernet.key`(600).
- **키 분실 = 저장된 모든 AWS/GHE 토큰 복호화 불가** → 전원 재연결 필요.
  키 파일은 백업 대상과 별개로 안전 보관(백업 tar에는 포함하지 않음).

## 4. 백업 / 복구

### 4.1 백업 (자동)
- `deploy/toolhub-studio-backup.timer` = 매일 03:30. 설치:
  ```bash
  sudo cp deploy/toolhub-studio-backup.{service,timer} /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now toolhub-studio-backup.timer
  systemctl list-timers toolhub-studio-backup.timer
  ```
- 수동 1회: `sudo -u toolhub .../python -m studio.manage backup`
- 산출물: `studio-YYYYmmdd-HHMMSS.db`(SQLite 온라인 백업=WAL 안전) +
  `attachments-….tar.gz`. 30일 초과분 자동 정리.

### 4.2 복구 (수동)
1. 서비스 중지: `sudo systemctl stop toolhub-studio`
2. DB 교체: 백업 db를 `STUDIO_DB` 위치로 복사. **동명의 `-wal`/`-shm`는 삭제**
   (스냅샷은 이미 체크포인트된 단일 파일).
   ```bash
   cp /opt/toolhub/backups/studio-<ts>.db /opt/toolhub/data/studio.db
   rm -f /opt/toolhub/data/studio.db-wal /opt/toolhub/data/studio.db-shm
   ```
3. 첨부 복구: `tar -xzf attachments-<ts>.tar.gz -C /opt/toolhub/data/`
   (tar 내부 경로가 `attachments/…`).
4. 서비스 시작: `sudo systemctl start toolhub-studio` → `health` 확인.
- Fernet 키(`.fernet.key`)는 백업에 없으므로 별도 보관본에서 원위치 확인.

## 5. 감사(audit) 조회

- 승인/리뷰/취소/PR/push/관리자 지정 등 행위 로그(§3.1.1).
- CLI: `manage.py audit --limit 50 [--user <id>]`
- HTTP: `GET /api/studio/admin/audit` (관리자 전용).

## 6. 인수인계 체크리스트 (관리자 2인)

- [ ] 두 번째 관리자 지정: UI 관리자 대시보드 “관리자 지정” 또는 `manage.py set-admin`
- [ ] `.fernet.key` 보관 위치·복구 절차 공유(문서화된 안전 저장소)
- [ ] 백업 타이머 동작 확인(`list-timers`) + 복구 1회 드릴
- [ ] 분석 md 관리 주체/갱신 주기 확인(§7.3)
- [ ] 이 런북 위치 공유
