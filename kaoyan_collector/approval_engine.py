# -*- coding: utf-8 -*-
"""人机协同审批状态机（Human-in-the-Loop Approval Engine）

在 Agent 自动流程的关键节点设置审批门，由人工确认后继续：

  DRAFT -> PENDING_APPROVAL -> APPROVED -> PUBLISHED
       \-> REJECTED (含原因) -> REWORK -> PENDING_APPROVAL
       \-> CANCELLED

支持审批超时自动处理、审批原因记录、驳回后重新生成策略。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from .config import CONFIG
from .schema import init_db


class ApprovalStatus(Enum):
    PENDING = "pending"           # 等待审批
    APPROVED = "approved"         # 已批准
    REJECTED = "rejected"         # 已驳回
    CANCELLED = "cancelled"      # 已取消
    EXPIRED = "expired"           # 已超时


class PublishStatus(Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"
    REWORK = "rework"


# 状态转换规则
STATE_TRANSITIONS = {
    PublishStatus.DRAFT: [PublishStatus.PENDING_APPROVAL],
    PublishStatus.PENDING_APPROVAL: [PublishStatus.APPROVED, PublishStatus.REJECTED, PublishStatus.PENDING_APPROVAL],
    PublishStatus.REJECTED: [PublishStatus.REWORK],
    PublishStatus.REWORK: [PublishStatus.PENDING_APPROVAL],
    PublishStatus.APPROVED: [PublishStatus.PUBLISHED],
    PublishStatus.PUBLISHED: [],
}


@dataclass
class ApprovalRecord:
    approval_id: str
    source_id: str
    title: str
    status: str
    draft_html_path: str = ""
    draft_preview_path: str = ""
    cover_path: str = ""
    created_at: str = ""
    updated_at: str = ""
    submitted_at: str = ""
    approved_at: str = ""
    approved_by: str = ""
    rejected_at: str = ""
    rejected_by: str = ""
    reject_reason: str = ""
    rework_count: int = 0
    max_rework: int = 3
    expires_at: str = ""
    auto_action_on_expire: str = "cancelled"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = db_path or CONFIG.database_path
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _utc_now() -> str:
    return datetime.utcnow().isoformat()


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS approval_records (
            approval_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            title TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            draft_html_path TEXT,
            draft_preview_path TEXT,
            cover_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            submitted_at TEXT,
            approved_at TEXT,
            approved_by TEXT,
            rejected_at TEXT,
            rejected_by TEXT,
            reject_reason TEXT,
            rework_count INTEGER NOT NULL DEFAULT 0,
            max_rework INTEGER NOT NULL DEFAULT 3,
            expires_at TEXT,
            auto_action_on_expire TEXT NOT NULL DEFAULT 'cancelled'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_approval_status ON approval_records(status, created_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_approval_source ON approval_records(source_id)
    """)
    conn.commit()


