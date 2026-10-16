"""智能问答（RAG）管线：问题预处理 → 意图识别 → 混合检索 → 重排 → 生成 → 兜底。

检索链路：
    向量相似度检索（Chroma + bge-small-zh）为主，
    叠加本地 BM25 关键词检索做混合召回 Top-20，
    再按“向量相似度 + BM25 + 关键词重合 + 标题命中”融合重排取 Top-5。

生成链路：
    把召回片段组装进 Prompt 交给大模型（默认使用 DeepSeek，自动复用当前 Codex
    的 API Key 与模型配置），要求只依据片段自然地总结回答、不标注来源；
    未配置密钥或接口异常时回退到本地摘录式生成，同样保证回答全部来自知识片段。
    检索不到相关内容时走兜底话术。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

from . import db, knowledge

# ---------- 可调参数（可用环境变量覆盖） ----------
TOP_K_RECALL = 20        # 混合召回候选数
TOP_K_RANK = 5           # 重排后保留数
VECTOR_WEIGHT = 0.55     # 重排：向量相似度权重
BM25_WEIGHT = 0.30       # 重排：BM25 权重
OVERLAP_WEIGHT = 0.15    # 重排：关键词重合权重
BM25_REF = float(os.environ.get("RAG_BM25_REF", "2.0"))  # BM25 归一化参考分
MIN_VECTOR_SIM = float(os.environ.get("RAG_MIN_VECTOR_SIM", "0.45"))
MIN_RERANK_SCORE = float(os.environ.get("RAG_MIN_RERANK_SCORE", "0.38"))

# ---------- 大模型（默认 DeepSeek，复用 Codex 凭证；失败时回退本地生成） ----------
LLM_ENABLED = os.environ.get("RAG_USE_LLM", "1") == "1"
LLM_TIMEOUT = float(os.environ.get("RAG_LLM_TIMEOUT", "30"))
LLM_MAX_TOKENS = int(os.environ.get("RAG_LLM_MAX_TOKENS", "800"))
MAX_HISTORY_MESSAGES = 12  # 回答时参考的最近对话条数（每轮问答约两条）

_logger = logging.getLogger("rag")


def _load_codex_auth_key() -> str:
    """读取当前 Codex 使用的 API Key（~/.codex/auth.json），不落盘、不进项目。"""
    try:
        data = json.loads(
            (Path.home() / ".codex" / "auth.json").read_text(encoding="utf-8")
        )
        return str(data.get("OPENAI_API_KEY", "")).strip()
    except Exception:  # noqa: BLE001
        return ""


def _load_codex_provider_config() -> tuple[str, str]:
    """读取 Codex 配置中的大模型 base_url 与模型名（model_providers.custom）。"""
    base_url = ""
    model = ""
    try:
        text = (Path.home() / ".codex" / "config.toml").read_text(encoding="utf-8")
        m = re.search(r'^\s*base_url\s*=\s*"([^"]+)"', text, re.M)
        if m:
            base_url = m.group(1).strip().rstrip("/")
        m = re.search(r'^\s*model\s*=\s*"([^"]+)"', text, re.M)
        if m:
            model = m.group(1).strip()
    except Exception:  # noqa: BLE001
        pass
    return base_url, model


_CODEX_BASE_URL, _CODEX_MODEL = _load_codex_provider_config()
LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip() or _load_codex_auth_key()
LLM_BASE_URL = (
    os.environ.get("OPENAI_BASE_URL", "").strip().rstrip("/")
    or _CODEX_BASE_URL
    or "https://api.deepseek.com"
)
LLM_MODEL = (
    os.environ.get("OPENAI_MODEL", "").strip() or _CODEX_MODEL or "deepseek-chat"
)


# ---------- 问题预处理 ----------

_FILLER_RE = re.compile(
    r"^(?:请问|你好|您好|哈喽|嗨|hello|hi|在吗|我想问一下|我想问|想问一下|想问下|"
    r"想问|问一下|问下|麻烦问一下|麻烦问下|帮我查一下|帮我看看|帮我查查|"
    r"咨询一下|我想咨询|咨询下|请问一下|求问)\s*[，,。.!！?？:：]*"
)
_TRAILING_PARTICLE_RE = re.compile(r"[吗呢啊吧呀哈哦]+\s*[？?。.!！]*$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")

# 常见错别字 / 异体写法 / 同义说法
_TYPO_MAP = {
    "退宽": "退款",
    "退貨": "退货",
    "發货": "发货",
    "郵费": "邮费",
    "快遞": "快递",
    "物留": "物流",
    "维休": "维修",
    "保休": "保修",
    "質保": "质保",
    "帳号": "账号",
    "帐号": "账号",
    "登錄": "登录",
    "密碼": "密码",
    "付宽": "付款",
    "退货退款": "退款",
    "快递多久": "多久到货",
}


def _fix_typos(text: str) -> str:
    for wrong, right in _TYPO_MAP.items():
        text = text.replace(wrong, right)
    return text


def preprocess_question(question: str) -> str:
    """去除问候废话、标点干扰与口语尾词，统一全半角，纠正常见错别字。"""
    q = (question or "").strip()
    q = unicodedata.normalize("NFKC", q)          # 全角字母数字转半角
    q = q.lower()
    # 连续去除开头的问候/询问废话（如“请问我想问一下”）
    for _ in range(5):
        new_q = _FILLER_RE.sub("", q).strip()
        if new_q == q:
            break
        q = new_q
    q = _TRAILING_PARTICLE_RE.sub("", q).strip()
    q = re.sub(r"[!?？?。.!！]{2,}", " ", q)       # 连续标点收敛
    q = re.sub(r"\s+", " ", q).strip()
    q = _fix_typos(q)
    return q


# ---------- 意图识别 ----------

_TRANSFER_PATTERNS = [
    "转人工",
    "转接人工",
    "找人工",
    "要人工",
    "人工服务",
    "真人客服",
    "叫客服",
    "人工客服来",
]
_GREETING_RE = re.compile(
    r"^(你好|您好|早上好|下午好|晚上好|哈喽|嗨|hello|hi|hey|在吗)"
    r"[呀啊哦哈的]*[!！.。?？~～]*$",
    re.I,
)
_THANKS_RE = re.compile(r"^(谢谢|感谢|多谢|辛苦|thank)", re.I)
_FAREWELL_RE = re.compile(r"^(再见|拜拜|下次聊|bye|88)", re.I)
_WHO_RE = re.compile(r"(你是谁|你叫什么|自我介绍|你是干嘛|你是什么)", re.I)

_CHITCHAT_REPLIES = {
    "greeting": (
        "您好，我是智能客服小助手，很高兴为您服务！"
        "您可以问我关于订单、退货、配送、保修等方面的问题。"
    ),
    "thanks": "不客气，能帮到您就好！如果还有其他问题，随时告诉我。",
    "farewell": "再见，祝您生活愉快！如有需要，随时可以回来找我。",
    "who": (
        "我是智能客服小助手，可以解答商品、订单、售后等问题。"
        "您可以直接把问题发给我。"
    ),
}


def detect_intent(question: str) -> str:
    """识别意图：kb（问知识库）/ transfer（转人工）/ chitchat（闲聊）。"""
    q = (question or "").strip()
    if any(p in q for p in _TRANSFER_PATTERNS):
        return "transfer"
    if _WHO_RE.search(q) or _GREETING_RE.match(q) or _THANKS_RE.match(q) or _FAREWELL_RE.match(q):
        return "chitchat"
    return "kb"


def _is_chitchat(question: str) -> bool:
    q = (question or "").strip().lower()
    return bool(
        _WHO_RE.search(q)
        or _GREETING_RE.match(q)
        or _THANKS_RE.match(q)
        or _FAREWELL_RE.match(q)
    )


def _chitchat_reply(question: str) -> str:
    if _WHO_RE.search(question):
        return _CHITCHAT_REPLIES["who"]
    if _THANKS_RE.match(question):
        return _CHITCHAT_REPLIES["thanks"]
    if _FAREWELL_RE.match(question):
        return _CHITCHAT_REPLIES["farewell"]
    return _CHITCHAT_REPLIES["greeting"]


# ---------- BM25 关键词检索 ----------


class BM25:
    """轻量 BM25：中文按单字 + 二元组切分，英文数字按词切分。"""

    k1 = 1.5
    b = 0.75

    def __init__(self, docs: list[str]) -> None:
        self.doc_tokens = [self.tokenize(d) for d in docs]
        self.doc_len = [len(t) for t in self.doc_tokens]
        self.N = len(docs)
        self.avgdl = sum(self.doc_len) / self.N if self.N else 0.0
        df: dict[str, int] = {}
        for toks in self.doc_tokens:
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        self.df = df

    @staticmethod
    def tokenize(text: str) -> list[str]:
        text = (text or "").lower()
        tokens: list[str] = re.findall(r"[a-z0-9]+", text)
        for seg in re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]+", text):
            for ch in seg:
                tokens.append(ch)
            for i in range(len(seg) - 1):
                tokens.append(seg[i : i + 2])
        return tokens

    def scores(self, query: str) -> list[float]:
        q_tokens = set(self.tokenize(query))
        results: list[float] = []
        for dt, dl in zip(self.doc_tokens, self.doc_len):
            score = 0.0
            for t in q_tokens:
                tf = dt.count(t)
                if not tf or t not in self.df:
                    continue
                idf = math.log((self.N - self.df[t] + 0.5) / (self.df[t] + 0.5) + 1)
                denom = tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                score += idf * (tf * (self.k1 + 1)) / denom
            results.append(score)
        return results


# ---------- 混合检索 ----------


def retrieve(question: str, top_k: int = TOP_K_RECALL) -> list[dict]:
    """向量检索 + BM25 混合召回，返回去重后的候选切片。"""
    rows = db.get_enabled_chunks()
    candidates: dict[int, dict[str, Any]] = {}
    for row in rows:
        candidates[row["id"]] = {
            "chunk_id": row["id"],
            "doc_id": row["doc_id"],
            "doc_name": row["doc_name"],
            "title": row["title"] or "",
            "page": row["page"],
            "snippet": row["content"],
            "vec_sim": 0.0,
            "bm25": 0.0,
        }

    # 1) 向量相似度检索（只取启用中的切片；未同步的孤儿向量直接忽略，
    #    以数据库的启用状态为准）
    for hit in knowledge.search(question, top_k=top_k):
        cid = hit["chunk_id"]
        if cid in candidates:
            candidates[cid]["vec_sim"] = hit["similarity"]

    # 2) BM25 关键词检索
    if candidates:
        bm25 = BM25([c["snippet"] for c in candidates.values()])
        for cand, score in zip(candidates.values(), bm25.scores(question)):
            cand["bm25"] = score

    ranked = rerank(question, list(candidates.values()), top_k=top_k)
    return ranked


# ---------- 重排 ----------


def rerank(question: str, candidates: list[dict], top_k: int = TOP_K_RANK) -> list[dict]:
    """融合向量相似度、BM25、关键词重合与标题命中进行重排。"""
    if not candidates:
        return []
    q_tokens = set(BM25.tokenize(question))

    for cand in candidates:
        # BM25 用固定参考分归一化，避免“矮子里拔将军”
        bm25_norm = min(cand["bm25"] / BM25_REF, 1.0) if BM25_REF > 0 else 0.0
        c_tokens = set(BM25.tokenize(cand["snippet"]))
        overlap = len(q_tokens & c_tokens) / max(1, len(q_tokens))
        title_tokens = set(BM25.tokenize(cand["title"]))
        title_bonus = 0.05 if (title_tokens and (q_tokens & title_tokens)) else 0.0
        cand["score"] = (
            VECTOR_WEIGHT * cand["vec_sim"]
            + BM25_WEIGHT * bm25_norm
            + OVERLAP_WEIGHT * overlap
            + title_bonus
        )
    return sorted(candidates, key=lambda c: c["score"], reverse=True)[:top_k]


# ---------- 生成 ----------

SYSTEM_PROMPT = (
    "你是一名专业的智能客服。请根据用户提供的资料和之前的对话记录，"
    "连贯地回答用户的最新问题；"
    "如果资料与问题无关或不足以回答，请如实说明暂时没有找到相关内容，"
    "并建议用户转人工客服处理。回答要求简洁、准确、口语化，"
    "直接输出自然的总结表述：不要提及资料来源、文件名或编号，"
    "不要使用 [1][2] 之类的标注，不要出现“知识库”等内部术语，"
    "禁止编造资料之外的信息。"
)


def build_history_text(history: list[dict]) -> str:
    """把历史消息转成“用户/客服：内容”的对话文本。"""
    lines = []
    for m in history or []:
        content = m.get("content") or {}
        if not isinstance(content, dict):
            continue
        text = str(content.get("text") or "").strip()
        if not text:
            continue
        role = "用户" if m.get("sender") == "user" else "客服"
        lines.append(f"{role}：{text}")
    return "\n".join(lines)


def build_prompt(
    question: str, chunks: list[dict], history: list[dict] | None = None
) -> str:
    lines = ["请根据以下资料回答用户问题：", ""]
    for i, chunk in enumerate(chunks, start=1):
        lines.append(f"【资料{i}】")
        lines.append(chunk["snippet"])
        lines.append("")
    hist_text = build_history_text(history)
    if hist_text:
        lines.append("以下是用户与客服之前的对话记录（供参考）：")
        lines.append(hist_text)
        lines.append("")
    lines.append(f"用户问题：{question}")
    return "\n".join(lines)


_KB_TERM_RE = re.compile(r"知识库|知识库来源|来源知识库|根据知识库|基于知识库")
_CITATION_MARK_RE = re.compile(r"\s*[\[【(（]\s*\d+\s*[\]】)）]\s*")
_SOURCE_LINE_RE = re.compile(r"^\s*(?:来源|出处|参考)[：:]")


def filter_answer_text(text: str) -> str:
    """过滤回答中的来源标注与“知识库”相关表述，只保留自然的总结文字。"""
    if not text:
        return text
    text = _CITATION_MARK_RE.sub("", text)
    kept = [
        line
        for line in text.splitlines()
        if not _KB_TERM_RE.search(line) and not _SOURCE_LINE_RE.match(line)
    ]
    text = "\n".join(kept)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _generate_local(question: str, chunks: list[dict]) -> dict:
    """无大模型时的本地摘录式生成：直接摘取命中的资料内容。"""
    top = chunks[0]
    text = f"为您找到以下相关信息：\n\n{top['snippet']}\n\n如果还有其他问题，随时可以继续问我。"
    return {
        "text": filter_answer_text(text),
        "citations": [],
    }


class _UnsupportedWire(Exception):
    """接口返回“不支持当前请求格式”类错误，用于自动切换到另一种格式。"""


def _call_llm_chat(
    client, headers: dict, prompt: str
) -> str:
    """标准 OpenAI 兼容格式：POST /chat/completions。"""
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": LLM_MAX_TOKENS,
    }
    resp = client.post(f"{LLM_BASE_URL}/chat/completions", json=payload, headers=headers)
    if resp.status_code in (400, 404, 405, 501):
        raise _UnsupportedWire(resp.status_code)
    resp.raise_for_status()
    message = resp.json()["choices"][0]["message"]
    text = (message.get("content") or "").strip()
    if not text:
        # DeepSeek 推理模型可能把正文放在 reasoning_content
        text = (message.get("reasoning_content") or "").strip()
    return text


def _call_llm_responses(
    client, headers: dict, prompt: str
) -> str:
    """Responses API 格式：POST /responses。"""
    payload = {
        "model": LLM_MODEL,
        "instructions": SYSTEM_PROMPT,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ],
        "max_output_tokens": LLM_MAX_TOKENS,
    }
    resp = client.post(f"{LLM_BASE_URL}/responses", json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    texts: list[str] = []
    for item in data.get("output", []):
        if item.get("type") == "message" and item.get("role") == "assistant":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    texts.append(content.get("text", ""))
    return "\n".join(t for t in texts if t).strip()


def _generate_llm(
    question: str, chunks: list[dict], history: list[dict] | None = None
) -> dict:
    """把召回片段组装进 Prompt 调用大模型（默认 DeepSeek），输出自然的总结回答。"""
    import httpx

    if not LLM_API_KEY:
        raise RuntimeError("未配置大模型 API Key")
    prompt = build_prompt(question, chunks, history)
    headers = {"Authorization": f"Bearer {LLM_API_KEY}"}
    wire = os.environ.get("RAG_LLM_WIRE_API", "auto").strip().lower()

    with httpx.Client(timeout=LLM_TIMEOUT) as client:
        if wire == "responses":
            text = _call_llm_responses(client, headers, prompt)
        elif wire == "chat":
            text = _call_llm_chat(client, headers, prompt)
        else:
            try:
                text = _call_llm_chat(client, headers, prompt)
            except _UnsupportedWire:
                text = _call_llm_responses(client, headers, prompt)

    if not text:
        raise RuntimeError("大模型返回了空内容")
    return {
        "text": filter_answer_text(text),
        "citations": [],
        "llm": True,
    }


def generate(
    question: str, chunks: list[dict], history: list[dict] | None = None
) -> dict:
    """生成回答：优先大模型（有密钥时），失败或未配置则回退本地摘录生成。"""
    if LLM_ENABLED and LLM_API_KEY:
        try:
            return _generate_llm(question, chunks, history)
        except Exception as exc:  # noqa: BLE001
            # 接口异常时回退本地生成，保证服务可用
            _logger.warning("大模型调用失败，已回退本地生成：%s", exc)
    return _generate_local(question, chunks)


# ---------- 兜底 ----------

FALLBACK_TEXT = (
    "抱歉，暂时没有找到与您问题相关的内容。"
    "您可以换个说法再试一次，或点击下方“转人工客服”按钮，由人工客服为您处理。"
)
REASK_TEXT = "抱歉，我没有理解您的问题，您可以换个说法再问我一次。"


# ---------- 对外入口 ----------


def answer(question: str, history: list[dict] | None = None) -> dict:
    """完整问答管线，返回与消息结构一致的 {text, citations, ...}。"""
    original = (question or "").strip()
    if not original:
        return {"text": REASK_TEXT, "citations": [], "intent": "unknown"}

    # 基于原始文本先识别转人工 / 闲聊，避免预处理误删问候语
    if any(p in original.lower() for p in _TRANSFER_PATTERNS):
        return {
            "text": (
                "好的，已识别到您需要转人工客服。请点击下方“转人工客服”按钮，"
                "人工客服会尽快接入为您服务。"
            ),
            "citations": [],
            "intent": "transfer",
        }
    if _is_chitchat(original):
        return {
            "text": _chitchat_reply(original),
            "citations": [],
            "intent": "chitchat",
        }

    cleaned = preprocess_question(original)
    if not cleaned:
        return {"text": REASK_TEXT, "citations": [], "intent": "unknown"}

    candidates = retrieve(cleaned, top_k=TOP_K_RECALL)
    ranked = rerank(cleaned, candidates, top_k=TOP_K_RANK)
    top_hit = ranked[0] if ranked else None
    if (
        not top_hit
        or top_hit["vec_sim"] < MIN_VECTOR_SIM
        or top_hit["score"] < MIN_RERANK_SCORE
    ):
        return {
            "text": FALLBACK_TEXT,
            "citations": [],
            "intent": "kb",
            "fallback": True,
        }

    result = generate(cleaned, ranked, (history or [])[-MAX_HISTORY_MESSAGES:])
    result["intent"] = "kb"
    return result
