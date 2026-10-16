"""人工客服模块冒烟测试：排队 → 接管 → 实时收发 → 快捷回复 → 标签备注。

运行方式（项目根目录）：
    .venv\\Scripts\\python.exe tests\\agent_console_test.py

前置条件：服务已启动（默认 127.0.0.1:8000，可用 CS_PORT 指定）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx
import websockets

PORT = os.environ.get("CS_PORT", "8000")
BASE = f"http://127.0.0.1:{PORT}"
WS_URL = f"ws://127.0.0.1:{PORT}/ws/chat"


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        mark = "[PASS]" if ok else "[FAIL]"
        print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    async def collect(ws, want, timeout=8.0):
        """持续接收直到出现满足 want 的事件，返回 (命中事件, 全部事件)。"""
        got: list[dict] = []
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=6)
            except asyncio.TimeoutError:
                continue
            evt = json.loads(raw)
            got.append(evt)
            if want(evt):
                return evt, got
        return None, got

    async with httpx.AsyncClient(base_url=BASE) as client:
        # 0. 工作台页面可访问
        r = await client.get("/agent")
        check("客服工作台页面可访问", r.status_code == 200 and "人工客服工作台" in r.text)

        # 1. 创建会话并转人工（进入排队）
        r = await client.post("/api/conversations", json={"title": "人工客服测试"})
        conv = r.json()
        check("创建会话", conv["status"] == "bot", f"id={conv['id']}")

        r = await client.post(f"/api/conversations/{conv['id']}/transfer")
        data = r.json()
        check("转人工后进入排队", data["status"] == "queued", data["status"])
        check("排队提示语已生成", "队列" in data["system_message"]["content"]["text"])

        # 2. 客服工作台会话列表能看到排队会话
        r = await client.get(
            "/api/agent/conversations", params={"agent_name": "客服小王"}
        )
        convs = r.json()
        queued = next((c for c in convs if c["id"] == conv["id"]), None)
        check(
            "排队会话出现在工作台列表",
            queued is not None and queued["status"] == "queued",
        )

        # 3. 客服接管会话
        r = await client.post(
            f"/api/agent/conversations/{conv['id']}/assign",
            json={"agent_name": "客服小王"},
        )
        data = r.json()
        check(
            "客服接管成功",
            data["status"] == "human"
            and data["conversation"]["agent_name"] == "客服小王",
        )

        # 4. 重复接管被拒绝
        r = await client.post(
            f"/api/agent/conversations/{conv['id']}/assign",
            json={"agent_name": "客服小李"},
        )
        check("重复接管被拒绝", r.status_code == 409)

        # 5. 其他客服列表看不到已接管的会话
        r = await client.get(
            "/api/agent/conversations", params={"agent_name": "客服小李"}
        )
        check(
            "其他客服看不到已接管会话",
            all(c["id"] != conv["id"] for c in r.json()),
        )

        # 6. 快捷回复 CRUD
        r = await client.post(
            "/api/agent/quick-replies",
            json={"title": "发货时间", "content": "亲，您的订单预计 48 小时内发出哦～"},
        )
        qr = r.json()
        check("新增快捷回复", r.status_code == 200 and qr["id"] > 0)
        r = await client.get("/api/agent/quick-replies")
        check("快捷回复列表", any(q["id"] == qr["id"] for q in r.json()))
        r = await client.put(
            f"/api/agent/quick-replies/{qr['id']}",
            json={"title": "发货时间", "content": "已帮您催促仓库，预计 24 小时内发货。"},
        )
        check(
            "编辑快捷回复",
            r.status_code == 200
            and "24 小时内" in r.json()["quick_reply"]["content"],
        )
        r = await client.post(
            "/api/agent/quick-replies", json={"title": "", "content": ""}
        )
        check("空快捷回复被拒绝", r.status_code == 400)

        # 7. 标签与备注
        r = await client.patch(
            f"/api/agent/conversations/{conv['id']}/meta",
            json={"tag": "物流咨询", "note": "用户语气着急，需优先处理"},
        )
        data = r.json()
        check(
            "标签备注已保存",
            data["conversation"]["tag"] == "物流咨询"
            and data["conversation"]["note"] == "用户语气着急，需优先处理",
        )

        # 8. WebSocket：用户发消息，客服实时收到并可回复
        user_msg = {
            "type": "send",
            "client_id": "u1",
            "msg_type": "text",
            "content": {"text": "我的快递什么时候到？"},
        }
        agent_msg = {
            "type": "send",
            "client_id": "a1",
            "msg_type": "text",
            "content": {"text": "您好，我帮您查一下，请稍等。"},
        }

        async with websockets.connect(
            f"{WS_URL}?conversation_id={conv['id']}&role=user"
        ) as user_ws, websockets.connect(
            f"{WS_URL}?conversation_id={conv['id']}&role=agent"
            "&agent_name=%E5%AE%A2%E6%9C%8D%E5%B0%8F%E7%8E%8B"
        ) as agent_ws:
            user_hello = json.loads(await user_ws.recv())
            agent_hello = json.loads(await agent_ws.recv())
            check(
                "用户连接 hello",
                user_hello["type"] == "hello" and user_hello["role"] == "user",
            )
            check(
                "客服连接 hello",
                agent_hello["type"] == "hello"
                and agent_hello["role"] == "agent"
                and agent_hello["agent_name"] == "客服小王",
            )

            # 用户发消息，客服侧应实时收到该消息广播
            await user_ws.send(json.dumps(user_msg))
            agent_evt, _ = await collect(
                agent_ws,
                lambda e: e["type"] == "message"
                and e["message"]["sender"] == "user"
                and e["message"]["content"]["text"] == "我的快递什么时候到？",
            )
            check("客服实时收到用户消息", agent_evt is not None)

            # 客服回复，用户侧应实时收到该消息广播
            await agent_ws.send(json.dumps(agent_msg))
            user_evt, _ = await collect(
                user_ws,
                lambda e: e["type"] == "message"
                and e["message"]["sender"] == "human"
                and e["message"]["content"]["text"].startswith("您好"),
            )
            check("用户实时收到客服回复", user_evt is not None)
            if user_evt:
                check(
                    "客服回复不带知识库引用",
                    "知识库" not in user_evt["message"]["content"]["text"],
                )

            # 客服输入中提示（服务端广播发送者角色，用户端统一渲染为对方正在输入）
            await agent_ws.send(json.dumps({"type": "typing", "sender": "human"}))
            typing_evt, _ = await collect(
                user_ws,
                lambda e: e["type"] == "typing" and e["is_typing"],
            )
            check("用户收到客服输入中提示", typing_evt is not None)

            # 客服打开会话时把用户消息标记为已读（已读状态广播给会话其他端）
            await agent_ws.send(json.dumps({"type": "read"}))
            read_evt, _ = await collect(
                user_ws,
                lambda e: e["type"] == "status" and e["status"] == "read",
            )
            check("用户消息被标记为已读", read_evt is not None)

        # 9. 消息持久化（human 发送者）
        r = await client.get(f"/api/conversations/{conv['id']}/messages")
        msgs = r.json()
        check(
            "客服回复已持久化",
            any(
                m["sender"] == "human"
                and m["content"]["text"].startswith("您好")
                for m in msgs
            ),
        )

        # 10. 放回排队
        r = await client.post(
            f"/api/agent/conversations/{conv['id']}/return",
            json={"agent_name": "客服小王"},
        )
        data = r.json()
        check("放回排队成功", data["status"] == "queued")
        r = await client.post(
            f"/api/agent/conversations/{conv['id']}/return",
            json={"agent_name": "客服小李"},
        )
        check("非接管客服不能放回排队", r.status_code == 409)

        # 11. 用户切回智能客服
        r = await client.post(f"/api/conversations/{conv['id']}/switch-ai")
        check("切回智能客服", r.json()["status"] == "bot")
        r = await client.get(
            "/api/agent/conversations", params={"agent_name": "客服小王"}
        )
        check("切回后从工作台消失", all(c["id"] != conv["id"] for c in r.json()))

        # 11.5 置顶功能
        r = await client.post("/api/conversations", json={"title": "置顶测试A"})
        pin_a = r.json()
        await client.post(f"/api/conversations/{pin_a['id']}/transfer")
        r = await client.post("/api/conversations", json={"title": "置顶测试B"})
        pin_b = r.json()
        await client.post(f"/api/conversations/{pin_b['id']}/transfer")

        r = await client.patch(
            f"/api/agent/conversations/{pin_a['id']}/pinned", json={"pinned": True}
        )
        check("置顶会话成功", r.status_code == 200 and r.json()["conversation"]["pinned"] is True)
        r = await client.get(
            "/api/agent/conversations", params={"agent_name": "客服小王"}
        )
        agent_list = r.json()
        check("置顶会话排在列表最前", agent_list[0]["id"] == pin_a["id"])
        r = await client.patch(
            f"/api/agent/conversations/{pin_a['id']}/pinned", json={"pinned": False}
        )
        check("取消置顶成功", r.json()["conversation"]["pinned"] is False)
        r = await client.patch(
            "/api/agent/conversations/999999/pinned", json={"pinned": True}
        )
        check("置顶不存在的会话返回 404", r.status_code == 404)

        # 12. 删除快捷回复
        r = await client.delete(f"/api/agent/quick-replies/{qr['id']}")
        check("删除快捷回复", r.status_code == 200)
        r = await client.get("/api/agent/quick-replies")
        check("快捷回复已删除", all(q["id"] != qr["id"] for q in r.json()))

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
