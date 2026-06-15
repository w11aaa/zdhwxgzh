from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import CONFIG
from .schema import init_db


@dataclass
class EventRecommendation:
    source_id: str
    title: str
    region: str
    category: str
    status: str
    job_count: int
    registration_deadline: str
    deadline_countdown: str
    source_origin_url: str
    attachment_count: int
    attachment_downloaded_count: int
    attachment_parsed_count: int
    publish_record_count: int
    score: int
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _deadline_days(deadline: str) -> int | None:
    if not deadline:
        return None
    try:
        return (datetime.strptime(deadline[:10], "%Y-%m-%d").date() - date.today()).days
    except Exception:
        return None


def _countdown(deadline: str) -> str:
    days = _deadline_days(deadline)
    if days is None:
        return ""
    if days < 0:
        return f"已截止{abs(days)}天"
    if days == 0:
        return "今日截止"
    return f"{days}天后"


def _repair_statuses(conn: sqlite3.Connection) -> None:
    today = date.today().isoformat()
    conn.execute(
        """
        UPDATE gongkao_events
        SET status = '报名结束'
        WHERE coalesce(registration_deadline, '') <> ''
          AND registration_deadline < ?
          AND status <> '报名结束'
        """,
        (today,),
    )
    conn.execute(
        """
        UPDATE gongkao_events
        SET status = '即将开始'
        WHERE coalesce(registration_start, '') <> ''
          AND registration_start > ?
          AND status <> '即将开始'
          AND (coalesce(registration_deadline, '') = '' OR registration_deadline >= ?)
        """,
        (today, today),
    )
    conn.execute(
        """
        UPDATE gongkao_events
        SET status = '正在报名'
        WHERE coalesce(registration_deadline, '') <> ''
          AND registration_deadline >= ?
          AND (coalesce(registration_start, '') = '' OR registration_start <= ?)
          AND status <> '正在报名'
        """,
        (today, today),
    )
    conn.commit()


def _score_row(row: sqlite3.Row) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    status = str(row["status"] or "")
    deadline = str(row["registration_deadline"] or "")
    days = _deadline_days(deadline)
    job_count = int(row["job_count"] or 0)
    attachment_count = int(row["attachment_count"] or 0)
    attachment_downloaded_count = int(row["attachment_downloaded_count"] or 0)
    attachment_parsed_count = int(row["attachment_parsed_count"] or 0)
    publish_record_count = int(row["publish_record_count"] or 0)

    if status == "正在报名":
        score += 35
        reasons.append("正在报名")
    elif status == "即将开始":
        score += 14
        reasons.append("即将开始，可提前准备")
    else:
        score -= 60
        reasons.append("非有效报名状态")

    if days is not None:
        if days < 0:
            score -= 100
            reasons.append("报名已截止")
        elif days == 0:
            score += 24
            reasons.append("今日截止，时效性强")
        elif days <= 3:
            score += 22
            reasons.append(f"{days}天后截止，适合提醒")
        elif days <= 7:
            score += 16
            reasons.append(f"{days}天后截止")
        elif days <= 30:
            score += 8
            reasons.append(f"{days}天后截止")
    else:
        score -= 10
        reasons.append("缺少截止日期")

    if job_count >= 100:
        score += 18
        reasons.append(f"招聘{job_count}人，规模较大")
    elif job_count >= 30:
        score += 14
        reasons.append(f"招聘{job_count}人")
    elif job_count > 0:
        score += 8
        reasons.append(f"招聘{job_count}人")

    if row["source_origin_url"]:
        score += 12
        reasons.append("已找到原公告")
    else:
        score -= 8
        reasons.append("未找到原公告")

    if attachment_parsed_count:
        score += 12
        reasons.append("已有可用附件解析")
    elif attachment_downloaded_count:
        score += 8
        reasons.append("已有附件下载")
    elif attachment_count:
        score += 5
        reasons.append("已有附件线索")

    category = str(row["category"] or row["fenbi_exam_type_name"] or "")
    if category in {"公务员", "事业单位", "教师", "国企", "医疗", "选调"}:
        score += 6
        reasons.append(f"{category}类目清晰")

    if publish_record_count:
        score -= 80
        reasons.append("已有公众号处理记录，避免重复发布")

    return max(0, min(100, score)), reasons


