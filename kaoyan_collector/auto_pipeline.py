from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .collect import run_platform_collection
from .config import CONFIG
from .generate_draft import _build_generation_topic, _load_llm_service, _normalize_draft_payload, _pick_topic
from .ingest import discover_latest_content_file, ingest_file
from .store import ContentStore
from .wechat_pipeline import (
    convert_markdown_to_wechat_html,
    export_wechat_markdown,
    get_wechat_publish_fields,
    publish_html_to_wechat_draft,
    save_wechat_payload_preview,
)


def _load_publisher_modules():
    repo_root = CONFIG.workspace_root
    publisher_root = repo_root / "xhs_ai_publisher"
    if str(publisher_root) not in sys.path:
        sys.path.insert(0, str(publisher_root))

    from src.core.services.system_image_template_service import system_image_template_service  # type: ignore
    from src.core.write_xiaohongshu import XiaohongshuPoster  # type: ignore

    return system_image_template_service, XiaohongshuPoster


def _save_draft(payload: dict[str, Any], source_id: str) -> Path:
    draft_dir = CONFIG.project_root / "drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)
    path = draft_dir / f"draft_{source_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _generate_images_for_draft(payload: dict[str, Any]) -> list[str]:
    system_image_template_service, _ = _load_publisher_modules()

    draft = payload.get("draft") or {}
    title = str(draft.get("title") or "").strip()
    content = str(draft.get("content") or "").strip()
    content_pages = draft.get("content_pages") or []
    if not isinstance(content_pages, list):
        content_pages = []

    generated = system_image_template_service.generate_post_images(
        title=title,
        content=content,
        content_pages=content_pages,
        page_count=max(3, len(content_pages) or 3),
    )
    if not generated:
        raise RuntimeError("图片生成失败，无法继续自动发布。")

    cover_path, content_paths = generated
    images = [cover_path] + list(content_paths or [])
    images = [str(Path(img)) for img in images if str(img).strip()]
    if not images:
        raise RuntimeError("图片生成结果为空，无法继续自动发布。")
    return images


def _publish_to_wechat(
    payload: dict[str, Any],
    *,
    source_id: str,
    author: str,
    theme: str,
    cover_path: str = "",
) -> tuple[Path, Path]:
    title, digest = get_wechat_publish_fields(payload)
    draft = payload.get("draft") or {}
    content = str(draft.get("content") or "").strip()
    if not title or not content:
        raise RuntimeError("草稿缺少标题或正文，无法推送到微信公众号。")

    markdown_path = export_wechat_markdown(payload, source_id=source_id, account_name=author)
    html_path = convert_markdown_to_wechat_html(markdown_path, theme=theme)
    publish_html_to_wechat_draft(
        title=title,
        html_path=html_path,
        author=author,
        cover_path=cover_path,
        digest=digest,
    )
    return markdown_path, html_path


