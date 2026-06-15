from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import CONFIG
from .gongkao_recommender import recommend_events, EventRecommendation
from .schema import init_db


def _utc_now() -> str:
    return datetime.utcnow().isoformat()


def _connect(db_path: Path) -> sqlite3.Connection:
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


# ── Agent Planner ──────────────────────────────────────────────


@dataclass
class PlanStep:
    step_index: int
    tool_name: str
    reasoning: str
    tool_args: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    observation: str = ""
    error_message: str = ""
    started_at: str = ""
    finished_at: str = ""
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GongkaoAgentPlanner:
    """考公公众号运营 Agent 的动态工具编排器。

    根据每条公告当前状态（原公告是否已找到、附件是否已扫描/下载/解析），
    动态决定需要调用哪些工具，按顺序执行，每步记录决策原因和结果。
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or CONFIG.database_path
        self.run_id: int = 0
        self.task_id: str = ""
        self.steps: list[PlanStep] = []

    # ── 编排入口 ─────────────────────────────────────────────

    def plan_and_execute(
        self,
        *,
        objective: str = "",
        count: int = 3,
        days_to_deadline: int = 30,
        author: str = "岸上信息站",
        wechat_cover: str = "",
        skip_publish: bool = False,
        include_attachment_images: bool = False,
        status: str = "正在报名",
    ) -> dict[str, Any]:
        """对推荐公告动态生成执行计划并逐步执行。"""
        self.task_id = uuid.uuid4().hex[:12]
        self.steps = []

        # ── Step 1: 查询推荐 ──
        self._start_agent_run(objective or f"选择{count}篇公告生成公众号草稿")
        recommendations = self._execute_query_recommendations(count, status)
        if not recommendations:
            self._finish_agent_run("completed", "没有找到可推荐的公告。")
            return {"ok": True, "drafts": 0, "message": "没有找到可推荐的公告。"}

        selected_ids = [r.source_id for r in recommendations]
        log(f"[AgentPlanner] 推荐了 {len(recommendations)} 条公告，分数如下：")
        for r in recommendations:
            log(f"[AgentPlanner]   score={r.score} id={r.source_id} {r.title}")
            log(f"[AgentPlanner]   理由：{'；'.join(r.reasons)}")

        # ── 对每条推荐：动态生成步骤 ──
        success_count = 0
        for event_rec in recommendations:
            log(f"\n{'─' * 60}")
            log(f"[AgentPlanner] 处理公告: {event_rec.source_id} {event_rec.title}")
            event_steps = self._plan_for_event(
                event_rec,
                days_to_deadline=days_to_deadline,
                author=author,
                wechat_cover=wechat_cover,
                skip_publish=skip_publish,
                include_attachment_images=include_attachment_images,
            )
            ok = self._execute_event_steps(event_steps)
            if ok:
                success_count += 1

        final_output = (
            f"计划完成：推荐 {len(recommendations)} 条，成功处理 {success_count} 条。"
        )
        self._finish_agent_run("completed", final_output)
        return {"ok": True, "drafts": success_count, "total": len(recommendations), "message": final_output}

    # ── 计划生成 ─────────────────────────────────────────────

    def _plan_for_event(
        self,
        event_rec: EventRecommendation,
        *,
        days_to_deadline: int,
        author: str,
        wechat_cover: str,
        skip_publish: bool,
        include_attachment_images: bool,
    ) -> list[PlanStep]:
        """根据事件状态动态生成步骤。"""
        event_steps: list[PlanStep] = []
        source_id = event_rec.source_id

        # 检查当前状态
        state = self._get_event_state(source_id)
        origin_status = state.get("origin_search_status", "pending")
        attachment_count = state.get("attachment_count", 0)
        attachment_downloaded = state.get("attachment_downloaded_count", 0)
        attachment_parsed = state.get("attachment_parsed_count", 0)
        has_job_tables = state.get("job_table_count", 0)
        job_tables_downloaded = state.get("job_table_downloaded_count", 0)

        step_index_base = len(self.steps)

        # ── 原公告检查 ──
        if origin_status == "pending" or (origin_status == "searched" and state.get("origin_search_attempts", 0) < 3):
            event_steps.append(PlanStep(
                step_index=step_index_base + len(event_steps) + 1,
                tool_name="search_event_origin",
                reasoning=f"原公告状态为 '{origin_status}'，自动搜索官方原文链接",
                tool_args={"source_id": source_id},
            ))
        elif origin_status == "found":
            log(f"[AgentPlanner]   原公告已找到，跳过搜索。")

        # ── 附件检查 ──
        if attachment_count == 0:
            event_steps.append(PlanStep(
                step_index=step_index_base + len(event_steps) + 1,
                tool_name="scan_attachments_metadata",
                reasoning="公告尚无附件记录，先扫描附件链接",
                tool_args={"source_id": source_id, "metadata_only": True},
            ))
            attach_state_after_scan = True  # 标记后续需重新检查
        else:
            attach_state_after_scan = False
            log(f"[AgentPlanner]   已有 {attachment_count} 个附件记录。")

        # ── 岗位表检查 ──
        need_job_tables = include_attachment_images and (has_job_tables == 0 or job_tables_downloaded == 0)
        if need_job_tables:
            if attach_state_after_scan:
                event_steps.append(PlanStep(
                    step_index=step_index_base + len(event_steps) + 1,
                    tool_name="download_job_tables",
                    reasoning="需要岗位表图片插入文章，先下载解析岗位表候选附件",
                    tool_args={"source_id": source_id, "max_attachments": 20, "job_tables_only": True},
                ))
            elif has_job_tables > 0 and job_tables_downloaded == 0:
                event_steps.append(PlanStep(
                    step_index=step_index_base + len(event_steps) + 1,
                    tool_name="download_job_tables",
                    reasoning=f"有 {has_job_tables} 个岗位表候选但尚未下载，现在下载",
                    tool_args={"source_id": source_id, "max_attachments": 20, "job_tables_only": True},
                ))
        elif include_attachment_images and job_tables_downloaded > 0:
            log(f"[AgentPlanner]   已有 {job_tables_downloaded} 个岗位表已下载，可生成图片。")

        # ── 生成文章 ──
        event_steps.append(PlanStep(
            step_index=step_index_base + len(event_steps) + 1,
            tool_name="generate_wechat_article",
            reasoning="公告状态就绪，生成公众号文章并质检",
            tool_args={
                "source_id": source_id,
                "days_to_deadline": days_to_deadline,
                "include_attachment_images": include_attachment_images,
                "author": author,
            },
        ))

        # ── 质检检查 ──
        event_steps.append(PlanStep(
            step_index=step_index_base + len(event_steps) + 1,
            tool_name="quality_check",
            reasoning="大模型质检：检查草稿是否存在与公告无关内容",
            tool_args={"source_id": source_id},
        ))

        # ── 提交草稿 ──
        if not skip_publish:
            event_steps.append(PlanStep(
                step_index=step_index_base + len(event_steps) + 1,
                tool_name="submit_wechat_draft",
                reasoning="质检通过后提交微信公众号草稿箱",
                tool_args={
                    "source_id": source_id,
                    "author": author,
                    "cover_path": wechat_cover,
                },
            ))
        else:
            event_steps.append(PlanStep(
                step_index=step_index_base + len(event_steps) + 1,
                tool_name="export_preview_only",
                reasoning="仅生成预览（--skip_publish），不提交草稿箱",
                tool_args={"source_id": source_id},
            ))

        return event_steps

    # ── 步骤执行 ─────────────────────────────────────────────

    def _execute_event_steps(self, event_steps: list[PlanStep]) -> bool:
        """按序执行一条公告的所有步骤，失败时停止。"""
        all_ok = True
        for step in event_steps:
            step.started_at = _utc_now()
            step.status = "running"
            self.steps.append(step)
            self._record_step(step, status="running")

            start = time.time()
            try:
                observation = self._dispatch_step(step)
                step.observation = observation[:8000]
                step.status = "completed"
            except Exception as exc:
                step.error_message = str(exc)[:2000]
                step.observation = str(exc)[:2000]
                step.status = "failed"
                all_ok = False
            finally:
                step.elapsed_seconds = round(time.time() - start, 2)
                step.finished_at = _utc_now()
                self._record_step(step, status=step.status)

            log(f"[AgentPlanner]   {step.tool_name}: {step.status} ({step.elapsed_seconds}s)")
            if not all_ok:
                log(f"[AgentPlanner]   错误: {step.error_message}")
                if step.tool_name == "quality_check":
                    log("[AgentPlanner]   质检未通过，停止提交草稿。")
                break
        return all_ok

    def _dispatch_step(self, step: PlanStep) -> str:
        """根据 tool_name 分发执行。"""
        source_id = str(step.tool_args.get("source_id", ""))
        tool = step.tool_name

        if tool == "search_event_origin":
            return self._tool_search_origin(source_id)
        elif tool == "scan_attachments_metadata":
            return self._tool_scan_attachments(source_id, metadata_only=True)
        elif tool == "download_job_tables":
            max_att = int(step.tool_args.get("max_attachments", 20))
            return self._tool_download_job_tables(source_id, max_att)
        elif tool == "generate_wechat_article":
            return self._tool_generate_article(step.tool_args)
        elif tool == "quality_check":
            return self._tool_quality_check(source_id)
        elif tool == "submit_wechat_draft":
            return self._tool_submit_draft(step.tool_args)
        elif tool == "export_preview_only":
            return self._tool_export_preview(source_id)
        else:
            return f"未知工具: {tool}"

    # ── 工具实现（in-process）─────────────────────────────────

    def _tool_search_origin(self, source_id: str) -> str:
        """搜索原公告原文。"""
        from .gongkaoleida_crawler import (
            _search_public_source,
            _fetch_html_text,
            _is_origin_text_match,
            _should_skip_origin_url,
        )

        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT source_platform, source_id, title, source_origin_url FROM gongkao_events WHERE source_id = ?",
                (source_id,),
            ).fetchone()
        if not row:
            return f"未找到公告 {source_id}"

        source_platform = row["source_platform"]
        sp_id = row["source_id"]
        title = row["title"] or ""

        # 尝试 DuckDuckGo 搜索
        candidate_url, _ = _search_public_source(title)
        if not candidate_url or _should_skip_origin_url(candidate_url):
            with _connect(self.db_path) as conn:
                conn.execute(
                    """UPDATE gongkao_events
                       SET origin_search_status = 'not_found',
                           origin_search_attempts = origin_search_attempts + 1,
                           origin_last_checked_at = ?
                       WHERE source_platform = ? AND source_id = ?""",
                    (_utc_now(), source_platform, sp_id),
                )
                conn.commit()
            return f"未搜索到原公告链接"

        # 尝试抓取
        import asyncio
        text = ""
        html = ""
        try:
            from playwright.async_api import async_playwright

            async def _fetch():
                async with async_playwright() as p:
                    browser = await p.chromium.launch(channel="chrome", headless=True)
                    page = await browser.new_page()
                    try:
                        t, h = await _fetch_html_text(page, candidate_url)
                        return t, h
                    finally:
                        await browser.close()

            text, html = asyncio.run(_fetch())
        except Exception:
            text, html = "", ""

        if text and _is_origin_text_match(title, text, candidate_url):
            with _connect(self.db_path) as conn:
                conn.execute(
                    """UPDATE gongkao_events
                       SET source_origin_url = ?,
                           source_origin_text = ?,
                           source_origin_html = ?,
                           origin_search_status = 'found',
                           origin_search_attempts = origin_search_attempts + 1,
                           origin_last_checked_at = ?
                       WHERE source_platform = ? AND source_id = ?""",
                    (candidate_url, text, html, _utc_now(), source_platform, sp_id),
                )
                conn.commit()
            return f"原公告已找到: {candidate_url}"
        else:
            with _connect(self.db_path) as conn:
                conn.execute(
                    """UPDATE gongkao_events
                       SET origin_search_status = 'searched',
                           origin_search_attempts = origin_search_attempts + 1,
                           origin_last_checked_at = ?
                       WHERE source_platform = ? AND source_id = ?""",
                    (_utc_now(), source_platform, sp_id),
                )
                conn.commit()
            return f"搜索到候选链接但内容不匹配: {candidate_url}"

    def _tool_scan_attachments(self, source_id: str, *, metadata_only: bool) -> str:
        """扫描附件链接元数据。"""
        try:
            from .gongkao_attachments import _fetch_events, process_event

            rows = _fetch_events(self.db_path, [source_id], 1, False)
            if not rows:
                return "未找到需要扫描的公告"
            registered, downloaded, parsed, failed = process_event(
                self.db_path, rows[0], max_attachments=0,
                use_office_com=False, metadata_only=True, job_tables_only=False,
            )
            return f"附件扫描完成: 登记 {registered} 个"
        except Exception as exc:
            return f"附件扫描失败: {exc}"

    def _tool_download_job_tables(self, source_id: str, max_attachments: int) -> str:
        """下载解析岗位表附件。"""
        try:
            from .gongkao_attachments import _fetch_events, process_event

            rows = _fetch_events(self.db_path, [source_id], 1, False)
            if not rows:
                return "未找到需要处理的公告"
            registered, downloaded, parsed, failed = process_event(
                self.db_path, rows[0], max_attachments=max_attachments,
                use_office_com=False, metadata_only=False, job_tables_only=True,
            )
            return f"岗位表处理: 下载 {downloaded} 个, 解析 {parsed} 个, 失败 {failed} 个"
        except Exception as exc:
            return f"岗位表下载失败: {exc}"

    def _tool_generate_article(self, tool_args: dict[str, Any]) -> str:
        """生成公众号文章并跑质检。"""
        from .gongkao_wechat_pipeline import (
            pick_gongkao_event,
            build_gongkao_payload,
            _run_quality_check,
            _save_payload,
        )
        from .wechat_pipeline import (
            export_wechat_markdown,
            convert_markdown_to_wechat_html,
            save_wechat_payload_preview,
        )

        source_id = str(tool_args["source_id"])
        days_to_deadline = int(tool_args.get("days_to_deadline", 30))
        include_images = bool(tool_args.get("include_attachment_images", False))
        author = str(tool_args.get("author", "岸上信息站"))

        event = pick_gongkao_event(
            self.db_path,
            topic_id=source_id,
            category="",
            region="",
            status="",
            require_deadline=False,
            days_to_deadline=days_to_deadline,
        )
        payload = build_gongkao_payload(event, include_attachment_images=include_images)
        payload_path = _save_payload(payload, event.source_id)
        preview_path = save_wechat_payload_preview(payload, source_id=event.source_id)
        markdown_path = export_wechat_markdown(payload, source_id=event.source_id, account_name=author)
        html_path = convert_markdown_to_wechat_html(markdown_path, theme="tech")

        return (
            f"文章已生成: title={payload['draft']['title'][:60]}\n"
            f"payload: {payload_path}\n"
            f"preview: {preview_path}\n"
            f"markdown: {markdown_path}\n"
            f"html: {html_path}"
        )

    def _tool_quality_check(self, source_id: str) -> str:
        """对草稿运行大模型质检。"""
        from .gongkao_wechat_pipeline import (
            pick_gongkao_event,
            build_gongkao_payload,
            _run_quality_check,
        )

        event = pick_gongkao_event(
            self.db_path,
            topic_id=source_id,
            category="",
            region="",
            status="",
            require_deadline=False,
            days_to_deadline=30,
        )
        payload = build_gongkao_payload(event, include_attachment_images=False)
        report_path = _run_quality_check(event, payload)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("pass"):
            return f"质检通过: {report.get('model_result', {}).get('summary', '通过')}"
        else:
            issues = report.get("model_result", {}).get("issues", [])
            raise RuntimeError(f"质检未通过: {'; '.join(issues[:5])}")

    def _tool_submit_draft(self, tool_args: dict[str, Any]) -> str:
        """提交微信公众号草稿箱。"""
        from .gongkao_wechat_pipeline import (
            pick_gongkao_event,
            build_gongkao_payload,
        )
        from .wechat_pipeline import (
            export_wechat_markdown,
            convert_markdown_to_wechat_html,
            publish_html_to_wechat_draft,
        )

        source_id = str(tool_args["source_id"])
        author = str(tool_args.get("author", "岸上信息站"))
        cover_path = str(tool_args.get("cover_path", ""))

        event = pick_gongkao_event(
            self.db_path,
            topic_id=source_id,
            category="",
            region="",
            status="",
            require_deadline=False,
            days_to_deadline=30,
        )
        payload = build_gongkao_payload(event, include_attachment_images=False)
        draft = payload.get("draft") or {}
        title = str(draft.get("title") or "")
        digest = str(draft.get("digest") or "")

        markdown_path = export_wechat_markdown(payload, source_id=event.source_id, account_name=author)
        html_path = convert_markdown_to_wechat_html(markdown_path, theme="tech")

        publish_html_to_wechat_draft(
            title=title,
            html_path=html_path,
            author=author,
            cover_path=cover_path,
            digest=digest,
            submit_publish=False,
            source_platform=event.source_platform,
            source_id=event.source_id,
        )
        return f"草稿已提交: title={title[:60]}"

    def _tool_export_preview(self, source_id: str) -> str:
        """仅导出预览文件。"""
        from .gongkao_wechat_pipeline import (
            pick_gongkao_event,
            build_gongkao_payload,
            _save_payload,
        )
        from .wechat_pipeline import (
            export_wechat_markdown,
            convert_markdown_to_wechat_html,
            save_wechat_payload_preview,
        )

        event = pick_gongkao_event(
            self.db_path,
            topic_id=source_id,
            category="",
            region="",
            status="",
            require_deadline=False,
            days_to_deadline=30,
        )
        payload = build_gongkao_payload(event, include_attachment_images=False)
        payload_path = _save_payload(payload, event.source_id)
        preview_path = save_wechat_payload_preview(payload, source_id=event.source_id)
        markdown_path = export_wechat_markdown(payload, source_id=event.source_id, account_name="岸上信息站")
        html_path = convert_markdown_to_wechat_html(markdown_path, theme="tech")

        return (
            f"预览已导出:\n"
            f"payload: {payload_path}\n"
            f"preview: {preview_path}\n"
            f"markdown: {markdown_path}\n"
            f"html: {html_path}"
        )

    # ── 状态查询 ─────────────────────────────────────────────

    def _get_event_state(self, source_id: str) -> dict[str, Any]:
        """获取公告当前状态。"""
        with _connect(self.db_path) as conn:
            row = conn.execute(
                """SELECT
                    coalesce(origin_search_status, 'pending') AS origin_search_status,
                    coalesce(origin_search_attempts, 0) AS origin_search_attempts,
                    coalesce(source_origin_url, '') AS source_origin_url,
                    (SELECT count(*) FROM gongkao_event_attachments a
                     WHERE a.event_source_id = e.source_id) AS attachment_count,
                    (SELECT count(*) FROM gongkao_event_attachments a
                     WHERE a.event_source_id = e.source_id
                       AND coalesce(a.download_status, '') = 'downloaded') AS attachment_downloaded_count,
                    (SELECT count(*) FROM gongkao_event_attachments a
                     WHERE a.event_source_id = e.source_id
                       AND coalesce(a.parse_status, '') = 'parsed') AS attachment_parsed_count,
                    (SELECT count(*) FROM gongkao_event_attachments a
                     WHERE a.event_source_id = e.source_id
                       AND (a.name LIKE '%岗位表%' OR a.name LIKE '%职位表%'
                            OR a.name LIKE '%招聘计划%' OR a.name LIKE '%岗位需求%')) AS job_table_count,
                    (SELECT count(*) FROM gongkao_event_attachments a
                     WHERE a.event_source_id = e.source_id
                       AND (a.name LIKE '%岗位表%' OR a.name LIKE '%职位表%'
                            OR a.name LIKE '%招聘计划%' OR a.name LIKE '%岗位需求%')
                       AND coalesce(a.download_status, '') = 'downloaded') AS job_table_downloaded_count
                FROM gongkao_events e
                WHERE e.source_id = ?""",
                (source_id,),
            ).fetchone()
        return dict(row) if row else {}

    def _execute_query_recommendations(self, count: int, status: str) -> list[EventRecommendation]:
        """Step 0: 查询推荐公告。"""
        step = PlanStep(
            step_index=1,
            tool_name="query_recommendations",
            reasoning=f"查询推荐引擎，获取 {count} 条最适合发布的{status}公告",
            tool_args={"limit": count, "status": status},
        )
        step.started_at = _utc_now()
        step.status = "running"
        self.steps.append(step)
        self._record_step(step, status="running")
        start = time.time()

        try:
            recs = recommend_events(
                db_path=self.db_path,
                limit=count,
                include_published=False,
                status=status,
            )
            step.observation = json.dumps(
                [{"id": r.source_id, "score": r.score, "title": r.title, "reasons": r.reasons}
                 for r in recs],
                ensure_ascii=False,
            )[:8000]
            step.status = "completed"
        except Exception as exc:
            step.error_message = str(exc)[:2000]
            step.status = "failed"

        step.elapsed_seconds = round(time.time() - start, 2)
        step.finished_at = _utc_now()
        self._record_step(step, status=step.status)
        return recs if step.status == "completed" else []

    # ── Agent Run / Step 记录 ─────────────────────────────────

    def _start_agent_run(self, objective: str) -> None:
        now = _utc_now()
        input_json = json.dumps({"task_id": self.task_id, "objective": objective}, ensure_ascii=False)
        with _connect(self.db_path) as conn:
            cursor = conn.execute(
                """INSERT INTO agent_runs(task_id, objective, status, trigger_source, input_json, started_at)
                   VALUES (?, ?, 'running', 'agent_planner', ?, ?)""",
                (self.task_id, objective, input_json, now),
            )
            self.run_id = int(cursor.lastrowid)
            conn.commit()
        log(f"[AgentPlanner] Agent Run #{self.run_id} started: {objective}")

    def _record_step(self, step: PlanStep, *, status: str) -> None:
        with _connect(self.db_path) as conn:
            existing = conn.execute(
                "SELECT id FROM agent_steps WHERE run_id = ? AND step_index = ?",
                (self.run_id, step.step_index),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE agent_steps
                       SET tool_name = ?, tool_args_json = ?, status = ?,
                           observation = ?, error_message = ?,
                           finished_at = ?, elapsed_seconds = ?
                       WHERE id = ?""",
                    (
                        step.tool_name,
                        json.dumps(step.tool_args, ensure_ascii=False),
                        status,
                        step.observation,
                        step.error_message,
                        step.finished_at,
                        step.elapsed_seconds,
                        existing["id"],
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO agent_steps(run_id, step_index, tool_name, tool_args_json, status,
                           observation, error_message, started_at, finished_at, elapsed_seconds)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        self.run_id,
                        step.step_index,
                        step.tool_name,
                        json.dumps(step.tool_args, ensure_ascii=False),
                        status,
                        step.observation,
                        step.error_message,
                        step.started_at,
                        step.finished_at,
                        step.elapsed_seconds,
                    ),
                )
            conn.commit()

    def _finish_agent_run(self, status: str, final_output: str) -> None:
        now = _utc_now()
        with _connect(self.db_path) as conn:
            run = conn.execute("SELECT started_at FROM agent_runs WHERE id = ?", (self.run_id,)).fetchone()
            elapsed = 0.0
            if run and run["started_at"]:
                try:
                    start_dt = datetime.fromisoformat(run["started_at"])
                    elapsed = (datetime.utcnow() - start_dt).total_seconds()
                except Exception:
                    pass
            conn.execute(
                """UPDATE agent_runs
                   SET status = ?, final_output = ?, finished_at = ?, elapsed_seconds = ?
                   WHERE id = ?""",
                (status, final_output, now, elapsed, self.run_id),
            )
            conn.commit()
        log(f"[AgentPlanner] Agent Run #{self.run_id} finished: {status}")


