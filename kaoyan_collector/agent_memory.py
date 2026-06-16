# -*- coding: utf-8 -*-
"""Agent 记忆与学习系统（Agent Memory & Learning）

为 Agent 提供短期记忆（会话上下文）和长期记忆（历史经验），
支持记忆的创建、检索、衰减和学习优化。

记忆类型：
- short_term：当前任务会话中的临时上下文
- long_term：跨会话持久化的经验教训
- pattern：从多次运行中学习到的模式规律

能力：
- 自动从 Agent 运行结果中提取经验教训
- 基于关键词 + 相似度检索相关记忆
- 记忆衰减：长期未使用的记忆权重降低
- 反馈学习：用户反馈（质检通过/驳回）更新记忆权重
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import CONFIG


def _utc_now() -> str:
    return datetime.utcnow().isoformat()


# ── 数据模型 ──────────────────────────────────────────────────


@dataclass
class AgentMemory:
    """一条 Agent 记忆。"""

    memory_id: str
    memory_type: str  # short_term / long_term / pattern
    category: str  # quality_issue / title_strategy / error_diagnosis / ...
    content: str  # 记忆正文
    keywords: str  # 逗号分隔的关键词
    source_run_id: int  # 来源 Agent Run ID
    weight: float = 1.0  # 权重（0-1），越高越重要
    decay_rate: float = 0.01  # 每次衰减比例
    last_accessed_at: str = ""
    created_at: str = ""
    access_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── DDL ───────────────────────────────────────────────────────


AGENT_MEMORY_DDL = """
CREATE TABLE IF NOT EXISTS agent_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT UNIQUE NOT NULL,
    memory_type TEXT NOT NULL DEFAULT 'long_term',
    category TEXT NOT NULL DEFAULT 'general',
    content TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    source_run_id INTEGER,
    weight REAL NOT NULL DEFAULT 1.0,
    decay_rate REAL NOT NULL DEFAULT 0.01,
    last_accessed_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_memory_type ON agent_memories(memory_type);
CREATE INDEX IF NOT EXISTS idx_memory_category ON agent_memories(category);
CREATE INDEX IF NOT EXISTS idx_memory_weight ON agent_memories(weight DESC);
CREATE INDEX IF NOT EXISTS idx_memory_keywords ON agent_memories(keywords);

CREATE TABLE IF NOT EXISTS agent_learning_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_name TEXT UNIQUE NOT NULL,
    pattern_type TEXT NOT NULL,
    description TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    evidence_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


# ── 记忆引擎 ──────────────────────────────────────────────────


