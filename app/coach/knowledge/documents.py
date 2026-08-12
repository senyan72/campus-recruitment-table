"""文档知识库：解析 md/txt/docx/pdf，按标题切片。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_HEADING = re.compile(r"^(#{1,4})\s+(.+)$", re.M)
_ALLOWED_EXT = {".md", ".txt", ".docx", ".pdf"}


def extract_text_from_bytes(*, filename: str, content: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in _ALLOWED_EXT:
        raise ValueError(f"不支持的文档类型: {suffix}，允许 {_ALLOWED_EXT}")
    if suffix in (".md", ".txt"):
        return content.decode("utf-8", errors="replace")
    if suffix == ".docx":
        try:
            from docx import Document  # type: ignore
        except ImportError as e:
            raise RuntimeError("解析 DOCX 需要安装 python-docx") from e
        from io import BytesIO

        doc = Document(BytesIO(content))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError as e:
            raise RuntimeError("解析 PDF 需要安装 pypdf") from e
        from io import BytesIO

        reader = PdfReader(BytesIO(content))
        parts = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
        return "\n".join(parts)
    return ""


def chunk_text_by_headings(text: str, *, max_chunk_chars: int = 1200) -> list[dict[str, Any]]:
    """按 Markdown 标题切片；无标题则按段落合并。"""
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[dict[str, Any]] = []
    matches = list(_HEADING.finditer(text))
    if matches:
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            heading = m.group(2).strip()
            if len(body) > max_chunk_chars:
                for j, part in enumerate(_split_long(body, max_chunk_chars)):
                    chunks.append(
                        {
                            "heading": heading if j == 0 else f"{heading} (续{j+1})",
                            "text": part,
                            "chunk_index": len(chunks),
                        }
                    )
            else:
                chunks.append({"heading": heading, "text": body, "chunk_index": len(chunks)})
        return chunks
    # 无标题：按空行分段
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 2 > max_chunk_chars and buf:
            chunks.append({"heading": "", "text": buf.strip(), "chunk_index": len(chunks)})
            buf = p
        else:
            buf = (buf + "\n\n" + p).strip() if buf else p
    if buf:
        chunks.append({"heading": "", "text": buf.strip(), "chunk_index": len(chunks)})
    if not chunks and text:
        chunks.append({"heading": "", "text": text[:max_chunk_chars], "chunk_index": 0})
    return chunks


def _split_long(text: str, size: int) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(text):
        out.append(text[i : i + size])
        i += size
    return out


def ingest_document(
    db: Any,
    *,
    filename: str,
    content: bytes,
    owner_id: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """解析文档、切片并写入 knowledge_documents / knowledge_chunks。"""
    import hashlib

    from app.timeutil import utc_now_iso

    text = extract_text_from_bytes(filename=filename, content=content)
    chunks = chunk_text_by_headings(text)
    if not chunks:
        raise ValueError("文档解析后无有效文本")
    sha = hashlib.sha256(content).hexdigest()
    now = utc_now_iso()
    doc_id = db.new_id("kdoc_")
    db.execute(
        "INSERT INTO knowledge_documents(id, owner_id, filename, content_type, tags_json, status, sha256, byte_size, meta_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            doc_id,
            owner_id,
            filename,
            Path(filename).suffix.lower().lstrip("."),
            db.dumps(tags or []),
            "ready",
            sha,
            len(content),
            db.dumps({"chunk_count": len(chunks), "char_count": len(text)}),
            now,
        ),
    )
    for ch in chunks:
        cid = db.new_id("kchk_")
        db.execute(
            "INSERT INTO knowledge_chunks(id, document_id, chunk_index, heading, text, meta_json, created_at) VALUES(?,?,?,?,?,?,?)",
            (
                cid,
                doc_id,
                int(ch.get("chunk_index", 0)),
                ch.get("heading") or "",
                ch.get("text") or "",
                db.dumps({}),
                now,
            ),
        )
    return {
        "document_id": doc_id,
        "filename": filename,
        "chunk_count": len(chunks),
        "char_count": len(text),
        "sha256": sha,
    }


def list_documents(
    db: Any,
    *,
    owner_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if owner_id:
        rows = db.fetchall(
            "SELECT * FROM knowledge_documents WHERE owner_id=? OR owner_id IS NULL ORDER BY created_at DESC LIMIT ?",
            (owner_id, limit),
        )
    else:
        rows = db.fetchall(
            "SELECT * FROM knowledge_documents ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    for r in rows:
        r["tags"] = db.loads(r.pop("tags_json"), [])
        r["meta"] = db.loads(r.pop("meta_json"), {})
    return rows


def search_document_chunks(
    db: Any,
    *,
    query: str,
    limit: int = 8,
    owner_id: str | None = None,
) -> list[dict[str, Any]]:
    """简易全文检索文档切片（LIKE）。"""
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    if owner_id:
        rows = db.fetchall(
            """
            SELECT c.id, c.document_id, c.chunk_index, c.heading, c.text, d.filename
            FROM knowledge_chunks c
            JOIN knowledge_documents d ON d.id = c.document_id
            WHERE (c.text LIKE ? OR c.heading LIKE ?)
              AND (d.owner_id IS NULL OR d.owner_id = ?)
            ORDER BY c.created_at DESC
            LIMIT ?
            """,
            (like, like, owner_id, limit),
        )
    else:
        rows = db.fetchall(
            """
            SELECT c.id, c.document_id, c.chunk_index, c.heading, c.text, d.filename
            FROM knowledge_chunks c
            JOIN knowledge_documents d ON d.id = c.document_id
            WHERE c.text LIKE ? OR c.heading LIKE ?
            ORDER BY c.created_at DESC
            LIMIT ?
            """,
            (like, like, limit),
        )
    return [
        {
            "kind": "document_chunk",
            "chunk_id": r["id"],
            "document_id": r["document_id"],
            "filename": r.get("filename"),
            "heading": r.get("heading"),
            "text": (r.get("text") or "")[:800],
            "chunk_index": r.get("chunk_index"),
        }
        for r in rows
    ]

