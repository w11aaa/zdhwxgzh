from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from .config import CONFIG


def build_query() -> str:
    return """
    SELECT
        platform,
        source_id,
        title,
        substr(content, 1, 200) AS content_preview,
        author_name,
        publish_time,
        like_count,
        comment_count,
        share_count,
        collect_count,
        source_url,
        source_keyword,
        relevance_score,
        relevance_label,
        (
            coalesce(like_count, 0) * 1.0 +
            coalesce(comment_count, 0) * 3.0 +
            coalesce(share_count, 0) * 2.0 +
            coalesce(collect_count, 0) * 2.0
        ) AS engagement_score
    FROM content_items
    WHERE is_relevant = 1
      AND relevance_score >= ?
      AND (
        ? = 0 OR
        publish_time >= datetime('now', '-' || ? || ' days')
      )
      AND (
        ? = '' OR
        platform = ?
      )
      AND (
        ? = '' OR
        title LIKE '%' || ? || '%' OR
        content LIKE '%' || ? || '%' OR
        source_keyword LIKE '%' || ? || '%'
      )
      AND (
        coalesce(like_count, 0) >= ? OR
        coalesce(comment_count, 0) >= ?
      )
    ORDER BY engagement_score DESC, publish_time DESC
    LIMIT ?
    """


def fetch_topics(
    db_path: Path,
    *,
    limit: int,
    min_relevance_score: int,
    min_like_count: int,
    min_comment_count: int,
    days: int,
    platform: str,
    keyword: str,
) -> list[dict[str, object]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            build_query(),
            (
                min_relevance_score,
                days,
                days,
                platform,
                platform,
                keyword,
                keyword,
                keyword,
                keyword,
                min_like_count,
                min_comment_count,
                limit,
            ),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def format_text(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "No matching topics found."

    lines: list[str] = []
    for index, row in enumerate(rows, start=1):
        lines.append(f"[{index}] {row['title']}")
        lines.append(
            " | ".join(
                [
                    f"platform={row['platform']}",
                    f"keyword={row['source_keyword']}",
                    f"relevance={row['relevance_score']}",
                    f"engagement={int(row['engagement_score'] or 0)}",
                    f"likes={row['like_count'] or 0}",
                    f"comments={row['comment_count'] or 0}",
                ]
            )
        )
        lines.append(f"author={row['author_name'] or ''}")
        lines.append(f"publish_time={row['publish_time'] or ''}")
        lines.append(f"url={row['source_url'] or ''}")
        lines.append(f"preview={row['content_preview'] or ''}")
        lines.append("")
    return "\n".join(lines).rstrip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Query candidate kaoyan topics from the unified SQLite store.")
    parser.add_argument("--db", default=str(CONFIG.database_path), help="SQLite database path.")
    parser.add_argument("--limit", type=int, default=20, help="Maximum number of topics to return.")
    parser.add_argument("--min_relevance_score", type=int, default=6, help="Minimum relevance score.")
    parser.add_argument("--min_like_count", type=int, default=20, help="Minimum likes threshold.")
    parser.add_argument("--min_comment_count", type=int, default=5, help="Minimum comments threshold.")
    parser.add_argument("--days", type=int, default=30, help="Only keep content from the last N days. Use 0 to disable.")
    parser.add_argument("--platform", default="", help="Optional platform filter, e.g. xhs or wb.")
    parser.add_argument("--keyword", default="", help="Optional text filter against title/content/source_keyword.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    args = parser.parse_args()

    rows = fetch_topics(
        Path(args.db),
        limit=args.limit,
        min_relevance_score=args.min_relevance_score,
        min_like_count=args.min_like_count,
        min_comment_count=args.min_comment_count,
        days=args.days,
        platform=args.platform.strip(),
        keyword=args.keyword.strip(),
    )

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    print(format_text(rows))


if __name__ == "__main__":
    main()
