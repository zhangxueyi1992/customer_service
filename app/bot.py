"""机器人回答入口：委托给智能问答（RAG）管线。"""

from __future__ import annotations

from . import rag


def answer(question: str, history: list | None = None) -> dict:
    """返回 {text, citations, intent, ...} 结构的回答（见 rag.answer）。"""
    return rag.answer(question, history)