# ── Helpers ───────────────────────────────────────────────────


def log(msg: str) -> None:
    print(msg, flush=True)


def _resolve_cover(path_str: str) -> str:
    if path_str:
        return path_str
    for candidate in (CONFIG.workspace_root / "wechat_cover.png", CONFIG.workspace_root / "考试通知.png"):
        if candidate.exists():
            return str(candidate)
    return ""


# ── CLI ───────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan and generate today's recommended WeChat drafts.")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--days_to_deadline", type=int, default=30)
    parser.add_argument("--author", default="岸上信息站")
    parser.add_argument("--wechat_cover", default="")
    parser.add_argument("--skip_publish", action="store_true", help="Only generate previews, do not submit drafts.")
    parser.add_argument("--include_attachment_images", action="store_true")
    parser.add_argument("--skip_attachment_download", action="store_true")
    args = parser.parse_args()

    count = max(1, min(args.count, 10))
    cover = _resolve_cover(args.wechat_cover)
    objective = f"选择今天最适合发布的 {count} 条公告并生成公众号{'预览' if args.skip_publish else '草稿'}"

    log("=" * 72)
    log(f"[today-agent] 目标：{objective}")

    planner = GongkaoAgentPlanner()
    result = planner.plan_and_execute(
        objective=objective,
        count=count,
        days_to_deadline=args.days_to_deadline,
        author=args.author,
        wechat_cover=cover,
        skip_publish=args.skip_publish,
        include_attachment_images=args.include_attachment_images and not args.skip_attachment_download,
    )

    log("=" * 72)
    log(f"[today-agent] {result['message']}")

    if result.get("ok"):
        log("[today-agent] 步骤明细（Agent Planner 自动决策）：")
        for step in planner.steps:
            icon = "✅" if step.status == "completed" else "❌"
            log(f"  {icon} Step {step.step_index}: {step.tool_name} — {step.reasoning[:80]}")
            if step.observation:
                first_line = step.observation.split("\n")[0][:120]
                log(f"     → {first_line}")
    else:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
