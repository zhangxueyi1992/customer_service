"""SQLite 数据访问层：会话、消息、评价、快捷回复的持久化。"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "customer_service.db"
_lock = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _lock:
        with _connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    title      TEXT NOT NULL,
                    status     TEXT NOT NULL DEFAULT 'bot',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL,
                    sender          TEXT NOT NULL,
                    msg_type        TEXT NOT NULL DEFAULT 'text',
                    content         TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'sent',
                    created_at      TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS feedback (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL,
                    rating          INTEGER NOT NULL,
                    comment         TEXT DEFAULT '',
                    created_at      TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    name       TEXT NOT NULL UNIQUE,
                    role       TEXT NOT NULL DEFAULT 'user',
                    password   TEXT DEFAULT '',
                    status     TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS quick_replies (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    title      TEXT NOT NULL,
                    content    TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge_docs (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename   TEXT NOT NULL,
                    doc_type   TEXT NOT NULL,
                    size       INTEGER NOT NULL DEFAULT 0,
                    uploader   TEXT NOT NULL DEFAULT 'admin',
                    status     TEXT NOT NULL DEFAULT 'processing',
                    enabled    INTEGER NOT NULL DEFAULT 1,
                    error      TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id      INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content     TEXT NOT NULL,
                    page        INTEGER,
                    title       TEXT,
                    metadata    TEXT DEFAULT '{}',
                    vector      TEXT,
                    enabled     INTEGER NOT NULL DEFAULT 1,
                    created_at  TEXT NOT NULL
                );
                """
            )
            # 兼容旧库：补充人工客服相关字段
            for column in (
                "ALTER TABLE conversations ADD COLUMN agent_name TEXT DEFAULT ''",
                "ALTER TABLE conversations ADD COLUMN tag TEXT DEFAULT ''",
                "ALTER TABLE conversations ADD COLUMN note TEXT DEFAULT ''",
                "ALTER TABLE conversations ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
            ):
                try:
                    conn.execute(column)
                except sqlite3.OperationalError:
                    pass  # 字段已存在
            # 首次初始化：预置管理员与客服账号
            count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if count == 0:
                now = _now()
                conn.executemany(
                    "INSERT INTO users (name, role, password, status, created_at) VALUES (?, ?, ?, 'active', ?)",
                    [
                        ("管理员", "admin", "admin123", now),
                        ("客服1", "agent", "", now),
                    ],
                )


def _row_to_message(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "sender": row["sender"],
        "type": row["msg_type"],
        "content": json.loads(row["content"]),
        "status": row["status"],
        "created_at": row["created_at"],
    }


def _row_to_conversation(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "agent_name": row["agent_name"] if "agent_name" in row.keys() else "",
        "tag": row["tag"] if "tag" in row.keys() else "",
        "note": row["note"] if "note" in row.keys() else "",
        "pinned": bool(row["pinned"]) if "pinned" in row.keys() else False,
        "created_at": row["created_at"],
        "last_message": row["last_message"] if "last_message" in row.keys() else None,
        "last_time": row["last_time"] if "last_time" in row.keys() else None,
        "last_type": row["last_type"] if "last_type" in row.keys() else None,
    }


