# ToolHub 공통 이력(감사) 규약 — studio / stage / release / signtool

목적: **로그인(SSO 접근) 이력**과 **행위 이력**(PUSH·빌드·배포·서명 등)을 4개 서비스가
**같은 형식·같은 키**로 남겨, 나중에 서비스가 분산돼 있어도 **통합 조회**할 수 있게 한다.
Studio가 이 규약의 **참조 구현(reference implementation)**이다.

> 범위 주의: stage/release/signtool은 별도 서비스(다른 팀/스택)다. 이 문서는 **공통
> 규약과 참조 구현**을 제공하며, 각 서비스가 자신의 코드에서 이 규약대로 기록하면 된다.

## 1. 두 계층

| 계층 | 무엇 | 어떻게 (권장) |
|---|---|---|
| **접근/로그인 이력** | 누가·언제·어디에 접근(=SSO 로그인) | **Apache 접근 로그에 `%u`(REMOTE_USER) 추가** — 앱 코드 0줄로 4개 서비스 전부 커버. `service=<name>` 태그 |
| **행위 이력** | 누가·무엇을 실행 (PUSH/빌드/배포/서명/승인…) | 각 서비스가 **공통 스키마**로 행위 로그 1건 기록 |

- **SSO 로그인 이력**: 사용자는 Knox SSO → `REMOTE_USER`로 각 서비스에 도달한다.
  Apache `LogFormat`에 `%u`를 넣으면 **모든 서비스의 접근(=로그인)이 동일 형식으로**
  남는다(§3.1.1). 앱 레벨에서 더 세밀히 남기려면 아래 `login` 행위를 추가로 기록.

## 2. 공통 스키마 (행위 로그)

```
(user_id, service, action, target, result, detail, timestamp)
```

| 필드 | 의미 | 예 |
|---|---|---|
| `user_id` | REMOTE_USER(사번/AD ID). 시스템 발생은 NULL | `hong` |
| `service` | 서비스 식별자 | `studio` / `stage` / `release` / `signtool` |
| `action` | 행위 종류(서비스별 어휘) | `login`, `push_dispatch`, `build_trigger`, `deploy`, `sign` |
| `target` | 대상 식별자 | `ST-ab12#2`, `buildid=…`, `pkg@1.2.3` |
| `result` | 결과 | `ok` / `fail` / `ci_running` / `new` / `resume` |
| `detail` | 부가 정보(사유·SHA 등) | `a1b2c3d4`, `push_conflict` |
| `timestamp` | 기록 시각 | `datetime('now')` |

- **저장소 통일 방식(택1)**: ① 공통 DB의 `action_log` 테이블에 4개 서비스가 함께 기록,
  또는 ② 각 서비스가 자기 DB/파일에 남기고 **동일 형식**으로 로그 수집(TIG/ELK)에 실어
  통합 조회. 어느 쪽이든 위 스키마를 지키면 된다.

## 3. 로그인 + PUSH 이력 (이번 요구 핵심)

- **로그인**: 각 서비스는 SSO 접근 시 `login` 행위를 남긴다(요청마다 폭주하지 않도록
  세션 창 스로틀 — Studio는 `LOGIN_AUDIT_THROTTLE_MIN`(기본 30분), `result=new|resume`).
  최소선은 Apache `%u` 접근 로그(코드 0줄)로도 충족된다.
- **PUSH**: PUSH가 **일어나는 서비스가** 기록한다.
  - Studio: 사용자 브랜치 커밋/push→dispatch 시 `action=push_dispatch`
    (`result=ci_running`=성공, `fail`/`push_conflict`=실패까지 **성공·실패 모두**).
  - stage: stage가 직접 git/빌드 산출물을 다루면 그 시점의 행위를 같은 규약으로 기록
    (예: `action=build_trigger`/`build_result`).
  - release: 배포 실행을 `action=deploy`로.
- 이렇게 각 서비스가 **자기가 수행한 행위**를 남기면, 합쳐서 사용자별 전체 이력이 된다.

## 4. Studio 참조 구현 (이미 동작)

- `studio/audit.py`: `record(user_id, action, target, result, detail)` — `service`는
  `config.SERVICE_NAME`(기본 `studio`, 환경변수 `STUDIO_SERVICE_NAME`로 덮어쓰기).
- 기록 지점: `login`(접근, 스로틀), `push_dispatch`(성공/실패), `requirements_approve`,
  `review_approve`/`review_reject`, `create_pr`, `cancel`, `grant_admin`/`revoke_admin`,
  `ci_result` 등 — **HTML의 상태 변경 동작이 API를 거치며 모두 기록**된다.
- 조회: `GET /api/studio/admin/audit?action=login|push_dispatch&user_id=…` (관리자),
  CLI `python -m studio.manage audit --action push_dispatch --user hong`.

## 5. 각 서비스 도입 체크리스트

- [ ] Apache `LogFormat`에 `%u` 추가(4개 서비스 접근/로그인 이력, 코드 0줄) + logrotate 보존
- [ ] 행위 로그를 §2 공통 스키마로 기록 (`service`는 자기 식별자)
- [ ] HTML의 상태 변경 동작마다 행위 1건(예: 버튼 클릭→서버 처리 시점)
- [ ] 로그인 이벤트(선택, 세션 스로틀) + 자기 서비스의 핵심 행위(PUSH/빌드/배포/서명)
- [ ] (통합 조회) 공통 테이블 또는 동일 형식 로그 수집으로 합류
