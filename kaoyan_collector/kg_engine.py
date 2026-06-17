# -*- coding: utf-8 -*-
"""Knowledge Graph Engine - SQLite-based lightweight graph database.

Entity nodes: Region, Organization, Announcement, ExamType, Position
Relationships: BELONGS_TO, PUBLISHES, SIMILAR_TO, HAS_CATEGORY

Usage:
    engine = KnowledgeGraphEngine()
    engine.build_from_db()
    related = engine.get_related_announcements(source_id)
"""

from __future__ import annotations
import sqlite3, json, re, hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import CONFIG
from .schema import init_db

KG_DDL = """
CREATE TABLE IF NOT EXISTS kg_nodes (
    node_id TEXT PRIMARY KEY,
    node_type TEXT NOT NULL,
    label TEXT NOT NULL,
    properties TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kg_type ON kg_nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_kg_label ON kg_nodes(label);

CREATE TABLE IF NOT EXISTS kg_edges (
    edge_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    properties TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(source_id) REFERENCES kg_nodes(node_id),
    FOREIGN KEY(target_id) REFERENCES kg_nodes(node_id)
);
CREATE INDEX IF NOT EXISTS idx_kg_edge_src ON kg_edges(source_id, relation);
CREATE INDEX IF NOT EXISTS idx_kg_edge_tgt ON kg_edges(target_id, relation);
"""


@dataclass
class GraphNode:
    node_id: str
    node_type: str
    label: str
    properties: dict = field(default_factory=dict)

@dataclass
class GraphEdge:
    edge_id: str
    source_id: str
    target_id: str
    relation: str
    weight: float = 1.0