class AgentMemoryEngine:
    """Agent 记忆与学习引擎。

    用法:
        engine = AgentMemoryEngine()
        engine.remember(
            memory_type="long_term",
            category="quality_issue",
            content="质检发现：报名已过期时标题不应包含截止日期提示",
            keywords="质检,截止日期,标题,过期",
        )
        memories = engine.recall("截止日期 标题 质检")
        engine.learn_from_run(run_id, feedback="passed")
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or CONFIG.database_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(AGENT_MEMORY_DDL)
            conn.commit()

    # ── 记忆 CRUD ──────────────────────────────────────────

    def remember(
        self,
        *,
        memory_type: str = "long_term",
        category: str = "general",
        content: str,
        keywords: str = "",
        source_run_id: int = 0,
        weight: float = 1.0,
    ) -> str:
        """创建一条新记忆。"""
        import hashlib
        import uuid

        memory_id = hashlib.md5(
            f"{memory_type}:{category}:{content[:100]}".encode()
        ).hexdigest()[:16]

        now = _utc_now()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id, weight, access_count FROM agent_memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            if existing:
                # 增强已有记忆的权重
                new_weight = min(1.0, float(existing["weight"]) + 0.1)
                conn.execute(
                    """UPDATE agent_memories
                       SET weight = ?, last_accessed_at = ?, access_count = access_count + 1
                       WHERE memory_id = ?""",
                    (new_weight, now, memory_id),
                )
            else:
                conn.execute(
                    """INSERT INTO agent_memories
                       (memory_id, memory_type, category, content, keywords,
                        source_run_id, weight, last_accessed_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        memory_id, memory_type, category, content, keywords,
                        source_run_id, weight, now, now,
                    ),
                )
            conn.commit()
        return memory_id

    def recall(
        self,
        query: str,
        *,
        memory_type: str = "",
        category: str = "",
        limit: int = 10,
        min_weight: float = 0.1,
    ) -> list[AgentMemory]:
        """检索相关记忆。

        基于关键词匹配 + 权重排序。
        """
        query_keywords = set(query.replace("，", ",").replace(" ", ",").split(","))
        query_keywords = {kw.strip() for kw in query_keywords if kw.strip()}

        clauses = ["1=1"]
        params: list[Any] = []

        if memory_type:
            clauses.append("memory_type = ?")
            params.append(memory_type)
        if category:
            clauses.append("category = ?")
            params.append(category)
        clauses.append("weight >= ?")
        params.append(min_weight)

        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT * FROM agent_memories
                    WHERE {' AND '.join(clauses)}
                    ORDER BY
                        CASE WHEN memory_type = 'pattern' THEN 0 ELSE 1 END,
                        weight DESC,
                        access_count DESC
                    LIMIT ?""",
                [*params, limit * 3],  # 先取 3 倍，再二次排序
            ).fetchall()

        # 关键词匹配打分
        scored: list[tuple[float, AgentMemory]] = []
        for row in rows:
            mem = AgentMemory(
                memory_id=row["memory_id"],
                memory_type=row["memory_type"],
                category=row["category"],
                content=row["content"],
                keywords=row["keywords"],
                source_run_id=row["source_run_id"] or 0,
                weight=row["weight"],
                decay_rate=row["decay_rate"],
                last_accessed_at=row["last_accessed_at"],
                created_at=row["created_at"],
                access_count=row["access_count"],
            )
            mem_keywords = set(mem.keywords.replace("，", ",").split(","))
            mem_keywords = {kw.strip() for kw in mem_keywords if kw.strip()}
            overlap = len(query_keywords & mem_keywords)
            # 内容中也搜一下
            for qk in query_keywords:
                if qk in mem.content:
                    overlap += 0.5
            score = overlap * mem.weight
            scored.append((score, mem))

        scored.sort(key=lambda x: x[0], reverse=True)

        # 更新 access 计数
        now = _utc_now()
        with self._connect() as conn:
            for _, mem in scored[:limit]:
                conn.execute(
                    """UPDATE agent_memories
                       SET last_accessed_at = ?, access_count = access_count + 1
                       WHERE memory_id = ?""",
                    (now, mem.memory_id),
                )
            conn.commit()

        return [mem for _, mem in scored[:limit]]

    def learn_from_run(
        self,
        run_id: int,
        *,
        feedback: str = "",  # "passed" / "failed" / 具体反馈
        auto_extract: bool = True,
    ) -> list[str]:
        """从一次 Agent 运行中学习。

        自动提取经验教训并创建/更新记忆。
        """
        new_memory_ids: list[str] = []

        with self._connect() as conn:
            run = conn.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if not run:
                return new_memory_ids

            steps = conn.execute(
                "SELECT * FROM agent_steps WHERE run_id = ? ORDER BY step_index",
                (run_id,),
            ).fetchall()

        # 从失败步骤中提取教训
        for step in steps:
            if step["status"] == "failed":
                error_msg = str(step.get("error_message") or step.get("observation") or "")
                tool = step["tool_name"]

                if "质检" in error_msg or "quality" in error_msg.lower():
                    new_memory_ids.append(
                        self.remember(
                            memory_type="long_term",
                            category="quality_issue",
                            content=f"[{tool}] 质检失败经验：{error_msg[:250]}",
                            keywords=f"质检,失败,{tool}",
                            source_run_id=run_id,
                            weight=0.8,
                        )
                    )
                elif "timeout" in error_msg.lower() or "超时" in error_msg:
                    new_memory_ids.append(
                        self.remember(
                            memory_type="pattern",
                            category="error_diagnosis",
                            content=f"[{tool}] 超时模式：{error_msg[:250]}",
                            keywords=f"超时,{tool},性能",
                            source_run_id=run_id,
                            weight=0.6,
                        )
                    )
                elif any(kw in error_msg for kw in ["401", "token", "auth", "credential"]):
                    new_memory_ids.append(
                        self.remember(
                            memory_type="long_term",
                            category="error_diagnosis",
                            content=f"[{tool}] 认证失败经验：{error_msg[:250]}",
                            keywords=f"认证,Token,失败,{tool}",
                            source_run_id=run_id,
                            weight=0.9,
                        )
                    )

        # 从成功运行中提取模式
        if feedback == "passed" or (run["status"] == "completed" and not feedback):
            # 记录成功执行路径
            tool_sequence = " → ".join(
                f"{s['tool_name']}({s['elapsed_seconds']}s)"
                for s in steps
                if s["status"] == "completed"
            )
            if tool_sequence:
                new_memory_ids.append(
                    self.remember(
                        memory_type="pattern",
                        category="success_pattern",
                        content=f"成功路径: {tool_sequence}",
                        keywords="成功,模式,路径",
                        source_run_id=run_id,
                        weight=0.4,
                    )
                )

        # 反馈学习
        if feedback == "passed":
            # 增强相关记忆的权重
            with self._connect() as conn:
                conn.execute(
                    """UPDATE agent_memories
                       SET weight = MIN(1.0, weight + 0.15)
                       WHERE source_run_id = ? AND memory_type = 'pattern'""",
                    (run_id,),
                )
                conn.commit()

        return new_memory_ids

    def decay_old_memories(self, days_threshold: int = 30) -> int:
        """衰减旧记忆。"""
        threshold = (datetime.utcnow() - timedelta(days=days_threshold)).isoformat()
        updated = 0
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE agent_memories
                   SET weight = MAX(0.05, weight - decay_rate)
                   WHERE last_accessed_at < ? AND memory_type = 'short_term'
                     AND weight > 0.05""",
                (threshold,),
            )
            updated = cursor.rowcount or 0

            # 删除权重极低的短时记忆
            conn.execute(
                "DELETE FROM agent_memories WHERE weight < 0.06 AND memory_type = 'short_term'"
            )
            conn.commit()
        return updated

    def get_memory_stats(self) -> dict[str, Any]:
        """获取记忆统计。"""
        with self._connect() as conn:
            total = conn.execute("SELECT count(*) FROM agent_memories").fetchone()[0]
            by_type = {
                row["memory_type"]: row["cnt"]
                for row in conn.execute(
                    "SELECT memory_type, count(*) cnt FROM agent_memories GROUP BY memory_type"
                ).fetchall()
            }
            by_category = {
                row["category"]: row["cnt"]
                for row in conn.execute(
                    "SELECT category, count(*) cnt FROM agent_memories GROUP BY category ORDER BY cnt DESC"
                ).fetchall()
            }
            patterns = conn.execute(
                "SELECT count(*) FROM agent_learning_patterns"
            ).fetchone()[0]
        return {
            "total_memories": total,
            "by_type": by_type,
            "by_category": by_category,
            "learning_patterns": patterns,
        }

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """搜索记忆（供 UI 使用）。"""
        memories = self.recall(query, limit=limit)
        return [m.to_dict() for m in memories]


