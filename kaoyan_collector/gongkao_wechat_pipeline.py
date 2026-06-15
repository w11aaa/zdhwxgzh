from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
import re
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from .attachment_images import generate_attachment_table_images
from .config import CONFIG
from .wechat_pipeline import (
    convert_markdown_to_wechat_html,
    export_wechat_markdown,
    publish_html_to_wechat_draft,
    save_wechat_payload_preview,
)


SOURCE_NOISE_PATTERNS = [
    r"免费报考咨询\s*考情随时掌握\s*尽在粉笔",
    r"考情随时掌握\s*尽在粉笔",
    r"免费报考咨询",
    r"扫码.*?(?:领资料|咨询|进群)",
    r"加入.*?(?:备考群|交流群)",
    r"[^。\n]{0,40}(?:招聘群|咨询群|资料群|备考群|交流群|微信群|学习群|专项招聘群)[^。\n]{0,40}",
]
QUALITY_REPORT_DIR = CONFIG.project_root / "quality_reports"
COMMON_CITY_NAMES = [
    "石家庄",
    "洛阳",
    "郑州",
    "开封",
    "保定",
    "邯郸",
    "唐山",
    "济南",
    "青岛",
    "南京",
    "苏州",
    "杭州",
    "宁波",
    "广州",
    "深圳",
    "成都",
    "重庆",
    "武汉",
    "长沙",
    "西安",
    "太原",
]


class QualityCheckError(RuntimeError):
    pass


@dataclass
class GongkaoSelection:
    source_platform: str
    source_id: str
    title: str
    region: str
    category: str
    org_name: str
    job_count: int | None
    qualification: str
    major_requirements: str
    registration_start: str
    registration_deadline: str
    registration_deadline_time: str
    exam_date: str
    status: str
    publish_time: str
    source_url: str
    article_url: str
    source_origin_url: str
    source_origin_html: str
    summary: str
    raw_text: str
    raw_json: str


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _deadline_days(deadline: str) -> int | None:
    if not deadline:
        return None
    try:
        return (datetime.strptime(deadline, "%Y-%m-%d").date() - date.today()).days
    except Exception:
        return None


def fetch_gongkao_events(
    db_path: Path,
    *,
    limit: int,
    category: str,
    region: str,
    status: str,
    require_deadline: bool,
    days_to_deadline: int,
) -> list[GongkaoSelection]:
    clauses = ["1=1"]
    params: list[Any] = []

    if category.strip():
        clauses.append("category = ?")
        params.append(category.strip())
    if region.strip():
        clauses.append("region = ?")
        params.append(region.strip())
    if status.strip():
        clauses.append("status = ?")
        params.append(status.strip())
    if require_deadline:
        clauses.append("coalesce(registration_deadline, '') <> ''")
    if days_to_deadline > 0:
        clauses.append("coalesce(registration_deadline, '') <> ''")
        clauses.append("registration_deadline <= date('now', '+' || ? || ' days')")
        clauses.append("registration_deadline >= date('now')")
        params.append(days_to_deadline)

    query = f"""
    SELECT
        source_platform,
        source_id,
        title,
        region,
        category,
        org_name,
        job_count,
        qualification,
        major_requirements,
        registration_start,
        registration_deadline,
        coalesce(registration_deadline_time, '') AS registration_deadline_time,
        exam_date,
        status,
        publish_time,
        source_url,
        article_url,
        source_origin_url,
        source_origin_html,
        summary,
        raw_text,
        raw_json
    FROM gongkao_events
    WHERE {' AND '.join(clauses)}
    ORDER BY
        CASE
            WHEN coalesce(source_origin_url, '') <> '' AND coalesce(source_origin_html, '') <> '' THEN 0
            ELSE 1
        END,
        CASE WHEN coalesce(registration_deadline, '') = '' THEN 1 ELSE 0 END,
        registration_deadline ASC,
        coalesce(job_count, 0) DESC,
        publish_time DESC
    LIMIT ?
    """
    params.append(limit)

    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [GongkaoSelection(**dict(row)) for row in rows]


