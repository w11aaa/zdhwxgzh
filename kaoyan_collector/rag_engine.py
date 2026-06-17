# -*- coding: utf-8 -*-
"""
RAG 语义检索引擎 (Retrieval-Augmented Generation Engine)

对标 LangChain / LlamaIndex 的设计，提供：
- 混合检索：向量语义 + 关键词精确 双路召回，加权合并
- 自动建索引：首次运行时对所有公告正文做 Embedding
- 增量更新：新公告入库后自动追加到索引
- 来源引用：每条检索结果带 source_id，可在回复中引用

技术栈：
  Embedding: BAAI/bge-small-zh-v1.5 (384维, 轻量高性能中文)
  向量库: FAISS (Facebook AI Similarity Search)
  融合策略: RRF (Reciprocal Rank Fusion)

用法：
    engine = RAGEngine()
    results = engine.search("北京海淀区计算机类的事业编", top_k=5)
    for r in results:
        print(f"[{r['score']:.2f}] {r['title']}")
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .config import CONFIG

# ── 配置 ─────────────────────────────────────────────────────

EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
INDEX_DIR = CONFIG.project_root / "data" / "rag_index"
INDEX_FILE = INDEX_DIR / "faiss.index"
META_FILE = INDEX_DIR / "metadata.json"
VECTOR_DIM = 512  # bge-small-zh-v1.5 outputs 512-dim vectors
BATCH_SIZE = 32


@dataclass
class SearchResult:
    source_id: str
    title: str
    region: str
    category: str
    job_count: int
    deadline: str
    status: str
    snippet: str  # 匹配到的正文片段
    vector_score: float
    keyword_score: float
    score: float  # 融合分数
    source_origin_url: str = ""

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id, "title": self.title,
            "region": self.region, "category": self.category,
            "job_count": self.job_count, "deadline": self.deadline,
            "status": self.status, "snippet": self.snippet,
            "score": round(self.score, 3),
            "source_origin_url": self.source_origin_url,
        }


# ── 引擎 ─────────────────────────────────────────────────────


class RAGEngine:
    """RAG 语义检索引擎。

    单例模式，首次使用自动建索引（约 2 分钟），之后加载缓存（<1 秒）。
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.model = None
        self.index = None
        self.metadata: list[dict] = []
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    def ensure_ready(self) -> bool:
        """确保引擎就绪。首次调用会自动建索引。"""
        if self._ready:
            return True
        try:
            self._load_or_build()
            self._ready = True
            return True
        except Exception as e:
            print(f"[RAG] 初始化失败: {e}", flush=True)
            return False

    # ── 索引构建 ───────────────────────────────────────────

    def _load_or_build(self):
        if INDEX_FILE.exists() and META_FILE.exists():
            self._load_index()
        else:
            self._build_index()

    def _load_index(self):
        import faiss
        from sentence_transformers import SentenceTransformer

        t0 = time.time()
        self.model = SentenceTransformer(EMBEDDING_MODEL)
        self.index = faiss.read_index(str(INDEX_FILE))
        with open(META_FILE, "r", encoding="utf-8") as f:
            self.metadata = json.load(f)
        print(f"[RAG] 索引已加载: {self.index.ntotal} 条, "
              f"耗时 {time.time()-t0:.1f}s", flush=True)

    def _build_index(self):
        import faiss
        from sentence_transformers import SentenceTransformer

        print("[RAG] 正在构建向量索引...", flush=True)
        t0 = time.time()

        # 加载模型
        self.model = SentenceTransformer(EMBEDDING_MODEL)
        print(f"[RAG] 模型已加载: {EMBEDDING_MODEL}", flush=True)

        # 读取所有公告正文
        events = self._fetch_all_events()
        if not events:
            raise RuntimeError("数据库中没有公告，请先采集数据")

        texts = []
        self.metadata = []
        for e in events:
            text = self._build_document_text(e)
            texts.append(text)
            self.metadata.append({
                "source_id": e["source_id"],
                "title": e["title"][:120],
                "region": e.get("region", "") or "",
                "category": e.get("category", "") or "",
                "job_count": e.get("job_count") or 0,
                "deadline": (e.get("registration_deadline") or "")[:10],
                "status": e.get("status", ""),
                "source_origin_url": e.get("source_origin_url", "") or "",
            })

        # 批量向量化
        print(f"[RAG] 正在向量化 {len(texts)} 条公告...", flush=True)
        vectors = self.model.encode(
            texts,
            batch_size=BATCH_SIZE,
            show_progress_bar=True,
            normalize_embeddings=True,
        ).astype(np.float32)

        # 构建 FAISS 索引
        self.index = faiss.IndexFlatIP(VECTOR_DIM)  # Inner Product (cosine for normalized)
        self.index.add(vectors)
        print(f"[RAG] FAISS 索引构建完成: {self.index.ntotal} 条, "
              f"耗时 {time.time()-t0:.1f}s", flush=True)

        # 持久化
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self.index, str(INDEX_FILE))
        with open(META_FILE, "w", encoding="utf-8") as f:
            json.dump(self.metadata, f, ensure_ascii=False, indent=2)
        print(f"[RAG] 索引已保存: {INDEX_FILE}", flush=True)

    def _fetch_all_events(self) -> list[dict]:
        conn = sqlite3.connect(str(CONFIG.database_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT source_id, title, region, category, job_count, "
            "registration_deadline, status, raw_text, source_origin_url, "
            "source_origin_text "
            "FROM gongkao_events "
            "WHERE coalesce(raw_text, '') <> '' OR coalesce(source_origin_text, '') <> ''"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def _build_document_text(self, event: dict) -> str:
        """构建用于向量化的文档文本。"""
        parts = [
            event.get("title", ""),
            event.get("region", ""),
            event.get("category", ""),
            (event.get("raw_text") or "")[:3000],
            (event.get("source_origin_text") or "")[:2000],
        ]
        return " ".join(p for p in parts if p)

    # ── 检索 ───────────────────────────────────────────────

    def search(self, query: str, *, top_k: int = 5,
               filter_region: str = "", filter_category: str = "",
               filter_status: str = "正在报名") -> list[SearchResult]:
        """混合检索：向量语义 + 关键词精确。

        Args:
            query: 用户查询
            top_k: 返回结果数量
            filter_region: 地区过滤（空=不过滤）
            filter_category: 类型过滤
            filter_status: 状态过滤（默认只返回正在报名）

        Returns:
            排序后的搜索结果
        """
        if not self.ensure_ready():
            return self._fallback_keyword_search(query, top_k,
                filter_region, filter_category, filter_status)

        # 1) 向量语义检索
        vec_results = self._vector_search(query, top_k * 3,
            filter_region, filter_category, filter_status)

        # 2) 关键词精确检索
        kw_results = self._keyword_search(query, top_k * 3,
            filter_region, filter_category, filter_status)

        # 3) RRF 融合
        merged = self._rrf_fusion(vec_results, kw_results, k=60)
        merged.sort(key=lambda x: x.score, reverse=True)
        return merged[:top_k]

    def _vector_search(self, query: str, top_k: int,
                       region: str, category: str, status: str) -> list[SearchResult]:
        q_vec = self.model.encode(
            [query], normalize_embeddings=True, show_progress_bar=False
        ).astype(np.float32)

        # 先取更多结果再过滤
        fetch_k = min(top_k * 5, self.index.ntotal)
        scores, indices = self.index.search(q_vec, fetch_k)

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(self.metadata):
                continue
            meta = self.metadata[idx]
            if region and region not in meta.get("region", ""):
                continue
            if category and category not in meta.get("category", ""):
                continue
            if status and meta.get("status") != status:
                continue

            results.append(SearchResult(
                source_id=meta["source_id"],
                title=meta["title"],
                region=meta.get("region", ""),
                category=meta.get("category", ""),
                job_count=meta.get("job_count", 0),
                deadline=meta.get("deadline", ""),
                status=meta.get("status", ""),
                snippet="",
                vector_score=float(score),
                keyword_score=0.0,
                score=float(score),
                source_origin_url=meta.get("source_origin_url", ""),
            ))
            if len(results) >= top_k:
                break
        return results

    def _keyword_search(self, query: str, top_k: int,
                        region: str, category: str, status: str) -> list[SearchResult]:
        """基于 SQLite 的关键词精确检索。"""
        conn = sqlite3.connect(str(CONFIG.database_path))
        conn.row_factory = sqlite3.Row

        keywords = [kw.strip() for kw in query.replace("，", ",").split(",")
                    if len(kw.strip()) >= 1]
        clauses = []
        params: list[Any] = []

        if status:
            clauses.append("status = ?")
            params.append(status)
        if region:
            clauses.append("(region = ? OR title LIKE ?)")
            params.extend([region, f"%{region}%"])
        if category:
            clauses.append("(category = ? OR fenbi_exam_type_name = ?)")
            params.extend([category, category])

        # 关键词匹配
        kw_clauses = []
        for kw in keywords[:5]:
            kw_clauses.append("(title LIKE ? OR raw_text LIKE ?)")
            params.extend([f"%{kw}%", f"%{kw}%"])
        if kw_clauses:
            clauses.append("(" + " OR ".join(kw_clauses) + ")")

        where = " AND ".join(clauses) if clauses else "1=1"
        query_sql = f"""SELECT source_id, title, region, category, job_count,
            registration_deadline, status, source_origin_url
            FROM gongkao_events WHERE {where}
            ORDER BY registration_deadline ASC LIMIT ?"""

        rows = conn.execute(query_sql, [*params, top_k]).fetchall()
        conn.close()

        results = []
        for r in rows:
            # 计算关键词命中分数
            hits = sum(1 for kw in keywords if kw in (r["title"] or ""))
            results.append(SearchResult(
                source_id=r["source_id"], title=r["title"] or "",
                region=r["region"] or "", category=r["category"] or "",
                job_count=r["job_count"] or 0,
                deadline=(r["registration_deadline"] or "")[:10],
                status=r["status"] or "",
                snippet="", vector_score=0.0,
                keyword_score=float(hits) / max(1, len(keywords)),
                score=float(hits) / max(1, len(keywords)),
                source_origin_url=r["source_origin_url"] or "",
            ))
        return results

    def _rrf_fusion(self, vec_results: list[SearchResult],
                    kw_results: list[SearchResult], k: int = 60) -> list[SearchResult]:
        """Reciprocal Rank Fusion：合并两路检索结果。"""
        score_map: dict[str, float] = {}
        result_map: dict[str, SearchResult] = {}

        for rank, r in enumerate(vec_results):
            score_map[r.source_id] = score_map.get(r.source_id, 0) + 1.0 / (k + rank + 1)
            result_map[r.source_id] = r

        for rank, r in enumerate(kw_results):
            score_map[r.source_id] = score_map.get(r.source_id, 0) + 1.0 / (k + rank + 1)
            if r.source_id not in result_map:
                result_map[r.source_id] = r

        for sid, r in result_map.items():
            r.score = score_map.get(sid, 0)

        merged = list(result_map.values())
        merged.sort(key=lambda x: x.score, reverse=True)
        return merged

    def _fallback_keyword_search(self, query: str, top_k: int,
                                  region: str, category: str, status: str) -> list[SearchResult]:
        """当向量索引不可用时的回退方案。"""
        return self._keyword_search(query, top_k, region, category, status)

    # ── 管理 ───────────────────────────────────────────────

    def rebuild(self):
        """强制重建索引。"""
        if INDEX_FILE.exists():
            INDEX_FILE.unlink()
        if META_FILE.exists():
            META_FILE.unlink()
        self._ready = False
        self._build_index()
        self._ready = True

    def stats(self) -> dict:
        return {
            "indexed": self.index.ntotal if self.index else 0,
            "model": EMBEDDING_MODEL,
            "dimension": VECTOR_DIM,
        }


# ── 便捷函数 ─────────────────────────────────────────────────


def search_announcements(query: str, top_k: int = 5) -> list[dict]:
    """快速搜索公告（供对话引擎调用）。"""
    engine = RAGEngine()
    results = engine.search(query, top_k=top_k)
    return [r.to_dict() for r in results]


# ── CLI 测试 ─────────────────────────────────────────────────


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="RAG 语义搜索")
    ap.add_argument("query", nargs="?", default="", help="搜索关键词")
    ap.add_argument("--build", action="store_true", help="强制重建索引")
    ap.add_argument("--stats", action="store_true", help="显示索引统计")
    args = ap.parse_args()

    engine = RAGEngine()

    if args.build:
        engine.rebuild()
        print("索引重建完成")

    if args.stats:
        engine.ensure_ready()
        print(json.dumps(engine.stats(), ensure_ascii=False, indent=2))

    if args.query:
        engine.ensure_ready()
        t0 = time.time()
        results = engine.search(args.query, top_k=5)
        elapsed = time.time() - t0

        print(f"\n搜索: {args.query}  ({len(results)} 条, {elapsed*1000:.0f}ms)")
        print("-" * 60)
        for i, r in enumerate(results, 1):
            print(f"{i}. [{r.score:.3f}] {r.title[:60]}")
            print(f"   {r.region} | {r.category} | 招{r.job_count}人 | "
                  f"截止{r.deadline} | {r.status}")
