from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def _to_int(value: Any) -> int | None:
    if value in (None, "", "None"):
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _to_iso_datetime(value: Any) -> str | None:
    text = _to_text(value)
    if not text:
        return None

    number = _to_int(value)
    if number is not None:
        if number > 10_000_000_000:
            number = number / 1000
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue

    return text


def _compact_json(value: Any) -> str:
    if value in (None, "", []):
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def normalize_content(platform: str, raw: dict[str, Any]) -> dict[str, Any]:
    handlers = {
        "xhs": _normalize_xhs,
        "dy": _normalize_douyin,
        "bili": _normalize_bilibili,
        "wb": _normalize_weibo,
        "zhihu": _normalize_zhihu,
        "tieba": _normalize_tieba,
        "ks": _normalize_kuaishou,
    }
    if platform not in handlers:
        raise ValueError(f"Unsupported platform: {platform}")

    normalized = handlers[platform](raw)
    normalized["platform"] = platform
    normalized["raw_json"] = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
    return normalized


def _normalize_xhs(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": _to_text(raw.get("note_id")),
        "source_type": _to_text(raw.get("type")),
        "title": _to_text(raw.get("title")),
        "content": _to_text(raw.get("desc")),
        "summary": _to_text(raw.get("desc")),
        "author_name": _to_text(raw.get("nickname")),
        "author_id": _to_text(raw.get("user_id")),
        "author_profile_url": "",
        "publish_time": _to_iso_datetime(raw.get("time")),
        "like_count": _to_int(raw.get("liked_count")),
        "comment_count": _to_int(raw.get("comment_count")),
        "share_count": _to_int(raw.get("share_count")),
        "collect_count": _to_int(raw.get("collected_count")),
        "view_count": None,
        "source_url": _to_text(raw.get("note_url")),
        "cover_url": _to_text(raw.get("image_list")).split(",")[0] if _to_text(raw.get("image_list")) else "",
        "tags": _to_text(raw.get("tag_list")),
        "source_keyword": _to_text(raw.get("source_keyword")),
    }


def _normalize_douyin(raw: dict[str, Any]) -> dict[str, Any]:
    cover_url = _to_text(raw.get("cover_url"))
    if not cover_url:
        cover_url = _to_text(raw.get("note_download_url")).split(",")[0] if _to_text(raw.get("note_download_url")) else ""
    return {
        "source_id": _to_text(raw.get("aweme_id")),
        "source_type": _to_text(raw.get("aweme_type")),
        "title": _to_text(raw.get("title")),
        "content": _to_text(raw.get("desc")),
        "summary": _to_text(raw.get("desc")),
        "author_name": _to_text(raw.get("nickname")),
        "author_id": _to_text(raw.get("user_id")),
        "author_profile_url": "",
        "publish_time": _to_iso_datetime(raw.get("create_time")),
        "like_count": _to_int(raw.get("liked_count")),
        "comment_count": _to_int(raw.get("comment_count")),
        "share_count": _to_int(raw.get("share_count")),
        "collect_count": _to_int(raw.get("collected_count")),
        "view_count": None,
        "source_url": _to_text(raw.get("aweme_url")),
        "cover_url": cover_url,
        "tags": "",
        "source_keyword": _to_text(raw.get("source_keyword")),
    }


def _normalize_bilibili(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": _to_text(raw.get("video_id")),
        "source_type": _to_text(raw.get("video_type")),
        "title": _to_text(raw.get("title")),
        "content": _to_text(raw.get("desc")),
        "summary": _to_text(raw.get("desc")),
        "author_name": _to_text(raw.get("nickname")),
        "author_id": _to_text(raw.get("user_id")),
        "author_profile_url": "",
        "publish_time": _to_iso_datetime(raw.get("create_time")),
        "like_count": _to_int(raw.get("liked_count")),
        "comment_count": _to_int(raw.get("video_comment")),
        "share_count": _to_int(raw.get("video_share_count")),
        "collect_count": _to_int(raw.get("video_favorite_count")),
        "view_count": _to_int(raw.get("video_play_count")),
        "source_url": _to_text(raw.get("video_url")),
        "cover_url": _to_text(raw.get("video_cover_url")),
        "tags": "",
        "source_keyword": _to_text(raw.get("source_keyword")),
    }