def list_conversations() -> list[dict]:
    with _lock:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT c.*,
                       m.content AS last_message,
                       m.msg_type AS last_type,
                       m.created_at AS last_time
                FROM conversations c
                LEFT JOIN messages m ON m.id = (
                    SELECT id FROM messages
                    WHERE conversation_id = c.id
                    ORDER BY id DESC LIMIT 1
                )
                ORDER BY COALESCE(m.id, c.id) DESC
                """
            ).fetchall()
    conversations = []
    for row in rows:
        conv = _row_to_conversation(row)
        if conv["last_message"]:
            try:
                content = json.loads(conv["last_message"])
                if "text" in content:
                    conv["last_message"] = content["text"]
                elif content.get("url") and conv["last_type"] == "image":
                    conv["last_message"] = "[图片]"
                elif "title" in content:
                    conv["last_message"] = f"[卡片] {content['title']}"
                else:
                    conv["last_message"] = "[消息]"
            except json.JSONDecodeError:
                pass
        conversations.append(conv)
    return conversations


def create_conversation(title: str = "新会话") -> dict:
    with _lock:
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO conversations (title, status, created_at) VALUES (?, 'bot', ?)",
                (title, _now()),
            )
            conv_id = cur.lastrowid
    return get_conversation(conv_id)


def get_conversation(conv_id: int) -> dict | None:
    with _lock:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()
    return _row_to_conversation(row) if row else None


def update_conversation_title(conv_id: int, title: str) -> None:
    with _lock:
        with _connect() as conn:
            conn.execute(
                "UPDATE conversations SET title = ? WHERE id = ?", (title, conv_id)
            )


def transfer_conversation(conv_id: int, status: str = "human") -> None:
    with _lock:
        with _connect() as conn:
            conn.execute(
                "UPDATE conversations SET status = ?, agent_name = '' WHERE id = ?",
                (status, conv_id),
            )


def assign_agent(conv_id: int, agent_name: str) -> bool:
    """把排队会话指派给某位客服，返回会话是否存在。"""
    with _lock:
        with _connect() as conn:
            cur = conn.execute(
                "SELECT id FROM conversations WHERE id = ? AND status = 'queued'",
                (conv_id,),
            )
            if cur.fetchone() is None:
                return False
            conn.execute(
                "UPDATE conversations SET status = 'human', agent_name = ? WHERE id = ?",
                (agent_name, conv_id),
            )
    return True


def return_to_queue(conv_id: int, agent_name: str) -> bool:
    """客服把会话重新放回排队（换人/挂起），仅限自己接管的会话。"""
    with _lock:
        with _connect() as conn:
            cur = conn.execute(
                """
                SELECT id FROM conversations
                WHERE id = ? AND status = 'human' AND agent_name = ?
                """,
                (conv_id, agent_name),
            )
            if cur.fetchone() is None:
                return False
            conn.execute(
                "UPDATE conversations SET status = 'queued', agent_name = '' WHERE id = ?",
                (conv_id,),
            )
    return True


def update_conversation_meta(conv_id: int, tag: str = "", note: str = "") -> bool:
    """更新会话标签与备注，返回会话是否存在。"""
    with _lock:
        with _connect() as conn:
            cur = conn.execute(
                "SELECT id FROM conversations WHERE id = ?", (conv_id,)
            )
            if cur.fetchone() is None:
                return False
            conn.execute(
                "UPDATE conversations SET tag = ?, note = ? WHERE id = ?",
                (tag, note, conv_id),
            )
    return True


def list_agent_conversations(agent_name: str) -> list[dict]:
    """客服工作台会话列表：待接管的排队会话 + 当前客服接管的会话。"""
    with _lock:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT c.*,
                       m.content AS last_message,
                       m.msg_type AS last_type,
                       m.created_at AS last_time
                FROM conversations c
                LEFT JOIN messages m ON m.id = (
                    SELECT id FROM messages
                    WHERE conversation_id = c.id
                    ORDER BY id DESC LIMIT 1
                )
                WHERE c.status = 'queued' OR (c.status = 'human' AND c.agent_name = ?)
                ORDER BY
                    c.pinned DESC,
                    CASE WHEN c.status = 'queued' THEN 0 ELSE 1 END,
                    COALESCE(m.id, c.id) DESC
                """,
                (agent_name,),
            ).fetchall()
    conversations = []
    for row in rows:
        conv = _row_to_conversation(row)
        if conv["last_message"]:
            try:
                content = json.loads(conv["last_message"])
                if "text" in content:
                    conv["last_message"] = content["text"]
                elif content.get("url") and conv["last_type"] == "image":
                    conv["last_message"] = "[图片]"
                elif "title" in content:
                    conv["last_message"] = f"[卡片] {content['title']}"
                else:
                    conv["last_message"] = "[消息]"
            except json.JSONDecodeError:
                pass
        conversations.append(conv)
    return conversations