class KnowledgeGraphEngine:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or CONFIG.database_path
        init_db(self.db_path)
        self._ensure_schema()

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self):
        with self._connect() as conn:
            conn.executescript(KG_DDL)
            conn.commit()

    def _make_id(self, *parts: str) -> str:
        return hashlib.md5("|".join(parts).encode()).hexdigest()[:16]

    def _now(self) -> str:
        return datetime.utcnow().isoformat()

    # ── Build from DB ─────────────────────────────────────

    def build_from_db(self, force_rebuild: bool = False) -> dict:
        """从公告库构建知识图谱。"""
        with self._connect() as conn:
            existing = conn.execute("SELECT count(*) c FROM kg_nodes").fetchone()["c"]
        if existing > 0 and not force_rebuild:
            return {"status": "skipped", "nodes": existing,
                    "message": f"已有 {existing} 个节点，使用 force_rebuild=True 重建"}

        with self._connect() as conn:
            conn.execute("DELETE FROM kg_edges")
            conn.execute("DELETE FROM kg_nodes")
            conn.commit()

        stats = {"regions": 0, "organizations": 0, "announcements": 0, "exam_types": 0, "edges": 0}
        now = self._now()

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source_id, title, region, category, org_name, fenbi_exam_type_name, "
                "job_count, registration_deadline, status, source_origin_url "
                "FROM gongkao_events WHERE title IS NOT NULL"
            ).fetchall()

        edges_batch = []
        for row in rows:
            sid = row["source_id"]
            title = row["title"] or ""
            region = row["region"] or ""
            category = row["category"] or ""
            org = row["org_name"] or ""
            fenbi_type = row["fenbi_exam_type_name"] or ""

            # Announcement node
            ann_id = self._make_id("ann", sid)
            self._upsert_node(conn, ann_id, "Announcement", title[:80], {
                "source_id": sid, "job_count": row["job_count"] or 0,
                "deadline": row["registration_deadline"] or "",
                "status": row["status"] or "", "has_origin": bool(row["source_origin_url"])
            }, now)
            stats["announcements"] += 1

            # Region node + edge
            if region:
                rid = self._make_id("region", region)
                self._upsert_node(conn, rid, "Region", region, {}, now)
                stats["regions"] = max(stats["regions"], 1)
                edges_batch.append((self._make_id("edge", rid, ann_id, "belongs"),
                                    rid, ann_id, "BELONGS_TO", 1.0, now))

            # Organization node + edge
            org_name = org or title[:30]
            if org_name:
                oid = self._make_id("org", org_name[:50])
                self._upsert_node(conn, oid, "Organization", org_name[:80], {}, now)
                stats["organizations"] += 1
                edges_batch.append((self._make_id("edge", oid, ann_id, "publishes"),
                                    oid, ann_id, "PUBLISHES", 1.0, now))

            # Exam type node + edge
            etype = fenbi_type or category or ""
            if etype:
                tid = self._make_id("type", etype)
                self._upsert_node(conn, tid, "ExamType", etype, {}, now)
                stats["exam_types"] = max(stats["exam_types"], 1)
                edges_batch.append((self._make_id("edge", ann_id, tid, "has_type"),
                                    ann_id, tid, "HAS_CATEGORY", 1.0, now))

        # Bulk insert edges
        for eid, src, tgt, rel, w, ts in edges_batch:
            self._upsert_edge(conn, eid, src, tgt, rel, w, ts)
        stats["edges"] = len(edges_batch)

        # Build similarity edges (same region + same type)
        sim_edges = []
        with self._connect() as conn2:
            pairs = conn2.execute(
                "SELECT a.node_id as n1, b.node_id as n2 FROM kg_nodes a "
                "JOIN kg_nodes b ON a.node_type='Announcement' AND b.node_type='Announcement' "
                "AND a.node_id < b.node_id"
            ).fetchall()
            for p in pairs[:5000]:
                # Check if they share region or type
                n1_edges = conn2.execute(
                    "SELECT target_id, relation FROM kg_edges WHERE source_id=? AND relation IN ('BELONGS_TO','HAS_CATEGORY')",
                    (p["n1"],)
                ).fetchall()
                n2_edges = conn2.execute(
                    "SELECT target_id, relation FROM kg_edges WHERE source_id=? AND relation IN ('BELONGS_TO','HAS_CATEGORY')",
                    (p["n2"],)
                ).fetchall()
                n1_set = {(e["target_id"], e["relation"]) for e in n1_edges}
                n2_set = {(e["target_id"], e["relation"]) for e in n2_edges}
                overlap = len(n1_set & n2_set)
                if overlap > 0:
                    weight = min(1.0, overlap * 0.35)
                    sim_edges.append((self._make_id("sim", p["n1"], p["n2"]),
                                      p["n1"], p["n2"], "SIMILAR_TO", weight, now))

        for e in sim_edges:
            self._upsert_edge(conn, e[0], e[1], e[2], e[3], e[4], e[5])
        stats["similarity_edges"] = len(sim_edges)

        return {"status": "built", **stats}

    # ── Query ────────────────────────────────────────────

    def get_related_announcements(self, source_id: str, limit: int = 5) -> list[dict]:
        """获取与指定公告相关的其他公告（通过图谱关系）。"""
        ann_id = self._make_id("ann", source_id)
        results = []
        with self._connect() as conn:
            # Same region + type
            rows = conn.execute(
                "SELECT DISTINCT b.label, b.properties FROM kg_edges e1 "
                "JOIN kg_edges e2 ON e1.target_id = e2.target_id AND e1.relation = e2.relation "
                "JOIN kg_nodes b ON e2.source_id = b.node_id AND b.node_type = 'Announcement' "
                "WHERE e1.source_id = ? AND e1.relation IN ('BELONGS_TO','HAS_CATEGORY') "
                "AND b.node_id != ? LIMIT ?",
                (ann_id, ann_id, limit)
            ).fetchall()
            for r in rows:
                props = json.loads(r["properties"])
                results.append({"title": r["label"], "source_id": props.get("source_id", ""),
                                "job_count": props.get("job_count", 0),
                                "relation": "same_region_or_type"})

            if len(results) < limit:
                # Same org
                org_rows = conn.execute(
                    "SELECT e2.source_id as nid FROM kg_edges e1 "
                    "JOIN kg_edges e2 ON e1.source_id = e2.source_id AND e1.relation = 'PUBLISHES' AND e2.relation = 'PUBLISHES' "
                    "WHERE e1.target_id = ? AND e2.target_id != ?",
                    (ann_id, ann_id)
                ).fetchall()
                for org_row in org_rows:
                    r = conn.execute(
                        "SELECT label, properties FROM kg_nodes WHERE node_id = ?",
                        (org_row["nid"],)
                    ).fetchone()
                    if r:
                        props = json.loads(r["properties"])
                        results.append({"title": r["label"], "source_id": props.get("source_id", ""),
                                        "job_count": props.get("job_count", 0),
                                        "relation": "same_organization"})

        return results[:limit]

    def get_region_announcements(self, region: str, limit: int = 10) -> list[dict]:
        rid = self._make_id("region", region)
        return self._get_neighbors(rid, "Announcement", "BELONGS_TO", limit)

    def get_org_history(self, org_name: str, limit: int = 10) -> list[dict]:
        oid = self._make_id("org", org_name[:50])
        return self._get_neighbors(oid, "Announcement", "PUBLISHES", limit)

    def search_entities(self, keyword: str, node_type: str = "", limit: int = 10) -> list[dict]:
        with self._connect() as conn:
            clauses = ["label LIKE ?"]
            params = [f"%{keyword}%"]
            if node_type:
                clauses.append("node_type = ?")
                params.append(node_type)
            rows = conn.execute(
                f"SELECT node_id, node_type, label, properties FROM kg_nodes "
                f"WHERE {' AND '.join(clauses)} LIMIT ?",
                [*params, limit]
            ).fetchall()
        return [{"node_id": r["node_id"], "type": r["node_type"],
                 "label": r["label"], "props": json.loads(r["properties"])} for r in rows]

    def stats(self) -> dict:
        with self._connect() as conn:
            nodes = conn.execute("SELECT count(*) c FROM kg_nodes").fetchone()["c"]
            edges = conn.execute("SELECT count(*) c FROM kg_edges").fetchone()["c"]
            by_type = {r["node_type"]: r["c"] for r in conn.execute(
                "SELECT node_type, count(*) c FROM kg_nodes GROUP BY node_type"
            ).fetchall()}
            by_rel = {r["relation"]: r["c"] for r in conn.execute(
                "SELECT relation, count(*) c FROM kg_edges GROUP BY relation"
            ).fetchall()}
        return {"total_nodes": nodes, "total_edges": edges,
                "nodes_by_type": by_type, "edges_by_relation": by_rel}

    def recommend_related(self, query: str, limit: int = 5) -> list[dict]:
        """根据查询关键词推荐相关实体。"""
        entities = self.search_entities(query, limit=limit)
        results = []
        for e in entities:
            results.append({"title": e["label"], "type": e["type"],
                           "reason": f"matched keyword: {query}"})
        return results

    # ── Internal ─────────────────────────────────────────

    def _upsert_node(self, conn, node_id, node_type, label, props, now):
        conn.execute(
            "INSERT OR REPLACE INTO kg_nodes(node_id, node_type, label, properties, created_at) VALUES (?,?,?,?,?)",
            (node_id, node_type, label, json.dumps(props, ensure_ascii=False),
             now))

    def _upsert_edge(self, conn, edge_id, src, tgt, rel, weight, now):
        try:
            conn.execute(
                "INSERT OR REPLACE INTO kg_edges(edge_id, source_id, target_id, relation, weight, properties, created_at) VALUES (?,?,?,?,?,?,?)",
                (edge_id, src, tgt, rel, weight, "{}", now))
        except sqlite3.IntegrityError:
            pass  # edge already exists

    def _get_neighbors(self, node_id, target_type, relation, limit):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT n.label, n.properties FROM kg_edges e "
                "JOIN kg_nodes n ON e.target_id = n.node_id AND n.node_type = ? "
                "WHERE e.source_id = ? AND e.relation = ? LIMIT ?",
                (target_type, node_id, relation, limit)
            ).fetchall()
        return [{"title": r["label"], "source_id": json.loads(r["properties"]).get("source_id", ""),
                 "job_count": json.loads(r["properties"]).get("job_count", 0)} for r in rows]
