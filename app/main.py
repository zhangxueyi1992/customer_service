"""FastAPI 入口：REST 接口 + WebSocket 实时通信。"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import bot, db, knowledge

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="智能人工客服系统")
db.init_db()


class ConversationCreate(BaseModel):
    title: str = "新会话"


class FeedbackCreate(BaseModel):
    rating: int
    comment: str = ""


class DocEnabled(BaseModel):
    enabled: bool


class KnowledgeTest(BaseModel):
    question: str
    top_k: int = 5


class AgentAssign(BaseModel):
    agent_name: str


class ConversationMeta(BaseModel):
    tag: str = ""
    note: str = ""


class ConversationPinned(BaseModel):
    pinned: bool


class QuickReplyCreate(BaseModel):
    title: str
    content: str


class UserCreate(BaseModel):
    name: str
    role: str = "agent"
    password: str = ""


class UserUpdate(BaseModel):
    role: str | None = None
    password: str | None = None
    status: str | None = None


class LoginBody(BaseModel):
    name: str
    password: str = ""


class ConnectionManager:
    """按会话维度管理 WebSocket 连接，支持一对多广播。"""

    def __init__(self) -> None:
        self.rooms: dict[int, set[WebSocket]] = defaultdict(set)

    async def connect(self, conv_id: int, ws: WebSocket) -> None:
        await ws.accept()
        self.rooms[conv_id].add(ws)

    def disconnect(self, conv_id: int, ws: WebSocket) -> None:
        self.rooms.get(conv_id, set()).discard(ws)

    async def send(self, ws: WebSocket, payload: dict) -> None:
        await ws.send_text(json.dumps(payload, ensure_ascii=False))

    async def broadcast(
        self, conv_id: int, payload: dict, exclude: WebSocket | None = None
    ) -> None:
        for ws in list(self.rooms.get(conv_id, set())):
            if ws is exclude:
                continue
            try:
                await ws.send_text(json.dumps(payload, ensure_ascii=False))
            except Exception:
                self.disconnect(conv_id, ws)


manager = ConnectionManager()


# ---------- REST API ----------

@app.get("/api/conversations")
def api_list_conversations():
    return db.list_conversations()


@app.post("/api/conversations")
def api_create_conversation(body: ConversationCreate):
    return db.create_conversation(body.title)


@app.get("/api/conversations/{conv_id}")
def api_get_conversation(conv_id: int):
    conv = db.get_conversation(conv_id)
    if conv is None:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    return conv


@app.get("/api/conversations/{conv_id}/messages")
def api_get_messages(conv_id: int):
    return db.get_messages(conv_id)


@app.post("/api/conversations/{conv_id}/transfer")
def api_transfer(conv_id: int):
    conv = db.get_conversation(conv_id)
    if conv is None:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    db.transfer_conversation(conv_id, "queued")
    system_msg = db.add_message(
        conv_id,
        "system",
        "text",
        {"text": "已为您接入人工客服队列，请稍候，客服会尽快接入本会话。"},
    )
    return {"status": "queued", "system_message": system_msg}


@app.post("/api/conversations/{conv_id}/switch-ai")
def api_switch_ai(conv_id: int):
    conv = db.get_conversation(conv_id)
    if conv is None:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    db.transfer_conversation(conv_id, "bot")
    system_msg = db.add_message(
        conv_id,
        "system",
        "text",
        {"text": "已为您切换回智能客服，机器人将继续为您服务。"},
    )
    return {"status": "bot", "system_message": system_msg}


@app.get("/api/agent/conversations")
def api_agent_conversations(agent_name: str = "客服1"):
    """客服工作台：排队中的会话 + 当前客服接管的会话。"""
    return db.list_agent_conversations(agent_name.strip() or "客服1")


@app.post("/api/agent/conversations/{conv_id}/assign")
async def api_agent_assign(conv_id: int, body: AgentAssign):
    """客服接管排队会话。"""
    conv = db.get_conversation(conv_id)
    if conv is None:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    agent_name = body.agent_name.strip() or "客服"
    if not db.assign_agent(conv_id, agent_name):
        return JSONResponse({"error": "该会话不在排队中，可能已被其他客服接管"}, status_code=409)
    system_msg = db.add_message(
        conv_id,
        "system",
        "text",
        {"text": f"人工客服 {agent_name} 已接入本会话，现在由人工客服为您服务。"},
    )
    updated = db.get_conversation(conv_id)
    await manager.broadcast(
        conv_id,
        {"type": "conversation_updated", "conversation": updated},
    )
    await manager.broadcast(
        conv_id, {"type": "message", "message": system_msg}
    )
    return {"status": "human", "conversation": updated, "system_message": system_msg}


@app.post("/api/agent/conversations/{conv_id}/return")
async def api_agent_return(conv_id: int, body: AgentAssign):
    """客服把会话放回排队（换人/暂不处理）。"""
    agent_name = body.agent_name.strip() or "客服"
    if not db.return_to_queue(conv_id, agent_name):
        return JSONResponse({"error": "该会话不是您接管的会话"}, status_code=409)
    system_msg = db.add_message(
        conv_id,
        "system",
        "text",
        {"text": "客服暂时离开，本会话已回到排队中，稍后会有客服继续为您服务。"},
    )
    updated = db.get_conversation(conv_id)
    await manager.broadcast(
        conv_id,
        {"type": "conversation_updated", "conversation": updated},
    )
    await manager.broadcast(
        conv_id, {"type": "message", "message": system_msg}
    )
    return {"status": "queued", "conversation": updated, "system_message": system_msg}


@app.patch("/api/agent/conversations/{conv_id}/meta")
def api_agent_meta(conv_id: int, body: ConversationMeta):
    """设置会话标签与备注。"""
    if not db.update_conversation_meta(conv_id, body.tag.strip(), body.note.strip()):
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    return {"ok": True, "conversation": db.get_conversation(conv_id)}


@app.patch("/api/agent/conversations/{conv_id}/pinned")
def api_agent_pinned(conv_id: int, body: ConversationPinned):
    """置顶/取消置顶会话。"""
    if not db.set_conversation_pinned(conv_id, body.pinned):
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    return {"ok": True, "conversation": db.get_conversation(conv_id)}


# ---------- 快捷回复 ----------


@app.get("/api/agent/quick-replies")
def api_list_quick_replies():
    return db.list_quick_replies()


@app.post("/api/agent/quick-replies")
def api_create_quick_reply(body: QuickReplyCreate):
    title = body.title.strip()
    content = body.content.strip()
    if not title or not content:
        return JSONResponse({"error": "标题和内容不能为空"}, status_code=400)
    return db.create_quick_reply(title, content)


@app.put("/api/agent/quick-replies/{qr_id}")
def api_update_quick_reply(qr_id: int, body: QuickReplyCreate):
    title = body.title.strip()
    content = body.content.strip()
    if not title or not content:
        return JSONResponse({"error": "标题和内容不能为空"}, status_code=400)
    if not db.update_quick_reply(qr_id, title, content):
        return JSONResponse({"error": "快捷回复不存在"}, status_code=404)
    return {"ok": True, "quick_reply": db.get_quick_reply(qr_id)}


@app.delete("/api/agent/quick-replies/{qr_id}")
def api_delete_quick_reply(qr_id: int):
    if not db.delete_quick_reply(qr_id):
        return JSONResponse({"error": "快捷回复不存在"}, status_code=404)
    return {"ok": True}


# ---------- 管理与统计 ----------


@app.get("/api/admin/stats")
def api_admin_stats(date_from: str = "", date_to: str = ""):
    """关键指标：知识库命中率、转人工率、平均响应时间、满意度评分。"""
    return db.stats_summary(date_from, date_to)


@app.get("/api/admin/conversations")
def api_admin_conversations(
    keyword: str = "",
    status: str = "",
    agent_name: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 200,
):
    """对话记录查询（关键词/状态/客服/时间范围过滤）。"""
    return db.query_conversations(
        keyword=keyword,
        status=status,
        agent_name=agent_name,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )


@app.get("/api/admin/conversations/{conv_id}")
def api_admin_conversation_detail(conv_id: int):
    """对话详情（完整消息）。"""
    detail = db.conversation_detail(conv_id)
    if detail is None:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    return detail


@app.get("/api/admin/export")
def api_admin_export(
    fmt: str = "csv",
    keyword: str = "",
    status: str = "",
    agent_name: str = "",
    date_from: str = "",
    date_to: str = "",
):
    """导出对话记录：CSV / JSON。"""
    data = db.export_conversations(
        keyword=keyword,
        status=status,
        agent_name=agent_name,
        date_from=date_from,
        date_to=date_to,
    )
    if fmt == "json":
        return Response(
            content=json.dumps(data, ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="conversations.json"'},
        )

    # CSV：一行一条消息，方便 Excel 打开
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["会话ID", "会话标题", "会话状态", "客服", "发送者", "消息类型", "内容", "时间"])
    for item in data:
        conv = item["conversation"]
        for msg in item["messages"]:
            content = msg.get("content") or {}
            text = content.get("text") or content.get("title") or content.get("url") or json.dumps(content, ensure_ascii=False)
            writer.writerow(
                [
                    conv["id"],
                    conv["title"],
                    conv["status"],
                    conv.get("agent_name", ""),
                    msg["sender"],
                    msg["type"],
                    text,
                    msg["created_at"],
                ]
            )
    buf.seek(0)
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="conversations.csv"'},
    )


@app.post("/api/admin/login")
def api_admin_login(body: LoginBody):
    """管理员登录校验（简单账号体系，按 Project.md 的角色分级）。"""
    user = db.verify_user(body.name.strip(), body.password)
    if user is None:
        return JSONResponse({"error": "账号或密码错误"}, status_code=401)
    if user["role"] != "admin":
        return JSONResponse({"error": "该账号不是管理员，无法进入管理后台"}, status_code=403)
    return {"ok": True, "user": user}


@app.get("/api/admin/users")
def api_admin_users():
    return db.list_users()


@app.post("/api/admin/users")
def api_admin_create_user(body: UserCreate):
    name = body.name.strip()
    if not name:
        return JSONResponse({"error": "账号名不能为空"}, status_code=400)
    if body.role not in ("admin", "agent", "user"):
        return JSONResponse({"error": "角色不合法"}, status_code=400)
    try:
        return db.create_user(name, body.role, body.password)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"创建失败：{exc}"}, status_code=400)


@app.put("/api/admin/users/{user_id}")
def api_admin_update_user(user_id: int, body: UserUpdate):
    if body.role is not None and body.role not in ("admin", "agent", "user"):
        return JSONResponse({"error": "角色不合法"}, status_code=400)
    if body.status is not None and body.status not in ("active", "disabled"):
        return JSONResponse({"error": "状态不合法"}, status_code=400)
    if not db.update_user(
        user_id,
        role=body.role or "",
        password=body.password,
        status=body.status or "",
    ):
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    return {"ok": True, "user": db.get_user(user_id)}


@app.delete("/api/admin/users/{user_id}")
def api_admin_delete_user(user_id: int):
    if not db.delete_user(user_id):
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    return {"ok": True}


@app.delete("/api/conversations/{conv_id}")
def api_delete_conversation(conv_id: int):
    if not db.delete_conversation(conv_id):
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    return {"ok": True}


@app.post("/api/conversations/{conv_id}/feedback")
def api_feedback(conv_id: int, body: FeedbackCreate):
    if body.rating < 1 or body.rating > 5:
        return JSONResponse({"error": "评分需在 1-5 之间"}, status_code=400)
    db.add_feedback(conv_id, body.rating, body.comment)
    return {"ok": True}


# ---------- 知识库管理 ----------


@app.post("/api/knowledge/documents")
async def api_upload_knowledge(
    files: list[UploadFile] = File(...), uploader: str = "admin"
):
    """批量上传文档：PDF / Word(.docx) / TXT / Markdown / HTML。"""
    results = []
    for f in files:
        raw = await f.read()
        try:
            doc = knowledge.ingest_document(f.filename or "未命名文档", raw, uploader)
            results.append({"filename": f.filename, "ok": True, "doc": doc})
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "filename": f.filename,
                    "ok": False,
                    "error": str(exc),
                }
            )
    return {"results": results}


@app.get("/api/knowledge/documents")
def api_list_knowledge_documents():
    """文档列表与知识库统计（含解析状态、切片数、启用数）。"""
    return {"documents": db.list_knowledge_docs(), "stats": db.kb_stats()}


@app.get("/api/knowledge/documents/{doc_id}")
def api_get_knowledge_document(doc_id: int):
    doc = db.get_knowledge_doc(doc_id)
    if doc is None:
        return JSONResponse({"error": "文档不存在"}, status_code=404)
    return doc


@app.get("/api/knowledge/documents/{doc_id}/chunks")
def api_get_knowledge_chunks(doc_id: int):
    if db.get_knowledge_doc(doc_id) is None:
        return JSONResponse({"error": "文档不存在"}, status_code=404)
    return db.get_knowledge_chunks(doc_id)


@app.delete("/api/knowledge/documents/{doc_id}")
def api_delete_knowledge_document(doc_id: int):
    if not knowledge.delete_document(doc_id):
        return JSONResponse({"error": "文档不存在"}, status_code=404)
    return {"ok": True}


@app.patch("/api/knowledge/documents/{doc_id}/enabled")
def api_toggle_knowledge_document(doc_id: int, body: DocEnabled):
    if not knowledge.set_doc_enabled(doc_id, body.enabled):
        return JSONResponse({"error": "文档不存在"}, status_code=404)
    return {"ok": True}


@app.post("/api/knowledge/documents/{doc_id}/reindex")
def api_reindex_knowledge_document(doc_id: int):
    """重新解析入库（更新）：用已保存的原始文件重建切片与向量。"""
    try:
        return knowledge.reindex_document(doc_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.post("/api/knowledge/test")
def api_knowledge_test(body: KnowledgeTest):
    """效果测试：输入问题，预览召回的知识切片与相似度。"""
    if body.top_k < 1 or body.top_k > 20:
        return JSONResponse({"error": "top_k 需在 1-20 之间"}, status_code=400)
    return {
        "question": body.question,
        "results": knowledge.search(body.question, body.top_k),
    }


# ---------- WebSocket ----------

async def _handle_bot_reply(conv_id: int, user_message: dict) -> None:
    """机器人：先显示“正在输入”，再结合历史上下文返回回答。"""
    conv = db.get_conversation(conv_id)
    if conv is None or conv["status"] != "bot":
        return  # 排队中 / 已转人工后机器人不再回复

    await manager.broadcast(
        conv_id, {"type": "typing", "sender": "bot", "is_typing": True}
    )
    await asyncio.sleep(1.0 + min(len(user_message["content"]["text"]) * 0.02, 1.8))

    history = [
        m
        for m in db.get_messages(conv_id)
        if m["id"] < user_message["id"]
        and m["sender"] in ("user", "bot")
        and m["type"] in ("text", "answer")
    ]
    result = await asyncio.to_thread(
        bot.answer, user_message["content"]["text"], history
    )
    reply = db.add_message(conv_id, "bot", "answer", result)

    # 机器人已“读”到用户消息，用户侧显示双勾
    db.update_message_status(user_message["id"], "read")

    await manager.broadcast(
        conv_id, {"type": "typing", "sender": "bot", "is_typing": False}
    )
    await manager.broadcast(
        conv_id,
        {"type": "status", "message_ids": [user_message["id"]], "status": "read"},
    )
    await manager.broadcast(conv_id, {"type": "message", "message": reply})


@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    try:
        conv_id = int(ws.query_params.get("conversation_id", "0"))
    except ValueError:
        conv_id = 0
    role = ws.query_params.get("role", "user")
    agent_name = ws.query_params.get("agent_name", "").strip() or "客服"
    if db.get_conversation(conv_id) is None:
        await ws.close(code=1008)
        return

    await manager.connect(conv_id, ws)
    conv = db.get_conversation(conv_id)
    await manager.send(
        ws,
        {"type": "hello", "conversation": conv, "role": role, "agent_name": agent_name},
    )

    # 新连接视为回到会话：用户侧把机器人消息标记已读，客服侧把用户消息标记已读
    read_sender = "bot" if role == "user" else "user"
    read_ids = db.mark_conversation_read(conv_id, read_sender)
    if read_ids:
        await manager.send(
            ws, {"type": "status", "message_ids": read_ids, "status": "read"}
        )

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")

            if msg_type == "send":
                payload = data.get("content") or {}
                kind = data.get("msg_type", "text")
                if kind == "text":
                    text = str(payload.get("text") or "").strip()
                    if not text:
                        continue
                    content = {"text": text}
                elif kind in ("image", "card"):
                    content = payload
                else:
                    continue

                client_id = data.get("client_id")
                sender = "human" if role == "agent" else "user"
                msg = db.add_message(
                    conv_id, sender, kind, content, status="sent"
                )
                # 第一条用户消息作为会话标题
                if sender == "user" and conv["title"] == "新会话" and kind == "text":
                    db.update_conversation_title(conv_id, text[:16])
                    conv = db.get_conversation(conv_id)

                await manager.send(
                    ws,
                    {
                        "type": "message_ack",
                        "client_id": client_id,
                        "message_id": msg["id"],
                        "status": "sent",
                    },
                )
                await manager.broadcast(
                    conv_id,
                    {
                        "type": "message",
                        "message": msg,
                        "sender": role,
                        "client_id": client_id,
                    },
                    exclude=ws,
                )
                if kind == "text":
                    if role == "user":
                        asyncio.create_task(_handle_bot_reply(conv_id, msg))

            elif msg_type == "typing":
                await manager.broadcast(
                    conv_id,
                    {
                        "type": "typing",
                        "sender": role,
                        "is_typing": True,
                    },
                    exclude=ws,
                )

            elif msg_type == "read":
                target = "bot" if role == "user" else "user"
                ids = db.mark_conversation_read(conv_id, target)
                if ids:
                    await manager.broadcast(
                        conv_id,
                        {"type": "status", "message_ids": ids, "status": "read"},
                        exclude=ws,
                    )

    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(conv_id, ws)


# ---------- 静态页面 ----------

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/agent")
def agent_console():
    return FileResponse(BASE_DIR / "static" / "agent.html")


@app.get("/admin")
def admin_console():
    return FileResponse(BASE_DIR / "static" / "admin.html")