def set_conversation_pinned(conv_id: int, pinned: bool) -> bool:
    """设置会话置顶状态，返回会话是否存在。"""
    with _lock:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE conversations SET pinned = ? WHERE id = ?",
                (1 if pinned else 0, conv_id),
            )
            return cur.rowcount > 0


def delete_conversation(conv_id: int) -> bool:
    """删除整个会话及其消息、评价，返回是否删除成功。"""
    with _lock:
        with _connect() as conn:
            cur = conn.execute(
                "DELETE FROM conversations WHERE id = ?", (conv_id,)
            )
            deleted = cur.rowcount > 0
            conn.execute(
                "DELETE FROM messages WHERE conversation_id = ?", (conv_id,)
            )
            conn.execute(
                "DELETE FROM feedback WHERE conversation_id = ?", (conv_id,)
            )
    return deleted


def get_messages(conv_id: int) -> list[dict]:
    with _lock:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id ASC",
                (conv_id,),
            ).fetchall()
    return [_row_to_message(r) for r in rows]


def add_message(
    conv_id: int, sender: str, msg_type: str, content: dict, status: str = "sent"
) -> dict:
    with _lock:
        with _connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO messages (conversation_id, sender, msg_type, content, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    conv_id,
                    sender,
                    msg_type,
                    json.dumps(content, ensure_ascii=False),
                    status,
                    _now(),
                ),
            )
            msg_id = cur.lastrowid
    with _lock:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE id = ?", (msg_id,)
            ).fetchone()
    return _row_to_message(row)


def update_message_status(message_id: int, status: str) -> None:
    with _lock:
        with _connect() as conn:
            conn.execute(
                "UPDATE messages SET status = ? WHERE id = ?", (status, message_id)
            )


def mark_conversation_read(conv_id: int, sender: str) -> list[int]:
    """把某发送者的消息全部标记为已读，返回被更新的消息 id。"""
    with _lock:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM messages
                WHERE conversation_id = ? AND sender = ? AND status != 'read'
                """,
                (conv_id, sender),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                conn.execute(
                    "UPDATE messages SET status = 'read' WHERE id IN ({})".format(
                        ",".join("?" * len(ids))
                    ),
                    ids,
                )
    return ids


def add_feedback(conv_id: int, rating: int, comment: str = "") -> None:
    with _lock:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO feedback (conversation_id, rating, comment, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (conv_id, rating, comment, _now()),
            )


# ---------- 快捷回复 ----------


def _row_to_quick_reply(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "content": row["content"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_quick_replies() -> list[dict]:
    with _lock:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM quick_replies ORDER BY id DESC"
            ).fetchall()
    return [_row_to_quick_reply(r) for r in rows]


def create_quick_reply(title: str, content: str) -> dict:
    now = _now()
    with _lock:
        with _connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO quick_replies (title, content, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (title, content, now, now),
            )
            qr_id = cur.lastrowid
    return get_quick_reply(qr_id)


def get_quick_reply(qr_id: int) -> dict | None:
    with _lock:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM quick_replies WHERE id = ?", (qr_id,)
            ).fetchone()
    return _row_to_quick_reply(row) if row else None


def update_quick_reply(qr_id: int, title: str, content: str) -> bool:
    with _lock:
        with _connect() as conn:
            cur = conn.execute(
                """
                UPDATE quick_replies SET title = ?, content = ?, updated_at = ?
                WHERE id = ?
                """,
                (title, content, _now(), qr_id),
            )
            return cur.rowcount > 0


def delete_quick_reply(qr_id: int) -> bool:
    with _lock:
        with _connect() as conn:
            cur = conn.execute(
                "DELETE FROM quick_replies WHERE id = ?", (qr_id,)
            )
            return cur.rowcount > 0


# ---------- 知识库 ----------


def _row_to_knowledge_doc(row: sqlite3.Row) -> dict:
    doc = {
        "id": row["id"],
        "filename": row["filename"],
        "doc_type": row["doc_type"],
        "size": row["size"],
        "uploader": row["uploader"],
        "status": row["status"],
        "enabled": bool(row["enabled"]),
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    keys = row.keys()
    if "chunk_count" in keys:
        doc["chunk_count"] = row["chunk_count"]
    if "enabled_chunks" in keys:
        doc["enabled_chunks"] = row["enabled_chunks"]
    return doc


def _row_to_knowledge_chunk(row: sqlite3.Row) -> dict:
    chunk = {
        "id": row["id"],
        "doc_id": row["doc_id"],
        "chunk_index": row["chunk_index"],
        "content": row["content"],
        "page": row["page"],
        "title": row["title"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
    }
    try:
        chunk["metadata"] = json.loads(row["metadata"] or "{}")
    except json.JSONDecodeError:
        chunk["metadata"] = {}
    chunk["has_vector"] = bool(row["vector"])
    return chunk


def create_knowledge_doc(
    filename: str, doc_type: str, size: int, uploader: str = "admin"
) -> dict:
    now = _now()
    with _lock:
        with _connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO knowledge_docs
                    (filename, doc_type, size, uploader, status, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'processing', 1, ?, ?)
                """,
                (filename, doc_type, size, uploader, now, now),
            )
            doc_id = cur.lastrowid
    return get_knowledge_doc(doc_id)


