"""문서 입력 파이프라인 (§8): PDF/DOCX/TXT/MD → 텍스트 추출.

- Bedrock에는 텍스트만 전달. 대용량은 청킹.
- HWP는 미지원 (§12.1 확인 항목) — 명시적으로 안내 메시지 반환.
- 추출 실패는 예외 대신 안내 텍스트로 반환 (파이프라인 중단 방지).
"""
import os

from . import config, db

CHUNK_CHARS = 40_000   # 청크당 문자 수 (대용량 문서 분할)


def extract_text(path: str, mime_type: str | None, filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    try:
        if ext in (".txt", ".md") or (mime_type or "").startswith("text/"):
            return _read_text(path)
        if ext == ".pdf" or mime_type == "application/pdf":
            return _read_pdf(path)
        if ext == ".docx" or (mime_type or "").endswith("wordprocessingml.document"):
            return _read_docx(path)
        if ext in (".hwp", ".hwpx"):
            return f"[HWP 미지원: {filename} — 추출 도구 미도입(§12.1). 텍스트로 변환 후 재첨부 필요]"
        return f"[미지원 포맷: {filename} ({ext or mime_type}) — 텍스트 추출 생략]"
    except Exception as e:
        return f"[추출 실패: {filename} — {e}]"


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _read_pdf(path: str) -> str:
    from pypdf import PdfReader
    reader = PdfReader(path)
    parts = []
    for i, page in enumerate(reader.pages):
        t = page.extract_text() or ""
        if t.strip():
            parts.append(f"--- page {i + 1} ---\n{t}")
    return "\n\n".join(parts) if parts else "[PDF에서 추출된 텍스트 없음 (스캔 이미지일 수 있음)]"


def _read_docx(path: str) -> str:
    import docx
    d = docx.Document(path)
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts) if parts else "[DOCX에서 추출된 텍스트 없음]"


def chunks(text: str) -> list[str]:
    """대용량 문서를 CHUNK_CHARS 단위로 분할 (문단 경계 우선)."""
    if len(text) <= CHUNK_CHARS:
        return [text]
    out, buf = [], []
    size = 0
    for para in text.split("\n"):
        if size + len(para) > CHUNK_CHARS and buf:
            out.append("\n".join(buf))
            buf, size = [], 0
        buf.append(para)
        size += len(para) + 1
    if buf:
        out.append("\n".join(buf))
    return out


def process_attachment(attachment_id: int) -> str:
    """업로드 직후 호출 (ThreadPool). 추출 → parsed_text_path 저장."""
    row = db.one("SELECT * FROM attachments WHERE attachment_id=?", (attachment_id,))
    if row is None:
        return ""
    text = extract_text(row["storage_path"], row["mime_type"], row["filename"])
    parsed_path = row["storage_path"] + ".txt"
    with open(parsed_path, "w", encoding="utf-8") as f:
        f.write(text)
    db.execute("UPDATE attachments SET parsed_text_path=? WHERE attachment_id=?",
               (parsed_path, attachment_id))
    return text


def session_attachment_text(session_id: str, *, max_chars: int = 120_000) -> str:
    """세션 첨부들의 추출 텍스트를 모아 생성 컨텍스트로 (§7.1 Step 2/3)."""
    rows = db.query(
        "SELECT filename, parsed_text_path FROM attachments "
        "WHERE session_id=? AND parsed_text_path IS NOT NULL ORDER BY attachment_id",
        (session_id,))
    blocks, total = [], 0
    for r in rows:
        try:
            with open(r["parsed_text_path"], encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        header = f"# 첨부: {r['filename']}\n"
        remaining = max_chars - total
        if remaining <= len(header):
            break
        body = content[:remaining - len(header)]
        blocks.append(header + body)
        total += len(header) + len(body)
    return "\n\n".join(blocks)
