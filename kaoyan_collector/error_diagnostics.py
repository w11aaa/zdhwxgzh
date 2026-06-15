# -*- coding: utf-8 -*-
"""失败自动诊断与恢复策略（Failure Auto-Diagnosis & Recovery）

当 Agent 任务失败时，根据错误日志自动匹配诊断规则，
输出诊断结果、可能原因、修复建议。支持以下场景：
- 微信公众号 API 错误（Token 过期、权限不足、IP 白名单）
- 网络请求失败（DNS、超时、代理）
- 附件下载/解析失败（空文件、格式不支持、伪链接）
- 数据库查询异常（锁定、字段缺失）
- 子进程超时/异常退出
- DeepSeek 质检 API 异常
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any

# ── 诊断结果 ──────────────────────────────────────────────────


@dataclass
class Diagnosis:
    """单条诊断。"""
    error_code: str = ""
    error_message: str = ""
    category: str = ""
    severity: str = "medium"  # low / medium / high / critical
    possible_causes: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    auto_recoverable: bool = False
    auto_recovery_action: str = ""


# ── 诊断规则 ──────────────────────────────────────────────────


# 微信公众号 API 错误码 → 诊断
WECHAT_ERROR_DIAGNOSIS: dict[str, Diagnosis] = {
    "40001": Diagnosis(
        error_code="40001",
        category="微信公众号 Access Token",
        severity="critical",
        possible_causes=[
            "access_token 已过期或使用了旧缓存的 token",
            "AppSecret 与当前应用不匹配",
            "微信公众号后台重置了 AppSecret",
            "access_token 缓存在不同机器上被覆盖",
        ],
        suggestions=[
            "清理 token 缓存文件 (~/.wechat-publisher/token_cache.json)",
            "重新运行「检测微信 Token」获取新 access_token",
            "检查 ~/.wechat-publisher/config.json 中的 appid/appsecret 是否匹配",
            "确认微信公众号后台未修改 AppSecret",
        ],
        auto_recoverable=True,
        auto_recovery_action="清理 token_cache.json 后重新获取 access_token",
    ),
    "40003": Diagnosis(
        error_code="40003",
        category="微信公众号用户管理",
        severity="high",
        possible_causes=[
            "传入的 OpenID 列表格式不正确",
            "OpenID 列表中有非关注用户的 OpenID",
        ],
        suggestions=["检查传入的 OpenID 是否有效", "确认用户是否已关注公众号"],
        auto_recoverable=False,
    ),
    "40009": Diagnosis(
        error_code="40009",
        category="微信公众号图片文件",
        severity="high",
        possible_causes=[
            "图片文件大小超过限制（最大 10MB）",
            "上传的图片格式不支持",
            "上传接口调用频率超限",
        ],
        suggestions=[
            "压缩封面图至 10MB 以内",
            "确保封面图为 JPG/PNG 格式",
            "等待 1 分钟后重试",
        ],
        auto_recoverable=False,
    ),
    "41001": Diagnosis(
        error_code="41001",
        category="微信公众号 Access Token",
        severity="critical",
        possible_causes=[
            "access_token 缺失或为空",
            "未正确获取 access_token",
            "access_token 在传输过程中被截断",
        ],
        suggestions=[
            "重新运行「检测微信 Token」",
            "检查 API 请求中是否携带了 access_token 参数",
        ],
        auto_recoverable=True,
        auto_recovery_action="重新获取 access_token",
    ),
    "42001": Diagnosis(
        error_code="42001",
        category="微信公众号 Access Token",
        severity="critical",
        possible_causes=["access_token 已过期（有效期 2 小时）"],
        suggestions=["重新获取 access_token", "在任务执行前先刷新 token"],
        auto_recoverable=True,
        auto_recovery_action="重新获取 access_token",
    ),
    "48001": Diagnosis(
        error_code="48001",
        category="微信公众号 API 权限",
        severity="high",
        possible_causes=[
            "当前公众号未获得该 API 权限",
            "公众号类型不满足接口要求（如未认证的订阅号）",
            "需要在公众号后台申请对应的接口权限",
        ],
        suggestions=[
            "当前账号可能不支持直接发布，采用「提交草稿箱 + 人工发布」模式",
            "确认公众号已通过微信认证",
            "在公众号后台「接口权限」中检查该 API 状态",
        ],
        auto_recoverable=False,
    ),
    "40014": Diagnosis(
        error_code="40014",
        category="微信公众号 Access Token",
        severity="critical",
        possible_causes=["access_token 不合法或已失效"],
        suggestions=["重新获取 access_token", "检查 appsecret 是否正确"],
        auto_recoverable=True,
        auto_recovery_action="重新获取 access_token",
    ),
}

# 通用错误模式 → 诊断
GENERIC_ERROR_PATTERNS: list[tuple[re.Pattern, Diagnosis]] = [
    (
        re.compile(r"(?i)connection.*(?:refused|reset|timeout|aborted)"),
        Diagnosis(
            category="网络连接",
            severity="high",
            possible_causes=[
                "目标服务器拒绝连接或不可达",
                "本地网络环境不稳定",
                "代理配置不正确",
                "目标服务暂时不可用",
            ],
            suggestions=[
                "检查网络连接状态",
                "如果使用代理，确认代理服务正常运行",
                "等待几分钟后重试",
                "检查目标 URL 是否正确",
            ],
            auto_recoverable=True,
            auto_recovery_action="等待 30 秒后重试",
        ),
    ),
    (
        re.compile(r"(?i)(?:dns|name resolution|getaddrinfo|no address associated)"),
        Diagnosis(
            category="DNS 解析",
            severity="high",
            possible_causes=[
                "DNS 服务器无法解析目标域名",
                "本地 DNS 缓存过期",
                "网络未连接到互联网",
            ],
            suggestions=[
                "检查 DNS 设置",
                "尝试刷新 DNS 缓存 (ipconfig /flushdns)",
                "确认网络已连接互联网",
            ],
            auto_recoverable=False,
        ),
    ),
    (
        re.compile(r"(?i)(?:download|fetch).*?(?:empty|0.?bytes|content.*?small)"),
        Diagnosis(
            category="附件下载",
            severity="medium",
            possible_causes=[
                "下载链接是检测链接而非真实附件（如粉笔 crawler/check 链接）",
                "服务器返回了空内容",
                "下载请求被重定向到无关页面",
            ],
            suggestions=[
                "这是粉笔 crawler/check 检测链接，不是真实附件，已自动跳过",
                "如需下载附件，请从原公告页面获取真实下载链接",
                "可以手动从原文链接下载附件",
            ],
            auto_recoverable=True,
            auto_recovery_action="自动跳过非真实附件，继续处理下一个",
        ),
    ),
    (
        re.compile(r"(?i)(?:403|forbidden|unauthorized|401|auth.*fail)"),
        Diagnosis(
            category="认证授权",
            severity="high",
            possible_causes=[
                "API Key 已过期或无效",
                "请求缺少必要的认证头",
                "IP 不在白名单中（如微信公众号 IP 白名单）",
                "权限不足",
            ],
            suggestions=[
                "检查 API Key / Token 是否有效",
                "如果是微信公众号接口，检查 IP 白名单配置",
                "确认 AppSecret 与当前环境匹配",
            ],
            auto_recoverable=False,
        ),
    ),
    (
        re.compile(r"(?i)(?:subprocess|timeout|timed.?out|exceed.*time)"),
        Diagnosis(
            category="任务超时",
            severity="medium",
            possible_causes=[
                "子任务执行时间超过了设定超时上限",
                "目标服务响应过慢",
                "网络延迟导致 HTTP 请求超时",
            ],
            suggestions=[
                "检查超时设置是否合理（当前多步超时为 600-900 秒）",
                "如果是附件下载超时，可减少并发附件数量",
                "如果是全网采集超时，可分批执行",
            ],
            auto_recoverable=True,
            auto_recovery_action="减少单次处理数据量后重试",
        ),
    ),
    (
        re.compile(r"(?i)(?:sqlite|database.*(?:lock|corrupt|readonly|malformed))"),
        Diagnosis(
            category="数据库异常",
            severity="critical",
            possible_causes=[
                "数据库文件被其他进程锁定",
                "数据库文件损坏",
                "磁盘空间不足导致写入失败",
                "数据库连接未正确关闭",
            ],
            suggestions=[
                "检查是否有其他进程正在使用数据库",
                "运行 SQLite 检查: PRAGMA integrity_check",
                "确认磁盘有足够空间",
                "备份当前数据库后尝试重建",
            ],
            auto_recoverable=False,
        ),
    ),
    (
        re.compile(r"(?i)(?:api.*key.*(?:invalid|missing|not.?found))"),
        Diagnosis(
            category="DeepSeek API 配置",
            severity="high",
            possible_causes=[
                "DeepSeek API Key 未配置或已过期",
                "kaoyan_collector/api.md 文件缺失",
                "环境变量 DEEPSEEK_API_KEY 未设置",
            ],
            suggestions=[
                "将有效 API Key 写入 kaoyan_collector/api.md（格式见 api.md.example）",
                "或设置环境变量 DEEPSEEK_API_KEY",
                "确认 API Key 有足够余额",
            ],
            auto_recoverable=False,
        ),
    ),
    (
        re.compile(r"(?i)(?:playwright|browser.*(?:launch|crash|closed|shut.?down))"),
        Diagnosis(
            category="浏览器自动化",
            severity="high",
            possible_causes=[
                "Playwright 浏览器未安装或版本不匹配",
                "Chrome 进程崩溃",
                "系统资源不足（内存/CPU）",
            ],
            suggestions=[
                "运行 playwright install chromium 安装浏览器",
                "检查系统可用内存",
                "重试或减少并发浏览器实例",
            ],
            auto_recoverable=False,
        ),
    ),
    (
        re.compile(r"(?i)(?:quality.*check|质检.*(?:失败|返回|超时|未通过|未能|无效|没有权限|不可用))"),
        Diagnosis(
            category="大模型质检",
            severity="medium",
            possible_causes=[
                "DeepSeek API Key 无效、过期或没有权限",
                "DeepSeek API 返回了非 JSON 格式结果",
                "质检模型返回空响应",
                "草稿内容过长超出模型上下文限制",
                "API 调用频率限制",
            ],
            suggestions=[
                "检查 DeepSeek API Key 是否正确（kaoyan_collector/api.md）",
                "确认 API Key 有足够余额和额度",
                "如果频率受限，等待 60 秒后重试",
                "检查草稿内容长度（当前截断到 18000 字符）",
            ],
            auto_recoverable=True,
            auto_recovery_action="等待 60 秒后重试质检",
        ),
    ),
    (
        re.compile(r"(?i)(?:下载内容过小|empty.?response|0.?bytes|content.?length.*0|疑似.*crawler.*检测)"),
        Diagnosis(
            category="非真实附件链接",
            severity="low",
            possible_causes=[
                "该链接是平台（粉笔/公考雷达）的访问检测或鉴权重定向",
                "不是真实的附件文件下载链接",
                "附件需要特殊 cookie 或 Referer 才能下载",
            ],
            suggestions=[
                "自动跳过即可，这不是系统故障",
                "如果该公告的附件很重要，可以手动访问原网站下载",
                "检查是否需要设置 Referer 头来下载附件",
            ],
            auto_recoverable=True,
            auto_recovery_action="自动跳过该附件，继续处理下一个",
        ),
    ),
    (
        re.compile(r"(?i)(?:Permission denied|publickey|Could not read from remote|Repository.*not found)"),
        Diagnosis(
            category="Git/版本控制错误",
            severity="high",
            possible_causes=[
                "SSH 公钥未添加到 GitHub",
                "仓库不存在或已被删除",
                "没有推送到该仓库的权限",
            ],
            suggestions=[
                "确认 SSH Key 已添加到 GitHub Settings",
                "用 ssh -T git@github.com 测试连接",
                "确认仓库 URL 正确且你有写入权限",
            ],
            auto_recoverable=False,
        ),
    ),
]


# ── 诊断引擎 ──────────────────────────────────────────────────


def diagnose_error(error_text: str) -> list[Diagnosis]:
    """根据错误文本诊断失败原因。

    Args:
        error_text: 错误日志或异常消息

    Returns:
        匹配的诊断列表（可能有多个）。如果没有匹配的诊断则返回通用诊断。
    """
    results: list[Diagnosis] = []

    # 1. 精确匹配微信公众号错误码
    wechat_code_match = re.search(r"(?:errcode|error_code)['\"]?\s*[:=]\s*['\"]?(\d+)", error_text)
    if wechat_code_match:
        code = wechat_code_match.group(1)
        if code in WECHAT_ERROR_DIAGNOSIS:
            results.append(WECHAT_ERROR_DIAGNOSIS[code])

    # 2. 通用模式匹配
    for pattern, diag in GENERIC_ERROR_PATTERNS:
        if pattern.search(error_text):
            diag.error_message = error_text[:500]
            results.append(diag)

    # 3. 没有任何匹配时的默认诊断
    if not results:
        results.append(
            Diagnosis(
                category="未知错误",
                severity="medium",
                error_message=error_text[:500],
                possible_causes=[
                    "这是一个未识别的新错误类型",
                    "可能是代码逻辑异常或外部依赖问题",
                ],
                suggestions=[
                    "查看完整的错误输出寻找更多线索",
                    "检查原始日志文件",
                    "如果持续出现，请联系开发者添加诊断规则",
                ],
                auto_recoverable=False,
            )
        )

    # 去重（按 category 去重，保留第一个）
    seen: set[str] = set()
    deduped: list[Diagnosis] = []
    for diag in results:
        if diag.category not in seen:
            seen.add(diag.category)
            deduped.append(diag)
    return deduped


def diagnose_and_format(error_text: str) -> str:
    """诊断并格式化为可读文本。"""
    diagnoses = diagnose_error(error_text)
    lines = ["失败自动诊断", "=" * 40, ""]
    for i, diag in enumerate(diagnoses, 1):
        sev_map = {"low": "低", "medium": "中", "high": "高", "critical": "严重"}
        lines.append(f"[诊断 {i}] {diag.category}（严重程度：{sev_map.get(diag.severity, diag.severity)}）")
        if diag.error_code:
            lines.append(f"  错误码：{diag.error_code}")
        if diag.possible_causes:
            lines.append("  可能原因：")
            for cause in diag.possible_causes:
                lines.append(f"    - {cause}")
        if diag.suggestions:
            lines.append("  建议操作：")
            for sug in diag.suggestions:
                lines.append(f"    - {sug}")
        if diag.auto_recoverable and diag.auto_recovery_action:
            lines.append(f"  自动恢复：{diag.auto_recovery_action}")
        lines.append("")
    return "\n".join(lines)


def diagnose_error_as_dict(error_text: str) -> dict[str, Any]:
    """诊断并以 dict 格式返回（供 API 使用）。"""
    return {
        "diagnoses": [asdict(d) for d in diagnose_error(error_text)],
        "auto_recoverable": any(d.auto_recoverable for d in diagnose_error(error_text)),
    }


# ── CLI 测试 ──────────────────────────────────────────────────


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="失败自动诊断工具")
    parser.add_argument("--error", default="", help="要诊断的错误文本")
    parser.add_argument("--test", action="store_true", help="运行自带测试用例")
    args = parser.parse_args()

    if args.test:
        test_cases = [
            "微信返回：{'errcode': 40001, 'errmsg': 'invalid credential'}",
            "Error: Connection reset by peer while downloading attachment",
            "附件下载失败：下载内容过小（0 bytes），疑似粉笔 crawler 检测链接",
            "subprocess.TimeoutExpired: Command timed out after 600 seconds",
            "sqlite3.OperationalError: database is locked",
            "RuntimeError: 大模型质检未能运行：DeepSeek API Key 无效",
        ]
        for case in test_cases:
            print(f"输入错误：{case}")
            print(diagnose_and_format(case))
            print("-" * 60)
    elif args.error:
        print(diagnose_and_format(args.error))
    else:
        print("使用 --error 传入错误文本，或 --test 运行测试用例。")