def update_knowledge_doc_status(doc_id: int, status: str, error: str = "") -> None:
    with _lock:
        with _connect() as conn:
            conn.execute(
                """
                UPDATE knowledge_docs
                SET status = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, error, _now(), doc_id),
            )


def list_knowledge_docs() -> list[dict]:
    with _lock:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT d.*,
                       COUNT(c.id) AS chunk_count,
                       COALESCE(SUM(CASE WHEN c.enabled = 1 THEN 1 ELSE 0 END), 0) AS enabled_chunks
                FROM knowledge_docs d
                LEFT JOIN knowledge_chunks c ON c.doc_id = d.id
                GROUP BY d.id
                ORDER BY d.id DESC
                """
            ).fetchall()
    return [_row_to_knowledge_doc(r) for r in rows]


def get_knowledge_doc(doc_id: int) -> dict | None:
    with _lock:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT d.*,
                       COUNT(c.id) AS chunk_count,
                       COALESCE(SUM(CASE WHEN c.enabled = 1 THEN 1 ELSE 0 END), 0) AS enabled_chunks
                FROM knowledge_docs d
                LEFT JOIN knowledge_chunks c ON c.doc_id = d.id
                WHERE d.id = ?
                GROUP BY d.id
                """,
                (doc_id,),
            ).fetchone()
    return _row_to_knowledge_doc(row) if row else None


def delete_knowledge_doc(doc_id: int) -> tuple[bool, list[int]]:
    """删除文档及其切片，返回 (是否存在, 被删除的切片 id 列表)。"""
    with _lock:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id FROM knowledge_chunks WHERE doc_id = ?", (doc_id,)
            ).fetchall()
            chunk_ids = [r["id"] for r in rows]
            cur = conn.execute(
                "DELETE FROM knowledge_docs WHERE id = ?", (doc_id,)
            )
            existed = cur.rowcount > 0
            conn.execute(
                "DELETE FROM knowledge_chunks WHERE doc_id = ?", (doc_id,)
            )
    return existed, chunk_ids


def delete_knowledge_chunks(doc_id: int) -> list[int]:
    """删除某文档的全部切片，返回被删除的切片 id 列表。"""
    with _lock:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id FROM knowledge_chunks WHERE doc_id = ?", (doc_id,)
            ).fetchall()
            chunk_ids = [r["id"] for r in rows]
            if chunk_ids:
                conn.execute(
                    "DELETE FROM knowledge_chunks WHERE doc_id = ?", (doc_id,)
                )
    return chunk_ids


def add_knowledge_chunk(
    doc_id: int,
    chunk_index: int,
    content: str,
    page: int | None,
    title: str | None,
    metadata: dict,
    vector: list[float] | None = None,
) -> int:
    with _lock:
        with _connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO knowledge_chunks
                    (doc_id, chunk_index, content, page, title, metadata, vector, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    doc_id,
                    chunk_index,
                    content,
                    page,
                    title,
                    json.dumps(metadata, ensure_ascii=False),
                    json.dumps(vector) if vector is not None else None,
                    _now(),
                ),
            )
            return cur.lastrowid


def update_chunk_vector(chunk_id: int, vector_json: str) -> None:
    with _lock:
        with _connect() as conn:
            conn.execute(
                "UPDATE knowledge_chunks SET vector = ? WHERE id = ?",
                (vector_json, chunk_id),
            )


def get_knowledge_chunks(doc_id: int) -> list[dict]:
    with _lock:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM knowledge_chunks
                WHERE doc_id = ?
                ORDER BY chunk_index ASC
                """,
                (doc_id,),
            ).fetchall()
    return [_row_to_knowledge_chunk(r) for r in rows]