def _normalize_weibo(raw: dict[str, Any]) -> dict[str, Any]:
    content = _to_text(raw.get("content"))
    return {
        "source_id": _to_text(raw.get("note_id")),
        "source_type": "post",
        "title": content[:40],
        "content": content,
        "summary": content[:160],
        "author_name": _to_text(raw.get("nickname")),
        "author_id": _to_text(raw.get("user_id")),
        "author_profile_url": _to_text(raw.get("profile_url")),
        "publish_time": _to_iso_datetime(raw.get("create_time") or raw.get("create_date_time")),
        "like_count": _to_int(raw.get("liked_count")),
        "comment_count": _to_int(raw.get("comments_count")),
        "share_count": _to_int(raw.get("shared_count")),
        "collect_count": None,
        "view_count": None,
        "source_url": _to_text(raw.get("note_url")),
        "cover_url": "",
        "tags": "",
        "source_keyword": _to_text(raw.get("source_keyword")),
    }


def _normalize_zhihu(raw: dict[str, Any]) -> dict[str, Any]:
    content = _to_text(raw.get("content_text"))
    desc = _to_text(raw.get("desc"))
    return {
        "source_id": _to_text(raw.get("content_id")),
        "source_type": _to_text(raw.get("content_type")),
        "title": _to_text(raw.get("title")),
        "content": content or desc,
        "summary": desc or content[:160],
        "author_name": _to_text(raw.get("user_nickname")),
        "author_id": _to_text(raw.get("user_id")),
        "author_profile_url": _to_text(raw.get("user_link")),
        "publish_time": _to_iso_datetime(raw.get("created_time")),
        "like_count": _to_int(raw.get("voteup_count")),
        "comment_count": _to_int(raw.get("comment_count")),
        "share_count": None,
        "collect_count": None,
        "view_count": None,
        "source_url": _to_text(raw.get("content_url")),
        "cover_url": "",
        "tags": _compact_json(
            {"question_id": _to_text(raw.get("question_id")), "url_token": _to_text(raw.get("user_url_token"))}
        ),
        "source_keyword": _to_text(raw.get("source_keyword")),
    }


def _normalize_tieba(raw: dict[str, Any]) -> dict[str, Any]:
    title = _to_text(raw.get("title"))
    desc = _to_text(raw.get("desc"))
    return {
        "source_id": _to_text(raw.get("note_id")),
        "source_type": "thread",
        "title": title,
        "content": desc or title,
        "summary": desc or title[:160],
        "author_name": _to_text(raw.get("user_nickname")),
        "author_id": "",
        "author_profile_url": _to_text(raw.get("user_link")),
        "publish_time": _to_iso_datetime(raw.get("publish_time")),
        "like_count": None,
        "comment_count": _to_int(raw.get("total_replay_num")),
        "share_count": None,
        "collect_count": None,
        "view_count": None,
        "source_url": _to_text(raw.get("note_url")),
        "cover_url": _to_text(raw.get("user_avatar")),
        "tags": _compact_json({"tieba_name": _to_text(raw.get("tieba_name"))}),
        "source_keyword": _to_text(raw.get("source_keyword")),
    }


def _normalize_kuaishou(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": _to_text(raw.get("video_id")),
        "source_type": _to_text(raw.get("video_type")),
        "title": _to_text(raw.get("title")),
        "content": _to_text(raw.get("desc")),
        "summary": _to_text(raw.get("desc")),
        "author_name": _to_text(raw.get("nickname")),
        "author_id": _to_text(raw.get("user_id")),
        "author_profile_url": "",
        "publish_time": _to_iso_datetime(raw.get("create_time")),
        "like_count": _to_int(raw.get("liked_count")),
        "comment_count": None,
        "share_count": None,
        "collect_count": None,
        "view_count": _to_int(raw.get("viewd_count")),
        "source_url": _to_text(raw.get("video_url")),
        "cover_url": _to_text(raw.get("video_cover_url")),
        "tags": "",
        "source_keyword": _to_text(raw.get("source_keyword")),
    }
