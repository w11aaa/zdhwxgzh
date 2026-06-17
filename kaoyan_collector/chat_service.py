# -*- coding: utf-8 -*-
"""Chat Service - SSE streaming with RAG + KG integration."""

from __future__ import annotations
import json, time, uuid, sqlite3, re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Generator

from .config import CONFIG

# SSE event helpers
def sse_event(event_type: str, data: dict) -> str:
    return f"data: {json.dumps({'type': event_type, **data}, ensure_ascii=False)}\n\n"

def sse_done() -> str:
    return "data: {\"type\": \"done\"}\n\n"

# Session store (in-memory for speed)
_sessions: dict[str, list[dict]] = {}

def get_session(session_id: str) -> list[dict]:
    if session_id not in _sessions:
        _sessions[session_id] = []
    return _sessions[session_id]

def add_to_session(session_id: str, role: str, content: str):
    sess = get_session(session_id)
    sess.append({"role": role, "content": content})
    if len(sess) > 20:
        sess.pop(0)

def generate_chat_stream(message: str, session_id: str = "") -> Generator[str, None, None]:
    """Main SSE streaming generator."""
    msg = message.strip()
    if not msg:
        yield sse_event("reply", {"content": "请输入消息"})
        yield sse_done()
        return

    # Load session context
    context = get_session(session_id)
    context_text = ""
    for m in context[-6:]:
        context_text += f"[{m['role']}]: {m['content'][:200]}\n"

    yield sse_event("thinking", {"content": "\U0001F50D \u5206\u6790\u60a8\u7684\u95ee\u9898..."})

    # Step 1: Try RAG search
    rag_results = []
    try:
        from .rag_engine import RAGEngine
        rag = RAGEngine()
        rag_results = rag.search(msg, top_k=3)
        if rag_results:
            yield sse_event("tool_call", {"tool": "rag_search", "count": len(rag_results)})
    except Exception as e:
        yield sse_event("tool_call", {"tool": "rag_search", "error": str(e)[:100]})

    # Step 2: Try KG recommendations
    kg_recs = []
    try:
        from .kg_engine import KnowledgeGraphEngine
        kg = KnowledgeGraphEngine()
        kg_recs = kg.recommend_related(msg)
        if kg_recs:
            yield sse_event("tool_call", {"tool": "kg_recommend", "count": len(kg_recs)})
    except Exception:
        pass

    # Step 3: Keyword search as fallback
    from .wechat_ai_service import _detect_region, _detect_exam_type, _search_events_for_user
    region = _detect_region(msg)
    exam_type = _detect_exam_type(msg)
    sql_results = []
    if region or exam_type:
        sql_results = _search_events_for_user(region, exam_type, limit=5)
        if sql_results:
            yield sse_event("tool_call", {"tool": "keyword_search", "count": len(sql_results)})

    # Build context for DeepSeek
    context_parts = []
    if rag_results:
        context_parts.append("=== RAG\u8bed\u4e49\u641c\u7d22\u7ed3\u679c ===")
        for i, r in enumerate(rag_results[:3], 1):
            context_parts.append(f"[{i}] {r.title} ({r.region}, {r.category})\n{r.preview[:300]}")
    if sql_results and not rag_results:
        context_parts.append("=== \u5173\u952e\u8bcd\u641c\u7d22\u7ed3\u679c ===")
        for i, r in enumerate(sql_results[:5], 1):
            context_parts.append(f"[{i}] {r['title'][:80]} ({r.get('region','')}, \u62db{r.get('job_count','?')}\u4eba, {r.get('deadline_countdown','')})")
    if kg_recs:
        context_parts.append("=== \u77e5\u8bc6\u56fe\u8c31\u63a8\u8350 ===")
        for r in kg_recs[:3]:
            context_parts.append(f"- {r.get('title','')[:80]} ({r.get('reason','')})")

    search_context = "\n".join(context_parts) if context_parts else ""

    # Step 4: DeepSeek generation
    yield sse_event("thinking", {"content": "\U0001F4DD \u751f\u6210\u56de\u590d\u4e2d..."})
    reply = _call_deepseek_with_context(msg, search_context, context_text)

    if not reply:
        reply = _simple_reply(msg, sql_results, rag_results)

    add_to_session(session_id, "user", msg)
    add_to_session(session_id, "assistant", reply)

    yield sse_event("reply", {"content": reply, "session_id": session_id})
    yield sse_done()


def _call_deepseek_with_context(user_msg: str, search_context: str, history: str) -> str:
    import os
    from urllib.request import Request, urlopen

    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        api_file = CONFIG.project_root / "api.md"
        if api_file.exists():
            m = re.search(r"sk-[A-Za-z0-9_-]{16,}", api_file.read_text(encoding="utf-8", errors="ignore"))
            if m:
                api_key = m.group(0)
    if not api_key:
        return ""

    model = (os.environ.get("DEEPSEEK_MODEL") or "deepseek-v4-flash").strip()
    endpoint = (os.environ.get("DEEPSEEK_API_ENDPOINT") or "https://api.deepseek.com/chat/completions").strip()

    system_prompt = (
        "\u4f60\u662f\u300c\u8003\u516c\u4fe1\u606f\u52a9\u624b\u300d\u7684 AI \u5ba2\u670d\u3002"
        "\u56de\u590d\u8981\u7b80\u6d01\u3001\u51c6\u786e\u3001\u6709\u7528\uff0c\u63a7\u5236\u5728 400 \u5b57\u4ee5\u5185\u3002"
        "\u4e0d\u8981\u7f16\u9020\u6ca1\u6709\u7684\u516c\u544a\u4fe1\u606f\u3002"
        "\u4e0d\u8981\u51fa\u73b0 Markdown \u683c\u5f0f\u3002"
    )

    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.append({"role": "system", "content": f"\u5bf9\u8bdd\u5386\u53f2:\n{history[-1500:]}"})
    if search_context:
        messages.append({"role": "system", "content": f"\u68c0\u7d22\u5230\u7684\u76f8\u5173\u4fe1\u606f:\n{search_context[:2000]}"})
    messages.append({"role": "user", "content": user_msg[:500]})

    try:
        req = Request(endpoint,
            data=json.dumps({"model": model, "messages": messages, "temperature": 0.3, "max_tokens": 600, "stream": False}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        choices = data.get("choices", [])
        if choices:
            return choices[0]["message"]["content"].strip()[:600]
    except Exception:
        pass
    return ""


def _simple_reply(msg: str, sql_results: list, rag_results: list) -> str:
    """Fallback reply without DeepSeek."""
    if rag_results:
        lines = ["\U0001F50D \u8bed\u4e49\u641c\u7d22\u7ed3\u679c:"]
        for r in rag_results[:3]:
            lines.append(f"\u25cf {r.title[:60]} ({r.region}, \u62db{r.job_count or '?'}\u4eba)")
        return "\n".join(lines)
    if sql_results:
        lines = [f"\u4e3a\u60a8\u627e\u5230 {len(sql_results)} \u6761\u76f8\u5173\u516c\u544a:"]
        for r in sql_results[:5]:
            lines.append(f"\u25cf {r['title'][:60]} ({r.get('deadline_countdown','')})")
        return "\n".join(lines)
    return "\u597d\u7684\uff0c\u6536\u5230\u3002\u53d1\u9001\u300c\u5e2e\u52a9\u300d\u67e5\u770b\u6211\u80fd\u4e3a\u4f60\u505a\u4ec0\u4e48\u3002"