def get_enabled_chunks() -> list[dict]:
    """返回启用状态下可参与检索的全部切片（含来源文档名）。"""
    with _lock:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT c.id, c.doc_id, c.chunk_index, c.content, c.page, c.title,
                       d.filename AS doc_name
                FROM knowledge_chunks c
                JOIN knowledge_docs d ON d.id = c.doc_id
                WHERE c.enabled = 1 AND d.enabled = 1
                ORDER BY c.id ASC
                """
            ).fetchall()
    return [dict(r) for r in rows]


def set_doc_enabled(doc_id: int, enabled: bool) -> bool:
    """停用/启用文档及其全部切片，返回文档是否存在。"""
    with _lock:
        with _connect() as conn:
            cur = conn.execute(
                "SELECT id FROM knowledge_docs WHERE id = ?", (doc_id,)
            )
            if cur.fetchone() is None:
                return False
            conn.execute(
                "UPDATE knowledge_docs SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, _now(), doc_id),
            )
            conn.execute(
                "UPDATE knowledge_chunks SET enabled = ? WHERE doc_id = ?",
                (1 if enabled else 0, doc_id),
            )
    return True


def kb_stats() -> dict:
    with _lock:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS docs,
                    COALESCE(SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END), 0) AS enabled_docs,
                    (SELECT COUNT(*) FROM knowledge_chunks) AS chunks,
                    (SELECT COUNT(*) FROM knowledge_chunks WHERE enabled = 1) AS enabled_chunks,
                    (SELECT COUNT(*) FROM knowledge_docs WHERE status = 'completed') AS completed_docs,
                    (SELECT COUNT(*) FROM knowledge_docs WHERE status = 'failed') AS failed_docs
                FROM knowledge_docs
                """
            ).fetchone()
    return {
        "docs": row["docs"],
        "enabled_docs": row["enabled_docs"],
        "chunks": row["chunks"],
        "enabled_chunks": row["enabled_chunks"],
        "completed_docs": row["completed_docs"],
        "failed_docs": row["failed_docs"],
    }


# ---------- 管理与统计 ----------


