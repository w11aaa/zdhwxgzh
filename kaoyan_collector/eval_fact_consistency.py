# -*- coding: utf-8 -*-
"""事实一致性评测集（Fact Consistency Evaluation）

从公告库随机抽样 N 条公告，自动生成公众号文章后，
对比关键字段的保留情况，输出量化评测报告。

评测维度：
- 招聘人数准确率（job_count_accuracy）
- 地区保留率（region_retention）
- 截止日期准确率（deadline_accuracy）
- 原文链接保留率（source_url_retention）
- 无关内容检出率（noise_detection_rate）
- 综合通过率（overall_pass_rate）

用法：
    python -m kaoyan_collector.eval_fact_consistency --n 20 --output report.json
    python -m kaoyan_collector.eval_fact_consistency --n 20 --json   # 输出 JSON 到 stdout
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import CONFIG
from .gongkao_wechat_pipeline import (
    GongkaoSelection,
    _build_gongkao_wechat_content,
    SOURCE_NOISE_PATTERNS,
)

# ── 数据模型 ────────────────────────────────────────────────────


@dataclass
class FactCheckResult:
    """单条公告的事实一致性检查结果。"""

    source_id: str
    title: str
    sample: dict[str, Any]  # 原始字段（用于展示）

    # 各维度结果
    job_count_pass: bool = True
    job_count_detail: str = ""

    region_pass: bool = True
    region_detail: str = ""

    deadline_pass: bool = True
    deadline_detail: str = ""

    source_url_pass: bool = True
    source_url_detail: str = ""

    noise_pass: bool = True
    noise_detail: str = ""

    overall_pass: bool = True

    generated_title: str = ""
    generated_content_preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvalReport:
    """评测报告。"""

    checked_at: str
    sample_count: int
    results: list[FactCheckResult] = field(default_factory=list)

    # 汇总统计
    job_count_accuracy: float = 0.0
    region_retention: float = 0.0
    deadline_accuracy: float = 0.0
    source_url_retention: float = 0.0
    noise_detection_rate: float = 0.0
    overall_pass_rate: float = 0.0

    warnings: list[str] = field(default_factory=list)

    def compute(self) -> None:
        n = max(1, len(self.results))
        self.job_count_accuracy = sum(1 for r in self.results if r.job_count_pass) / n
        self.region_retention = sum(1 for r in self.results if r.region_pass) / n
        self.deadline_accuracy = sum(1 for r in self.results if r.deadline_pass) / n
        self.source_url_retention = sum(1 for r in self.results if r.source_url_pass) / n
        self.noise_detection_rate = sum(1 for r in self.results if r.noise_pass) / n
        self.overall_pass_rate = sum(1 for r in self.results if r.overall_pass) / n

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        lines = [
            "=" * 60,
            "        事实一致性评测报告",
            "=" * 60,
            f"评测时间：{self.checked_at}",
            f"评测样本：{self.sample_count} 条",
            "",
            "维度                 准确率",
            "-" * 40,
            f"招聘人数准确率       {self.job_count_accuracy:.0%}",
            f"地区保留率           {self.region_retention:.0%}",
            f"截止日期准确率       {self.deadline_accuracy:.0%}",
            f"原文链接保留率       {self.source_url_retention:.0%}",
            f"无关内容检出率       {self.noise_detection_rate:.0%}",
            "-" * 40,
            f"综合通过率           {self.overall_pass_rate:.0%}",
            "",
        ]
        if self.warnings:
            lines.append("注意事项：")
            for w in self.warnings:
                lines.append(f"  ⚠ {w}")
            lines.append("")
        lines.append("=" * 60)
        lines.append("各样本详情：")
        for i, r in enumerate(self.results, 1):
            status = "✅" if r.overall_pass else "❌"
            lines.append(
                f"\n{status} 样本 {i}: {r.source_id} | {r.title[:60]}"
            )
            if r.job_count_detail:
                lines.append(f"   人数: {r.job_count_detail}")
            if r.region_detail:
                lines.append(f"   地区: {r.region_detail}")
            if r.deadline_detail:
                lines.append(f"   截止: {r.deadline_detail}")
            if r.source_url_detail:
                lines.append(f"   链接: {r.source_url_detail}")
            if r.noise_detail:
                lines.append(f"   噪声: {r.noise_detail}")
        return "\n".join(lines)


# ── 评测逻辑 ────────────────────────────────────────────────────


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _sample_events(db_path: Path, n: int) -> list[GongkaoSelection]:
    """随机采样 N 条有实质内容的公告。"""
    with _connect(db_path) as conn:
        # 优先选有原文、有截止日期、有招聘人数的
        rows = conn.execute(
            """
            SELECT
                source_platform, source_id, title, region, category,
                org_name, job_count, qualification, major_requirements,
                registration_start, registration_deadline,
                coalesce(registration_deadline_time, registration_deadline, '') AS registration_deadline_time,
                exam_date, status, publish_time, source_url, article_url,
                coalesce(source_origin_url, '') AS source_origin_url,
                coalesce(source_origin_html, '') AS source_origin_html,
                summary, raw_text, raw_json
            FROM gongkao_events
            WHERE coalesce(raw_text, '') <> ''
              AND coalesce(title, '') <> ''
            ORDER BY random()
            LIMIT ?
            """,
            (max(n, 1),),
        ).fetchall()

    if not rows:
        raise ValueError("数据库中无可评测公告。请先采集数据。")

    return [GongkaoSelection(**dict(row)) for row in rows]


def _normalize(text: str) -> str:
    """去空格、全角半角统一，用于模糊比较。"""
    text = str(text or "")
    text = re.sub(r"\s+", "", text)
    text = text.replace("（", "(").replace("）", ")")
    text = text.replace("：", ":").replace("，", ",").replace("。", ".")
    return text


def _check_job_count(event: GongkaoSelection, content: str) -> tuple[bool, str]:
    """检查招聘人数是否保留。"""
    jc = event.job_count
    if jc is None:
        return True, "原始数据无招聘人数，跳过检查"
    # 检查内容或标题中是否有该数字
    if str(jc) in content:
        return True, f"招聘人数 {jc} 出现在正文中"
    # 检查 "招N人" 模式
    if re.search(rf"招\s*{jc}\s*人", content):
        return True, f"「招{jc}人」出现在正文中"
    return False, f"招聘人数 {jc} 未在生成内容中找到"


def _check_region(event: GongkaoSelection, content: str) -> tuple[bool, str]:
    """检查地区是否保留。"""
    region = str(event.region or "").strip()
    if not region:
        return True, "原始数据无地区字段，跳过检查"
    if region in content:
        return True, f"地区「{region}」出现在正文中"
    # 也检查标题原始文本中的地名
    title = str(event.title or "")
    cities = re.findall(r"([\u4e00-\u9fa5]{2,6})(?:市|省|区|县)", title)
    for city in cities:
        if city in content:
            return True, f"从标题提取的地名「{city}」出现在正文中"
    return False, f"地区信息「{region}」未在生成内容中找到"


def _check_deadline(event: GongkaoSelection, content: str) -> tuple[bool, str]:
    """检查截止日期是否保留。"""
    deadline = str(event.registration_deadline or "").strip()[:10]
    if not deadline:
        return True, "原始数据无截止日期，跳过检查"
    normalized_content = _normalize(content)
    if deadline in normalized_content:
        return True, f"截止日期 {deadline} 出现在正文中"
    # 尝试其他格式
    try:
        dt = datetime.strptime(deadline, "%Y-%m-%d")
        variants = [
            f"{dt.year}年{dt.month}月{dt.day}日",
            f"{dt.month}月{dt.day}日",
            deadline.replace("-", "/"),
        ]
        for v in variants:
            if _normalize(v) in normalized_content:
                return True, f"截止日期变体「{v}」出现在正文中"
    except Exception:
        pass
    return False, f"截止日期 {deadline} 未在生成内容中找到"


def _check_source_url(event: GongkaoSelection, content: str) -> tuple[bool, str]:
    """检查原文链接是否保留。"""
    url = str(event.source_origin_url or event.article_url or event.source_url or "").strip()
    if not url:
        return True, "无可用原文链接，跳过检查"
    if url in content:
        return True, "原文链接出现在正文中"
    # 有时只剩域名部分
    domain = re.search(r"https?://([^/]+)", url)
    if domain and domain.group(1) in content:
        return True, f"原文域名 {domain.group(1)} 出现在正文中"
    return False, f"原文链接 {url[:60]}... 未在生成内容中找到"


def _check_noise(event: GongkaoSelection, content: str) -> tuple[bool, str]:
    """检查是否包含无关/噪声内容。"""
    hits: list[str] = []
    for pattern in SOURCE_NOISE_PATTERNS:
        if re.search(pattern, content, flags=re.I):
            hits.append(pattern)
    if not hits:
        return True, "未检测到无关内容"
    return False, f"检测到 {len(hits)} 条噪声模式: {', '.join(hits[:3])}"


def evaluate_event(event: GongkaoSelection) -> FactCheckResult:
    """对单条公告进行完整评测。"""
    title, content = _build_gongkao_wechat_content(event)

    jc_ok, jc_detail = _check_job_count(event, content)
    region_ok, region_detail = _check_region(event, content)
    deadline_ok, deadline_detail = _check_deadline(event, content)
    url_ok, url_detail = _check_source_url(event, content)
    noise_ok, noise_detail = _check_noise(event, content)

    overall = all([jc_ok, region_ok, deadline_ok, url_ok, noise_ok])

    return FactCheckResult(
        source_id=event.source_id,
        title=event.title,
        sample={
            "region": event.region,
            "category": event.category,
            "job_count": event.job_count,
            "registration_deadline": event.registration_deadline,
            "source_origin_url": event.source_origin_url,
        },
        job_count_pass=jc_ok,
        job_count_detail=jc_detail,
        region_pass=region_ok,
        region_detail=region_detail,
        deadline_pass=deadline_ok,
        deadline_detail=deadline_detail,
        source_url_pass=url_ok,
        source_url_detail=url_detail,
        noise_pass=noise_ok,
        noise_detail=noise_detail,
        overall_pass=overall,
        generated_title=title,
        generated_content_preview=content[:500],
    )


def run_evaluation(n: int = 20, db_path: Path | None = None) -> EvalReport:
    """运行完整评测流程。"""
    db_path = db_path or CONFIG.database_path
    events = _sample_events(db_path, n)

    result_count = len(events)
    report = EvalReport(
        checked_at=datetime.utcnow().isoformat(),
        sample_count=result_count,
    )

    if result_count < n:
        report.warnings.append(
            f"请求 {n} 条样本，实际只有 {result_count} 条符合条件的公告。"
        )

    for i, event in enumerate(events, 1):
        print(
            f"[eval] {i}/{result_count} 评测 {event.source_id}: {event.title[:50]}...",
            flush=True,
        )
        try:
            result = evaluate_event(event)
            report.results.append(result)
        except Exception as exc:
            print(f"[eval]   ⚠ 评测异常: {exc}", flush=True)
            result = FactCheckResult(
                source_id=event.source_id,
                title=event.title,
                sample={},
                overall_pass=False,
                noise_detail=f"评测异常: {exc}",
            )
            report.results.append(result)

    report.compute()
    return report


# ── CLI ─────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="事实一致性评测：抽样检查生成文章是否正确保留公告关键信息。"
    )
    parser.add_argument("--n", type=int, default=20, help="抽样数量（默认 20）")
    parser.add_argument("--db", default=str(CONFIG.database_path), help="数据库路径")
    parser.add_argument(
        "--output",
        default="",
        help="输出报告文件路径（JSON 格式）。不指定则打印文本报告后保存到 quality_reports/ 目录。",
    )
    parser.add_argument("--json", action="store_true", help="仅输出 JSON 报告到 stdout")
    args = parser.parse_args()

    report = run_evaluation(n=max(1, min(args.n, 100)), db_path=Path(args.db))

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return

    print(report.summary())

    # 默认输出目录
    output_dir = CONFIG.project_root / "quality_reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output or str(
        output_dir / f"eval_fact_consistency_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    )
    Path(output_path).write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n报告已保存: {output_path}")


if __name__ == "__main__":
    main()