def recommend_events(
    db_path: Path | None = None,
    *,
    limit: int = 10,
    include_published: bool = False,
    status: str = "正在报名",
    category: str = "",
    region: str = "",
) -> list[EventRecommendation]:
    db_path = db_path or CONFIG.database_path
    init_db(db_path)
    clauses = ["1=1"]
    params: list[Any] = []
    if status:
        clauses.append("e.status = ?")
        params.append(status)
    if category:
        clauses.append("(e.category = ? OR e.fenbi_exam_type_name = ?)")
        params.extend([category, category])
    if region:
        clauses.append("e.region = ?")
        params.append(region)
    if not include_published:
        clauses.append(
            """
            NOT EXISTS (
                SELECT 1 FROM wechat_publish_records w
                WHERE w.source_id = e.source_id
                  AND w.status IN ('draft_created', 'draft_created_publish_failed', 'submitted', 'published')
            )
            """
        )

    query = f"""
        SELECT
            e.source_id,
            e.title,
            coalesce(e.region, '') AS region,
            coalesce(e.category, '') AS category,
            coalesce(e.fenbi_exam_type_name, '') AS fenbi_exam_type_name,
            e.status,
            coalesce(e.job_count, 0) AS job_count,
            coalesce(e.registration_deadline, '') AS registration_deadline,
            coalesce(e.source_origin_url, '') AS source_origin_url,
            (
                SELECT count(*) FROM gongkao_event_attachments a
                WHERE a.event_source_platform = e.source_platform
                  AND a.event_source_id = e.source_id
            ) AS attachment_count,
            (
                SELECT count(*) FROM gongkao_event_attachments a
                WHERE a.event_source_platform = e.source_platform
                  AND a.event_source_id = e.source_id
                  AND coalesce(a.download_status, '') = 'downloaded'
            ) AS attachment_downloaded_count,
            (
                SELECT count(*) FROM gongkao_event_attachments a
                WHERE a.event_source_platform = e.source_platform
                  AND a.event_source_id = e.source_id
                  AND coalesce(a.parse_status, '') = 'parsed'
            ) AS attachment_parsed_count,
            (
                SELECT count(*) FROM wechat_publish_records w
                WHERE w.source_id = e.source_id
            ) AS publish_record_count
        FROM gongkao_events e
        WHERE {' AND '.join(clauses)}
        ORDER BY
            CASE WHEN coalesce(e.registration_deadline, '') = '' THEN 1 ELSE 0 END,
            e.registration_deadline ASC,
            coalesce(e.job_count, 0) DESC,
            e.imported_at DESC
        LIMIT ?
    """
    params.append(max(1, min(limit * 5, 500)))
    with _connect(db_path) as conn:
        _repair_statuses(conn)
        rows = conn.execute(query, params).fetchall()

    recommendations: list[EventRecommendation] = []
    for row in rows:
        score, reasons = _score_row(row)
        recommendations.append(
            EventRecommendation(
                source_id=str(row["source_id"] or ""),
                title=str(row["title"] or ""),
                region=str(row["region"] or ""),
                category=str(row["category"] or row["fenbi_exam_type_name"] or ""),
                status=str(row["status"] or ""),
                job_count=int(row["job_count"] or 0),
                registration_deadline=str(row["registration_deadline"] or ""),
                deadline_countdown=_countdown(str(row["registration_deadline"] or "")),
                source_origin_url=str(row["source_origin_url"] or ""),
                attachment_count=int(row["attachment_count"] or 0),
                attachment_downloaded_count=int(row["attachment_downloaded_count"] or 0),
                attachment_parsed_count=int(row["attachment_parsed_count"] or 0),
                publish_record_count=int(row["publish_record_count"] or 0),
                score=score,
                reasons=reasons,
            )
        )
    recommendations.sort(key=lambda item: (item.score, item.job_count), reverse=True)
    return recommendations[:limit]


def main() -> None:
    parser = argparse.ArgumentParser(description="Recommend gongkao events for WeChat publishing.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--include_published", action="store_true")
    parser.add_argument("--status", default="正在报名")
    parser.add_argument("--category", default="")
    parser.add_argument("--region", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    items = recommend_events(
        limit=args.limit,
        include_published=args.include_published,
        status=args.status,
        category=args.category,
        region=args.region,
    )
    if args.json:
        print(json.dumps([item.to_dict() for item in items], ensure_ascii=False, indent=2))
        return
    for index, item in enumerate(items, 1):
        print(f"{index}. [{item.score}] {item.source_id} {item.title}")
        print(f"   {item.region} {item.category} {item.deadline_countdown} 招{item.job_count}人")
        print("   推荐理由：" + "；".join(item.reasons))


if __name__ == "__main__":
    main()