def _parse_ts(ts: str | None) -> float | None:
    """把 'YYYY-MM-DD HH:MM:SS' 解析为时间戳，失败返回 None。"""
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def query_conversations(
    keyword: str = "",
    status: str = "",
    agent_name: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 200,
) -> list[dict]:
    """管理后台对话记录查询：支持关键词/状态/客服/时间范围过滤。"""
    sql = [
        """
        SELECT c.*,
               m.content AS last_message,
               m.msg_type AS last_type,
               m.created_at AS last_time,
               (SELECT COUNT(*) FROM messages x WHERE x.conversation_id = c.id) AS msg_count,
               (SELECT COUNT(*) FROM messages x WHERE x.conversation_id = c.id AND x.sender = 'human') AS human_msgs
        FROM conversations c
        LEFT JOIN messages m ON m.id = (
            SELECT id FROM messages
            WHERE conversation_id = c.id
            ORDER BY id DESC LIMIT 1
        )
        WHERE 1=1
        """
    ]
    params: list = []
    if status:
        sql.append("AND c.status = ?")
        params.append(status)
    if agent_name:
        sql.append("AND c.agent_name = ?")
        params.append(agent_name)
    if date_from:
        sql.append("AND c.created_at >= ?")
        params.append(date_from)
    if date_to:
        sql.append("AND c.created_at <= ?")
        params.append(date_to)
    if keyword:
        sql.append(
            "AND (c.title LIKE ? OR c.tag LIKE ? OR c.note LIKE ? OR c.id IN ("
            "SELECT DISTINCT conversation_id FROM messages WHERE content LIKE ?))"
        )
        like = f"%{keyword}%"
        params.extend([like, like, like, like])
    sql.append("ORDER BY c.id DESC LIMIT ?")
    params.append(max(1, min(limit, 1000)))

    with _lock:
        with _connect() as conn:
            rows = conn.execute(" ".join(sql), params).fetchall()
    conversations = []
    for row in rows:
        conv = _row_to_conversation(row)
        conv["msg_count"] = row["msg_count"]
        conv["human_msgs"] = row["human_msgs"]
        if conv["last_message"]:
            try:
                content = json.loads(conv["last_message"])
                if "text" in content:
                    conv["last_message"] = content["text"]
                elif content.get("url") and conv["last_type"] == "image":
                    conv["last_message"] = "[图片]"
                elif "title" in content:
                    conv["last_message"] = f"[卡片] {content['title']}"
                else:
                    conv["last_message"] = "[消息]"
            except json.JSONDecodeError:
                pass
        conversations.append(conv)
    return conversations


def conversation_detail(conv_id: int) -> dict | None:
    """对话详情：会话信息 + 完整消息列表。"""
    conv = get_conversation(conv_id)
    if conv is None:
        return None
    conv["messages"] = get_messages(conv_id)
    return conv


def _bot_intent(content: dict) -> str:
    """从机器人回答内容判断意图：kb（知识库命中）/ fallback（兜底）/ chitchat / transfer。"""
    intent = content.get("intent", "")
    if intent:
        return intent
    text = content.get("text", "")
    if "知识库暂无" in text or "没有找到" in text or "换个说法" in text:
        return "fallback"
    if "很高兴" in text or "聊" in text:
        return "chitchat"
    return "kb"


def stats_summary(date_from: str = "", date_to: str = "") -> dict:
    """关键指标：会话量、知识库命中率、转人工率、平均响应时间、满意度。"""
    conditions = []
    params: list = []
    if date_from:
        conditions.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("created_at <= ?")
        params.append(date_to)
    cond_sql = (" AND " + " AND ".join(conditions)) if conditions else ""

    with _lock:
        with _connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM conversations WHERE 1=1{cond_sql}", params
            ).fetchone()[0]
            human_total = conn.execute(
                f"SELECT COUNT(*) FROM conversations WHERE status IN ('queued','human'){cond_sql}",
                params,
            ).fetchone()[0]
            transfer_count = conn.execute(
                f"""
                SELECT COUNT(*) FROM conversations
                WHERE EXISTS (
                    SELECT 1 FROM messages m
                    WHERE m.conversation_id = conversations.id
                      AND m.sender = 'system'
                      AND (m.content LIKE '%接入人工%' OR m.content LIKE '%排队%')
                ){cond_sql}
                """,
                params,
            ).fetchone()[0]

            # 知识库命中率：bot answer 消息中 intent=kb 的比例
            kb_hits = kb_total = 0
            rows = conn.execute(
                """
                SELECT content FROM messages
                WHERE sender = 'bot' AND msg_type = 'answer'
                """
            ).fetchall()
            for row in rows:
                try:
                    content = json.loads(row["content"])
                except json.JSONDecodeError:
                    continue
                intent = _bot_intent(content)
                if intent in ("kb", "fallback"):
                    kb_total += 1
                    if intent == "kb":
                        kb_hits += 1

            # 平均响应时间：同一会话内用户消息到下一条 bot 消息的间隔
            rows = conn.execute(
                """
                SELECT m.conversation_id, m.created_at, m.sender
                FROM messages m
                WHERE m.sender IN ('user', 'bot')
                ORDER BY m.conversation_id ASC, m.id ASC
                """
            ).fetchall()
            resp_secs = []
            last_user: float | None = None
            last_conv: int | None = None
            for row in rows:
                ts = _parse_ts(row["created_at"])
                if ts is None:
                    continue
                if row["sender"] == "user":
                    last_user = ts
                    last_conv = row["conversation_id"]
                elif (
                    row["sender"] == "bot"
                    and last_user is not None
                    and row["conversation_id"] == last_conv
                ):
                    resp_secs.append(ts - last_user)
                    last_user = None

            avg_response = sum(resp_secs) / len(resp_secs) if resp_secs else 0.0

            # 满意度：平均分与分布
            fb_row = conn.execute(
                """
                SELECT COUNT(*) AS cnt, AVG(rating) AS avg_rating
                FROM feedback
                """
            ).fetchone()
            dist_rows = conn.execute(
                "SELECT rating, COUNT(*) AS cnt FROM feedback GROUP BY rating ORDER BY rating"
            ).fetchall()

    return {
        "total_conversations": total,
        "human_conversations": human_total,
        "transfer_count": transfer_count,
        "transfer_rate": round(transfer_count / total * 100, 1) if total else 0.0,
        "kb_hit_rate": round(kb_hits / kb_total * 100, 1) if kb_total else 0.0,
        "kb_answered": kb_total,
        "kb_hits": kb_hits,
        "avg_response_secs": round(avg_response, 1),
        "feedback_count": fb_row["cnt"],
        "avg_rating": round(fb_row["avg_rating"], 2) if fb_row["avg_rating"] else 0.0,
        "rating_dist": {str(r["rating"]): r["cnt"] for r in dist_rows},
    }