# ── 种子记忆（出厂自带的最佳实践）─────────────────────────────


def seed_default_memories(engine: AgentMemoryEngine) -> None:
    """初始化种子记忆——Agent 出厂的默认经验。"""
    defaults = [
        (
            "long_term",
            "quality_rule",
            "公告报名已过期时，标题中不应出现「今日截止」「N天后截止」等截止提示，应改为「已截止」或不含截止信息。",
            "质检,截止日期,标题,过期,规则",
        ),
        (
            "long_term",
            "quality_rule",
            "公众号文章中不应出现粉笔、公考雷达等第三方平台的推广信息、备考群、扫码咨询等内容。",
            "质检,广告,推广,规则",
        ),
        (
            "pattern",
            "success_pattern",
            "优先发布有原文和岗位表的公告，这类公告生成质量最高、质检通过率近 100%。",
            "成功,原文,岗位表,质量",
        ),
        (
            "pattern",
            "title_strategy",
            "标题模板「地区+招N人+岗位亮点+截止日期」点击率高于纯原标题。",
            "标题,模板,点击率,策略",
        ),
        (
            "long_term",
            "error_diagnosis",
            "粉笔爬虫返回的附件链接带 /crawler/check 路径的是鉴权中转链接，不是真实附件，应自动跳过。",
            "附件,粉笔,跳过,诊断",
        ),
        (
            "long_term",
            "strategy",
            "事业单位和教师类公告用户关注度高，应优先发布；选调和派遣类公告受众较小，可降低优先级。",
            "优先级,策略,事业单位,教师",
        ),
    ]
    for mem_type, cat, content, keywords in defaults:
        engine.remember(
            memory_type=mem_type,
            category=cat,
            content=content,
            keywords=keywords,
            weight=0.8,
        )


# ── CLI ───────────────────────────────────────────────────────


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Agent 记忆与学习系统")
    parser.add_argument("--seed", action="store_true", help="初始化种子记忆")
    parser.add_argument("--recall", default="", help="检索记忆（输入关键词）")
    parser.add_argument("--learn", type=int, default=0, help="从指定 Agent Run 中学习")
    parser.add_argument("--stats", action="store_true", help="显示记忆统计")
    parser.add_argument("--decay", action="store_true", help="衰减旧记忆")
    args = parser.parse_args()

    engine = AgentMemoryEngine()

    if args.seed:
        seed_default_memories(engine)
        stats = engine.get_memory_stats()
        print(f"种子记忆已初始化。当前共有 {stats['total_memories']} 条记忆。")

    if args.recall:
        memories = engine.recall(args.recall)
        print(f"搜索「{args.recall}」找到 {len(memories)} 条记忆：")
        for i, mem in enumerate(memories, 1):
            print(f"\n  [{i}] {mem.category} (权重: {mem.weight:.2f})")
            print(f"      {mem.content[:120]}...")

    if args.learn:
        ids = engine.learn_from_run(args.learn, feedback="passed")
        print(f"从 Run #{args.learn} 中学到 {len(ids)} 条经验")

    if args.stats:
        stats = engine.get_memory_stats()
        print(json.dumps(stats, ensure_ascii=False, indent=2))

    if args.decay:
        n = engine.decay_old_memories()
        print(f"衰减了 {n} 条旧记忆")


if __name__ == "__main__":
    main()
