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
# 获取公众号自动回复规则（调试诊断用）
# ═══════════════════════════════════════════════════════════════


def get_autoreply_rules(access_token: str = "") -> dict:
    """调用微信 API 获取公众号当前自动回复规则。

    接口: GET /cgi-bin/get_current_autoreply_info
    返回: 关注后自动回复、消息自动回复、关键词自动回复的完整配置。

    Args:
        access_token: 微信公众号 access_token。为空则自动获取。

    Returns:
        解析后的自动回复规则，失败时返回 {"error": "..."}
    """
    from urllib.request import Request, urlopen, HTTPError, URLError

    if not access_token:
        token_result = _get_access_token()
        access_token = token_result.get("access_token", "")
        if not access_token:
            return {"error": token_result.get("error", "无法获取 access_token")}

    url = f"https://api.weixin.qq.com/cgi-bin/get_current_autoreply_info?access_token={access_token}"

    try:
        with urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if "errcode" in data and data.get("errcode") != 0:
            return {"error": f"微信 API 错误: errcode={data.get('errcode')} {data.get('errmsg','')}"}
        return data
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        return {"error": f"HTTP {e.code}: {body}"}
    except URLError as e:
        return {"error": f"网络错误: {e}"}
    except Exception as e:
        return {"error": str(e)}


def diagnose_autoreply_conflict(our_ai_enabled: bool = True) -> dict:
    """诊断微信公众号自动回复与 AI 客服的冲突情况。

    如果公众号后台开启了「消息自动回复」且我们也在回调里回复，
    会冲突（微信只选一个回复）。

    Returns:
        诊断报告 dict
    """
    import json as _json

    result = {
        "our_ai_enabled": our_ai_enabled,
        "conflicts": [],
        "suggestions": [],
        "wechat_rules": None,
        "status": "unknown",
    }

    rules = get_autoreply_rules()
    result["wechat_rules"] = rules

    if "error" in rules:
        result["status"] = "error"
        result["suggestions"].append(
            f"无法获取微信自动回复规则: {rules['error']}。")
        result["suggestions"].append(
            "请检查公众号 AppSecret 是否配置正确，以及 IP 白名单是否包含当前服务器。")
        return result

    # 检查消息自动回复
    is_msg_open = rules.get("is_autoreply_open", 0)
    msg_default = rules.get("message_default_autoreply_info") or {}

    # 检查关注后自动回复
    is_follow_open = rules.get("is_add_friend_reply_open", 0)
    follow_info = rules.get("add_friend_autoreply_info") or {}

    # 检查关键词自动回复
    keyword_info = rules.get("keyword_autoreply_info") or {}
    keyword_list = keyword_info.get("list", [])

    if our_ai_enabled and is_msg_open == 1:
        result["conflicts"].append({
            "type": "message_autoreply",
            "detail": f"公众号后台开启了消息自动回复，"
                      f"当前回复内容: {msg_default.get('content', '(非文本)')[:50]}",
            "impact": "用户发消息时会由微信后台自动回复，我们的 AI 客服消息不会生效",
        })

    if is_msg_open == 0:
        result["suggestions"].append("消息自动回复未开启，自定义 AI 客服消息不受影响。")

    if is_follow_open == 1:
        follow_content = follow_info.get("content", "(非文本)")
        result["suggestions"].append(
            f"关注后自动回复已开启: {str(follow_content)[:50]}。如果需要自定义欢迎语，"
            f"可以保留此项，关注事件会触发回调，我们也能回复。"
        )

    if keyword_list:
        rules_summary = []
        for kw_rule in keyword_list:
            rule_name = kw_rule.get("rule_name", "")
            keywords = [
                k.get("content", "") for k in kw_rule.get("keyword_list_info", [])
            ]
            rules_summary.append(f"{rule_name}: {'/'.join(keywords[:5])}")
        result["suggestions"].append(
            f"检测到 {len(keyword_list)} 条关键词自动回复规则: {', '.join(rules_summary)}。"
            f"这些规则会优先于我们的 AI 消息回调，关键词匹配时会直接触发微信内置回复。"
        )

    # 最终判断
    if result["conflicts"]:
        result["status"] = "conflict"
        result["suggestions"].insert(0,
            "建议: 在公众号后台「内容与互动 → 自动回复」中关闭「消息自动回复」，"
            "让我们的 AI 客服接管所有消息。关键词自动回复如果有用可以保留。")
    else:
        result["status"] = "ok"
        result["suggestions"].insert(0, "未检测到冲突。AI 客服可以正常接管消息。")

    return result


def _get_access_token() -> dict:
    """获取微信公众号 access_token。"""
    from pathlib import Path as _Path
    import json as _json

    config_path = _Path.home() / ".wechat-publisher" / "config.json"
    if not config_path.exists():
        return {"error": f"未找到微信配置: {config_path}"}
    try:
        cfg = _json.loads(config_path.read_text(encoding="utf-8"))
        appid = str(cfg.get("appid") or "")
        secret = str(cfg.get("appsecret") or "")
        url = (
            "https://api.weixin.qq.com/cgi-bin/token?"
            f"grant_type=client_credential&appid={appid}&secret={secret}"
        )
        from urllib.request import urlopen
        with urlopen(url, timeout=15) as resp:
            return _json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


import json  # noqa (for module-level use above)


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