class ApprovalEngine:
    """人机协同审批引擎。

    用法:
        engine = ApprovalEngine()
        rec = engine.submit_for_approval("source_123", "标题", draft_html="/path/to/file.html")
        engine.approve(rec.approval_id, approved_by="管理员")
        # 或
        engine.reject(rec.approval_id, rejected_by="管理员", reason="标题需修改")
    """

    def __init__(self, db_path: Path | None = None, expire_hours: int = 48) -> None:
        self.db_path = db_path or CONFIG.database_path
        self.expire_hours = expire_hours

    def submit_for_approval(
        self,
        source_id: str,
        title: str,
        draft_html_path: str = "",
        draft_preview_path: str = "",
        cover_path: str = "",
        max_rework: int = 3,
    ) -> ApprovalRecord:
        """提交草稿到审批队列。"""
        now = _utc_now()
        expires = (datetime.utcnow() + timedelta(hours=self.expire_hours)).isoformat()
        approval_id = uuid.uuid4().hex[:12]

        record = ApprovalRecord(
            approval_id=approval_id,
            source_id=source_id,
            title=title,
            status=PublishStatus.PENDING_APPROVAL.value,
            draft_html_path=draft_html_path,
            draft_preview_path=draft_preview_path,
            cover_path=cover_path,
            created_at=now,
            updated_at=now,
            submitted_at=now,
            max_rework=max_rework,
            expires_at=expires,
        )

        with _connect(self.db_path) as conn:
            _ensure_table(conn)
            conn.execute(
                """INSERT INTO approval_records
                   (approval_id, source_id, title, status, draft_html_path, draft_preview_path,
                    cover_path, created_at, updated_at, submitted_at, max_rework, expires_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record.approval_id, record.source_id, record.title, record.status,
                 record.draft_html_path, record.draft_preview_path, record.cover_path,
                 record.created_at, record.updated_at, record.submitted_at,
                 record.max_rework, record.expires_at),
            )
            conn.commit()

        print(f"[Approval] 提交审批: {approval_id} | {title[:50]} | 超时 {self.expire_hours}h")
        return record

    def approve(self, approval_id: str, approved_by: str = "admin") -> ApprovalRecord:
        """批准草稿，允许发布。"""
        return self._transition(approval_id, PublishStatus.APPROVED, operator=approved_by)

    def reject(
        self, approval_id: str, rejected_by: str = "admin", reason: str = "未说明"
    ) -> ApprovalRecord:
        """驳回草稿，附原因。"""
        return self._transition(
            approval_id, PublishStatus.REJECTED,
            operator=rejected_by, reason=reason,
        )

    def mark_rework(self, approval_id: str) -> ApprovalRecord:
        """标记为重新生成中。"""
        return self._transition(approval_id, PublishStatus.REWORK)

    def mark_published(self, approval_id: str) -> ApprovalRecord:
        """标记为已发布。"""
        return self._transition(approval_id, PublishStatus.PUBLISHED)

    def cancel(self, approval_id: str) -> ApprovalRecord:
        """取消审批。"""
        return self._transition(approval_id, PublishStatus.CANCELLED)

    def get_pending(self, limit: int = 50) -> list[ApprovalRecord]:
        """获取待审批列表。"""
        self._expire_overdue()
        with _connect(self.db_path) as conn:
            _ensure_table(conn)
            rows = conn.execute(
                """SELECT * FROM approval_records
                   WHERE status = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (PublishStatus.PENDING_APPROVAL.value, limit),
            ).fetchall()
        return [ApprovalRecord(**dict(r)) for r in rows]

    def get_by_source(self, source_id: str) -> ApprovalRecord | None:
        """根据 source_id 查询审批记录。"""
        with _connect(self.db_path) as conn:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT * FROM approval_records WHERE source_id = ? ORDER BY created_at DESC LIMIT 1",
                (source_id,),
            ).fetchone()
        return ApprovalRecord(**dict(row)) if row else None

    def get_all(self, status: str = "", limit: int = 100) -> list[ApprovalRecord]:
        """获取审批记录列表。"""
        with _connect(self.db_path) as conn:
            _ensure_table(conn)
            if status:
                rows = conn.execute(
                    "SELECT * FROM approval_records WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM approval_records ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [ApprovalRecord(**dict(r)) for r in rows]

    def stats(self) -> dict[str, int]:
        """审批统计数据。"""
        with _connect(self.db_path) as conn:
            _ensure_table(conn)
            pending = conn.execute(
                "SELECT count(*) c FROM approval_records WHERE status=?",
                (PublishStatus.PENDING_APPROVAL.value,),
            ).fetchone()["c"]
            approved = conn.execute(
                "SELECT count(*) c FROM approval_records WHERE status=?",
                (PublishStatus.APPROVED.value,),
            ).fetchone()["c"]
            rejected = conn.execute(
                "SELECT count(*) c FROM approval_records WHERE status=?",
                (PublishStatus.REJECTED.value,),
            ).fetchone()["c"]
            published = conn.execute(
                "SELECT count(*) c FROM approval_records WHERE status=?",
                (PublishStatus.PUBLISHED.value,),
            ).fetchone()["c"]
        return {
            "pending": pending,
            "approved": approved,
            "rejected": rejected,
            "published": published,
        }

    # ── 内部 ──

    def _transition(
        self,
        approval_id: str,
        target: PublishStatus,
        operator: str = "",
        reason: str = "",
    ) -> ApprovalRecord:
        with _connect(self.db_path) as conn:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT * FROM approval_records WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"审批记录不存在: {approval_id}")

            record = dict(row)
            current = PublishStatus(record["status"])
            allowed = STATE_TRANSITIONS.get(current, [])
            if target not in allowed:
                raise ValueError(f"不允许的状态转换: {current.value} -> {target.value}")

            now = _utc_now()
            updates = {"status": target.value, "updated_at": now}

            if target == PublishStatus.APPROVED:
                updates["approved_at"] = now
                updates["approved_by"] = operator
            elif target == PublishStatus.REJECTED:
                updates["rejected_at"] = now
                updates["rejected_by"] = operator
                updates["reject_reason"] = reason

            set_clause = ", ".join(f"{k}=?" for k in updates)
            params = list(updates.values()) + [approval_id]
            conn.execute(
                f"UPDATE approval_records SET {set_clause} WHERE approval_id = ?",
                params,
            )
            conn.commit()

            # 如果驳回，检查是否需要标记 rework
            if target == PublishStatus.REJECTED:
                rework_count = record["rework_count"] + 1
                if rework_count <= record["max_rework"]:
                    conn.execute(
                        "UPDATE approval_records SET rework_count = ? WHERE approval_id = ?",
                        (rework_count, approval_id),
                    )
                    conn.commit()
                    record["rework_count"] = rework_count
                    print(f"[Approval] 驳回 {approval_id}: {reason} (第{rework_count}次)")
                    print(f"[Approval] 建议: 根据驳回原因修改后重新提交")
                else:
                    conn.execute(
                        "UPDATE approval_records SET status = ?, rework_count = ?, updated_at = ? WHERE approval_id = ?",
                        (PublishStatus.CANCELLED.value, rework_count, now, approval_id),
                    )
                    conn.commit()
                    record["status"] = PublishStatus.CANCELLED.value
                    print(f"[Approval] 驳回 {approval_id}: 已达最大重试次数({record['max_rework']})，自动取消")

            record.update(updates)
        return ApprovalRecord(**record)

    def _expire_overdue(self) -> int:
        """处理超时审批。"""
        now = _utc_now()
        with _connect(self.db_path) as conn:
            _ensure_table(conn)
            cursor = conn.execute(
                """UPDATE approval_records
                   SET status = 'expired', updated_at = ?
                   WHERE status = ? AND expires_at IS NOT NULL AND expires_at < ?""",
                (now, PublishStatus.PENDING_APPROVAL.value, now),
            )
            conn.commit()
            return cursor.rowcount or 0


