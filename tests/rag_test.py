"""智能问答（RAG）模块测试：预处理 / 意图识别 / 混合检索 / 重排 / 生成 / 兜底。

运行方式（项目根目录）：
    .venv\\Scripts\\python.exe tests\\rag_test.py

无需启动服务；会自动上传测试样例文档并清理。
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path

# 离线测试不调用真实大模型（避免产生 API 费用）
os.environ["RAG_USE_LLM"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import bot, db, knowledge, rag

SAMPLES = Path(__file__).parent / "samples"
RUN_TAG = datetime.now().strftime("%m%d%H%M%S")


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        mark = "[PASS]" if ok else "[FAIL]"
        print(f"  {mark} {name}" + (f"  -> {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    # 0. 问题预处理
    cleaned = rag.preprocess_question("请问我想问一下，退货怎么操作呢？")
    check(
        "问题预处理去除废话与尾词",
        "请问" not in cleaned and "我想问一下" not in cleaned and "呢" not in cleaned and "退货怎么操作" in cleaned,
        cleaned,
    )
    cleaned = rag.preprocess_question("我想退宽，钱什么时候到账啊")
    check("错别字容错（退宽→退款）", "退宽" not in cleaned and "退款" in cleaned, cleaned)

    # 1. 意图识别
    check("意图识别：转人工", rag.detect_intent("我要转人工客服") == "transfer")
    check("意图识别：闲聊", rag.detect_intent("你好") == "chitchat")
    check("意图识别：知识库", rag.detect_intent("退货政策是什么") == "kb")

    # 2. BM25 分词与打分
    bm25 = rag.BM25(["退货政策：7天内可无理由退货", "配送说明：48小时内发货"])
    scores = bm25.scores("退货")
    check("BM25 关键词打分", scores[0] > scores[1], f"{scores}")

    # 3. 准备知识库样例
    created: list[int] = []
    saved_enabled: dict[int, bool] = {}
    try:
        # 隔离测试环境：临时停用既有文档（不删除，结束后恢复）
        for doc in db.list_knowledge_docs():
            saved_enabled[doc["id"]] = doc["enabled"]
            if doc["enabled"]:
                knowledge.set_doc_enabled(doc["id"], False)

        names = [
            (f"退货政策_{RUN_TAG}.md", "退货政策.md"),
            (f"服务说明_{RUN_TAG}.txt", "服务说明.txt"),
            (f"帮助中心_{RUN_TAG}.html", "帮助中心.html"),
        ]
        for upload_name, sample_name in names:
            raw = (SAMPLES / sample_name).read_bytes()
            doc = knowledge.ingest_document(upload_name, raw, uploader="test")
            created.append(doc["id"])
            check(f"上传样例 {sample_name}", doc["status"] == "completed" and doc["chunk_count"] > 0,
                  f"chunks={doc['chunk_count']}")

        # 4. 知识库问题：RAG 回答不带来源引用
        result = bot.answer("我想退货，钱多久能退回来？")
        check(
            "知识库问题回答不带来源引用",
            result["intent"] == "kb"
            and not result["citations"]
            and "退货" in result["text"]
            and "知识库" not in result["text"]
            and not re.search(r"\[\d+\]", result["text"]),
            f"intent={result.get('intent')}, citations={len(result.get('citations', []))}",
        )

        # 4.1 上下文：回答参考之前的对话记录
        prompt = rag.build_prompt(
            "多久能到账？",
            rag.retrieve("多久能到账？", top_k=rag.TOP_K_RECALL)[:2],
            [
                {"sender": "user", "content": {"text": "我想退货"}},
                {"sender": "bot", "content": {"text": "好的，请提供订单号"}},
                {"sender": "user", "content": {"text": "订单号是 12345"}},
            ],
        )
        check(
            "Prompt 携带历史对话上下文",
            "我想退货" in prompt
            and "订单号是 12345" in prompt
            and "用户问题：多久能到账？" in prompt,
        )
        result = bot.answer(
            "多久能到账？",
            [{"sender": "user", "content": {"text": "我想退货，钱什么时候能退？"}}],
        )
        check(
            "带上下文回答正常且不出现知识库字眼",
            result["intent"] == "kb"
            and "知识库" not in result["text"]
            and not re.search(r"\[\d+\]", result["text"]),
            result["text"][:40],
        )

        # 5. 混合检索返回 Top-5（重排）
        ranked = rag.rerank(
            "我想退货，钱多久能退回来？",
            rag.retrieve("我想退货，钱多久能退回来？", top_k=rag.TOP_K_RECALL),
            top_k=rag.TOP_K_RANK,
        )
        check("重排后切片数为 Top-5", 0 < len(ranked) <= rag.TOP_K_RANK, f"{len(ranked)} 条")

        # 6. 停用后该文档切片不再参与检索
        knowledge.set_doc_enabled(created[0], False)
        retrieved = rag.retrieve("退货政策是什么？", top_k=rag.TOP_K_RECALL)
        excluded = all(c["doc_id"] != created[0] for c in retrieved)
        check(
            "停用文档后不再参与检索",
            excluded,
            f"retrieved={len(retrieved)}",
        )
        knowledge.set_doc_enabled(created[0], True)

        # 7. 兜底：无关问题
        result = bot.answer("今天股市行情怎么样？")
        check(
            "无关问题走兜底话术",
            result.get("fallback") is True
            and not result["citations"]
            and "没有找到" in result["text"],
            result["text"][:40],
        )

        # 8. 闲聊 / 转人工
        result = bot.answer("你好呀")
        check("闲聊回答", result["intent"] == "chitchat" and "很高兴" in result["text"], result["intent"])
        result = bot.answer("我要转人工客服")
        check("转人工意图回答", result["intent"] == "transfer" and "转人工客服" in result["text"], result["intent"])

        # 9. 空问题
        result = bot.answer("")
        check("空问题友好提示", result["intent"] == "unknown" and result["text"], result["intent"])

    finally:
        for doc_id in created:
            knowledge.delete_document(doc_id)
        for doc_id, enabled in saved_enabled.items():
            if enabled:
                knowledge.set_doc_enabled(doc_id, True)

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{', '.join(failures)}")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
