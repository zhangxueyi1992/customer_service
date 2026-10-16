"""Chroma 最小验证流程：文本 -> 向量化 -> 入库 -> 检索。

运行方式（在项目根目录）：
    .venv\\Scripts\\python.exe demo_chroma.py
首次运行会自动下载中文向量模型（约 95MB，走国内镜像）。
"""

import os
import sys

# Windows 下强制 UTF-8 输出，避免终端中文乱码
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import chromadb
from fastembed import TextEmbedding

# 1. 准备测试知识（模拟客服知识库条目）
KB_DOCS = [
    "退货政策：自签收之日起 7 天内，商品保持完好且不影响二次销售，可申请无理由退货。",
    "退款到账时间：退货审核通过后，退款将在 1-3 个工作日内原路返回您的支付账户。",
    "配送说明：全国大部分地区 48 小时内发货，偏远地区（新疆、西藏等）可能需要 3-5 天。",
    "运费规则：订单金额满 99 元免运费，不满 99 元收取 8 元基础运费。",
    "保修政策：电子产品整机保修一年，充电器等配件保修三个月。",
    "客服工作时间：工作日 9:00-18:00，节假日在线客服服务时间会另行公告。",
]

QUESTIONS = [
    "买了东西想退怎么办？",
    "多久能送到？",
    "手机坏了在保修期内吗？",
]


def main() -> None:
    # 2. 加载中文向量模型（首次运行会下载模型）
    print("加载中文向量模型...")
    embed_model = TextEmbedding("BAAI/bge-small-zh-v1.5")

    # 3. 将知识文本向量化
    print(f"向量化 {len(KB_DOCS)} 条知识...")
    embeddings = [emb.tolist() for emb in embed_model.embed(KB_DOCS)]

    # 4. 存入 Chroma（持久化到本地 chroma_data 目录）
    client = chromadb.PersistentClient(path="chroma_data")
    collection = client.get_or_create_collection(name="kb_test")
    collection.upsert(
        ids=[f"doc_{i}" for i in range(len(KB_DOCS))],
        documents=KB_DOCS,
        embeddings=embeddings,
    )
    print(f"知识库现有 {collection.count()} 条记录\n")

    # 5. 用问题检索，验证召回效果
    for question in QUESTIONS:
        q_vec = list(embed_model.embed([question]))[0].tolist()
        results = collection.query(query_embeddings=[q_vec], n_results=2)
        print(f"问题：{question}")
        for doc, dist in zip(results["documents"][0], results["distances"][0]):
            print(f"  [距离 {dist:.4f}] {doc}")
        print()


if __name__ == "__main__":
    main()