def pick_gongkao_event(
    db_path: Path,
    *,
    topic_id: str,
    category: str,
    region: str,
    status: str,
    require_deadline: bool,
    days_to_deadline: int,
) -> GongkaoSelection:
    if topic_id.strip():
        with _connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT
                    source_platform,
                    source_id,
                    title,
                    region,
                    category,
                    org_name,
                    job_count,
                    qualification,
                    major_requirements,
                    registration_start,
                    registration_deadline,
                    coalesce(registration_deadline_time, '') AS registration_deadline_time,
                    exam_date,
                    status,
                    publish_time,
                    source_url,
                    article_url,
                    source_origin_url,
                    source_origin_html,
                    summary,
                    raw_text,
                    raw_json
                FROM gongkao_events
                WHERE source_id = ?
                ORDER BY imported_at DESC
                LIMIT 1
                """,
                (topic_id.strip(),),
            ).fetchone()
        if row:
            return GongkaoSelection(**dict(row))
    rows = fetch_gongkao_events(
        db_path,
        limit=50,
        category=category,
        region=region,
        status=status,
        require_deadline=require_deadline,
        days_to_deadline=days_to_deadline,
    )
    if not rows:
        raise ValueError("没有找到可用于公众号生成的考公公告，请先补充采集数据或放宽筛选条件。")
    if not topic_id.strip():
        return rows[0]
    raise ValueError(f"没有找到 source_id={topic_id} 的考公公告。")


def _build_gongkao_wechat_content(event: GongkaoSelection, *, include_attachment_images: bool = False) -> tuple[str, str]:
    if event.source_origin_url and event.source_origin_html:
        return _build_origin_first_content(event, include_attachment_images=include_attachment_images)

    raw = _parse_raw_json(event.raw_json)
    org_name = _clean_org_name(event.org_name or event.title)
    registration_range = _format_registration_range(event.registration_start, event.registration_deadline_time or event.registration_deadline)
    registration_method = _extract_registration_method(event.raw_text)
    deadline_days = _deadline_days(event.registration_deadline)
    deadline_hint = ""
    if deadline_days is not None:
        if deadline_days == 0:
            deadline_hint = "今日截止"
        elif deadline_days > 0:
            deadline_hint = f"{deadline_days}天后截止"

    title = _build_wechat_title(event)

    lines: list[str] = []
    if event.source_origin_url:
        lines.append("原文链接已放在文章最后，方便需要查看完整公告的同学核对。")
        lines.append("")
    intro_bits = [f"今天整理一条{event.region}的" if event.region else "今天整理一条"]
    if event.category:
        intro_bits.append(event.category)
    intro_bits.append(f"信息：{org_name or event.title}。")
    known_bits: list[str] = []
    if event.job_count is not None:
        known_bits.append(f"招聘 {event.job_count} 人")
    if event.registration_deadline:
        known_bits.append(f"报名截止时间为 {event.registration_deadline_time or event.registration_deadline}")
    if known_bits:
        intro_bits.append("已识别到" + "，".join(known_bits) + "。")
    lines.append("".join(intro_bits))
    lines.append("")
    lines.append("## 先看重点")
    lines.append("")
    _append_known_line(lines, "单位", org_name)
    _append_known_line(lines, "地区", event.region)
    _append_known_line(lines, "类型", event.category)
    if event.job_count is not None:
        _append_known_line(lines, "人数", f"{event.job_count}人")
    _append_known_line(lines, "学历", event.qualification)
    _append_known_line(lines, "专业", event.major_requirements)
    _append_known_line(lines, "报名", registration_range)
    _append_known_line(lines, "考试", event.exam_date)
    if include_attachment_images:
        lines.extend(_build_attachment_image_markdown(event))
    lines.append("")
    if registration_method:
        lines.append("## 怎么报名")
        lines.append("")
        lines.append(registration_method)
        lines.append("")
    lines.append("## 公告要点")
    lines.append("")
    lines.extend(_build_summary_paragraphs(event))
    lines.append("")
    lines.append("## 报考提醒")
    lines.append("")
    if event.registration_deadline:
        if deadline_days is None:
            lines.append(f"报名截止时间为 {event.registration_deadline_time or event.registration_deadline}，建议尽早准备材料并完成报名。")
        elif deadline_days < 0:
            lines.append(f"这条公告的报名截止时间是 {event.registration_deadline_time or event.registration_deadline}，已过期，发布前请人工复核。")
        elif deadline_days == 0:
            lines.append("这条公告今天截止，适合做成紧急提醒类推文。")
        else:
            lines.append(f"距离报名截止还有 {deadline_days} 天，建议优先发布，标题和封面都突出截止时间。")
    else:
        lines.append("建议发布前人工核对报名材料、岗位条件和时间节点。")
    lines.append("")
    body = "\n".join(lines).strip() + "\n"
    return title, body


def _build_origin_first_content(event: GongkaoSelection, *, include_attachment_images: bool = False) -> tuple[str, str]:
    title = _build_wechat_title(event)
    body_parts: list[str] = []
    body_parts.append("原文链接已放在文章最后，方便需要查看完整公告的同学核对。")
    body_parts.append("")
    body_parts.append(_strip_source_noise(_html_to_plain_text(event.source_origin_html)))
    if include_attachment_images:
        attachment_block = "\n".join(_build_attachment_image_markdown(event)).strip()
        if attachment_block:
            body_parts.append("")
            body_parts.append(attachment_block)
    body_parts.append("")
    body_parts.append("原文网址：")
    body_parts.append(event.source_origin_url)
    body_parts.append("")
    return title, "\n".join(part for part in body_parts if part is not None).strip() + "\n"


def _build_attachment_image_markdown(event: GongkaoSelection) -> list[str]:
    _ensure_attachments_ready(event)
    image_paths = generate_attachment_table_images(event.source_id, max_images=2)
    if not image_paths:
        return []
    lines = ["", "## 附件岗位表", ""]
    output_dir = CONFIG.project_root / "wechat_outputs"
    for path in image_paths:
        try:
            rel_path = path.resolve().relative_to(output_dir.resolve()).as_posix()
        except Exception:
            rel_path = path.as_posix()
        lines.append(f'<img src="{rel_path}" alt="附件岗位表" style="width:100%;height:auto;">')
        lines.append("")
    return lines


def _ensure_attachments_ready(event: GongkaoSelection) -> None:
    try:
        from .gongkao_attachments import _fetch_events, process_event

        rows = _fetch_events(CONFIG.database_path, [event.source_id], 1, False)
        if not rows:
            return
        process_event(CONFIG.database_path, rows[0], max_attachments=10, use_office_com=False)
    except Exception as exc:
        print(f"[attachments] 自动处理附件失败，跳过附件图片: {exc}", flush=True)


def _build_origin_title(event: GongkaoSelection) -> str:
    base = event.title.strip() or "招聘公告"
    if len(base) <= 64:
        return base
    return base[:64]


def _format_title_deadline(event: GongkaoSelection) -> str:
    deadline = (event.registration_deadline or "").strip()[:10]
    if not deadline:
        return ""
    if event.source_origin_html and not _deadline_appears_in_origin(event, deadline):
        return ""
    days = _deadline_days(deadline)
    if days == 0:
        return "今日截止"
    if days == 1:
        return "明日截止"
    try:
        dt = datetime.strptime(deadline, "%Y-%m-%d")
        return f"{dt.month}月{dt.day}日截止"
    except Exception:
        return f"{deadline}截止"


def _deadline_appears_in_origin(event: GongkaoSelection, deadline: str) -> bool:
    text = _html_to_plain_text(event.source_origin_html or "")
    try:
        dt = datetime.strptime(deadline, "%Y-%m-%d")
    except Exception:
        return deadline in text
    variants = {
        deadline,
        deadline.replace("-", "/"),
        f"{dt.year}/{dt.month}/{dt.day}",
        f"{dt.year}/{dt.month:02d}/{dt.day:02d}",
        f"{dt.year}年{dt.month}月{dt.day}日",
        f"{dt.year}年{dt.month:02d}月{dt.day:02d}日",
        f"{dt.month}月{dt.day}日",
        f"{dt.month:02d}月{dt.day:02d}日",
    }
    normalized = re.sub(r"\s+", "", text)
    return any(item in normalized for item in variants)


def _shorten_cn(text: str, limit: int) -> str:
    text = re.sub(r"\s+", "", str(text or ""))
    return text if len(text) <= limit else text[:limit]


def _build_job_highlight(event: GongkaoSelection) -> str:
    title = str(event.title or "").strip()
    highlight = title
    display_region = _display_region(event)
    remove_patterns = [
        r"\d{4}年[^，。；\s]{0,12}",
        r"公开招聘公告$",
        r"招聘公告$",
        r"招募公告$",
        r"公告$",
        r"简章$",
        r"通知$",
        r"公开招聘",
        r"招聘",
        r"招募",
    ]
    for pattern in remove_patterns:
        highlight = re.sub(pattern, "", highlight)
    if event.region:
        highlight = highlight.replace(event.region, "")
    if display_region:
        highlight = highlight.replace(display_region, "")
        highlight = highlight.replace(f"{display_region}市", "")
    highlight = highlight.strip(" -_，。:：")

    title_for_roles = title
    role_keywords = [
        "辅导员",
        "科普辅导员",
        "教师",
        "工作人员",
        "事业单位",
        "见习",
        "医疗",
        "护士",
        "医生",
        "社区工作者",
        "警务辅助",
        "国企",
    ]
    for keyword in role_keywords:
        if keyword in title_for_roles:
            before = title_for_roles[: title_for_roles.find(keyword)]
            start = max(before.rfind("，"), before.rfind("、"), before.rfind(" "), before.rfind("："))
            candidate = title_for_roles[start + 1 : title_for_roles.find(keyword) + len(keyword)]
            candidate = re.sub(r"^\d{4}年.*?学期", "", candidate)
            candidate = candidate.strip(" -_，。:：")
            candidate = _normalize_title_highlight(candidate, display_region)
            if 2 <= len(candidate) <= 22:
                return candidate

    if highlight:
        return _shorten_cn(_normalize_title_highlight(highlight, display_region), 22)
    if event.org_name:
        return _shorten_cn(_clean_org_name(event.org_name), 18)
    return _shorten_cn(event.category or "招聘公告", 18)


def _build_wechat_title(event: GongkaoSelection) -> str:
    region = _display_region(event)
    people = f"招{event.job_count}人" if event.job_count is not None else ""
    highlight = _build_job_highlight(event)
    deadline = _format_title_deadline(event)

    first = region
    if people:
        first += people
    if first and highlight:
        title = f"{first}！{highlight}"
    else:
        title = highlight or event.title.strip() or "招聘公告"
    if deadline:
        title = f"{title}，{deadline}"
    return title[:64]


def _display_region(event: GongkaoSelection) -> str:
    title = str(event.title or "")
    match = re.search(r"([\u4e00-\u9fa5]{2,6})市", title)
    if match:
        return match.group(1)
    for city in COMMON_CITY_NAMES:
        if city in title:
            return city
    return (event.region or "").strip() or "全国"


def _normalize_title_highlight(text: str, display_region: str) -> str:
    value = str(text or "")
    for region in {display_region, f"{display_region}市"}:
        if region:
            value = value.replace(region, "")
    replacements = {
        "科学技术馆": "科技馆",
        "就业见习大学生": "",
        "就业见习": "",
        "大学生": "",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    value = re.sub(r"\s+", "", value)
    return value.strip(" -_，。:：")


def _html_to_plain_text(html: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", html or "")
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>|</div>|</li>|</h[1-6]>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _strip_source_noise(text: str) -> str:
    cleaned = str(text or "")
    for pattern in SOURCE_NOISE_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.I)
    lines: list[str] = []
    previous_blank = False
    for raw_line in cleaned.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            if not previous_blank:
                lines.append("")
            previous_blank = True
            continue
        if re.search(r"(?:招聘群|咨询群|资料群|备考群|交流群|微信群|学习群|加群|进群|领资料|扫码关注|微信群)", line, flags=re.I):
            continue
        previous_blank = False
        lines.append(line)
    return "\n".join(lines).strip()


def _parse_raw_json(raw_json: str) -> dict[str, Any]:
    if not raw_json.strip():
        return {}
    try:
        data = json.loads(raw_json)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _format_requirement(text: str, *, fallback: str) -> str:
    text = str(text or "").strip()
    return text if text else fallback


def _clean_org_name(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"20\d{2}年(?:上半年|下半年)?$", "", value).strip()
    return value or str(text or "").strip()


def _format_registration_range(start: str, deadline: str) -> str:
    start = str(start or "").strip()
    deadline = str(deadline or "").strip()
    if start and deadline:
        if start == deadline:
            return deadline
        return f"{start} 至 {deadline}"
    return start or deadline


def _append_known_line(lines: list[str], label: str, value: object) -> None:
    text = str(value or "").strip()
    if text:
        lines.append(f"- {label}：{text}")


def _trim_sentence(text: str, *, limit: int = 90) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _extract_registration_method(raw_text: str) -> str:
    text = re.sub(r"\s+", " ", str(raw_text or "")).strip()
    if not text:
        return ""
    email_hits = sorted(set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)))
    chunks: list[str] = []
    patterns = [
        r"(?:(?:报名|投递|简历).{0,100}(?:邮箱|邮件|电子邮件).{0,160})",
        r"(?:请按以下格式.{0,80})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            chunks.append(_trim_sentence(match.group(0), limit=140))
    if email_hits:
        chunks.append("投递邮箱：" + "、".join(email_hits[:6]))
    method = ""
    if chunks:
        method = chunks[0]
    if len(chunks) > 1:
        extra = chunks[1]
        if len(extra) <= 70:
            method = method + " " + extra
    deduped: list[str] = []
    for chunk in ([method] if method else []) + chunks[2:]:
        if chunk and chunk not in deduped:
            deduped.append(chunk)
    if not deduped:
        return ""
    return "\n".join(f"- {chunk}" for chunk in deduped[:3])


def _build_summary_paragraphs(event: GongkaoSelection) -> list[str]:
    title = event.title
    summary = str(event.summary or "").strip()
    lines: list[str] = []
    if event.region or event.category:
        parts = []
        if event.region:
            parts.append(f"`{event.region}`")
        if event.category:
            parts.append(f"`{event.category}`")
        lines.append(f"这是一条 {' / '.join(parts)} 信息，标题为《{title}》。")
    if summary:
        clean_summary = re.sub(r"\s+", " ", summary).strip()
        clean_summary = re.sub(r"^(?:国家|中国|北京|上海|广东|江苏|浙江).{0,20}?公告", "", clean_summary)
        lines.append(_trim_sentence(clean_summary, limit=120))
    if event.job_count is not None:
        lines.append(f"公告当前可识别到招聘人数为 `{event.job_count}` 人。")
    if event.qualification:
        lines.append(f"正文里已识别到学历门槛：`{event.qualification}`。")
    return lines or [title]


def _build_attachment_lines(raw: dict[str, Any]) -> list[str]:
    attachments = raw.get("article_attachments") or []
    if not isinstance(attachments, list):
        attachments = []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in attachments:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        oldsrc = str(item.get("oldsrc") or "").strip()
        href = str(item.get("href") or "").strip()
        label = text or oldsrc or href
        label = label.replace("附件：附件：", "附件：")
        label = re.sub(r"^附件：", "", label).strip()
        if not label or label in seen:
            continue
        seen.add(label)
        suffix = ""
        if oldsrc:
            suffix = f"（文件代号：{oldsrc}）"
        elif href and href != "javascript:;":
            suffix = f"（链接：{href}）"
        normalized.append(f"- {label}{suffix}")
    return normalized


def build_gongkao_payload(event: GongkaoSelection, *, include_attachment_images: bool = False) -> dict[str, Any]:
    title, content = _build_gongkao_wechat_content(event, include_attachment_images=include_attachment_images)
    content = _strip_source_noise(content)
    digest = content.replace("\n", " ").strip()[:120]
    raw = _parse_raw_json(event.raw_json)
    return {
        "generated_at": datetime.utcnow().isoformat(),
        "source_topic": {
            "platform": event.source_platform or "gongkao",
            "source_id": event.source_id,
            "title": event.title,
            "author_name": "公考雷达",
            "source_keyword": event.category,
            "publish_time": event.publish_time,
            "source_url": event.source_url,
        },
        "draft": {
            "title": title,
            "content": content,
            "digest": digest,
            "raw_text": event.raw_text,
            "suppress_source_meta": True,
            "wechat_preamble": [],
            "wechat_footer": [],
            "raw_json": {
                "title": title,
                "full_content": content,
                "source_url": event.source_url,
                "article_url": event.article_url,
                "source_origin_url": event.source_origin_url,
                "article_attachments": raw.get("article_attachments") or [],
                "article_attachment_details": raw.get("article_attachment_details") or [],
                "event": {
                    "region": event.region,
                    "category": event.category,
                    "org_name": event.org_name,
                    "job_count": event.job_count,
                    "qualification": event.qualification,
                    "major_requirements": event.major_requirements,
                    "registration_start": event.registration_start,
            "registration_deadline": event.registration_deadline,
            "registration_deadline_time": event.registration_deadline_time,
                    "exam_date": event.exam_date,
                    "status": event.status,
                },
            },
        },
    }


def _load_deepseek_api_key() -> str:
    value = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if value:
        return value
    api_doc = CONFIG.project_root / "api.md"
    if api_doc.exists():
        text = api_doc.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"sk-[A-Za-z0-9_\-]{16,}", text)
        if match:
            return match.group(0)
    value = (os.environ.get("XHS_LLM_API_KEY") or "").strip()
    if value:
        return value
    return ""


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{.*\}", raw, flags=re.S)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _call_quality_model(*, event: GongkaoSelection, title: str, content: str) -> dict[str, Any]:
    api_key = _load_deepseek_api_key()
    if not api_key:
        raise QualityCheckError("未找到大模型 API Key，无法完成草稿质检。请配置 DEEPSEEK_API_KEY 或检查 kaoyan_collector/api.md。")

    endpoint = (os.environ.get("DEEPSEEK_API_ENDPOINT") or "https://api.deepseek.com/chat/completions").strip()
    model = (os.environ.get("DEEPSEEK_MODEL") or "deepseek-v4-flash").strip()
    content_for_check = content[:18000]
    messages = [
        {
            "role": "system",
            "content": (
                "你是微信公众号公考公告草稿的严格质检员。只判断草稿是否完全围绕原公告。"
                "任何平台广告、课程营销、备考群、资料领取、与招聘公告无关的口号、编者主观补写、"
                "非公告来源说明，都应判定为不通过。只输出 JSON。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请检查下面草稿是否存在与公告无关内容。\n"
                "判定标准：\n"
                "1. 允许保留公告原文、报名条件、岗位信息、报名方式、原文网址，以及“原文链接在文章最后”这类核验提醒。\n"
                "2. 允许草稿标题按“地区+招聘人数+岗位亮点+报名截止”重写，前提是地区、人数、截止日期来自公告结构化字段或正文。\n"
                "3. 允许保留来源页面抽取出的公告类型、招聘人数、岗位数、报名时间摘要，例如“教师招考公告”“招聘52人20个岗位”。\n"
                "4. 允许插入由公告附件岗位表生成的图片和对应小标题“附件岗位表”。\n"
                "5. 不允许出现粉笔/培训/咨询/领资料/进群/课程等推广内容。\n"
                "6. 不允许出现公众号运营者额外补写的无关营销引导。\n"
                "7. 如果只有格式空行、重复标题，不算严重无关，但要在 warnings 里提示。\n"
                "请输出严格 JSON："
                "{\"pass\": true/false, \"issues\": [\"问题\"], \"unrelated_phrases\": [\"原文片段\"], "
                "\"warnings\": [\"提示\"], \"summary\": \"一句话结论\"}\n\n"
                f"公告标题：{event.title}\n"
                f"地区：{event.region}\n"
                f"类型：{event.category}\n"
                f"原文网址：{event.source_origin_url or event.article_url or event.source_url}\n"
                f"草稿标题：{title}\n"
                "草稿正文：\n"
                f"{content_for_check}"
            ),
        },
    ]
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 1200,
        "stream": False,
    }
    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        if exc.code in (401, 403):
            raise QualityCheckError(
                "大模型质检未能运行：DeepSeek API Key 无效或没有权限。"
                "请更新 DEEPSEEK_API_KEY，或检查 kaoyan_collector/api.md 中配置的 Key。"
            ) from exc
        raise QualityCheckError(f"大模型质检接口返回错误: HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise QualityCheckError(f"大模型质检请求失败: {exc}") from exc

    choices = data.get("choices") or []
    if not choices:
        raise QualityCheckError(f"大模型质检响应为空: {data}")
    message = choices[0].get("message") or {}
    result = _extract_json_object(str(message.get("content") or ""))
    if not result:
        raise QualityCheckError(f"大模型质检响应无法解析为 JSON: {message.get('content')}")
    return result


def _quality_report_path(source_id: str) -> Path:
    QUALITY_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return QUALITY_REPORT_DIR / f"quality_{source_id}.json"


def _run_quality_check(event: GongkaoSelection, payload: dict[str, Any]) -> Path:
    draft = payload.get("draft") or {}
    title = str(draft.get("title") or "")
    content = str(draft.get("content") or "")
    deterministic_issues: list[str] = []
    for pattern in SOURCE_NOISE_PATTERNS:
        if re.search(pattern, content, flags=re.I):
            deterministic_issues.append(f"命中规则噪声: {pattern}")

    path = _quality_report_path(event.source_id)
    try:
        model_result = _call_quality_model(event=event, title=title, content=content)
    except QualityCheckError as exc:
        report = {
            "checked_at": datetime.utcnow().isoformat(),
            "source_id": event.source_id,
            "title": title,
            "pass": False,
            "deterministic_issues": deterministic_issues,
            "model_result": None,
            "error": str(exc),
        }
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Quality report saved: {path}")
        raise RuntimeError(f"{exc}\n草稿已停止导出/提交，避免未经质检的内容进入微信公众号。") from exc

    passed = bool(model_result.get("pass")) and not deterministic_issues
    report = {
        "checked_at": datetime.utcnow().isoformat(),
        "source_id": event.source_id,
        "title": title,
        "pass": passed,
        "deterministic_issues": deterministic_issues,
        "model_result": model_result,
    }
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Quality report saved: {path}")
    if not passed:
        issues = list(deterministic_issues)
        issues.extend(str(item) for item in (model_result.get("issues") or []))
        phrases = model_result.get("unrelated_phrases") or []
        if phrases:
            issues.append("无关片段: " + "；".join(str(item) for item in phrases[:5]))
        raise RuntimeError("草稿大模型质检未通过，已停止提交。\n" + "\n".join(f"- {item}" for item in issues))
    print("Quality check passed: 未发现与公告无关内容。")
    return path


def _save_payload(payload: dict[str, Any], source_id: str) -> Path:
    draft_dir = CONFIG.project_root / "drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)
    path = draft_dir / f"gongkao_wechat_{source_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and publish a WeChat draft from gongkao_events.")
    parser.add_argument("--db", default=str(CONFIG.database_path), help="SQLite database path.")
    parser.add_argument("--topic_id", default="", help="Optional gongkao source_id to force a specific event.")
    parser.add_argument("--category", default="事业单位", help="Optional category filter, e.g. 事业单位 / 国企 / 公务员.")
    parser.add_argument("--region", default="", help="Optional region filter.")
    parser.add_argument("--status", default="", help="Status filter.")
    parser.add_argument("--require_deadline", action="store_true", help="Only allow items with a parsed registration deadline.")
    parser.add_argument("--days_to_deadline", type=int, default=7, help="Only allow items whose deadline is within N days. Use 0 to disable.")
    parser.add_argument("--author", default="岸上信息站", help="WeChat author/account name.")
    parser.add_argument("--wechat_theme", default="tech", choices=["tech", "minimal", "business"], help="Theme used for WeChat HTML conversion.")
    parser.add_argument(
        "--wechat_cover",
        default=str(CONFIG.workspace_root / "wechat_cover.png")
        if (CONFIG.workspace_root / "wechat_cover.png").exists()
        else str(CONFIG.workspace_root / "考试通知.png"),
        help="Optional custom cover image path.",
    )
    parser.add_argument("--skip_publish", action="store_true", help="Only generate payload/markdown/html without pushing draft to WeChat.")
    parser.add_argument("--submit_publish", action="store_true", help="Create a WeChat draft and immediately submit it for publishing.")
    parser.add_argument("--skip_quality_check", action="store_true", help="Skip LLM quality check before exporting/publishing.")
    parser.add_argument("--include_attachment_images", action="store_true", help="Render parsed attachment job tables as images in the article.")
    args = parser.parse_args()

    event = pick_gongkao_event(
        Path(args.db),
        topic_id=args.topic_id,
        category=args.category.strip(),
        region=args.region.strip(),
        status=args.status.strip(),
        require_deadline=args.require_deadline,
        days_to_deadline=args.days_to_deadline,
    )
    payload = build_gongkao_payload(event, include_attachment_images=args.include_attachment_images)
    quality_report_path = None
    if not args.skip_quality_check:
        try:
            quality_report_path = _run_quality_check(event, payload)
        except RuntimeError as exc:
            print(f"ERROR: {exc}")
            raise SystemExit(1) from None
    payload_path = _save_payload(payload, event.source_id)
    preview_path = save_wechat_payload_preview(payload, source_id=event.source_id)
    markdown_path = export_wechat_markdown(payload, source_id=event.source_id, account_name=args.author.strip())
    html_path = convert_markdown_to_wechat_html(markdown_path, theme=args.wechat_theme)

    print(f"Selected event: {event.title}")
    if quality_report_path:
        print(f"Quality check report: {quality_report_path}")
    print(f"Payload saved: {payload_path}")
    print(f"WeChat preview saved: {preview_path}")
    print(f"Markdown generated: {markdown_path}")
    print(f"HTML generated: {html_path}")

    if args.skip_publish:
        print("Skip publish enabled. Pipeline stopped before WeChat draft push.")
        return

    draft = payload.get("draft") or {}
    publish_html_to_wechat_draft(
        title=str(draft.get("title") or ""),
        html_path=html_path,
        author=args.author.strip(),
        cover_path=args.wechat_cover.strip(),
        digest=str(draft.get("digest") or ""),
        submit_publish=args.submit_publish,
        source_platform=event.source_platform or "gongkao",
        source_id=event.source_id,
    )
    if args.submit_publish:
        print("WeChat live publish submit step completed.")
    else:
        print("WeChat draft publish step completed.")


if __name__ == "__main__":
    main()
