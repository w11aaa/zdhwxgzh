from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .schema import init_db


class ContentStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        init_db(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def create_crawl_run(
        self,
        *,
        platform: str,
        keywords: str,
        crawler_type: str,
        save_data_path: str,
        status: str,
    ) -> int:
        started_at = datetime.utcnow().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO crawl_runs(platform, keywords, crawler_type, save_data_path, status, started_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (platform, keywords, crawler_type, save_data_path, status, started_at),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def finish_crawl_run(
        self,
        run_id: int,
        *,
        status: str,
        source_file: str | None = None,
        error_message: str | None = None,
    ) -> None:
        finished_at = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE crawl_runs
                SET status = ?, source_file = ?, error_message = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, source_file, error_message, finished_at, run_id),
            )
            conn.commit()

    def upsert_content_item(self, item: dict[str, object]) -> None:
        imported_at = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO content_items(
                    platform, source_id, source_type, title, content, summary,
                    author_name, author_id, author_profile_url, publish_time,
                    like_count, comment_count, share_count, collect_count, view_count,
                    source_url, cover_url, tags, source_keyword, is_relevant,
                    relevance_score, relevance_label, relevance_reason, raw_json, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, source_id) DO UPDATE SET
                    source_type=excluded.source_type,
                    title=excluded.title,
                    content=excluded.content,
                    summary=excluded.summary,
                    author_name=excluded.author_name,
                    author_id=excluded.author_id,
                    author_profile_url=excluded.author_profile_url,
                    publish_time=excluded.publish_time,
                    like_count=excluded.like_count,
                    comment_count=excluded.comment_count,
                    share_count=excluded.share_count,
                    collect_count=excluded.collect_count,
                    view_count=excluded.view_count,
                    source_url=excluded.source_url,
                    cover_url=excluded.cover_url,
                    tags=excluded.tags,
                    source_keyword=excluded.source_keyword,
                    is_relevant=excluded.is_relevant,
                    relevance_score=excluded.relevance_score,
                    relevance_label=excluded.relevance_label,
                    relevance_reason=excluded.relevance_reason,
                    raw_json=excluded.raw_json,
                    imported_at=excluded.imported_at
                """,
                (
                    item["platform"],
                    item["source_id"],
                    item.get("source_type"),
                    item.get("title"),
                    item.get("content"),
                    item.get("summary"),
                    item.get("author_name"),
                    item.get("author_id"),
                    item.get("author_profile_url"),
                    item.get("publish_time"),
                    item.get("like_count"),
                    item.get("comment_count"),
                    item.get("share_count"),
                    item.get("collect_count"),
                    item.get("view_count"),
                    item.get("source_url"),
                    item.get("cover_url"),
                    item.get("tags"),
                    item.get("source_keyword"),
                    item.get("is_relevant", 0),
                    item.get("relevance_score", 0),
                    item.get("relevance_label", "unknown"),
                    item.get("relevance_reason", ""),
                    item["raw_json"],
                    imported_at,
                ),
            )
            conn.commit()

    def upsert_gongkao_event(self, item: dict[str, object]) -> None:
        imported_at = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gongkao_events(
                    source_platform, source_id, title, region, category,
                    fenbi_exam_type_id, fenbi_exam_type_name, org_name,
                    job_count, qualification, major_requirements, registration_start,
                    registration_deadline, registration_deadline_time, exam_date, status, publish_time, source_url,
                    article_url, source_origin_url, source_origin_text, source_origin_html,
                    origin_search_status, origin_search_attempts, origin_last_checked_at,
                    summary, raw_text, raw_json, hash_id, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_platform, source_id) DO UPDATE SET
                    title=excluded.title,
                    region=excluded.region,
                    category=excluded.category,
                    fenbi_exam_type_id=excluded.fenbi_exam_type_id,
                    fenbi_exam_type_name=excluded.fenbi_exam_type_name,
                    org_name=excluded.org_name,
                    job_count=excluded.job_count,
                    qualification=excluded.qualification,
                    major_requirements=excluded.major_requirements,
                    registration_start=excluded.registration_start,
                    registration_deadline=excluded.registration_deadline,
                    registration_deadline_time=excluded.registration_deadline_time,
                    exam_date=excluded.exam_date,
                    status=excluded.status,
                    publish_time=excluded.publish_time,
                    source_url=excluded.source_url,
                    article_url=excluded.article_url,
                    source_origin_url=CASE
                        WHEN coalesce(excluded.source_origin_url, '') <> '' THEN excluded.source_origin_url
                        ELSE gongkao_events.source_origin_url
                    END,
                    source_origin_text=CASE
                        WHEN coalesce(excluded.source_origin_text, '') <> '' THEN excluded.source_origin_text
                        ELSE gongkao_events.source_origin_text
                    END,
                    source_origin_html=CASE
                        WHEN coalesce(excluded.source_origin_html, '') <> '' THEN excluded.source_origin_html
                        ELSE gongkao_events.source_origin_html
                    END,
                    origin_search_status=CASE
                        WHEN coalesce(excluded.origin_search_status, 'pending') = 'pending'
                             AND coalesce(gongkao_events.origin_search_status, '') <> ''
                        THEN gongkao_events.origin_search_status
                        ELSE excluded.origin_search_status
                    END,
                    origin_search_attempts=MAX(
                        coalesce(gongkao_events.origin_search_attempts, 0),
                        coalesce(excluded.origin_search_attempts, 0)
                    ),
                    origin_last_checked_at=CASE
                        WHEN coalesce(excluded.origin_last_checked_at, '') <> '' THEN excluded.origin_last_checked_at
                        ELSE gongkao_events.origin_last_checked_at
                    END,
                    summary=excluded.summary,
                    raw_text=excluded.raw_text,
                    raw_json=excluded.raw_json,
                    hash_id=excluded.hash_id,
                    imported_at=excluded.imported_at
                """,
                (
                    item["source_platform"],
                    item["source_id"],
                    item["title"],
                    item.get("region"),
                    item.get("category"),
                    item.get("fenbi_exam_type_id"),
                    item.get("fenbi_exam_type_name"),
                    item.get("org_name"),
                    item.get("job_count"),
                    item.get("qualification"),
                    item.get("major_requirements"),
                    item.get("registration_start"),
                    item.get("registration_deadline"),
                    item.get("registration_deadline_time"),
                    item.get("exam_date"),
                    item.get("status", "unknown"),
                    item.get("publish_time"),
                    item["source_url"],
                    item.get("article_url"),
                    item.get("source_origin_url"),
                    item.get("source_origin_text"),
                    item.get("source_origin_html"),
                    item.get("origin_search_status", "pending"),
                    item.get("origin_search_attempts", 0),
                    item.get("origin_last_checked_at"),
                    item.get("summary"),
                    item.get("raw_text"),
                    item["raw_json"],
                    item["hash_id"],
                    imported_at,
                ),
            )
            conn.commit()

    def replace_gongkao_event_attachments(
        self,
        *,
        source_platform: str,
        source_id: str,
        attachments: list[dict[str, object]],
    ) -> None:
        imported_at = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM gongkao_event_attachments
                WHERE event_source_platform = ? AND event_source_id = ?
                """,
                (source_platform, source_id),
            )
            for item in attachments:
                conn.execute(
                    """
                    INSERT INTO gongkao_event_attachments(
                        event_source_platform, event_source_id, attachment_scope, name,
                        href, oldsrc, file_ext, parse_status, parsed_text, raw_json, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_platform,
                        source_id,
                        item.get("attachment_scope", "article"),
                        item.get("name"),
                        item.get("href"),
                        item.get("oldsrc"),
                        item.get("file_ext"),
                        item.get("parse_status", "metadata_only"),
                        item.get("parsed_text"),
                        item.get("raw_json", "{}"),
                        imported_at,
                    ),
                )
            conn.commit()

    def get_gongkao_event_search_state(self, *, source_platform: str, source_id: str) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    origin_search_status,
                    origin_search_attempts,
                    origin_last_checked_at,
                    source_origin_url,
                    source_origin_text,
                    source_origin_html
                FROM gongkao_events
                WHERE source_platform = ? AND source_id = ?
                """,
                (source_platform, source_id),
            ).fetchone()
            return dict(row) if row else None
