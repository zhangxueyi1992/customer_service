"""对话界面冒烟测试：REST + WebSocket 全链路验证。

运行方式（项目根目录）：
    .venv\\Scripts\\python.exe tests\\smoke_test.py

前置条件：服务已启动（.venv\\Scripts\\python.exe -m uvicorn app.main:app --port 8000）
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import httpx
import websockets

PORT = os.environ.get("CS_PORT", "8000")
BASE = f"http://127.0.0.1:{PORT}"
WS_URL = f"ws://127.0.0.1:{PORT}/ws/chat"


async def ws_exchange(conv_id: int, text: str) -> dict:
    """连接 WebSocket，发送一条消息，收集 ack / typing / 机器人回复。"""
    events: list[dict] = []
    async with websockets.connect(f"{WS_URL}?conversation_id={conv_id}") as ws:
        # 收到 hello
        hello = json.loads(await ws.recv())
        assert hello["type"] == "hello", hello
        events.append(hello)

        await ws.send(
            json.dumps(
                {
                    "type": "send",
                    "client_id": "test-1",
                    "msg_type": "text",
                    "content": {"text": text},
                }
            )
        )

        deadline = asyncio.get_event_loop().time() + 15
        got_reply = False
        while asyncio.get_event_loop().time() < deadline and not got_reply:
            raw = await asyncio.wait_for(ws.recv(), timeout=8)
            data = json.loads(raw)
            events.append(data)
            if data["type"] == "message" and data["message"]["sender"] == "bot":
                got_reply = True
    return {
        "events": events,
        "reply": next(
            (e["message"] for e in events if e["type"] == "message" and e["message"]["sender"] == "bot"),
            None,
        ),
    }


async def ws_send_payload(conv_id: int, payload: dict) -> list[dict]:
    """连接 WebSocket 发送一条非文本消息（图片/卡片），返回收到的全部事件。"""
    events: list[dict] = []
    async with websockets.connect(f"{WS_URL}?conversation_id={conv_id}") as ws:
        events.append(json.loads(await ws.recv()))  # hello
        await ws.send(json.dumps(payload))
        # 等待 ack
        deadline = asyncio.get_event_loop().time() + 8
        while asyncio.get_event_loop().time() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            events.append(json.loads(raw))
            if events[-1]["type"] == "message_ack":
                break
    return events


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        mark = "[PASS]" if ok else "[FAIL]"
        print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    async with httpx.AsyncClient(base_url=BASE) as client:
        # 0. 预置知识库样例（RAG 回答依赖知识库内容）
        sample = Path(__file__).parent / "samples" / "退货政策.md"
        r = await client.post(
            "/api/knowledge/documents",
            files={"files": ("冒烟测试_退货政策.md", sample.read_bytes(), "text/markdown")},
        )
        smoke_doc = r.json()["results"][0]["doc"]
        check("预置知识库样例", r.status_code == 200 and smoke_doc["status"] == "completed",
              smoke_doc.get("status"))

        # 1. 创建会话（默认标题，验证首条消息自动命名）
        r = await client.post("/api/conversations", json={})
        conv = r.json()
        check("创建会话", r.status_code == 200 and conv["status"] == "bot", f"id={conv['id']}")

        # 2. 空历史
        r = await client.get(f"/api/conversations/{conv['id']}/messages")
        check("初始历史为空", r.json() == [])

        # 3. WebSocket 发送消息：ack + typing + 不带来源引用的机器人回答
        result = await ws_exchange(conv["id"], "我想退货怎么办？")
        events = result["events"]
        ack = next((e for e in events if e["type"] == "message_ack"), None)
        typing = next((e for e in events if e["type"] == "typing" and e["is_typing"]), None)
        check("收到发送回执 ack", ack is not None and ack["status"] == "sent")
        check("收到“正在输入”事件", typing is not None and typing["sender"] == "bot")

        reply = result["reply"]
        check("机器人回答了消息", reply is not None and reply["sender"] == "bot")
        if reply:
            check("回答类型为 answer", reply["type"] == "answer")
            check(
                "回答不携带来源引用",
                not reply["content"].get("citations")
                and "知识库" not in reply["content"]["text"]
                and not re.search(r"\[\d+\]", reply["content"]["text"]),
                reply["content"]["text"][:40],
            )

        # 3.5 兜底与闲聊：无关问题明确告知并引导转人工，闲聊不引用知识库
        result = await ws_exchange(conv["id"], "今天股市行情怎么样")
        fallback_reply = result["reply"]
        if fallback_reply:
            check(
                "无关问题走兜底话术",
                "转人工" in fallback_reply["content"]["text"]
                and not fallback_reply["content"].get("citations"),
            )
        result = await ws_exchange(conv["id"], "你好")
        chat_reply = result["reply"]
        if chat_reply:
            check(
                "闲聊回答不引用知识库",
                "很高兴" in chat_reply["content"]["text"]
                and not chat_reply["content"].get("citations"),
            )

        # 4. 历史持久化（刷新不丢失）
        r = await client.get(f"/api/conversations/{conv['id']}/messages")
        msgs = r.json()
        check("历史已持久化", len(msgs) >= 2, f"{len(msgs)} 条")
        user_msg = next((m for m in msgs if m["sender"] == "user"), None)
        if user_msg:
            check("用户消息已读状态（双勾）", user_msg["status"] == "read", user_msg["status"])

        # 4.5 图片消息与链接卡片消息实时发送
        events = await ws_send_payload(
            conv["id"],
            {
                "type": "send",
                "client_id": "test-img",
                "msg_type": "image",
                "content": {"url": "https://example.com/a.png", "alt": "示例图片"},
            },
        )
        ack_img = next((e for e in events if e["type"] == "message_ack"), None)
        check("图片消息收到回执", ack_img is not None and ack_img["status"] == "sent")

        events = await ws_send_payload(
            conv["id"],
            {
                "type": "send",
                "client_id": "test-card",
                "msg_type": "card",
                "content": {
                    "title": "配送服务说明",
                    "url": "https://example.com/shipping",
                    "description": "全国大部分地区 48 小时内发货",
                },
            },
        )
        ack_card = next((e for e in events if e["type"] == "message_ack"), None)
        check("链接卡片消息收到回执", ack_card is not None and ack_card["status"] == "sent")

        r = await client.get(f"/api/conversations/{conv['id']}/messages")
        types = [m["type"] for m in r.json()]
        check("图片与卡片已持久化", "image" in types and "card" in types, str(types))

        # 5. 会话标题随首条消息更新
        r = await client.get(f"/api/conversations/{conv['id']}")
        check("会话标题随首条消息更新", "退货" in r.json()["title"], r.json()["title"])

        # 6. 转人工（进入排队）
        r = await client.post(f"/api/conversations/{conv['id']}/transfer")
        data = r.json()
        check("转人工进入排队", data["status"] == "queued" and data["system_message"]["sender"] == "system")

        # 6.5 转人工后切回智能客服，机器人恢复应答
        r = await client.post(f"/api/conversations/{conv['id']}/switch-ai")
        data = r.json()
        check(
            "切回智能客服成功",
            data["status"] == "bot" and data["system_message"]["sender"] == "system",
        )
        result = await ws_exchange(conv["id"], "怎么查物流？")
        check("切回后机器人恢复应答", result["reply"] is not None)

        # 7. 满意度评价
        r = await client.post(
            f"/api/conversations/{conv['id']}/feedback",
            json={"rating": 5, "comment": "很好"},
        )
        check("提交满意度评价", r.status_code == 200 and r.json().get("ok"))
        r = await client.post(
            f"/api/conversations/{conv['id']}/feedback", json={"rating": 9}
        )
        check("非法评分被拒绝", r.status_code == 400)

        # 8. 会话列表包含最新消息预览
        r = await client.get("/api/conversations")
        convs = r.json()
        check("会话列表有数据", len(convs) >= 1)

        # 9. 删除会话
        r = await client.post("/api/conversations", json={})
        del_conv = r.json()
        r = await client.delete(f"/api/conversations/{del_conv['id']}")
        check("删除会话接口", r.status_code == 200 and r.json().get("ok"))
        r = await client.get(f"/api/conversations/{del_conv['id']}")
        check("删除后会话不存在", r.status_code == 404)
        r = await client.get("/api/conversations")
        check(
            "删除后不在会话列表",
            all(c["id"] != del_conv["id"] for c in r.json()),
        )
        r = await client.delete("/api/conversations/999999")
        check("删除不存在的会话返回 404", r.status_code == 404)

        # 10. 清理知识库样例
        r = await client.delete(f"/api/knowledge/documents/{smoke_doc['id']}")
        check("清理知识库样例", r.status_code == 200 and r.json().get("ok"))

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{', '.join(failures)}")
        return 1
    print("全部通过 ✔")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main()))
