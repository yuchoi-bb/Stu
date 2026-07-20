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

from studio import db, ghe, pipeline  # noqa: E402
from studio.app import app             # noqa: E402
from flask import jsonify     # noqa: E402

DRAFT = ("## R1. 출력 내림 처리\n- floor, 소수 3자리 기준\n\n"
         "## 확인 필요\n- 음수 입력의 내림 방향?\n")
GEN = ("변경 요약: R1 반영, floor3 도입\n"
       "```file:src/parser.c\n#include \"parser.h\"\n"
       "  system(\"echo build\");\n"   # 정적 검사(§12.4) high 유발 — 리뷰 카드 확인용
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
# UI에서 PR 버튼(§6.6 안 B)을 실동작으로 확인하기 위한 mock:
# GHE 호출 없이 PR 생성 성공을 흉내낸다.
ghe.create_pull_request = lambda *a, **k: (77, "https://ghe.test/toolhub/thr/pull/77", False)


@app.post("/api/studio/test/pass/<int:build_id>")   # 테스트 전용: 회차를 pass로 강제
def _test_pass(build_id):
    db.execute("UPDATE builds SET status='pass', commit_sha='deadbeefcafe', "
               "completed_at=datetime('now') WHERE build_id=?", (build_id,))
    return jsonify({"ok": True})


db.execute("INSERT OR IGNORE INTO users (user_id) VALUES ('hong')")
db.execute("INSERT OR REPLACE INTO user_branch_config (user_id, repo, branch_name) "
           "VALUES ('hong','thr','feature/foo')")

if __name__ == "__main__":
    port = int(os.environ.get("STUDIO_UI_PORT", "5057"))
    app.run(host="127.0.0.1", port=port)
