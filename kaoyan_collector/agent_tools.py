from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class AgentToolSpec:
    name: str
    module: str
    description: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    risk_level: str
    human_review: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


AGENT_TOOLS: tuple[AgentToolSpec, ...] = (
    AgentToolSpec(
        name="crawl_fenbi_tool",
        module="kaoyan_collector.fenbi_crawler",
        description="按粉笔考试类型、报名状态和页码采集考公公告并入库。",
        inputs=("category/type", "page", "max_items", "enroll_status"),
        outputs=("gongkao_events", "raw_json", "source_origin_html"),
        risk_level="low",
        human_review=False,
    ),
    AgentToolSpec(
        name="crawl_gongkaoleida_tool",
        module="kaoyan_collector.gongkaoleida_crawler",
        description="从公考雷达采集公告，作为备用聚合信息源。",
        inputs=("category", "page", "max_items", "use_origin_search"),
        outputs=("gongkao_events", "origin_search_status"),
        risk_level="low",
        human_review=False,
    ),
    AgentToolSpec(
        name="attachment_scan_tool",
        module="kaoyan_collector.gongkao_attachments",
        description="扫描公告原文中的附件链接，只登记元数据，不下载文件。",
        inputs=("topic_id/topic_ids", "limit", "metadata_only"),
        outputs=("gongkao_event_attachments",),
        risk_level="low",
        human_review=False,
    ),
    AgentToolSpec(
        name="job_table_download_tool",
        module="kaoyan_collector.gongkao_attachments",
        description="筛选岗位表候选附件，下载并解析为结构化文本。",
        inputs=("topic_id/topic_ids", "max_attachments", "job_tables_only"),
        outputs=("local_path", "parsed_text", "parsed_json"),
        risk_level="medium",
        human_review=False,
    ),
    AgentToolSpec(
        name="event_recommendation_tool",
        module="kaoyan_collector.gongkao_recommender",
        description="基于状态、截止时间、招聘人数、原公告、附件和发布记录为公告打分推荐。",
        inputs=("limit", "status", "category", "region", "include_published"),
        outputs=("score", "reasons", "recommended_events"),
        risk_level="low",
        human_review=False,
    ),
    AgentToolSpec(
        name="today_draft_planner_tool",
        module="kaoyan_collector.gongkao_today_agent",
        description="自动选择今日推荐公告，处理岗位表附件，并批量生成公众号草稿或预览。",
        inputs=("count", "days_to_deadline", "include_attachment_images", "skip_publish"),
        outputs=("selected_ids", "wechat_outputs", "quality_reports", "wechat_publish_records"),
        risk_level="high",
        human_review=True,
    ),
    AgentToolSpec(
        name="attachment_image_tool",
        module="kaoyan_collector.attachment_images",
        description="把已解析的岗位表附件渲染为公众号可插入的图片。",
        inputs=("source_id", "max_images", "max_rows"),
        outputs=("attachment_table_png",),
        risk_level="medium",
        human_review=True,
    ),
    AgentToolSpec(
        name="wechat_article_generate_tool",
        module="kaoyan_collector.gongkao_wechat_pipeline",
        description="根据公告原文、结构化字段和附件图片生成公众号文章。",
        inputs=("topic_id", "days_to_deadline", "include_attachment_images"),
        outputs=("markdown", "html", "payload_json", "quality_report"),
        risk_level="medium",
        human_review=True,
    ),
    AgentToolSpec(
        name="wechat_draft_submit_tool",
        module="wechat_article_skills.wechat-draft-publisher.publisher",
        description="调用微信公众号接口提交草稿箱。",
        inputs=("title", "content_html", "cover", "digest", "author"),
        outputs=("media_id", "wechat_publish_records"),
        risk_level="high",
        human_review=True,
    ),
)


def list_agent_tools() -> list[dict[str, Any]]:
    return [tool.to_dict() for tool in AGENT_TOOLS]


def get_agent_tool(name: str) -> AgentToolSpec | None:
    for tool in AGENT_TOOLS:
        if tool.name == name:
            return tool
    return None
