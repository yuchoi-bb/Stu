"""git 커밋/push 계층 (§6.3, §6.5) — GHE API와 분리된 순수 git 조작.

가드 3종:
- workflow 파일 수정 금지 (§6.3)
- blob SHA 가드: 생성 시점 원문과 push 시점 원격 파일이 다르면 push_conflict —
  조용한 덮어쓰기 절대 금지 (§6.5). 원문 baseline이 없는데 원격에 파일이
  존재하는 경우도 충돌로 처리한다 (읽지 않은 파일을 덮어쓰는 경로 차단).
- non-fast-forward: 1회 재시도(재clone) 후에도 실패하면 push_conflict.
"""
import os
import shutil
import subprocess
import tempfile


class PushConflict(Exception):
    pass


class WorkflowGuardViolation(Exception):
    pass


def _git(cwd, *args) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def blob_sha_at_head(workdir: str, path: str) -> str | None:
    r = subprocess.run(["git", "rev-parse", f"HEAD:{path}"],
                       cwd=workdir, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def commit_and_push(remote_url: str, branch: str, files: dict[str, str],
                    base_blobs: dict[str, str | None],
                    author_name: str, author_email: str,
                    commit_message: str, *,
                    guard_exempt: set[str] | None = None,
                    _retry: bool = True) -> str:
    """원격 HEAD 기반 커밋 생성 + push (§6.5). 반환: commit SHA.

    guard_exempt: blob SHA 가드 예외 경로 (studio 소유 파일 — requirements md 등,
    studio 자신만 쓰는 파일이라 회차 간 재커밋이 정상).
    """
    guard_exempt = guard_exempt or set()
    for path in files:
        norm = os.path.normpath(path)
        if norm.startswith(".github" + os.sep + "workflows") or norm.startswith(".."):
            raise WorkflowGuardViolation(path)

    tmp = tempfile.mkdtemp(prefix="studio-git-")
    try:
        _git(None, "clone", "--branch", branch, "--single-branch",
             remote_url, tmp)

        # blob SHA 가드 (§6.5)
        for path in files:
            if path in guard_exempt:
                continue
            current = blob_sha_at_head(tmp, path)
            base = base_blobs.get(path)
            if current is not None and current != base:
                raise PushConflict(
                    f"{path}: 생성 중 브랜치에서 파일이 변경됨 "
                    f"(base={base}, remote={current})")

        for path, content in files.items():
            dest = os.path.join(tmp, path)
            os.makedirs(os.path.dirname(dest) or tmp, exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(content)

        _git(tmp, "add", "-A")
        env_author = [
            "-c", f"user.name={author_name}",
            "-c", f"user.email={author_email}",
        ]
        subprocess.run(["git", *env_author, "commit",
                        "--author", f"{author_name} <{author_email}>",
                        "-m", commit_message],
                       cwd=tmp, capture_output=True, text=True, check=True)
        sha = _git(tmp, "rev-parse", "HEAD")

        r = subprocess.run(["git", "push", "origin", branch],
                           cwd=tmp, capture_output=True, text=True)
        if r.returncode != 0:
            if _retry:   # push 사이에 사용자가 먼저 push한 경우: 재fetch 후 재생성 1회
                shutil.rmtree(tmp, ignore_errors=True)
                return commit_and_push(remote_url, branch, files, base_blobs,
                                       author_name, author_email,
                                       commit_message, _retry=False)
            raise PushConflict(f"push 거부 (non-fast-forward): {r.stderr.strip()}")
        return sha
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def fetch_file(remote_url: str, branch: str, path: str) -> tuple[str, str] | None:
    """Step 3 pass 2: 원문 fetch — (content, blob_sha) 반환, 없으면 None."""
    tmp = tempfile.mkdtemp(prefix="studio-fetch-")
    try:
        _git(None, "clone", "--branch", branch, "--single-branch",
             "--depth", "1", remote_url, tmp)
        full = os.path.join(tmp, path)
        if not os.path.isfile(full):
            return None
        with open(full, encoding="utf-8") as f:
            content = f.read()
        return content, blob_sha_at_head(tmp, path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
