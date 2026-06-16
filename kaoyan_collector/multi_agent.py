# -*- coding: utf-8 -*-
"""多 Agent 协作架构（Multi-Agent Orchestrator）

将考公运营流程拆分为 4 个专业 Agent，通过编排器协调：
- CrawlerAgent：数据采集与原文搜索
- EditorAgent：内容生成与排版
- QAAgent：大模型质检与事实核验
- PublisherAgent：公众号草稿提交与发布

Agent 之间通过统一的消息协议通信，编排器管理上下文传递和错误回退。

用法：
    orchestrator = MultiAgentOrchestrator()
    result = orchestrator.run(source_id="466990783814656")

    # 流水线模式：推荐 → 采集 → 编辑 → 质检 → 发布
    result = orchestrator.run_pipeline(count=3)
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .config import CONFIG
from .schema import init_db


# ── 消息协议 ──────────────────────────────────────────────────


class AgentRole(Enum):
    CRAWLER = "crawler"
    EDITOR = "editor"
    QA = "qa"
    PUBLISHER = "publisher"
    ORCHESTRATOR = "orchestrator"


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    NEEDS_HUMAN = "needs_human"


@dataclass
class AgentMessage:
    """Agent 间通信的消息协议。"""
    msg_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    from_role: AgentRole = AgentRole.ORCHESTRATOR
    to_role: AgentRole = AgentRole.ORCHESTRATOR
    task_type: str = ""                      # 任务类型标识
    payload: dict[str, Any] = field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    observation: str = ""                    # Agent 观察/输出
    reasoning: str = ""                      # 决策原因
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    elapsed_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Base Agent ────────────────────────────────────────────────


class BaseAgent:
    """所有 Agent 的基类。"""

    role: AgentRole
    description: str = ""
    tools: list[str] = field(default_factory=list)

    def __init__(self, name: str = "") -> None:
        self.name = name or self.role.value
        self.log: list[AgentMessage] = []
        self.context: dict[str, Any] = {}

    def receive(self, msg: AgentMessage) -> AgentMessage:
        """接收消息并处理，返回结果消息。"""
        msg.started_at = datetime.utcnow().isoformat()
        msg.status = TaskStatus.RUNNING
        start = time.time()

        try:
            result = self._handle(msg)
            msg.status = TaskStatus.COMPLETED
            msg.observation = result
        except Exception as exc:
            msg.status = TaskStatus.FAILED
            msg.error = str(exc)[:2000]
            msg.observation = str(exc)[:2000]

        msg.elapsed_ms = round((time.time() - start) * 1000, 1)
        msg.finished_at = datetime.utcnow().isoformat()
        self.log.append(msg)
        return msg

    def _handle(self, msg: AgentMessage) -> str:
        """子类必须实现的具体处理逻辑。"""
        raise NotImplementedError

    def status_report(self) -> dict[str, Any]:
        """返回 Agent 运行状态报告。"""
        completed = sum(1 for m in self.log if m.status == TaskStatus.COMPLETED)
        failed = sum(1 for m in self.log if m.status == TaskStatus.FAILED)
        return {
            "role": self.role.value,
            "name": self.name,
            "tasks_completed": completed,
            "tasks_failed": failed,
            "total_tasks": len(self.log),
            "last_active": self.log[-1].finished_at if self.log else "",
        }


# ── 专业 Agent 实现 ──────────────────────────────────────────


class CrawlerAgent(BaseAgent):
    """数据采集 Agent：负责从粉笔/公考雷达采集公告，搜索原文。"""

    role = AgentRole.CRAWLER
    description = "负责从粉笔和公考雷达采集考试公告，补全官方原文链接"
    tools = ["crawl_fenbi_tool", "crawl_gongkaoleida_tool", "search_event_origin"]

    def __init__(self) -> None:
        super().__init__("CrawlerAgent")

    def _handle(self, msg: AgentMessage) -> str:
        task = msg.task_type
        payload = msg.payload
        source_id = str(payload.get("source_id", ""))

        if task == "check_origin":
            return self._check_origin(source_id)
        elif task == "search_origin":
            return self._search_origin(source_id)
        elif task == "get_event_info":
            return self._get_event_info(source_id)
        elif task == "collect_new":
            category = str(payload.get("category", "事业单位"))
            max_items = int(payload.get("max_items", 20))
            return self._collect_new(category, max_items)
        else:
            return f"[CrawlerAgent] 未知任务类型: {task}"

    def _check_origin(self, source_id: str) -> str:
        """检查公告是否有原公告。"""
        import sqlite3
        conn = sqlite3.connect(str(CONFIG.database_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT source_origin_url, origin_search_status FROM gongkao_events WHERE source_id=?",
            (source_id,)
        ).fetchone()
        conn.close()
        if row and row["source_origin_url"]:
            return f"原公告已存在: {row['source_origin_url'][:80]}"
        return f"原公告状态: {row['origin_search_status'] if row else '未知'}"

    def _search_origin(self, source_id: str) -> str:
        """搜索公告原文。"""
        from .gongkaoleida_crawler import _search_public_source
        import sqlite3
        conn = sqlite3.connect(str(CONFIG.database_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT title, source_platform FROM gongkao_events WHERE source_id=?",
            (source_id,)
        ).fetchone()
        conn.close()
        if not row:
            return f"未找到公告 {source_id}"
        url, _ = _search_public_source(row["title"])
        if url:
            return f"搜索到候选链接: {url}"
        return "未搜索到原公告链接"

    def _get_event_info(self, source_id: str) -> str:
        """获取公告关键信息摘要。"""
        import sqlite3
        conn = sqlite3.connect(str(CONFIG.database_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT title, region, category, job_count, registration_deadline, status,
                      source_origin_url, (SELECT count(*) FROM gongkao_event_attachments a
                       WHERE a.event_source_id = e.source_id) AS att_count
               FROM gongkao_events e WHERE source_id=?""",
            (source_id,)
        ).fetchone()
        conn.close()
        if not row:
            return f"未找到公告 {source_id}"
        return json.dumps({
            "title": row["title"], "region": row["region"], "category": row["category"],
            "job_count": row["job_count"], "deadline": row["registration_deadline"],
            "status": row["status"], "has_origin": bool(row["source_origin_url"]),
            "attachment_count": row["att_count"],
        }, ensure_ascii=False)

    def _collect_new(self, category: str, max_items: int) -> str:
        """采集新公告。"""
        from .fenbi_crawler import crawl_fenbi, _normalize_exam_type
        from .store import ContentStore
        exam_type = _normalize_exam_type(category)
        events = crawl_fenbi(max_items=max_items, page=1, exam_type=exam_type,
                             year=str(datetime.now().year))
        store = ContentStore(CONFIG.database_path)
        for e in events:
            store.upsert_gongkao_event(e.__dict__)
        return f"采集完成: {len(events)} 条公告已入库"


