#!/bin/bash
# ToolHub Studio — SessionStart hook: 웹 세션에서 테스트/실행이 되도록 의존성 설치.
set -euo pipefail

# 웹(원격) 세션에서만 실행 — 로컬 개발 환경은 건드리지 않는다
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

# 런타임 + 개발(Playwright) 의존성. Chromium은 /opt/pw-browsers 사용(다운로드 생략).
python3 -m pip install --quiet --disable-pip-version-check -r requirements-dev.txt

# tests가 studio 패키지를 임포트할 수 있도록 프로젝트 루트를 경로에 추가
echo 'export PYTHONPATH="."' >> "${CLAUDE_ENV_FILE:-/dev/null}"

# 린트 상태 안내 (비차단 — 세션은 계속 진행)
ruff check . >/dev/null 2>&1 && echo "session-start: 린트 통과" \
  || echo "session-start: ⚠ 린트 경고 있음 (ruff check .)"

echo "session-start: 의존성 설치 완료"
