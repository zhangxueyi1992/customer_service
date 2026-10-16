"""知识库管理服务：文档解析与清洗、文本切片、向量化入库、检索测试。

支持 PDF / Word(.docx) / TXT / Markdown / HTML 五种格式，
向量模型使用本地缓存的 BAAI/bge-small-zh-v1.5，向量库使用 Chroma。
"""

from __future__ import annotations

import io
import json
import re
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import chromadb
from fastembed import TextEmbedding

from . import db

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
CHROMA_DIR = BASE_DIR / "chroma_data"
COLLECTION_NAME = "knowledge_chunks"
EMBED_MODEL = "BAAI/bge-small-zh-v1.5"

ALLOWED_EXT = {".pdf", ".docx", ".txt", ".md", ".markdown", ".html", ".htm"}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB

# 切片参数：目标长度 / 超长句硬切长度 / 相邻切片重叠字符数
TARGET_CHUNK = 500
MAX_CHUNK = 900
OVERLAP = 80

_model: TextEmbedding | None = None
_collection: Any = None


def get_embed_model() -> TextEmbedding:
    global _model
    if _model is None:
        _model = TextEmbedding(EMBED_MODEL)
    return _model


def get_collection():
    global _collection
    if _collection is None:
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        _collection = client.get_or_create_collection(
            name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        )
    return _collection


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    model = get_embed_model()
    return [vec.tolist() for vec in model.embed(texts)]


# ---------- 文本解码与清洗 ----------


def _decode(raw: bytes) -> str:
    """依次尝试 UTF-8 / GB18030 / Big5 / Latin-1 解码，兼容不同编码的 TXT。"""
    for enc in ("utf-8", "utf-8-sig", "gb18030", "big5", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


_CONTROL_RE = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u2028\u2029]")
_PAGE_NUM_RE = re.compile(
    r"^\s*(?:第\s*[0-9一二三四五六七八九十百千]+\s*页|"
    r"page\s*\d+|\d+\s*/\s*\d+|[-–—]?\s*\d+\s*[-–—]?)\s*$",
    re.I,
)
_LEADING_PAGE_RE = re.compile(
    r"^(?:第\s*[0-9一二三四五六七八九十百千]+\s*页|"
    r"page\s*\d+|[-–—]\s*\d+\s*[-–—])\s+",
    re.I,
)