def export_conversations(
    keyword: str = "",
    status: str = "",
    agent_name: str = "",
    date_from: str = "",
    date_to: str = "",
) -> list[dict]:
    """导出对话记录（含完整消息列表，供 CSV/JSON 导出）。"""
    convs = query_conversations(
        keyword=keyword,
        status=status,
        agent_name=agent_name,
        date_from=date_from,
        date_to=date_to,
        limit=1000,
    )
    export = []
    with _lock:
        with _connect() as conn:
            for conv in convs:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id ASC",
                    (conv["id"],),
                ).fetchall()
                export.append(
                    {
                        "conversation": conv,
                        "messages": [_row_to_message(r) for r in rows],
                    }
                )
    return export


# ---------- 账号管理 ----------


def _row_to_user(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "role": row["role"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


def list_users() -> list[dict]:
    with _lock:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY id ASC"
            ).fetchall()
    return [_row_to_user(r) for r in rows]


def verify_user(name: str, password: str) -> dict | None:
    """校验账号密码，返回用户信息（不含密码）。"""
    with _lock:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE name = ? AND status = 'active'",
                (name,),
            ).fetchone()
    if row is None or row["password"] != password:
        return None
    return _row_to_user(row)


def create_user(name: str, role: str, password: str = "") -> dict:
    now = _now()
    with _lock:
        with _connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO users (name, role, password, status, created_at)
                VALUES (?, ?, ?, 'active', ?)
                """,
                (name, role, password, now),
            )
            user_id = cur.lastrowid
    return get_user(user_id)


def get_user(user_id: int) -> dict | None:
    with _lock:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
    return _row_to_user(row) if row else None


def update_user(user_id: int, role: str = "", password: str | None = None, status: str = "") -> bool:
    """更新账号角色/密码/状态，返回是否存在。"""
    sets: list[str] = []
    params: list = []
    if role:
        sets.append("role = ?")
        params.append(role)
    if password is not None:
        sets.append("password = ?")
        params.append(password)
    if status:
        sets.append("status = ?")
        params.append(status)
    if not sets:
        return False
    params.append(user_id)
    with _lock:
        with _connect() as conn:
            cur = conn.execute(
                f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params
            )
            return cur.rowcount > 0


def delete_user(user_id: int) -> bool:
    with _lock:
        with _connect() as conn:
            cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            return cur.rowcount > 0
