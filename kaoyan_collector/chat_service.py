# -*- coding: utf-8 -*-
"""Chat Service - SSE streaming with visible thinking & decision chain."""

from __future__ import annotations
import json, time, re
from typing import Generator
from .config import CONFIG

_sessions: dict[str, list[dict]] = {}

def sse(e_type: str, **data) -> str:
    return f"data: {json.dumps({'type': e_type, **data}, ensure_ascii=False)}\n\n"

def sse_done() -> str:
    return 'data: {"type":"done"}\n\n'

def get_session(sid: str) -> list[dict]:
    if sid not in _sessions: _sessions[sid] = []
    return _sessions[sid]

def add_to_session(sid: str, role: str, content: str):
    sess = get_session(sid)
    sess.append({"role": role, "content": content})
    if len(sess) > 20: sess.pop(0)

def generate_chat_stream(message: str, session_id: str = "") -> Generator[str, None, None]:
    msg = message.strip()
    if not msg:
        yield sse("reply", content="请输入消息")
        yield sse_done()
        return

    context = get_session(session_id)
    history_text = "\n".join(f"[{m['role']}]: {m['content'][:200]}" for m in context[-6:])

    # Step 0: Intent analysis
    yield sse("step", icon="\U0001f9e0", title="\u610f\u56fe\u5206\u6790",
              detail="\u5206\u6790\u60a8\u7684\u95ee\u9898\u4e2d\u7684\u5173\u952e\u4fe1\u606f...")
    from .wechat_ai_service import _detect_region, _detect_exam_type
    region = _detect_region(msg)
    exam_type = _detect_exam_type(msg)
    intent_info = []
    if region: intent_info.append(f"\u5730\u533a: {region}")
    if exam_type: intent_info.append(f"\u7c7b\u578b: {exam_type}")
    if intent_info:
        yield sse("decision", icon="\u2714", text="\u8bc6\u522b\u5230\u4e86: " + ", ".join(intent_info))
    else:
        yield sse("decision", icon="\u26a0", text="\u672a\u8bc6\u522b\u5230\u7279\u5b9a\u5730\u533a\u6216\u8003\u8bd5\u7c7b\u578b\uff0c\u5c06\u4f7f\u7528 AI \u5168\u9762\u56de\u7b54")

    # Step 1: RAG
    yield sse("step", icon="\U0001f50d", title="RAG \u8bed\u4e49\u641c\u7d22",
              detail="\u5728 737 \u6761\u516c\u544a\u4e2d\u641c\u7d22...")
    rag_results = []
    try:
        from .rag_engine import RAGEngine
        rag = RAGEngine()
        rag_results = rag.search(msg, top_k=3)
        if rag_results:
            hits = [f"{r.title[:30]}({r.region})" for r in rag_results[:3]]
            yield sse("decision", icon="\u2714", text=f"RAG: \u627e\u5230 {len(rag_results)} \u6761\u76f8\u5173\u516c\u544a")
            yield sse("tool_result", tool="rag_search", count=len(rag_results), hits=hits)
        else:
            yield sse("decision", icon="\u26a0", text="RAG \u641c\u7d22\u672a\u547d\u4e2d")
    except Exception as e:
        yield sse("decision", icon="\u26a0", text=f"RAG: {str(e)[:60]}")
        yield sse("step", icon="\U0001f50e", title="\u5173\u952e\u8bcd\u641c\u7d22",
                  detail="\u5207\u6362\u5230\u5173\u952e\u8bcd\u641c\u7d22...")

    # Step 2: Keyword search
    sql_results = []
    if region or exam_type:
        from .wechat_ai_service import _search_events_for_user
        sql_results = _search_events_for_user(region, exam_type, limit=5)
        if sql_results:
            yield sse("step", icon="\U0001f50e", title="\u5173\u952e\u8bcd\u641c\u7d22",
                      detail=f"\u67e5\u8be2\u5b8c\u6210\uff0c\u627e\u5230 {len(sql_results)} \u6761")
            yield sse("decision", icon="\u2714", text=f"SQL: \u627e\u5230 {len(sql_results)} \u6761\u6b63\u5728\u62a5\u540d\u7684\u516c\u544a")

    # Step 3: AI Generation
    yield sse("step", icon="\U0001f4dd", title="AI \u751f\u6210\u56de\u590d",
              detail="\u5c06\u641c\u7d22\u7ed3\u679c\u63d0\u4f9b\u7ed9 DeepSeek...")

    reply = _call_deepseek(msg, sql_results, rag_results, history_text)
    if not reply:
        yield sse("decision", icon="\u26a0", text="DeepSeek \u672a\u8fd4\u56de\uff0c\u4f7f\u7528\u5907\u7528\u56de\u590d")
        reply = _fallback_reply(msg, sql_results, rag_results)

    add_to_session(session_id, "user", msg)
    add_to_session(session_id, "assistant", reply)

    yield sse("reply", content=reply, session_id=session_id)
    yield sse_done()