async def _publish_to_xiaohongshu(
    payload: dict[str, Any],
    images: list[str],
    auto_publish: bool,
    *,
    phone: str = "",
    country_code: str = "+86",
) -> None:
    _, XiaohongshuPoster = _load_publisher_modules()

    draft = payload.get("draft") or {}
    title = str(draft.get("title") or "").strip()
    content = str(draft.get("content") or "").strip()
    if not title or not content:
        raise RuntimeError("草稿缺少标题或正文，无法发布。")

    poster = XiaohongshuPoster()
    try:
        await poster.initialize()
        if phone.strip():
            await poster.login(phone.strip(), country_code.strip() or "+86")
        await poster.post_article(title, content, images=images, auto_publish=auto_publish)
        if not auto_publish:
            print("已到达小红书最终操作界面。请手动点击“发布”或“暂存离开”，完成后回到终端按回车结束。")
            try:
                input()
            except EOFError:
                print("当前终端不支持交互等待，请手动处理后直接结束进程。")
    finally:
        await poster.close(force=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the end-to-end kaoyan automation pipeline.")
    parser.add_argument("--platforms", default="xhs", help="Comma-separated platforms to collect from.")
    parser.add_argument("--keywords", default=",".join(CONFIG.default_keywords), help="Comma-separated keywords.")
    parser.add_argument("--crawler_max_notes_count", type=int, default=20, help="Maximum notes per platform crawl.")
    parser.add_argument("--get_comment", action="store_true", help="Enable comment collection during crawl.")
    parser.add_argument("--headless", action="store_true", help="Enable headless crawl mode.")
    parser.add_argument("--login_type", default="qrcode", help="MediaCrawler login type.")
    parser.add_argument("--crawler_type", default="search", help="MediaCrawler crawler type.")
    parser.add_argument("--python", default="python", help="Python executable used for MediaCrawler.")
    parser.add_argument("--skip_collect", action="store_true", help="Skip crawl step and reuse existing raw data.")
    parser.add_argument("--topic_id", default="", help="Optional source_id to force a specific candidate topic.")
    parser.add_argument("--topic_platform", default="xhs", help="Candidate topic platform filter.")
    parser.add_argument("--topic_keyword", default="", help="Candidate topic keyword filter.")
    parser.add_argument("--days", type=int, default=30, help="Only keep candidate topics from the last N days.")
    parser.add_argument("--min_relevance_score", type=int, default=8, help="Minimum relevance score for candidate query.")
    parser.add_argument("--min_like_count", type=int, default=20, help="Minimum likes threshold for candidate query.")
    parser.add_argument("--min_comment_count", type=int, default=5, help="Minimum comments threshold for candidate query.")
    parser.add_argument("--header_title", default="计算机考研", help="Header title for draft generation.")
    parser.add_argument("--author", default="研路笔记", help="Author/persona for draft generation.")
    parser.add_argument("--skip_publish", action="store_true", help="Stop after draft and image generation.")
    parser.add_argument("--auto_publish", action="store_true", help="Experimental: auto click the final Xiaohongshu action. Omit this flag to stop at the final screen for manual confirmation.")
    parser.add_argument("--phone", default="", help="Optional Xiaohongshu login phone for automatic login recovery.")
    parser.add_argument("--country_code", default="+86", help="Phone country code used for automatic login recovery.")
    parser.add_argument("--publish_target", default="xhs", choices=["xhs", "wechat"], help="Choose the publishing target.")
    parser.add_argument("--wechat_theme", default="tech", choices=["tech", "minimal", "business"], help="Theme used for WeChat HTML conversion.")
    parser.add_argument("--wechat_cover", default="", help="Optional custom cover image path for WeChat draft publishing.")
    args = parser.parse_args()

    store = ContentStore(CONFIG.database_path)
    platforms = [item.strip() for item in args.platforms.split(",") if item.strip()]

    if not args.skip_collect:
        for platform in platforms:
            run_platform_collection(
                platform=platform,
                keywords=args.keywords,
                crawler_max_notes_count=args.crawler_max_notes_count,
                get_comment=args.get_comment,
                headless=args.headless,
                login_type=args.login_type,
                crawler_type=args.crawler_type,
                python_executable=args.python,
            )
            file_path = discover_latest_content_file(CONFIG.raw_data_root, platform)
            imported = ingest_file(file_path, platform, store)
            print(f"[{platform}] imported {imported} items from {file_path.name}")

    selected_topic = _pick_topic(
        db_path=CONFIG.database_path,
        topic_id=args.topic_id.strip(),
        platform=args.topic_platform.strip(),
        keyword=args.topic_keyword.strip(),
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
        raise SystemExit(f"模型配置不可用：{reason}")

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
    draft_path = _save_draft(payload, str(selected_topic.get("source_id") or "unknown"))
    print(f"Draft saved: {draft_path}")
    wechat_preview_path = save_wechat_payload_preview(payload, source_id=str(selected_topic.get("source_id") or "unknown"))

    images: list[str] = []
    if args.publish_target == "xhs":
        images = _generate_images_for_draft(payload)
        print(f"Generated images: {len(images)}")
    else:
        print(f"WeChat payload preview saved: {wechat_preview_path}")

    if args.skip_publish:
        print("Skip publish enabled. Pipeline stopped after draft/image generation.")
        return

    if args.publish_target == "wechat":
        print("准备推送到微信公众号草稿箱。请先确认 ~/.wechat-publisher/config.json 已配置 appid/appsecret，且公众号接口权限与 IP 白名单已就绪。")
        markdown_path, html_path = _publish_to_wechat(
            payload,
            source_id=str(selected_topic.get("source_id") or "unknown"),
            author=args.author.strip(),
            theme=args.wechat_theme,
            cover_path=args.wechat_cover.strip(),
        )
        print(f"WeChat markdown generated: {markdown_path}")
        print(f"WeChat HTML generated: {html_path}")
        print("WeChat draft publish step completed.")
        return

    asyncio.run(
        _publish_to_xiaohongshu(
            payload,
            images,
            auto_publish=args.auto_publish,
            phone=args.phone,
            country_code=args.country_code,
        )
    )
    print("Publish step completed.")


if __name__ == "__main__":
    main()