def clean_text(text: str) -> str:
    """去掉 BOM、乱码替换符、零宽字符、控制字符、独立页码与多余空行。"""
    text = (text or "").replace("\ufeff", "")
    text = text.replace("\ufffd", "").replace("锟斤拷", "")
    text = re.sub(r"[\u200b\u200c\u200d\u2060]", "", text)
    text = _CONTROL_RE.sub("", text)
    text = unicodedata.normalize("NFC", text)
    lines: list[str] = []
    blank = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            if not blank:
                lines.append("")
            blank = True
            continue
        blank = False
        line = _LEADING_PAGE_RE.sub("", line).strip()
        if not line or _PAGE_NUM_RE.match(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _strip_repeated_lines(pages: list[str]) -> list[str]:
    """去掉跨页重复出现的页眉/页脚（短行、非完整句子）。"""
    page_lines = [[ln.strip() for ln in p.splitlines() if ln.strip()] for p in pages]
    counter: dict[str, int] = {}
    for lines in page_lines:
        for ln in set(lines):
            counter[ln] = counter.get(ln, 0) + 1
    drop = {ln for ln, n in counter.items() if n >= 2 and _looks_like_header_footer(ln)}
    return ["\n".join(ln for ln in lines if ln not in drop) for lines in page_lines]


def _looks_like_header_footer(line: str) -> bool:
    if len(line) > 40:
        return False
    if _PAGE_NUM_RE.match(line):
        return True
    if line.endswith(("。", "！", "？", ".", "!", "?")):
        return False
    # 含常用正文虚词的行更像是正文内容，保守保留
    if re.search(r"[的是了在和有为]", line):
        return False
    return True


# ---------- 文档解析 ----------

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _join_lines(lines: list[str]) -> str:
    """段落内换行拼接：中文字符之间不加空格，其余用空格连接。"""
    text = ""
    for ln in lines:
        if not text:
            text = ln
        elif _CJK_RE.search(text[-1]) and _CJK_RE.search(ln[0]):
            text += ln
        else:
            text += " " + ln
    return text


def _blocks_from_lines(text: str, page: int | None) -> list[dict]:
    """把纯文本/带 Markdown 标题的文本转换为块列表。"""
    blocks: list[dict] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append({"text": _join_lines(para), "heading": None, "page": page})
            para.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        m = _HEADING_RE.match(line)
        if m:
            flush()
            blocks.append(
                {"text": m.group(2).strip(), "heading": len(m.group(1)), "page": page}
            )
        else:
            para.append(line)
    flush()
    return blocks


class _HTMLTextExtractor(HTMLParser):
    """提取 HTML 正文，保留段落结构，并把 h1~h6 转成 Markdown 标题标记。"""

    _SKIP_TAGS = {"script", "style", "head", "title", "noscript", "svg"}
    _BLOCK_TAGS = {
        "p", "div", "li", "tr", "br", "section", "article",
        "blockquote", "ul", "ol", "table",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        if len(tag) == 2 and tag[0] == "h" and tag[1] in "123456":
            self.parts.append("\n" + "#" * int(tag[1]) + " ")
        elif tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag in ("p", "div", "li", "tr", "section", "article", "blockquote"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def _parse_html(raw: bytes) -> list[dict]:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(_decode(raw))
        parser.close()
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"HTML 解析失败：{exc}") from exc
    body = clean_text("".join(parser.parts))
    return _blocks_from_lines(body, page=None)


def _parse_pdf(raw: bytes) -> list[dict]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"PDF 解析失败：{exc}") from exc
    if not pages:
        raise ValueError("PDF 没有可解析的页面")
    if not any(p.strip() for p in pages):
        raise ValueError("无法从 PDF 提取文本（可能为扫描件或加密文档）")
    pages = _strip_repeated_lines(pages)
    blocks: list[dict] = []
    for idx, text in enumerate(pages, start=1):
        blocks.extend(_blocks_from_lines(text, page=idx))
    return blocks


def _parse_docx(raw: bytes) -> list[dict]:
    try:
        import docx

        document = docx.Document(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Word 文档解析失败：{exc}") from exc

    blocks: list[dict] = []
    for p in document.paragraphs:
        text = (p.text or "").strip()
        if not text:
            continue
        style_name = (p.style.name or "") if p.style else ""
        m = re.match(r"^(?:Heading|标题)\s*(\d+)", style_name)
        if m:
            blocks.append({"text": text, "heading": int(m.group(1)), "page": None})
        else:
            blocks.append({"text": text, "heading": None, "page": None})

    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            # 合并单元格可能出现重复，按顺序去重
            unique_cells = list(dict.fromkeys(cells))
            text = " | ".join(c for c in unique_cells if c)
            if text:
                blocks.append({"text": text, "heading": None, "page": None})

    if not blocks:
        raise ValueError("Word 文档中未提取到文本内容")
    return blocks


def parse_document(doc_type: str, raw: bytes) -> list[dict]:
    """按文件类型解析为块列表：{text, heading?, page?}。"""
    doc_type = doc_type.lower()
    if doc_type in ("txt", "md", "markdown"):
        return _blocks_from_lines(clean_text(_decode(raw)), page=None)
    if doc_type in ("html", "htm"):
        return _parse_html(raw)
    if doc_type == "pdf":
        return _parse_pdf(raw)
    if doc_type == "docx":
        return _parse_docx(raw)
    raise ValueError(f"不支持的文件类型：.{doc_type}")


# ---------- 文本切片 ----------


def _hard_split(text: str, target: int, overlap: int) -> list[str]:
    """超长文本按固定长度硬切，保留重叠。"""
    out: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + target, n)
        out.append(text[start:end])
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return out


def _chunk_paragraph(
    text: str, target: int = TARGET_CHUNK, overlap: int = OVERLAP
) -> list[str]:
    """按句子边界聚合段落，超长内容按固定长度带重叠切块。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= target:
        return [text]

    parts = [p for p in re.split(r"(?<=[。！？；.!?;])\s*", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for part in parts:
        if len(part) > target:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_hard_split(part, target, overlap))
            continue
        if not buf:
            buf = part
        elif len(buf) + len(part) <= target:
            buf += part
        else:
            chunks.append(buf)
            buf = (buf[-overlap:] if overlap else "") + part
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


def build_chunks(blocks: list[dict]) -> list[dict]:
    """按标题结构分节、按段落聚合并切块，保留标题/页码元数据。"""
    chunks: list[dict] = []
    title: str | None = None
    page: int | None = None
    pending: list[str] = []

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        merged = "\n".join(p for p in pending if p.strip())
        for part in _chunk_paragraph(merged):
            chunks.append({"content": part, "title": title, "page": page})
        pending = []

    last_page: int | None = None
    for block in blocks:
        text = clean_text(block.get("text") or "")
        if not text:
            continue
        if block.get("heading"):
            flush()
            title = text
            if block.get("page") is not None:
                page = block["page"]
            last_page = page
            continue
        block_page = block.get("page")
        if block_page is not None and last_page is not None and block_page != last_page:
            flush()
        if block_page is not None:
            page = block_page
        pending.append(text)
        last_page = block_page if block_page is not None else last_page
    flush()
    return chunks


# ---------- 入库 / 管理 / 检索 ----------


def _ingest(doc: dict, raw: bytes) -> None:
    """对已建档的文档执行：解析 → 清洗 → 切片 → 向量化 → 写库。"""
    blocks = parse_document(doc["doc_type"], raw)
    chunks = build_chunks(blocks)
    if not chunks:
        raise ValueError("文档解析后未提取到有效文本，未生成切片")

    rows: list[dict] = []
    for idx, chunk in enumerate(chunks, start=1):
        meta = {
            "source": doc["filename"],
            "page": chunk["page"],
            "title": chunk["title"],
        }
        chunk_id = db.add_knowledge_chunk(
            doc_id=doc["id"],
            chunk_index=idx,
            content=chunk["content"],
            page=chunk["page"],
            title=chunk["title"],
            metadata=meta,
            vector=None,
        )
        rows.append({"id": chunk_id, "content": chunk["content"]})

    texts = [r["content"] for r in rows]
    vectors = embed_texts(texts)
    collection = get_collection()
    metadatas = []
    for row, chunk in zip(rows, chunks):
        metadatas.append(
            {
                "doc_id": doc["id"],
                "doc_name": doc["filename"],
                "title": chunk["title"] or "",
                "page": chunk["page"] if chunk["page"] is not None else 0,
                "chunk_id": row["id"],
                "enabled": 1,
            }
        )
    collection.add(
        ids=[f"chunk_{r['id']}" for r in rows],
        documents=texts,
        embeddings=vectors,
        metadatas=metadatas,
    )
    for row, vec in zip(rows, vectors):
        db.update_chunk_vector(row["id"], json.dumps(vec))
    db.update_knowledge_doc_status(doc["id"], "completed")


def ingest_document(filename: str, raw: bytes, uploader: str = "admin") -> dict:
    """上传并处理单个文档，返回带状态的文档信息。"""
    filename = (filename or "").strip() or "未命名文档"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(
            f"不支持的文件类型：{ext or '（无扩展名）'}，"
            "仅支持 PDF / Word(.docx) / TXT / Markdown / HTML"
        )
    if not raw:
        raise ValueError("文件内容为空")
    if len(raw) > MAX_FILE_SIZE:
        raise ValueError("文件超过 20MB 大小上限")

    doc = db.create_knowledge_doc(filename, ext.lstrip("."), len(raw), uploader)
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        (UPLOAD_DIR / f"{doc['id']}{ext}").write_bytes(raw)
        _ingest(doc, raw)
        return db.get_knowledge_doc(doc["id"])
    except Exception as exc:  # noqa: BLE001
        db.update_knowledge_doc_status(doc["id"], "failed", error=str(exc)[:500])
        raise


def reindex_document(doc_id: int) -> dict:
    """重新解析入库（更新），用于内容修订后的刷新。"""
    doc = db.get_knowledge_doc(doc_id)
    if doc is None:
        raise ValueError("文档不存在")
    save_path = UPLOAD_DIR / f"{doc_id}.{doc['doc_type']}"
    if not save_path.exists():
        raise ValueError("原始文件已丢失，无法重新解析")

    old_ids = db.delete_knowledge_chunks(doc_id)
    if old_ids:
        try:
            get_collection().delete(ids=[f"chunk_{i}" for i in old_ids])
        except Exception:  # noqa: BLE001
            pass
    db.update_knowledge_doc_status(doc_id, "processing")
    try:
        _ingest(doc, save_path.read_bytes())
        return db.get_knowledge_doc(doc_id)
    except Exception as exc:  # noqa: BLE001
        db.update_knowledge_doc_status(doc_id, "failed", error=str(exc)[:500])
        raise


def delete_document(doc_id: int) -> bool:
    """删除文档、切片、向量与本地原始文件。"""
    existed, chunk_ids = db.delete_knowledge_doc(doc_id)
    if chunk_ids:
        try:
            get_collection().delete(ids=[f"chunk_{i}" for i in chunk_ids])
        except Exception:  # noqa: BLE001
            pass
    for path in UPLOAD_DIR.glob(f"{doc_id}.*"):
        try:
            path.unlink()
        except OSError:
            pass
    return existed


def set_doc_enabled(doc_id: int, enabled: bool) -> bool:
    """停用/启用文档（同步更新全部切片与向量库元数据）。"""
    doc = db.get_knowledge_doc(doc_id)
    if doc is None:
        return False
    db.set_doc_enabled(doc_id, enabled)
    chunk_ids = [c["id"] for c in db.get_knowledge_chunks(doc_id)]
    if chunk_ids:
        try:
            get_collection().update(
                ids=[f"chunk_{i}" for i in chunk_ids],
                metadatas=[{"enabled": 1 if enabled else 0}] * len(chunk_ids),
            )
        except Exception:  # noqa: BLE001
            pass
    return True


def search(question: str, top_k: int = 5) -> list[dict]:
    """向量检索测试：返回命中的切片、来源文档、标题、页码与相似度。"""
    question = (question or "").strip()
    if not question:
        return []
    collection = get_collection()
    if collection.count() == 0:
        return []
    q_vec = embed_texts([question])[0]
    try:
        res = collection.query(
            query_embeddings=[q_vec],
            n_results=top_k,
            where={"enabled": 1},
            include=["documents", "metadatas", "distances"],
        )
    except Exception:  # noqa: BLE001
        return []

    results: list[dict] = []
    documents = res.get("documents", [[]])[0]
    metadatas = res.get("metadatas", [[]])[0]
    distances = res.get("distances", [[]])[0]
    for doc_text, meta, dist in zip(documents, metadatas, distances):
        page = meta.get("page")
        page = int(page) if isinstance(page, int) and page > 0 else None
        results.append(
            {
                "chunk_id": meta.get("chunk_id"),
                "doc_id": meta.get("doc_id"),
                "doc_name": meta.get("doc_name") or "",
                "title": meta.get("title") or "",
                "page": page,
                "snippet": doc_text,
                "similarity": round(max(0.0, 1.0 - float(dist)), 4),
            }
        )
    return results
