# ToolHub 공통 이력(감사) — 참조 구현 패키지

stage / release / signtool 팀이 **바로 붙일 수 있는** 참조 구현. 규약 전체는
`docs/toolhub-audit-contract.md` 참조. Studio는 이 규약의 참조 구현이다(자체 audit).

## 무엇을 넣나
- **SSO 로그인/접근 이력** → `apache-access-log.conf.reference`
  각 서비스 VirtualHost에 `%u`(REMOTE_USER) LogFormat만 추가. **앱 코드 0줄로 4개
  서비스 로그인 이력 커버.**
- **행위 이력(HTML 동작·PUSH·빌드·배포·서명)** → 서비스 언어에 맞게 택1:
  - PHP: `audit.php` (`Audit::login()`, `Audit::record()`)
  - Python: `audit.py` (`log_login()`, `record()`)
  - 다른 언어: `schema.sql`의 테이블에 같은 컬럼으로 INSERT.

## 붙이는 법 (3줄 요약)
1. 접근 로그: Apache에 `%u` LogFormat + CustomLog(서비스별 `service=` 태그).
2. 로그인: 요청 처리 진입부에서 `login(REMOTE_USER)` 1회(세션 스로틀 내장).
3. 동작: 상태 변경(버튼→서버 처리) 시점마다 `record(user, action, target, result)`.
   - stage: `build_trigger` / `build_result` · release: `deploy` · signtool: `sign`

## 통합 조회
공통 테이블을 함께 쓰거나, 각자 DB에 같은 형식으로 남기고 로그 수집(TIG/ELK)에서
`service` 필드로 필터. 스키마·인덱스는 `schema.sql`.
