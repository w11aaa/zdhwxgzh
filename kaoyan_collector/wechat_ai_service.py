# -*- coding: utf-8 -*-
"""
微信公众号 AI 客服消息回调模块

处理微信服务器推送的用户消息，支持被动回复。
当前 Demo：无论用户发什么消息，固定回复「好的，收到」。

接入步骤：
1. 将此回调 URL 配置到微信公众号后台「开发 → 基本配置 → 服务器配置」
   URL: https://你的域名/wechat/callback
   Token: 自定义一个字符串（与 WECHAT_TOKEN 一致）
2. 微信服务器会先 GET 请求验证，再 POST 用户消息

扩展方向：
- 接入 DeepSeek 实现真正的 AI 对话
- 接入 agent_tools 让用户通过对话触发采集/查询
- 关键词匹配回复（如 "北京 事业编" → 查询数据库返回公告列表）
"""

from __future__ import annotations

import hashlib
import re
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler
from typing import Any

# ═══════════════════════════════════════════════════════════════
# 配置（部署时修改）
# ═══════════════════════════════════════════════════════════════

# 与微信公众号后台「服务器配置」中的 Token 一致
WECHAT_TOKEN = "gongkao_agent_token_2024"

# ═══════════════════════════════════════════════════════════════
# 签名验证（微信 GET 请求时会带 signature/timestamp/nonce/echostr）
# ═══════════════════════════════════════════════════════════════


def verify_signature(signature: str, timestamp: str, nonce: str) -> bool:
    """验证微信服务器签名。

    微信规则：将 token、timestamp、nonce 字典排序后 SHA1，与 signature 比较。
    """
    tmp_list = sorted([WECHAT_TOKEN, timestamp, nonce])
    tmp_str = "".join(tmp_list)
    computed = hashlib.sha1(tmp_str.encode("utf-8")).hexdigest()
    return computed == signature


# ═══════════════════════════════════════════════════════════════
# XML 消息解析
# ═══════════════════════════════════════════════════════════════


def parse_message(xml_body: str) -> dict[str, str]:
    """解析微信推送的 XML 消息体。

    返回字典，包含 ToUserName / FromUserName / MsgType / Content 等字段。
    """
    root = ET.fromstring(xml_body)
    msg: dict[str, str] = {}
    for child in root:
        msg[child.tag] = (child.text or "").strip()
    return msg


def build_text_reply(to_user: str, from_user: str, content: str) -> str:
    """构造微信被动回复文本消息的 XML。

    注意：ToUserName 和 FromUserName 要反过来（发给发送者）。
    """
    xml = (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        "</xml>"
    )
    return xml


# ═══════════════════════════════════════════════════════════════
# AI 回复引擎（可扩展）
# ═══════════════════════════════════════════════════════════════


def generate_reply(user_message: str, user_id: str = "") -> str:
    """根据用户消息生成 AI 回复。

    当前 Demo：固定回复「好的，收到」。
    后续可接入 DeepSeek 实现智能对话。

    Args:
        user_message: 用户发送的消息文本
        user_id: 用户 OpenID（用于上下文/个性化）

    Returns:
        回复文本
    """
    # ─── Demo: 固定回复 ───
    return "好的，收到"

    # ─── 升级方案 1: 关键词匹配 ───
    # if "北京" in user_message and ("事业" in user_message or "考公" in user_message):
    #     return "正在为您查询北京地区事业单位招聘信息，请稍候..."
    # if "报名" in user_message:
    #     return "请输入您的省份和考试类型，如「北京 事业编」「江苏 公务员」"

    # ─── 升级方案 2: 接入 DeepSeek ───
    # from .gongkao_wechat_pipeline import _call_quality_model
    # ...


# ═══════════════════════════════════════════════════════════════
# HTTP 处理器（集成到 ui_app.py 的 HTTP 服务器中）
# ═══════════════════════════════════════════════════════════════


def handle_wechat_callback(handler: BaseHTTPRequestHandler) -> None:
    """处理微信公众号回调请求（GET 验证 + POST 消息）。

    直接在 ui_app.py 的 do_GET / do_POST 中调用此函数。
    """
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(handler.path)
    params = parse_qs(parsed.query)

    if handler.command == "GET":
        # ── 微信服务器验证 ──
        signature = (params.get("signature") or [""])[0]
        timestamp = (params.get("timestamp") or [""])[0]
        nonce = (params.get("nonce") or [""])[0]
        echostr = (params.get("echostr") or [""])[0]

        if verify_signature(signature, timestamp, nonce):
            _text_response(handler, echostr, "text/plain; charset=utf-8")
            print("[WechatCallback] GET 验证通过", flush=True)
        else:
            handler.send_response(403)
            handler.end_headers()
            handler.wfile.write(b"Forbidden")
            print("[WechatCallback] GET 验证失败：签名不匹配", flush=True)

    elif handler.command == "POST":
        # ── 接收用户消息 → 回复 ──
        length = int(handler.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            handler.send_response(400)
            handler.end_headers()
            return

        xml_body = handler.rfile.read(length).decode("utf-8")
        msg = parse_message(xml_body)

        msg_type = msg.get("MsgType", "")
        from_user = msg.get("FromUserName", "")
        to_user = msg.get("ToUserName", "")
        content = msg.get("Content", "")

        print(
            f"[WechatCallback] 收到消息: type={msg_type}, "
            f"from={from_user[:12]}..., content={content[:50]}",
            flush=True,
        )

        # 生成回复
        reply_text = generate_reply(content, from_user)

        # 构造回复 XML
        reply_xml = build_text_reply(to_user=from_user, from_user=to_user, content=reply_text)

        _text_response(handler, reply_xml, "application/xml; charset=utf-8")
        print(f"[WechatCallback] 回复: {reply_text}", flush=True)


def _text_response(handler: BaseHTTPRequestHandler, text: str, content_type: str) -> None:
    """发送 HTTP 响应。"""
    payload = text.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


# ═══════════════════════════════════════════════════════════════
# 本地测试（模拟微信服务器请求）
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="微信 AI 客服回调测试")
    parser.add_argument("--verify", action="store_true", help="测试签名验证")
    parser.add_argument("--reply", default="", help="模拟用户消息，测试回复")
    parser.add_argument("--server", action="store_true", help="启动本地测试服务器")
    args = parser.parse_args()

    if args.verify:
        # 生成测试签名
        ts = str(int(time.time()))
        nonce = "test_nonce_123"
        tmp = sorted([WECHAT_TOKEN, ts, nonce])
        sig = hashlib.sha1("".join(tmp).encode()).hexdigest()
        ok = verify_signature(sig, ts, nonce)
        print(f"签名验证: {'✅ 通过' if ok else '❌ 失败'}")
        print(f"  signature={sig}")
        print(f"  timestamp={ts}")
        print(f"  nonce={nonce}")

    if args.reply:
        text = args.reply
        reply = generate_reply(text)
        print(f"用户: {text}")
        print(f"回复: {reply}")

    if args.server:
        from http.server import HTTPServer, BaseHTTPRequestHandler
        import sys

        class TestHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/wechat/callback"):
                    handle_wechat_callback(self)
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"OK")

            def do_POST(self):
                if self.path.startswith("/wechat/callback"):
                    handle_wechat_callback(self)
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"OK")

        port = 8860
        server = HTTPServer(("127.0.0.1", port), TestHandler)
        print(f"测试服务器: http://127.0.0.1:{port}/wechat/callback")
        print("按 Ctrl+C 停止")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()
            print("已停止")