class EditorAgent(BaseAgent):
    """内容编辑 Agent：负责生成公众号标题、正文和排版。"""

    role = AgentRole.EDITOR
    description = "负责根据公告信息生成公众号文章，含标题模板引擎和适合人群提醒"
    tools = ["wechat_article_generate_tool", "attachment_image_tool"]

    def __init__(self) -> None:
        super().__init__("EditorAgent")

    def _handle(self, msg: AgentMessage) -> str:
        task = msg.task_type
        payload = msg.payload
        source_id = str(payload.get("source_id", ""))

        if task == "generate_draft":
            include_images = bool(payload.get("include_attachment_images", False))
            return self._generate_draft(source_id, include_images)
        elif task == "generate_title_only":
            return self._generate_title_only(source_id)
        else:
            return f"[EditorAgent] 未知任务类型: {task}"

    def _generate_draft(self, source_id: str, include_images: bool) -> str:
        from .gongkao_wechat_pipeline import pick_gongkao_event, build_gongkao_payload, _save_payload
        from .wechat_pipeline import export_wechat_markdown, convert_markdown_to_wechat_html

        event = pick_gongkao_event(CONFIG.database_path, topic_id=source_id,
                                    category="", region="", status="",
                                    require_deadline=False, days_to_deadline=30)
        payload = build_gongkao_payload(event, include_attachment_images=include_images)
        _save_payload(payload, event.source_id)
        markdown_path = export_wechat_markdown(payload, source_id=event.source_id, account_name="岸上信息站")
        html_path = convert_markdown_to_wechat_html(markdown_path, theme="tech")

        draft = payload["draft"]
        return json.dumps({
            "title": draft["title"],
            "word_count": len(draft["content"]),
            "has_audience_hint": "适合人群" in draft["content"],
            "payload_path": str(markdown_path),
            "html_path": str(html_path),
        }, ensure_ascii=False)

    def _generate_title_only(self, source_id: str) -> str:
        from .gongkao_wechat_pipeline import pick_gongkao_event, _build_wechat_title
        event = pick_gongkao_event(CONFIG.database_path, topic_id=source_id,
                                    category="", region="", status="",
                                    require_deadline=False, days_to_deadline=30)
        return _build_wechat_title(event)


