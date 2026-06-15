from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import CONFIG
from .query_topics import fetch_topics


def _load_llm_service():
    repo_root = CONFIG.workspace_root
    publisher_root = repo_root / "xhs_ai_publisher"
    if str(publisher_root) not in sys.path:
        sys.path.insert(0, str(publisher_root))

    from src.core.services.llm_service import LLMService, LLMServiceError  # type: ignore
    from src.config.config import Config  # type: ignore

    return LLMService, LLMServiceError, Config


def _pick_topic(
    *,
    db_path: Path,
    topic_id: str,
    platform: str,
    keyword: str,
    days: int,
    min_relevance_score: int,
    min_like_count: int,
    min_comment_count: int,
) -> dict[str, Any]:
    rows = fetch_topics(
        db_path,
        limit=50,
        min_relevance_score=min_relevance_score,
        min_like_count=min_like_count,
        min_comment_count=min_comment_count,
        days=days,
        platform=platform,
        keyword=keyword,
    )

    if not rows:
        raise ValueError("没有找到可生成草稿的候选选题，请先放宽筛选条件或补充采集数据。")

    if not topic_id:
        return rows[0]

    for row in rows:
        if str(row.get("source_id") or "") == topic_id:
            return row

    raise ValueError(f"没有找到 source_id={topic_id} 的候选选题。")


def _build_generation_topic(row: dict[str, Any]) -> str:
    title = str(row.get("title") or "").strip()
    preview = str(row.get("content_preview") or "").strip()
    source_keyword = str(row.get("source_keyword") or "").strip()
    platform = str(row.get("platform") or "").strip()
    author = str(row.get("author_name") or "").strip()
    likes = row.get("like_count") or 0
    comments = row.get("comment_count") or 0

    parts = [
        f"请参考这条来自{platform}平台的热门内容，改写成适合小红书发布的计算机考研图文笔记。",
        f"原标题：{title}",
        f"主题关键词：{source_keyword}",
        f"作者：{author}",
        f"互动数据：点赞{likes}，评论{comments}",
        "内容摘要：",
        preview,
        "要求保留有价值的信息结构，但不要照抄原文；输出更适合小红书用户阅读的表达。",
    ]
    return "\n".join(part for part in parts if part)


def _normalize_draft_payload(selected_topic: dict[str, Any], llm_response: Any) -> dict[str, Any]:
    raw_json = getattr(llm_response, "raw_json", None) or {}
    hashtags = raw_json.get("hashtags") or raw_json.get("tags") or []
    if isinstance(hashtags, str):
        hashtags = [item.strip() for item in hashtags.split() if item.strip()]
    if not isinstance(hashtags, list):
        hashtags = []

    content_pages = raw_json.get("content_pages") or []
    if not isinstance(content_pages, list):
        content_pages = []

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "source_topic": {
            "platform": selected_topic.get("platform"),
            "source_id": selected_topic.get("source_id"),
            "title": selected_topic.get("title"),
            "author_name": selected_topic.get("author_name"),
            "source_keyword": selected_topic.get("source_keyword"),
            "publish_time": selected_topic.get("publish_time"),
            "source_url": selected_topic.get("source_url"),
            "engagement_score": selected_topic.get("engagement_score"),
            "relevance_score": selected_topic.get("relevance_score"),
        },
        "draft": {
            "title": getattr(llm_response, "title", ""),
            "content": getattr(llm_response, "content", ""),
            "hashtags": hashtags,
            "content_pages": content_pages,
            "raw_text": getattr(llm_response, "raw_text", ""),
            "raw_json": raw_json,
        },
    }


def _default_output_path(topic: dict[str, Any]) -> Path:
    draft_dir = CONFIG.project_root / "drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)
    source_id = str(topic.get("source_id") or "unknown").strip() or "unknown"
    return draft_dir / f"draft_{source_id}.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Xiaohongshu draft content from candidate kaoyan topics.")
    parser.add_argument("--db", default=str(CONFIG.database_path), help="SQLite database path.")
    parser.add_argument("--topic_id", default="", help="Optional source_id to generate from.")
    parser.add_argument("--platform", default="xhs", help="Optional platform filter for candidate query.")
    parser.add_argument("--keyword", default="", help="Optional keyword filter for candidate query.")
    parser.add_argument("--days", type=int, default=30, help="Only keep content from the last N days.")
    parser.add_argument("--min_relevance_score", type=int, default=8, help="Minimum relevance score for candidate query.")
    parser.add_argument("--min_like_count", type=int, default=20, help="Minimum likes threshold.")
    parser.add_argument("--min_comment_count", type=int, default=5, help="Minimum comments threshold.")
    parser.add_argument("--header_title", default="计算机考研", help="Optional eyebrow/header title for generation.")
    parser.add_argument("--author", default="研路笔记", help="Optional author/persona name for generation.")
    parser.add_argument("--output", default="", help="Optional output JSON path.")
    args = parser.parse_args()

    selected_topic = _pick_topic(
        db_path=Path(args.db),
        topic_id=args.topic_id.strip(),
        platform=args.platform.strip(),
        keyword=args.keyword.strip(),
        days=args.days,
        min_relevance_score=args.min_relevance_score,
        min_like_count=args.min_like_count,
        min_comment_count=args.min_comment_count,
    )

    LLMService, LLMServiceError, Config = _load_llm_service()
    llm_service = LLMService(Config())
    model_config = llm_service.config.get_model_config()
    ok, reason = llm_service.is_model_configured(model_config)
    if not ok:
        raise SystemExit(
            "当前还不能生成草稿，因为 xhs_ai_publisher 的模型配置不可用："
            f"{reason}。请先在 ~/.xhs_system/settings.json 或环境变量中配置模型。"
        )

    generation_topic = _build_generation_topic(selected_topic)

    try:
        llm_response = llm_service.generate_xiaohongshu_content(
            topic=generation_topic,
            header_title=args.header_title.strip(),
            author=args.author.strip(),
        )
    except LLMServiceError as exc:
        raise SystemExit(f"草稿生成失败：{exc}") from exc

    payload = _normalize_draft_payload(selected_topic, llm_response)
    output_path = Path(args.output).resolve() if args.output else _default_output_path(selected_topic)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Draft generated: {output_path}")
    print(f"Source topic: {selected_topic.get('title')}")
    print(f"Draft title: {payload['draft']['title']}")


if __name__ == "__main__":
    main()