# ── CLI 测试 ──

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="人机协同审批状态机")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()

    if args.test:
        engine = ApprovalEngine(expire_hours=1)
        print("=== 测试审批流程 ===\n")

        # 提交
        rec = engine.submit_for_approval("test_001", "洛阳招9人！科技馆科普辅导员", draft_html_path="/tmp/test.html")
        print(f"1. 提交: {rec.approval_id} status={rec.status}\n")

        # 驳回
        rec = engine.reject(rec.approval_id, rejected_by="管理员", reason="标题太长，建议缩短到 20 字以内")
        print(f"2. 驳回: status={rec.status} reason='{rec.reject_reason}'\n")

        # 重新提交（模拟 rework 后重新提交）
        rec2 = engine.submit_for_approval("test_001", "洛阳招9人！科技馆", draft_html_path="/tmp/test_v2.html")
        print(f"3. 重新提交: {rec2.approval_id} status={rec2.status}\n")

        # 批准
        rec2 = engine.approve(rec2.approval_id, approved_by="管理员")
        print(f"4. 批准: status={rec2.status} approved_at={rec2.approved_at}\n")

        # 统计
        stats = engine.stats()
        print(f"5. 统计: {stats}")
    else:
        print("使用 --test 运行测试流程")


if __name__ == "__main__":
    main()