class QAAgent(BaseAgent):
    """质检 Agent：负责大模型质量检查和事实一致性验证。"""

    role = AgentRole.QA
    description = "负责调用 DeepSeek 进行草稿质检，以及事实一致性规则检查"
    tools = ["quality_check_tool", "fact_consistency_check"]

    def __init__(self) -> None:
        super().__init__("QAAgent")

    def _handle(self, msg: AgentMessage) -> str:
        task = msg.task_type
        payload = msg.payload
        source_id = str(payload.get("source_id", ""))

        if task == "quality_check":
            return self._quality_check(source_id)
        elif task == "fact_check":
            return self._fact_check(source_id)
        elif task == "diagnose_error":
            return self._diagnose(str(payload.get("error_text", "")))
        else:
            return f"[QAAgent] 未知任务类型: {task}"

    def _quality_check(self, source_id: str) -> str:
        from .gongkao_wechat_pipeline import pick_gongkao_event, build_gongkao_payload, _run_quality_check

        event = pick_gongkao_event(CONFIG.database_path, topic_id=source_id,
                                    category="", region="", status="",
                                    require_deadline=False, days_to_deadline=30)
        payload = build_gongkao_payload(event, include_attachment_images=False)
        try:
            report_path = _run_quality_check(event, payload)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            return json.dumps({
                "pass": report.get("pass", False),
                "summary": report.get("model_result", {}).get("summary", ""),
                "issues": report.get("model_result", {}).get("issues", []),
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"pass": False, "summary": str(exc), "issues": [str(exc)]}, ensure_ascii=False)

    def _fact_check(self, source_id: str) -> str:
        from .eval_fact_consistency import evaluate_event
        from .gongkao_wechat_pipeline import pick_gongkao_event

        event = pick_gongkao_event(CONFIG.database_path, topic_id=source_id,
                                    category="", region="", status="",
                                    require_deadline=False, days_to_deadline=30)
        result = evaluate_event(event)
        return json.dumps({
            "overall_pass": result.overall_pass,
            "job_count_pass": result.job_count_pass,
            "region_pass": result.region_pass,
            "deadline_pass": result.deadline_pass,
            "source_url_pass": result.source_url_pass,
            "noise_pass": result.noise_pass,
        }, ensure_ascii=False)

    def _diagnose(self, error_text: str) -> str:
        from .error_diagnostics import diagnose_error_as_dict
        return json.dumps(diagnose_error_as_dict(error_text), ensure_ascii=False)