def _call_deepseek(user_msg: str, sql_results: list, rag_results: list, history: str) -> str:
    import os
    from urllib.request import Request, urlopen

    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        api_file = CONFIG.project_root / "api.md"
        if api_file.exists():
            m = re.search(r"sk-[A-Za-z0-9_-]{16,}", api_file.read_text(encoding="utf-8", errors="ignore"))
            if m: api_key = m.group(0)
    if not api_key: return ""

    model = os.environ.get("DEEPSEEK_MODEL") or "deepseek-v4-flash"
    endpoint = os.environ.get("DEEPSEEK_API_ENDPOINT") or "https://api.deepseek.com/chat/completions"

    # Build context
    context_parts = []
    if rag_results:
        context_parts.append("RAG results:")
        for r in rag_results[:3]: context_parts.append(f"  - {r.title[:60]} ({r.region}, {r.category})")
    if sql_results:
        context_parts.append("DB results:")
        for r in sql_results[:5]: context_parts.append(f"  - {r['title'][:60]} ({r.get('deadline_countdown','')})")
    ctx = "\n".join(context_parts)

    system = "\u4f60\u662f\u300c\u8003\u516c\u4fe1\u606f\u52a9\u624b\u300dAI\u5ba2\u670d\u3002\u56de\u590d\u7b80\u6d01\u3001\u51c6\u786e\u3001\u6709\u7528\u3002\u63a7\u5236\u5728400\u5b57\u4ee5\u5185\u3002\u4e0d\u8981\u7f16\u9020\u516c\u544a\u4fe1\u606f\u3002\u4e0d\u8981Markdown\u3002"
    msgs = [{"role": "system", "content": system}]
    if history: msgs.append({"role": "system", "content": f"\u5bf9\u8bdd\u5386\u53f2:\n{history[-1500:]}"})
    if ctx: msgs.append({"role": "system", "content": f"\u53c2\u8003\u4fe1\u606f:\n{ctx[:2000]}"})
    msgs.append({"role": "user", "content": user_msg[:500]})

    try:
        req = Request(endpoint,
            data=json.dumps({"model": model, "messages": msgs, "temperature": 0.3, "max_tokens": 600, "stream": False}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
        with urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read().decode())
        if d.get("choices"): return d["choices"][0]["message"]["content"].strip()[:600]
    except: pass
    return ""


def _fallback_reply(msg: str, sql_results: list, rag_results: list) -> str:
    if rag_results:
        lines = ["\u2714 \u8bed\u4e49\u641c\u7d22\u7ed3\u679c:"]
        for r in rag_results[:3]: lines.append(f"\u2022 {r.title[:60]} ({r.region})")
        return "\n".join(lines)
    if sql_results:
        lines = [f"\u2714 \u4e3a\u60a8\u627e\u5230 {len(sql_results)} \u6761\u76f8\u5173\u516c\u544a:"]
        for r in sql_results[:5]: lines.append(f"\u2022 {r['title'][:60]} ({r.get('deadline_countdown','')})")
        return "\n".join(lines)
    return "\u597d\u7684\uff0c\u6536\u5230\u3002\u53d1\u9001\u300c\u5e2e\u52a9\u300d\u67e5\u770b\u6211\u80fd\u4e3a\u4f60\u505a\u4ec0\u4e48\u3002"
