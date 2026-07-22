#!/usr/bin/env bash
# ToolHub Studio 배포 준비 게이트 — "큰 그림이 다 도는가"를 한 명령으로 검증.
#
# 코드 레벨 준비도(어디서나 실행 가능)를 순서대로 확인한다:
#   1) 런타임 의존성 import   2) 앱 부팅 + 라우트   3) 전체 테스트 스위트
#   4) 엔드투엔드 데모(파이프라인 완주)   5) 린트
# 하나라도 실패하면 즉시 비-0 종료(배포 보류). 전부 통과면 GO.
#
# 대상 서버의 환경(경로/키/SSO/GHE 등) 준비도는 별도로:
#   python -m studio.manage doctor --net
#
# 실행: bash scripts/preflight.sh
set -u
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-.}"
fail=0
step() { printf '\n\033[1m▶ %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓ %s\033[0m\n' "$1"; }
bad()  { printf '  \033[31m✗ %s\033[0m\n' "$1"; fail=1; }

step "1/5 런타임 의존성 import"
if python3 -c "import flask,gunicorn,boto3,cryptography,requests,pypdf,docx" 2>/dev/null; then
  ok "flask/gunicorn/boto3/cryptography/requests/pypdf/docx"
else
  bad "의존성 누락 — pip install -r requirements.txt"
fi

step "2/5 앱 부팅 + 라우트"
n=$(python3 -c "from studio.app import app; print(sum(1 for _ in app.url_map.iter_rules()))" 2>/dev/null) \
  && ok "앱 로드 OK — 라우트 ${n}개" || bad "앱 부팅 실패 (import 오류)"

step "3/5 전체 테스트 스위트"
if python3 tests/run_all.py >/tmp/preflight_tests.log 2>&1; then
  ok "$(tail -1 /tmp/preflight_tests.log)"
else
  bad "테스트 실패 — tail:"; tail -6 /tmp/preflight_tests.log | sed 's/^/    /'
fi

step "4/5 엔드투엔드 데모 (파이프라인 완주)"
if python3 scripts/demo.py >/tmp/preflight_demo.log 2>&1 \
   && grep -q "엔드투엔드 데모 완료" /tmp/preflight_demo.log; then
  ok "Step2→생성→리뷰→CI실패→회차2→통과→PR→지표 완주"
else
  bad "데모 미완주 — tail:"; tail -6 /tmp/preflight_demo.log | sed 's/^/    /'
fi

step "5/5 린트 (ruff)"
if command -v ruff >/dev/null 2>&1; then
  ruff check . >/tmp/preflight_lint.log 2>&1 && ok "ruff 통과" \
    || { bad "린트 경고/에러:"; tail -6 /tmp/preflight_lint.log | sed 's/^/    /'; }
else
  printf '  \033[33m~ ruff 미설치 (skip)\033[0m\n'
fi

echo
if [ "$fail" -eq 0 ]; then
  printf '\033[1;32m✅ PREFLIGHT PASS — 코드 레벨 준비 완료. 대상 서버에서 `manage.py doctor --net` 확인 후 배포.\033[0m\n'
else
  printf '\033[1;31m⛔ PREFLIGHT FAIL — 위 항목을 고친 뒤 재실행. 배포 보류.\033[0m\n'
fi
exit $fail