class PublisherAgent(BaseAgent):
    """发布 Agent：负责提交公众号草稿和管理发布状态。"""

    role = AgentRole.PUBLISHER
    description = "负责提交微信公众号草稿箱，管理发布状态"
    tools = ["wechat_draft_submit_tool", "check_wechat_token"]

    def __init__(self) -> None:
        super().__init__("PublisherAgent")

    def _handle(self, msg: AgentMessage) -> str:
        task = msg.task_type
        payload = msg.payload
        source_id = str(payload.get("source_id", ""))

        if task == "submit_draft":
            return self._submit_draft(source_id, payload)
        elif task == "check_token":
            return self._check_token()
        elif task == "export_preview":
            return self._export_preview(source_id)
        else:
            return f"[PublisherAgent] 未知任务类型: {task}"

    def _submit_draft(self, source_id: str, payload: dict[str, Any]) -> str:
        from .gongkao_wechat_pipeline import pick_gongkao_event, build_gongkao_payload
        from .wechat_pipeline import export_wechat_markdown, convert_markdown_to_wechat_html, publish_html_to_wechat_draft

        author = str(payload.get("author", "岸上信息站"))
        cover = str(payload.get("wechat_cover", ""))

        event = pick_gongkao_event(CONFIG.database_path, topic_id=source_id,
                                    category="", region="", status="",
                                    require_deadline=False, days_to_deadline=30)
        p = build_gongkao_payload(event, include_attachment_images=False)
        draft = p.get("draft") or {}
        title = str(draft.get("title") or "")
        digest = str(draft.get("digest") or "")

        md_path = export_wechat_markdown(p, source_id=event.source_id, account_name=author)
        html_path = convert_markdown_to_wechat_html(md_path, theme="tech")

        publish_html_to_wechat_draft(
            title=title, html_path=html_path, author=author, cover_path=cover,
            digest=digest, submit_publish=False,
            source_platform=event.source_platform, source_id=event.source_id,
        )
        return json.dumps({"title": title, "submitted": True}, ensure_ascii=False)

    def _check_token(self) -> str:
        from urllib.request import urlopen
        config_path = Path.home() / ".wechat-publisher" / "config.json"
        if not config_path.exists():
            return "微信配置未找到"
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        url = f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={cfg['appid']}&secret={cfg['appsecret']}"
        try:
            with urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return json.dumps({"valid": "access_token" in data, "expires_in": data.get("expires_in", 0)}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False)

    def _export_preview(self, source_id: str) -> str:
        from .gongkao_wechat_pipeline import pick_gongkao_event, build_gongkao_payload
        from .wechat_pipeline import export_wechat_markdown, convert_markdown_to_wechat_html, save_wechat_payload_preview

        event = pick_gongkao_event(CONFIG.database_path, topic_id=source_id,
                                    category="", region="", status="",
                                    require_deadline=False, days_to_deadline=30)
        p = build_gongkao_payload(event, include_attachment_images=False)
        preview_path = save_wechat_payload_preview(p, source_id=event.source_id)
        return json.dumps({"preview_path": str(preview_path)}, ensure_ascii=False)


# ── 编排器 ────────────────────────────────────────────────────


@dataclass
class PipelineStep:
    """流水线步骤定义。"""
    agent: BaseAgent
    task_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""


