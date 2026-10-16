"""管理与统计模块测试：登录、指标、对话记录查询/导出、账号管理。

运行方式（项目根目录）：
    .venv\\Scripts\\python.exe tests\\admin_test.py

前置条件：服务已启动（默认 127.0.0.1:8000，可用 CS_PORT 指定）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

PORT = os.environ.get("CS_PORT", "8000")
BASE = f"http://127.0.0.1:{PORT}"


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        mark = "[PASS]" if ok else "[FAIL]"
        print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    async with httpx.AsyncClient(base_url=BASE) as c:
        # 0. 管理后台页面可访问
        r = await c.get("/admin")
        check("管理后台页面可访问", r.status_code == 200 and "管理后台" in r.text)

        # 1. 登录：正确/错误/非管理员
        r = await c.post("/api/admin/login", json={"name": "管理员", "password": "admin123"})
        check("管理员登录成功", r.status_code == 200 and r.json()["user"]["role"] == "admin")
        r = await c.post("/api/admin/login", json={"name": "管理员", "password": "wrong"})
        check("错误密码被拒绝", r.status_code == 401)
        r = await c.post("/api/admin/login", json={"name": "客服1", "password": ""})
        check("非管理员登录被拒绝", r.status_code == 403)

        # 2. 准备数据：创建会话、问答、转人工、评价
        r = await c.post("/api/conversations", json={"title": "管理统计测试"})
        conv = r.json()
        cid = conv["id"]
        await c.post(
            "/api/knowledge/documents",
            files={"files": ("测试_退货政策.md", "# 退货政策\n\n支持 7 天无理由退货。".encode(), "text/markdown")},
        )
        # 通过消息直接写入数据（模拟问答与转人工过程）
        async with httpx.AsyncClient(base_url=BASE) as c2:
            await c2.post(
                f"/api/conversations/{cid}/transfer"
            )
            await c2.post(
                f"/api/conversations/{cid}/feedback",
                json={"rating": 5, "comment": "服务很好"},
            )

        # 3. 统计指标
        r = await c.get("/api/admin/stats")
        stats = r.json()
        check("统计接口可用", r.status_code == 200)
        r = await c.get(
            "/api/admin/stats",
            params={"date_from": "2026-08-26", "date_to": "2026-08-30"},
        )
        check("统计接口带日期过滤可用", r.status_code == 200)
        r = await c.get("/api/admin/stats", params={"date_from": "2026-08-01"})
        check("统计接口仅开始日期可用", r.status_code == 200)
        r = await c.get("/api/admin/stats", params={"date_to": "2026-12-31"})
        check("统计接口仅结束日期可用", r.status_code == 200)
        check("总会话数统计", stats["total_conversations"] >= 1, str(stats["total_conversations"]))
        check("转人工率统计", stats["transfer_count"] >= 1, str(stats["transfer_count"]))
        check("满意度统计", stats["feedback_count"] >= 1 and stats["avg_rating"] > 0)
        check("命中率字段存在", "kb_hit_rate" in stats and "avg_response_secs" in stats)

        # 4. 对话记录查询
        r = await c.get("/api/admin/conversations", params={"keyword": "管理统计测试"})
        convs = r.json()
        check("对话记录按关键词查询", any(x["id"] == cid for x in convs))
        r = await c.get("/api/admin/conversations", params={"status": "queued"})
        check("对话记录按状态过滤", all(x["status"] == "queued" for x in r.json()))

        # 5. 对话详情
        r = await c.get(f"/api/admin/conversations/{cid}")
        detail = r.json()
        check("对话详情含消息", detail["id"] == cid and isinstance(detail["messages"], list))
        r = await c.get("/api/admin/conversations/999999")
        check("详情不存在的会话返回 404", r.status_code == 404)

        # 6. 导出 CSV / JSON
        r = await c.get("/api/admin/export", params={"fmt": "csv"})
        check("导出 CSV", r.status_code == 200 and "会话ID" in r.text and "attachment" in r.headers.get("content-disposition", ""))
        r = await c.get("/api/admin/export", params={"fmt": "json"})
        data = r.json()
        check("导出 JSON", r.status_code == 200 and isinstance(data, list))

        # 7. 账号管理
        r = await c.get("/api/admin/users")
        users = r.json()
        check("账号列表含管理员与客服", any(u["name"] == "管理员" for u in users) and any(u["name"] == "客服1" for u in users))
        r = await c.post("/api/admin/users", json={"name": "测试客服", "role": "agent", "password": "pw123"})
        new_user = r.json()
        check("新增账号", r.status_code == 200 and new_user["id"] > 0)
        r = await c.post("/api/admin/users", json={"name": "测试客服", "role": "agent"})
        check("重复账号被拒绝", r.status_code == 400)
        r = await c.put(f"/api/admin/users/{new_user['id']}", json={"role": "user", "password": "newpw"})
        check("编辑账号角色与密码", r.status_code == 200 and r.json()["user"]["role"] == "user")
        r = await c.post("/api/admin/login", json={"name": "测试客服", "password": "newpw"})
        check("新密码可登录（非管理员仍被拒）", r.status_code == 403)
        r = await c.put(f"/api/admin/users/{new_user['id']}", json={"status": "disabled"})
        check("停用账号", r.status_code == 200 and r.json()["user"]["status"] == "disabled")
        r = await c.delete(f"/api/admin/users/{new_user['id']}")
        check("删除账号", r.status_code == 200)
        r = await c.delete("/api/admin/users/999999")
        check("删除不存在的账号返回 404", r.status_code == 404)

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
