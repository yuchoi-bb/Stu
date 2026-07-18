"""studio.html Playwright E2E — tests/ui_server.py를 서브프로세스로 띄워 검증.

실행: python3 tests/ui_test.py
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from playwright.sync_api import sync_playwright  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SHOT = os.environ.get("UI_SHOT_DIR", "/tmp")

# 빈 포트를 골라 백그라운드 실행 간 충돌을 원천 차단
_s = socket.socket()
_s.bind(("127.0.0.1", 0))
PORT = _s.getsockname()[1]
_s.close()
BASE = f"http://127.0.0.1:{PORT}"

env = dict(os.environ, STUDIO_UI_PORT=str(PORT))
server = subprocess.Popen([sys.executable, os.path.join(HERE, "ui_server.py")],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          env=env)
try:
    for _ in range(40):
        try:
            urllib.request.urlopen(f"{BASE}/studio", timeout=1)
            break
        except Exception:
            time.sleep(0.5)

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("dialog", lambda d: d.accept("테스트 부족합니다")
                if d.type == "prompt" else d.accept())

        page.goto(f"{BASE}/studio")
        page.click(".new-btn")
        page.wait_for_selector(".sess.active", timeout=8000)

        page.fill("#input", "parser 출력을 내림 처리로 바꿔줘")
        page.click("text=전송")
        page.wait_for_selector("textarea.req", timeout=15000)

        page.fill("textarea.req", "## R1(수정). 내림 + 음수는 0으로")
        page.wait_for_timeout(6500)
        assert page.input_value("textarea.req").startswith("## R1(수정)")
        print("OK: Step 2 카드 + 폴링 중 편집 보존")
        page.screenshot(path=f"{SHOT}/ui_step2.png")

        page.click("text=승인하고 진행")
        page.wait_for_selector(".ftab", timeout=20000)
        tabs = page.locator(".ftab").all_inner_texts()
        assert any("parser.c" in t for t in tabs), tabs
        page.locator(".ftab").nth(1).click()
        page.screenshot(path=f"{SHOT}/ui_step35.png")
        print("OK: Step 3.5 리뷰 카드 + 파일 탭 전환", tabs)

        assert page.locator(".p-id").inner_text().startswith("ST-")
        assert "awaiting_review" in page.locator(".panel").inner_text()

        detail = json.loads(urllib.request.urlopen(
            f"{BASE}/api/studio/sessions/"
            + page.evaluate("state.sid")).read())
        assert detail["studio"]["requirements"].startswith("## R1(수정)")
        print("OK: 사용자 수정본이 승인 requirements로 반영")

        page.click("text=거부 (사유)")
        page.wait_for_selector(".chip.fail", timeout=15000)
        page.screenshot(path=f"{SHOT}/ui_rejected.png")
        print("OK: 리뷰 거부 → 회차 fail 반영")

        page.fill("#input", "테스트를 더 추가해서 다시")
        page.click("text=전송")
        page.wait_for_selector("text=#2", timeout=15000)
        print("OK: 새 회차(attempt 2) 표시")

        page.wait_for_selector(".ftab", timeout=20000)
        page.click("text=승인 → push + 검증")
        page.wait_for_selector(".chip.fail, .chip.pushing, .chip.ci_running",
                               timeout=20000)
        print("OK: 승인 → push 단계 전이 (GHE 미연결 → fail 종결)")
        page.screenshot(path=f"{SHOT}/ui_final.png")
        browser.close()
    print("UI TESTS PASSED")
finally:
    server.terminate()
