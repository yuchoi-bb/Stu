"""UI 테스트용 서버 — LLM mock + 개발 사용자로 studio.html 실동작 확인."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="studio-ui-")
os.environ["STUDIO_DB"] = os.path.join(TMP, "studio.db")
os.environ["STUDIO_FERNET_KEY"] = os.path.join(TMP, ".fernet.key")
os.environ["STUDIO_ATTACH_DIR"] = os.path.join(TMP, "attachments")
os.environ["STUDIO_DEV_USER"] = "hong"

from studio import db, pipeline  # noqa: E402
from studio.app import app       # noqa: E402

DRAFT = ("## R1. 출력 내림 처리\n- floor, 소수 3자리 기준\n\n"
         "## 확인 필요\n- 음수 입력의 내림 방향?\n")
GEN = ("변경 요약: R1 반영, floor3 도입\n"
       "```file:src/parser.c\n#include \"parser.h\"\n"
       + "double floor3(double v) { return floor(v * 1000) / 1000; }\n" * 20
       + "```\n"
       "```file:test/test_parser.c\nTEST(floor3_basic) { ... }\n```\n")

_calls = {"n": 0}


def fake_invoke(user_id, session_id, messages, system, **kw):
    _calls["n"] += 1
    text = DRAFT if "정제 담당" in system else GEN
    return {"output": {"message": {"content": [{"text": text}]}},
            "usage": {"inputTokens": 10, "outputTokens": 10}}


pipeline.invoke_claude = fake_invoke
db.execute("INSERT OR IGNORE INTO users (user_id) VALUES ('hong')")
db.execute("INSERT OR REPLACE INTO user_branch_config (user_id, repo, branch_name) "
           "VALUES ('hong','thr','feature/foo')")

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5057)