class MultiAgentOrchestrator:
    """多 Agent 编排器。

    协调 CrawlerAgent / EditorAgent / QAAgent / PublisherAgent 的工作流。
    支持单公告全流程和批量流水线两种模式。
    """

    def __init__(self) -> None:
        self.crawler = CrawlerAgent()
        self.editor = EditorAgent()
        self.qa = QAAgent()
        self.publisher = PublisherAgent()
        self.all_agents = [self.crawler, self.editor, self.qa, self.publisher]
        self.agents: dict[AgentRole, BaseAgent] = {
            AgentRole.CRAWLER: self.crawler,
            AgentRole.EDITOR: self.editor,
            AgentRole.QA: self.qa,
            AgentRole.PUBLISHER: self.publisher,
        }
        self.history: list[AgentMessage] = []
        self._run_id: str = ""

    # ── 公开 API ─────────────────────────────────────────────

    def run(self, *, source_id: str, include_images: bool = False,
            skip_publish: bool = False, wechat_cover: str = "") -> dict[str, Any]:
        """对单条公告执行完整 Agent 流程。"""
        self._run_id = uuid.uuid4().hex[:8]
        self.history.clear()
        results: dict[str, Any] = {"run_id": self._run_id, "source_id": source_id}

        # Step 1: CrawlerAgent 获取公告信息
        msg1 = self._send(AgentRole.CRAWLER, "get_event_info", {"source_id": source_id},
                          reasoning="获取公告关键信息和状态")
        results["crawler"] = self._safe_json(msg1.observation)

        # Step 2: CrawlerAgent 检查原公告
        msg2 = self._send(AgentRole.CRAWLER, "check_origin", {"source_id": source_id},
                          reasoning="检查是否已有原公告链接")
        results["origin"] = self._safe_json(msg2.observation)
        if msg2.status == TaskStatus.COMPLETED:
            self.msg.reasoning = "原公告已找到，跳过搜索" if "已存在" in str(msg2.observation) else "需要搜索原公告"

        # Step 3: EditorAgent 生成文章
        msg3 = self._send(AgentRole.EDITOR, "generate_draft",
                          {"source_id": source_id, "include_attachment_images": include_images},
                          reasoning="生成公众号文章（标题+正文+适合人群提醒）")
        results["editor"] = self._safe_json(msg3.observation)

        # Step 4: QAAgent 质检
        msg4 = self._send(AgentRole.QA, "quality_check", {"source_id": source_id},
                          reasoning="大模型质检：检查草稿是否有无关内容和事实错误")
        results["qa"] = self._safe_json(msg4.observation)

        # Step 5: 根据质检结果决定是否发布
        qa_result = self._safe_json(msg4.observation)
        if qa_result.get("pass"):
            self.msg.reasoning = "质检通过，准备提交草稿箱"
            if not skip_publish:
                msg5 = self._send(AgentRole.PUBLISHER, "submit_draft",
                                  {"source_id": source_id, "wechat_cover": wechat_cover},
                                  reasoning="提交微信公众号草稿箱")
                results["publisher"] = self._safe_json(msg5.observation)
            else:
                msg5 = self._send(AgentRole.PUBLISHER, "export_preview",
                                  {"source_id": source_id},
                                  reasoning="仅导出预览（跳过发布）")
                results["publisher"] = self._safe_json(msg5.observation)
        else:
            self.msg.reasoning = f"质检未通过，停止发布。原因: {qa_result.get('summary', '未知')}"
            results["publisher"] = {"blocked": True, "reason": qa_result.get("summary", "")}

        # Step 6: QAAgent 事实一致性评测
        msg6 = self._send(AgentRole.QA, "fact_check", {"source_id": source_id},
                          reasoning="事实一致性评测：量化检查关键字段保留情况")
        results["fact_check"] = self._safe_json(msg6.observation)

        results["agent_status"] = {a.role.value: a.status_report() for a in self.all_agents}
        results["message_count"] = len(self.history)
        return results

    def run_pipeline(self, *, count: int = 3, status: str = "正在报名",
                     include_images: bool = False, skip_publish: bool = False) -> dict[str, Any]:
        """批量流水线：推荐 → 逐条处理。"""
        from .gongkao_recommender import recommend_events

        self._run_id = uuid.uuid4().hex[:8]
        self.history.clear()

        # 推荐
        recs = recommend_events(limit=count, include_published=False, status=status)
        results: dict[str, Any] = {
            "run_id": self._run_id,
            "recommendations": [{"id": r.source_id, "score": r.score, "title": r.title,
                                  "reasons": r.reasons} for r in recs],
            "results": [],
        }

        for i, rec in enumerate(recs):
            self._emit_event("progress", f"处理 {i+1}/{len(recs)}: {rec.title[:40]}...")
            r = self.run(source_id=rec.source_id, include_images=include_images,
                         skip_publish=skip_publish)
            results["results"].append(r)

        results["agent_status"] = {a.role.value: a.status_report() for a in self.all_agents}
        return results

    def status(self) -> dict[str, Any]:
        """获取所有 Agent 的运行状态。"""
        return {
            "orchestrator_run_id": self._run_id,
            "history_count": len(self.history),
            "agents": {a.role.value: a.status_report() for a in self.all_agents},
        }

    # ── 内部方法 ──────────────────────────────────────────────

    def _send(self, role: AgentRole, task_type: str, payload: dict[str, Any],
              reasoning: str = "") -> AgentMessage:
        """向指定 Agent 发送消息并等待响应。"""
        agent = self.agents[role]
        msg = AgentMessage(
            from_role=AgentRole.ORCHESTRATOR,
            to_role=role,
            task_type=task_type,
            payload=payload,
            reasoning=reasoning,
        )
        self._emit_event("agent_call", f"{role.value} → {task_type}: {reasoning}")
        result = agent.receive(msg)
        self.history.append(result)

        status_icon = "✅" if result.status == TaskStatus.COMPLETED else "❌"
        self._emit_event("agent_result",
                         f"{status_icon} {role.value} {task_type}: {result.status.value} "
                         f"({result.elapsed_ms:.0f}ms)")
        if result.error:
            self._emit_event("agent_error", f"  ⚠ {result.error[:200]}")
        return result

    def _emit_event(self, event_type: str, message: str) -> None:
        """发出事件（供外部监听器使用）。"""
        print(f"[Orchestrator] {message}", flush=True)

    def _safe_json(self, text: str) -> dict[str, Any]:
        """安全解析 JSON。"""
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return {"raw": str(text)[:500]}
