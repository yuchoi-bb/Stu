"""생성 코드 위험 패턴 정적 검사 게이트 (§12.4).

Step 3.5 리뷰 게이트와 짝을 이루는 방어선. push 전 생성 파일을 정규식 휴리스틱으로
훑어 위험 패턴을 표시한다. 코드가 runner(네트워크 개방)에서 실행되므로, 자동 승인
모드(§6.4)에서 사람 검토가 생략될 때 마지막 안전망이 된다.

- high: 임의 명령 실행 / 파괴적 파일 조작 / 시크릿 하드코딩 / 네트워크 유출.
  auto_approve 모드여도 high가 있으면 자동 승인을 보류하고 사람 검토를 강제한다.
- medium: 안전하지 않은 역직렬화·TLS 검증 비활성 등 — 경고만.

정규식 휴리스틱이므로 오탐 가능. 차단이 아니라 "표시 + 자동승인 보류"가 기본 정책.
"""
import re

# (rule, severity, compiled_regex, message)
_RAW_RULES = [
    # --- high: 임의 명령 실행 ---
    ("exec-shell-true", "high",
     r"subprocess\.(?:call|run|Popen|check_output|check_call)\([^)]*shell\s*=\s*True",
     "subprocess shell=True — 셸 인젝션 위험"),
    ("os-system", "high", r"\bos\.system\s*\(", "os.system — 셸 명령 실행"),
    ("py-eval-exec", "high", r"(?<![\w.])(?:eval|exec)\s*\(",
     "eval/exec — 임의 코드 실행"),
    ("c-system", "high", r"\bsystem\s*\(\s*[\"a-zA-Z_]", "system() — 셸 명령 실행"),
    ("c-popen", "high", r"\b_?popen\s*\(", "popen — 셸 명령 실행"),
    ("java-exec", "high", r"Runtime\.getRuntime\(\)\.exec",
     "Runtime.exec — 명령 실행"),
    ("node-child-process", "high",
     r"require\(['\"]child_process['\"]\)|\bchild_process\.\w",
     "child_process — 명령 실행"),
    ("shell-pipe-exec", "high", r"(?:curl|wget)\s+[^\n|]*\|\s*(?:ba)?sh",
     "원격 스크립트 파이프 실행 (curl|sh)"),
    ("reverse-shell", "high", r"/dev/tcp/|nc\s+-e|bash\s+-i\s+>&",
     "리버스 셸 흔적"),
    # --- high: 파괴적 파일 조작 ---
    ("rm-rf-root", "high", r"rm\s+-rf?\s+(?:--no-preserve-root\s+)?/(?:\s|$|\*)",
     "rm -rf / — 파괴적 삭제"),
    ("rmtree-root", "high", r"shutil\.rmtree\s*\(\s*['\"]?/(?:['\"]|\s|$)",
     "rmtree('/') — 파괴적 삭제"),
    ("fork-bomb", "high", r":\(\)\s*\{\s*:\|:&\s*\}\s*;:", "fork bomb"),
    ("disk-write", "high", r"\bdd\s+if=|\bmkfs\.", "저수준 디스크 쓰기(dd/mkfs)"),
    # --- high: 시크릿 하드코딩 ---
    ("aws-akid", "high", r"\bAKIA[0-9A-Z]{16}\b", "AWS Access Key ID 하드코딩"),
    ("private-key", "high",
     r"-----BEGIN (?:RSA|OPENSSH|EC|DSA|PGP) PRIVATE KEY-----",
     "개인 키 하드코딩"),
    ("gh-token", "high", r"\bgh[pousr]_[0-9A-Za-z]{30,}\b", "GitHub 토큰 하드코딩"),
    ("slack-token", "high", r"\bxox[baprs]-[0-9A-Za-z-]{10,}", "Slack 토큰 하드코딩"),
    # --- medium: 안전하지 않은 관행 ---
    ("py-pickle", "medium", r"\bpickle\.loads?\s*\(", "pickle 역직렬화 — 신뢰 입력만"),
    ("yaml-unsafe", "medium", r"\byaml\.load\s*\((?![^)]*Safe)",
     "yaml.load — SafeLoader 권장"),
    ("tls-verify-off", "medium", r"verify\s*=\s*False|InsecureSkipVerify\s*:\s*true",
     "TLS 검증 비활성"),
    ("hardcoded-secret", "medium",
     r"(?i)(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*['\"][^'\"]{6,}['\"]",
     "자격증명으로 보이는 값 하드코딩(확인)"),
]
_RULES = [(name, sev, re.compile(pat), msg) for name, sev, pat, msg in _RAW_RULES]

# 명령 실행/파괴적 조작 규칙은 주석 속 흔적(예: `# do not use eval(`)을 걸러
# 오탐이 auto_approve를 무력화하지 않도록, 라인 주석을 제거한 코드부만 검사한다.
# 시크릿 하드코딩 규칙은 주석에 있어도 위험하므로 원문 전체를 검사한다.
_CODE_ONLY = {
    "exec-shell-true", "os-system", "py-eval-exec", "c-system", "c-popen",
    "java-exec", "node-child-process", "shell-pipe-exec", "reverse-shell",
    "rm-rf-root", "rmtree-root", "fork-bomb", "disk-write",
}

_MAX_PER_FILE = 20   # 파일당 표시 상한 (플러딩 방지)


def _strip_line_comments(s: str) -> str:
    """`#` 및 `//` 라인 주석 제거 — 단 `://`(URL 스킴)는 보호."""
    h = s.find("#")
    if h != -1:
        s = s[:h]
    i = 0
    while True:
        i = s.find("//", i)
        if i == -1:
            break
        if i == 0 or s[i - 1] != ":":
            return s[:i]
        i += 2
    return s


def scan_text(path: str, content: str) -> list[dict]:
    """한 파일 내용을 훑어 findings 리스트 반환."""
    out = []
    for i, line in enumerate(content.splitlines(), 1):
        if len(line) > 4000:          # 압축/자동생성 라인은 건너뜀
            continue
        code = _strip_line_comments(line)
        for name, sev, rx, msg in _RULES:
            target = code if name in _CODE_ONLY else line
            if rx.search(target):
                out.append({"path": path, "line": i, "rule": name,
                            "severity": sev, "message": msg,
                            "snippet": line.strip()[:200]})
                if len(out) >= _MAX_PER_FILE:
                    return out
    return out


def scan_files(files: dict[str, str]) -> list[dict]:
    """생성 파일 전체 스캔. high가 앞에 오도록 정렬."""
    findings = []
    for path, content in files.items():
        findings.extend(scan_text(path, content))
    order = {"high": 0, "medium": 1}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["path"], f["line"]))
    return findings


def counts(findings: list[dict]) -> dict[str, int]:
    c = {"high": 0, "medium": 0}
    for f in findings:
        c[f["severity"]] = c.get(f["severity"], 0) + 1
    return c


def summarize(findings: list[dict]) -> str:
    """대화 주입용 요약 문자열."""
    c = counts(findings)
    head = f"[정적 검사] 위험 패턴 {len(findings)}건 (high {c['high']} / medium {c['medium']})"
    lines = [f"  - [{f['severity']}] {f['path']}:{f['line']} {f['message']}"
             for f in findings[:12]]
    if len(findings) > 12:
        lines.append(f"  … 외 {len(findings) - 12}건")
    tail = ("\nhigh 항목이 있어 자동 승인을 보류하고 사람 검토가 필요합니다."
            if c["high"] else "")
    return head + "\n" + "\n".join(lines) + tail
