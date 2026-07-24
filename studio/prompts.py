"""프롬프트 버전관리 (§11): prompts 테이블 + repo 파일(prompt_files/) 이중 관리.

기동 시 파일 내용이 테이블 최신 버전과 다르면 새 버전으로 시드한다.
"""
import os

from . import db

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt_files")
NAMES = ("requirements", "generate", "select_files")


def seed() -> None:
    for name in NAMES:
        path = os.path.join(_DIR, f"{name}.md")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        row = db.one(
            "SELECT version, content FROM prompts WHERE name=? "
            "ORDER BY version DESC LIMIT 1", (name,))
        if row is None:
            db.execute("INSERT INTO prompts (name, version, content) VALUES (?,1,?)",
                       (name, content))
        elif row["content"] != content:
            db.execute("INSERT INTO prompts (name, version, content) VALUES (?,?,?)",
                       (name, row["version"] + 1, content))


def get(name: str) -> str:
    row = db.one(
        "SELECT content FROM prompts WHERE name=? ORDER BY version DESC LIMIT 1",
        (name,))
    if row is None:
        raise KeyError(name)
    return row["content"]
