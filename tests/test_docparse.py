"""문서 추출 자원 고갈 방어 테스트 (§8): 압축 폭탄 거부 + 추출 상한.

worker 1 프로세스라 추출 중 OOM은 전체 장애 → 상한/폭탄 가드가 방어선.

실행: python3 tests/test_docparse.py
"""
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="studio-dp-")
os.environ["STUDIO_DB"] = os.path.join(TMP, "studio.db")
os.environ["STUDIO_FERNET_KEY"] = os.path.join(TMP, ".fernet.key")
os.environ["STUDIO_LOG_DIR"] = os.path.join(TMP, "logs")

from studio import docparse   # noqa: E402

# ---------- 압축 폭탄 거부 ----------
bomb = os.path.join(TMP, "bomb.docx")
with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("word/document.xml", b"A" * (10 * 1024 * 1024))   # 10MB → 압축비 큼
raised = False
try:
    docparse._docx_bomb_check(bomb)
except ValueError:
    raised = True
assert raised, "압축 폭탄이 거부되지 않음"
# extract_text는 예외 대신 안내 텍스트 반환(파이프라인 중단 방지)
out = docparse.extract_text(bomb, None, "bomb.docx")
assert "추출 실패" in out, out
print("OK: DOCX 압축 폭탄 거부 → 안내 텍스트(파이프라인 미중단)")

# ---------- 정상 비율 zip은 통과 ----------
ok = os.path.join(TMP, "ok.docx")
with zipfile.ZipFile(ok, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("word/document.xml", os.urandom(2048))   # 압축 안 됨 → 비율 낮음
docparse._docx_bomb_check(ok)   # 예외 없어야
print("OK: 정상 압축비 DOCX는 통과")

# ---------- 텍스트 추출 상한 ----------
big = os.path.join(TMP, "big.txt")
with open(big, "w") as f:
    f.write("x" * (docparse.MAX_EXTRACT_CHARS + 5000))
t = docparse.extract_text(big, "text/plain", "big.txt")
assert len(t) <= docparse.MAX_EXTRACT_CHARS + len(docparse._TRUNC), len(t)
assert t.endswith(docparse._TRUNC)
print("OK: 대용량 텍스트 추출 상한 절단")

# ---------- 정상 소형 텍스트는 그대로 ----------
small = os.path.join(TMP, "s.md")
with open(small, "w") as f:
    f.write("# hello\nworld")
assert docparse.extract_text(small, None, "s.md") == "# hello\nworld"
print("OK: 소형 텍스트는 그대로 추출")

print("\nALL DOCPARSE TESTS PASSED")
