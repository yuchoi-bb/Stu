"""ToolHub Studio 엔드투엔드 데모 — 전체 흐름을 현실적 mock으로 시연.

Bedrock/GHE 외부 호출만 mock하고, 나머지는 실제 앱(HTTP API)을 그대로 태운다.
Step 2 확정 → Step 3 생성 → Step 3.5 리뷰 → push → CI 실패 → 회차2 → CI 통과
→ PR 생성 → 지표까지, 실제 엔드포인트로 한 번에 보여준다. 산 문서 겸 스모크.

실행: python3 scripts/demo.py
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="studio-demo-")
os.environ["STUDIO_DB"] = os.path.join(_TMP, "studio.db")
os.environ["STUDIO_FERNET_KEY"] = os.path.join(_TMP, ".fernet.key")
os.environ["STUDIO_ATTACH_DIR"] = os.path.join(_TMP, "attach")
os.environ["STUDIO_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["STUDIO_LOG_LEVEL"] = "WARNING"          # 데모 출력만 보이게
os.environ["STUDIO_CI_WEBHOOK_SECRET"] = "demo-secret"

from studio.app import app                    # noqa: E402
from studio import db, ghe, jobs, pipeline    # noqa: E402

# ---------- 외부 경계만 mock ----------
jobs.submit = lambda fn, *a, **k: fn(*a, **k)   # 백그라운드 작업을 동기로

REQ_MD = ("## R1. 출력 내림 처리\n- 수치 출력은 floor, 소수 3자리\n"
          "- 음수 입력은 0으로 절단\n\n## 확인 필요\n- 반올림이 아닌 내림이 맞나?\n")
GEN = ("변경 요약: R1 반영 — floor3 도입, 음수 클램프. 회차 실패 시 원인 반영.\n"
       "```file:src/parser.c\n"
       "#include \"parser.h\"\n#include <math.h>\n"
       "double floor3(double v){ if(v<0) v=0; return floor(v*1000)/1000; }\n"
       "```\n"
       "```file:test/test_parser.c\n"
       "#include \"parser.h\"\n"
       "void test_floor3(void){ assert(floor3(1.23456)==1.234); assert(floor3(-1)==0); }\n"
       "```\n")


def fake_invoke(user_id, session_id, messages, system, **kw):
    if "정제 담당" in system:
        text = REQ_MD
    elif "선정 담당" in system:
        text = "```paths\n```"          # 전부 신규 — 원문 fetch 불필요
    else:
        text = GEN
    usage = {"inputTokens": 1200, "outputTokens": 800, "cacheReadInputTokens": 900}
    from studio.bedrock import _record_usage    # 실제 경로처럼 usage 기록
    _record_usage(user_id, session_id, kw.get("studio_id"), kw.get("build_id"), usage)
    return {"output": {"message": {"content": [{"text": text}]}}, "usage": usage}


pipeline.invoke_claude = fake_invoke
ghe.get_token = lambda u: "tok"
ghe.commit_and_push = lambda *a, **k: ("a1b2c3d4e5", {p: "blob" for p in a[2]})
ghe._dispatch = lambda b, token: None
ghe._find_run_id = lambda b, token: 4242
ghe.cancel_run = lambda *a, **k: None
ghe.create_pull_request = lambda *a, **k: (
    101, "https://github.samsungds.net/toolhub/thr/pull/101", False)

client = app.test_client()
H = {"X-Remote-User": "hong"}
SECRET = os.environ["STUDIO_CI_WEBHOOK_SECRET"].encode()


def hr(title):
    print("\n" + "=" * 64 + f"\n▶ {title}\n" + "=" * 64)


def show(label, obj):
    print(f"  {label}: " + json.dumps(obj, ensure_ascii=False)[:300])


def ci_callback(studio_id, attempt, status, fail_summary=None):
    body = json.dumps({"studio_id": studio_id, "attempt": attempt,
                       "status": status, "buildid": f"B{attempt}",
                       "fail_summary": fail_summary}).encode()
    sig = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    return client.post("/api/studio/ci-callback", data=body,
                       headers={"Content-Type": "application/json",
                                "X-Studio-Signature": sig})


def main():
    # 데모 사용자: 브랜치 설정 (실제 연결은 mock)
    db.execute("INSERT OR IGNORE INTO users (user_id) VALUES ('hong')")
    db.execute("INSERT OR REPLACE INTO user_branch_config (user_id, repo, branch_name) "
               "VALUES ('hong','thr','feature/floor')")

    hr("0. 세션 생성 + 문서 첨부")
    sid = client.post("/api/studio/sessions", headers=H,
                      json={"title": "parser 내림 처리"}).get_json()["session_id"]
    print(f"  세션: {sid}")
    att = os.path.join(_TMP, "spec.txt")
    open(att, "w").write("출력은 floor, 소수 3자리. 음수는 0.")
    import io
    client.post(f"/api/studio/sessions/{sid}/attachments", headers=H,
                data={"file": (io.BytesIO(open(att, "rb").read()), "spec.txt")},
                content_type="multipart/form-data")
    atts = client.get(f"/api/studio/sessions/{sid}", headers=H).get_json()["attachments"]
    show("첨부", atts)

    hr("1~2. 요구조건 초안 생성 → 확정 게이트")
    d = client.post("/api/studio/requirements/draft", headers=H,
                    json={"session_id": sid, "content": "출력을 내림 처리로 바꿔줘"}
                    ).get_json()
    print("  [Claude가 생성한 requirements 초안]")
    print("  " + REQ_MD.replace("\n", "\n  ").rstrip())
    print("  → 사용자가 검토·수정 후 승인 (승인 전 코딩 진입 불가)")
    r = client.post(f"/api/studio/requirements/{d['draft_id']}/approve", headers=H,
                    json={}).get_json()
    studio_id, build_id = r["studio_id"], r["build_id"]
    print(f"  ✓ studio 발급: {studio_id}  (회차 #1 build={build_id})")

    hr("3~3.5. 코드+testcase 생성 → 정적 검사 → 리뷰 게이트")
    files = client.get(f"/api/studio/builds/{build_id}/files", headers=H).get_json()
    for f in files:
        scan = f" [정적검사 {len(f['scan'])}건]" if f.get("scan") else ""
        print(f"  · {f['path']}  ({f['line_count']}줄){scan}")
    print("  → 작업자 승인 시 push. (자동 승인 모드면 high 위험패턴만 사람 검토 강제)")
    client.post(f"/api/studio/builds/{build_id}/review", headers=H,
                json={"action": "approve"})
    st = client.get(f"/api/studio/status/{studio_id}", headers=H).get_json()
    show("회차1 상태", [b["status"] for b in st["builds"]])

    hr("4~5. CI 실패 → 실패 요약 자동 주입 → 회차2 누적")
    ci_callback(studio_id, 1, "fail", "test_floor3: 음수 클램프 경계 실패")
    print("  ✗ CI 실패 — 실패 요약이 대화에 자동 주입됨 (다음 회차 컨텍스트)")
    m = client.post("/api/studio/message", headers=H,
                    json={"session_id": sid, "studio_id": studio_id,
                          "content": "실패 반영해서 다시"}).get_json()
    build2 = m.get("build_id")
    print(f"  ✓ 회차 #2 생성 (같은 studio_id 아래 누적) build={build2}")
    client.post(f"/api/studio/builds/{build2}/review", headers=H,
                json={"action": "approve"})

    hr("5. CI 통과")
    ci_callback(studio_id, 2, "pass")
    st = client.get(f"/api/studio/status/{studio_id}", headers=H).get_json()
    show("회차 상태(누적)", [f"#{b['attempt']}:{b['status']}" for b in st["builds"]])
    print(f"  can_pr = {st['can_pr']}  (CI 통과 회차 존재 → PR 생성 가능)")

    hr("6. 본인 명의 PR 생성 (§6.6 안 B — 자동 merge 금지)")
    pr = client.post(f"/api/studio/studios/{studio_id}/create-pr", headers=H,
                     json={}).get_json()
    show("PR", pr)
    print("  → 사람 리뷰 후 수동 merge. merge 되면 studio를 '채택'으로 종결.")
    client.post(f"/api/studio/studios/{studio_id}/close", headers=H,
                json={"status": "done"})

    hr("지표 (§10.1) + 감사 로그 (§3.1.1)")
    db.execute("UPDATE users SET is_admin=1 WHERE user_id='hong'")
    ov = client.get("/api/studio/admin/metrics", headers=H).get_json()["overall"]
    print(f"  studio {ov['studios_total']}건 · CI pass 도달률 "
          f"{ov['ci_pass_rate']} · 채택률 {ov['adoption_rate']} · "
          f"평균 회차 {ov['avg_attempts_per_studio']}")
    print(f"  토큰 in {ov['input_tokens']} / out {ov['output_tokens']} / "
          f"cache {ov['cache_read_tokens']}")
    al = client.get("/api/studio/admin/audit", headers=H).get_json()
    print("  최근 행위: " + ", ".join(a["action"] for a in al[:8]))

    print("\n" + "=" * 64 + "\n✅ 엔드투엔드 데모 완료 — 전체 흐름 정상 동작\n" + "=" * 64)


if __name__ == "__main__":
    main()
