"""Studio 설정. 배포 환경에서는 환경변수로 덮어쓴다."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATH = os.environ.get("STUDIO_DB", os.path.join(BASE_DIR, "studio.db"))
LOG_DIR = os.environ.get("STUDIO_LOG_DIR", os.path.join(BASE_DIR, "logs"))
LOG_LEVEL = os.environ.get("STUDIO_LOG_LEVEL", "INFO")
FERNET_KEY_PATH = os.environ.get("STUDIO_FERNET_KEY",
                                 os.path.join(BASE_DIR, ".fernet.key"))
ATTACH_DIR = os.environ.get("STUDIO_ATTACH_DIR", os.path.join(BASE_DIR, "attachments"))
ATTACH_MAX_BYTES = 20 * 1024 * 1024          # §8: 파일당 20MB

# 백업 (§12.4): studio.db + 첨부 일 1회, 기본 30일 보존
BACKUP_DIR = os.environ.get("STUDIO_BACKUP_DIR", os.path.join(BASE_DIR, "backups"))
BACKUP_RETENTION_DAYS = int(os.environ.get("STUDIO_BACKUP_RETENTION_DAYS", "30"))

# 모델 정책 (§11): MODEL_ID 고정, 교체 시 스모크 테스트 후 전환
# Bedrock 모델 ID는 anthropic. 접두사. cross-region inference profile 사용 시
# 지역 접두사(apac. 등)가 붙은 프로파일 ID로 교체 (§12.1 확인 항목)
MODEL_ID = os.environ.get("STUDIO_MODEL_ID", "anthropic.claude-opus-4-8")
BEDROCK_REGION = os.environ.get("STUDIO_BEDROCK_REGION", "ap-northeast-2")

# AWS SSO (§3.2) — 사내 IAM Identity Center 값으로 설정
SSO_START_URL = os.environ.get("STUDIO_SSO_START_URL", "")
SSO_REGION = os.environ.get("STUDIO_SSO_REGION", "ap-northeast-2")
SSO_ACCOUNT_ID = os.environ.get("STUDIO_SSO_ACCOUNT_ID", "")
SSO_ROLE_NAME = os.environ.get("STUDIO_SSO_ROLE_NAME", "")
SSO_REFRESH_MARGIN_MIN = 30                  # 만료 30분 전 선제 refresh

# 대상 repo 기본값 (사용자별 브랜치 설정이 없을 때)
DEFAULT_REPO = os.environ.get("STUDIO_DEFAULT_REPO", "thr")

# 작업 모델 (§4.3)
EXECUTOR_WORKERS = 8
MAX_CONCURRENT_PER_USER = 1                  # §4.2: 사용자당 동시 진행 1건

# 멀티턴 히스토리 (§4.2): 최근 N턴 원문 + 이전 요약
HISTORY_RECENT_TURNS = 6

# GHE (§3.3, §6) — 사내 값으로 설정
GHE_BASE_URL = os.environ.get("STUDIO_GHE_BASE", "https://github.samsungds.net")
GHE_API_URL = os.environ.get("STUDIO_GHE_API", GHE_BASE_URL + "/api/v3")
GHE_OWNER = os.environ.get("STUDIO_GHE_OWNER", "toolhub")
# PR base 브랜치 (§6.6 안 B: CI 통과 후 본인 명의 PR 생성 시 대상)
GHE_DEFAULT_BASE_BRANCH = os.environ.get("STUDIO_GHE_BASE_BRANCH", "main")
GHE_OAUTH_CLIENT_ID = os.environ.get("STUDIO_GHE_OAUTH_CLIENT_ID", "")
GHE_OAUTH_CLIENT_SECRET = os.environ.get("STUDIO_GHE_OAUTH_CLIENT_SECRET", "")
GHE_OAUTH_CALLBACK = os.environ.get(
    "STUDIO_GHE_OAUTH_CALLBACK",
    "https://khtoolhubw02/api/studio/ghe/oauth/callback")
VERIFY_WORKFLOW = "studio-verify.yml"          # §6.2 (stage 측 파일)
CI_WEBHOOK_SECRET = os.environ.get("STUDIO_CI_WEBHOOK_SECRET", "")
CI_POLL_INTERVAL_SEC = 60                       # webhook 유실 대비 폴링 fallback
