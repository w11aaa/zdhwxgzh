# -*- coding: utf-8 -*-
"""Token cost tracker - records LLM usage costs."""

from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from .config import CONFIG
from .schema import init_db

PRICING = {
    "deepseek-v4-flash": (1.0, 4.0),
    "deepseek-v4-pro": (2.0, 8.0),
    "deepseek-chat": (1.0, 2.0),
    "deepseek-reasoner": (4.0, 16.0),
    "default": (1.0, 4.0),
}

TOKEN_DDL = """
CREATE TABLE IF NOT EXISTS token_usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    prompt_cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
    prompt_cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
    cost_input REAL NOT NULL DEFAULT 0.0,
    cost_output REAL NOT NULL DEFAULT 0.0,
    cost_total REAL NOT NULL DEFAULT 0.0,
    task_name TEXT, source_id TEXT, agent_name TEXT,
    duration_ms INTEGER, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tu_created ON token_usage_log(created_at);
CREATE INDEX IF NOT EXISTS idx_tu_model ON token_usage_log(model);
"""


class TokenTracker:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or CONFIG.database_path
        init_db(self.db_path)
        self._ensure_table()

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_table(self):
        with self._connect() as conn:
            conn.executescript(TOKEN_DDL)
            conn.commit()

    def record(self, *, model: str, prompt_tokens: int = 0,
               completion_tokens: int = 0,
               prompt_cache_hit_tokens: int = 0,
               prompt_cache_miss_tokens: int = 0,
               task_name: str = "", source_id: str = "",
               agent_name: str = "", duration_ms: int = 0) -> int:
        total = prompt_tokens + completion_tokens
        p_in, p_out = PRICING.get(model, PRICING["default"])
        cost_input = prompt_tokens / 1_000_000 * p_in
        cost_output = completion_tokens / 1_000_000 * p_out
        cost_total = cost_input + cost_output
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO token_usage_log(model, prompt_tokens, completion_tokens,
                total_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens,
                cost_input, cost_output, cost_total, task_name, source_id,
                agent_name, duration_ms, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (model, prompt_tokens, completion_tokens, total,
                 prompt_cache_hit_tokens, prompt_cache_miss_tokens,
                 round(cost_input, 6), round(cost_output, 6), round(cost_total, 6),
                 task_name, source_id, agent_name, duration_ms,
                 datetime.utcnow().isoformat()))
            conn.commit()
            return int(cur.lastrowid)

    def summary(self, *, days: int = 7) -> dict[str, Any]:
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT count(*) c, sum(prompt_tokens) pt, sum(completion_tokens) ct, "
                "sum(total_tokens) tt, sum(prompt_cache_hit_tokens) cht, "
                "sum(prompt_cache_miss_tokens) cmt, sum(cost_total) cost "
                "FROM token_usage_log WHERE created_at >= ?", (since,)).fetchone()
        if not row or not row["c"]:
            return {"calls": 0, "total_tokens": 0, "total_cost": 0.0, "days": days}
        pt = row["pt"] or 0; ct = row["ct"] or 0
        cht = row["cht"] or 0; cmt = row["cmt"] or 0
        cache_total = cht + cmt
        return {"calls": row["c"], "total_tokens": row["tt"] or 0,
                "total_cost": round(row["cost"] or 0, 4),
                "prompt_tokens": pt, "completion_tokens": ct,
                "cache_hit_tokens": cht, "cache_hit_rate":
                round(cht / max(1, cache_total), 3), "days": days}

    def breakdown(self, *, days: int = 7) -> list[dict]:
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_name, model, count(*) c, sum(total_tokens) t, "
                "sum(cost_total) cost FROM token_usage_log WHERE created_at >= ? "
                "GROUP BY task_name, model ORDER BY cost DESC", (since,)).fetchall()
        return [dict(r) for r in rows]

    def today_summary(self) -> dict:
        return self.summary(days=1)

    def all_time_summary(self) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT count(*) c, sum(total_tokens) t, sum(cost_total) cost "
                "FROM token_usage_log").fetchone()
        return {"calls": row["c"] or 0, "total_tokens": row["t"] or 0,
                "total_cost": round(row["cost"] or 0, 4)}
