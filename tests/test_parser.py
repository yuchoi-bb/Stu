"""생성 파일 블록 파서 견고성 테스트 (§7.1).

핵심: 파일 내용에 인라인/들여쓴 ```가 있어도 파일이 잘리지 않아야 한다.
(정규식 non-greedy가 첫 ```에서 잘라 파일을 조용히 손상시키던 버그 회귀 방지.)

실행: python3 tests/test_parser.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("STUDIO_DB", os.path.join(tempfile.mkdtemp(), "s.db"))
os.environ.setdefault("STUDIO_FERNET_KEY", os.path.join(tempfile.mkdtemp(), ".k"))

from studio import pipeline   # noqa: E402

# ---------- 기본: 다중 블록 ----------
text = ("변경 요약: R1 반영\n"
        "```file:src/a.c\n#include <x>\nint main(){}\n```\n"
        "```file:test/a_test.c\nTEST(){}\n```\n")
files = pipeline.parse_file_blocks(text)
assert set(files) == {"src/a.c", "test/a_test.c"}, files
assert files["src/a.c"] == "#include <x>\nint main(){}\n", repr(files["src/a.c"])
assert files["src/a.c"].endswith("\n")   # 파일 개행 복원
print("OK: 다중 블록 파싱 + 경로/내용 + 개행 복원")

# ---------- 핵심 회귀: 인라인 ```가 내용에 있어도 안 잘림 ----------
tricky = (
    '```file:src/emit.py\n'
    'FENCE = "```"          # 인라인 트리플 백틱\n'
    'def wrap(s):\n'
    '    return f"```\\n{s}\\n```"\n'
    'print(FENCE)\n'
    '```\n'
)
f = pipeline.parse_file_blocks(tricky)
assert "src/emit.py" in f, f
body = f["src/emit.py"]
# 5줄 전부 보존되어야 (예전 버그면 첫 ```에서 잘렸음)
assert 'FENCE = "```"' in body and "print(FENCE)" in body, repr(body)
assert body.count("\n") >= 4, repr(body)
print("OK: 인라인 ```(문자열/f-string) 포함 파일 미절단")

# ---------- 들여쓴 ``` (docstring 내 마크다운 펜스)도 보존 ----------
doc = (
    '```file:mod.py\n'
    '"""\n'
    'Example:\n'
    '    ```\n'
    '    do_thing()\n'
    '    ```\n'
    '"""\n'
    'x = 1\n'
    '```\n'
)
f = pipeline.parse_file_blocks(doc)
b = f["mod.py"]
assert "do_thing()" in b and b.rstrip().endswith("x = 1"), repr(b)
print("OK: 들여쓴 ``` (docstring 펜스) 보존 — 단독 컬럼0 ```에서만 닫힘")

# ---------- 빈 블록 ----------
f = pipeline.parse_file_blocks("```file:empty.txt\n```\n")
assert f == {"empty.txt": ""}, f
print("OK: 빈 파일 블록")

# ---------- 블록 없음 ----------
assert pipeline.parse_file_blocks("파일 없음. 그냥 설명.") == {}
print("OK: 블록 없으면 빈 dict")

print("\nALL PARSER TESTS PASSED")
