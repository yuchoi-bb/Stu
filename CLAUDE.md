# CLAUDE.md — ToolHub Studio

> Studio 관련 작업 시 **먼저 `docs/toolhub-studio-design.md`(단일 기준 문서)**를 읽을 것.
> 이 파일은 그 요약 + 작업 규칙이다. 설계 변경은 design.md §0 변경이력에 기록 후 진행.

## 무엇인가
사내 개발자(≤7명)가 요구사항+문서를 웹 UI로 넣으면 Claude(AWS Bedrock)가 요구조건·
코드·테스트케이스를 생성해 사용자가 지정한 GHE 자유 브랜치에 커밋하고, 기존 `stage`
CI/CD(studio-verify.yml, workflow_dispatch)로 검증하는 내부 도구. 실패 시 같은 studio_id
아래 회차(build)를 누적해 이전 실패를 다음 생성 컨텍스트로 주입하는 반복 개선 루프.

## 아키텍처 (핵심만)
- Flask + gunicorn **worker 1 × threads 16 고정**(§4.3) — 늘리지 말 것. 취소 플래그·CI
  폴러가 단일 프로세스 전제. `bind=127.0.0.1:5000`(외부는 Apache 경유만).
- SQLite + WAL (`studio.db`). 작업 큐 = ThreadPoolExecutor(8). 진행 상황은 REST 폴링.
- 1 studio_id : N build 회차. push는 blob SHA 가드(§6.5)로 조용한 덮어쓰기 차단.
- 게이트: Step 2(요구조건 확정) · Step 3.5(코드 리뷰) · §6.8 stage 전달(보류 옵션) ·
  §6.6 안 B(사용자 PR 버튼).

## 개발 명령
```bash
bash scripts/preflight.sh   # 배포 준비 게이트: 의존성·부팅·테스트·E2E데모·린트 한 번에
python tests/run_all.py     # 전체 테스트(22 스위트) — 반드시 green 유지
ruff check .                # 린트(ruff.toml, line-length 100, E/W/F)
python scripts/demo.py      # 엔드투엔드 데모(전체 파이프라인 완주, 산 문서 겸 스모크)
python -m studio.manage doctor [--net]   # 대상 서버 환경 진단(경로/키/설정/도달성)
python -m studio.manage health           # 상태 점검
```
커밋 전 **테스트 + 린트 green** 확인. 배포 전 **`preflight.sh`(코드) + `doctor`(서버 환경)**
둘 다 통과. SessionStart 훅이 린트/의존성을 자동 점검.

## 하드 제약 (지키지 않으면 회귀/보안 사고)
- **`.github/workflows` 수정 금지.**
- **경로 탈출 차단**: 파일 경로는 `is_unsafe_path`/`UnsafePath`로 검증(절대경로·`..` 거부).
- **자격증명 마스킹**: git 에러 출력은 `_redact`로 토큰 제거. 원문 노출 금지.
- **인증 신뢰 경계**: 앱은 `X-Remote-User`(Apache가 REMOTE_USER로만 주입)를 신뢰.
  Apache는 클라이언트발 헤더를 반드시 `RequestHeader unset`. CSRF는 Origin 검사.
- **생성 코드 실행 경계**(§6.4): 사람 리뷰(Step 3.5) 없이 stage runner로 보내지 않음.
  위험 패턴 스캔(scan.py) high면 auto_approve여도 사람 검토 강제.
- **프롬프트 주입 신뢰 경계**(§6.4.2): 첨부/외부 텍스트는 untrusted로 표시해 주입.
- git subprocess는 `GIT_TIMEOUT`(180s). 생성 컨텍스트는 상한(clip)으로 폭주 방지.

## 디버그 / 로그 (사후 추적)
- 앱 로그: `data/logs/studio.log`(RotatingFileHandler). studio별: `logs/studio/
  S-YYMMDD-HHMMSS.log`(생성 시각 기준, `logs.slog`). 에러 시 OBS(MinIO) 자동 업로드.
- 행위/로그인 이력: `action_log`(audit) — `manage.py audit`. 4개 서비스 공통 규약.
- 문제 진단 지도: `docs/toolhub-studio-install.md` §C-1(증상→로그→원인→조치).

## 문서 맵
- `docs/toolhub-studio-design.md` — **기준 문서**(결정이력·아키텍처·인증·파이프라인·§12 원장)
- `docs/toolhub-studio-install.md` — 설치·이식 런북(신규 배포 + 공통 감사 이식, SSO는 A-8 후순위)
- `docs/toolhub-studio-ops.md` — 운영 런북(장애·토큰·백업복구)
- `docs/toolhub-studio-guide.md` — 사용자 가이드
- `docs/toolhub-studio-precheck.md` — 사전 확인 질문지(Knox/GHE/AWS/보안 조직)
- `docs/toolhub-audit-contract.md` + `deploy/audit-contract/` — 공통 이력 규약·참조 구현
- `docs/analysis/a-tool.md` — 첫 대상 tool(NVMe 덤프 확보, C++) 분석 문서

## 브랜치 / 커밋
- 작업 브랜치: `claude/discussion-1ej9fb` (push: `git push -u origin claude/discussion-1ej9fb`).
- 다른 브랜치로 push 금지. PR은 명시 요청 시에만 생성.
