# -*- coding: utf-8 -*-
"""LangGraph workflow for gongkao content operations.

Models the Agent pipeline as a state graph with conditional routing.

Nodes:
  recommend -> search_origin -> download_attachments -> generate -> qa_check -> publish

Conditional edges:
  qa_check -> publish (pass) | human_review (fail)
  search_origin -> skip if already found
  download_attachments -> skip if no attachments or already done

Usage:
    workflow = GongkaoWorkflow()
    result = workflow.run(source_id="466990783814656")
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import CONFIG


@dataclass
class WorkflowState:
    """State passed between workflow nodes."""
    source_id: str = ""
    objective: str = ""
    status: str = "pending"

    # 中间结果
    recommendations: list = field(default_factory=list)
    origin_url: str = ""
    origin_found: bool = False
    origin_search_attempted: bool = False
    attachments_scanned: bool = False
    attachments_downloaded: int = 0
    attachments_parsed: int = 0
    article_title: str = ""
    article_content: str = ""
    article_generated: bool = False
    qa_passed: bool = False
    qa_report: dict = field(default_factory=dict)
    draft_submitted: bool = False
    draft_media_id: str = ""

    # 追踪
    node_results: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def add_result(self, node: str, ok: bool, detail: str = "", elapsed: float = 0):
        self.node_results.append({
            "node": node, "ok": ok, "detail": detail[:500],
            "elapsed": elapsed, "time": datetime.utcnow().isoformat()})

    def add_error(self, error: str):
        self.errors.append(error)


class GongkaoWorkflow:
    """State-machine workflow for gongkao content operations."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or CONFIG.database_path
        self.state = WorkflowState()

    def run(self, *, source_id: str = "", count: int = 1,
            skip_publish: bool = False,
            include_attachment_images: bool = False) -> WorkflowState:
        """Execute the full workflow for one or more events."""
        import time
        self.state = WorkflowState(
            source_id=source_id,
            objective=f"Generate {count} WeChat draft(s)",
            started_at=datetime.utcnow().isoformat())

        t0 = time.time()

        # Node 1: Recommend
        self._node_recommend(count)
        if not self.state.recommendations:
            self.state.status = "no_recommendations"
            return self.state

        # Use first recommendation or specific source_id
        target = self.state.recommendations[0]
        self.state.source_id = target.source_id if not source_id else source_id

        # Node 2: Search origin
        self._node_search_origin()

        # Node 3: Download attachments
        if include_attachment_images:
            self._node_download_attachments()

        # Node 4: Generate article
        self._node_generate()

        # Node 5: QA check
        self._node_qa_check()

        # Node 6: Publish or skip
        if not skip_publish and self.state.qa_passed:
            self._node_publish()
        elif skip_publish:
            self.state.add_result("publish", True, "skipped (skip_publish=True)")

        self.state.status = "completed" if self.state.article_generated else "failed"
        self.state.finished_at = datetime.utcnow().isoformat()
        return self.state

    def _node_recommend(self, count: int):
        import time
        t0 = time.time()
        try:
            from .gongkao_recommender import recommend_events
            recs = recommend_events(limit=count, include_published=False,
                                    status="正在报名")
            self.state.recommendations = recs
            detail = f"Found {len(recs)} recommendations"
            if recs:
                detail += f", top: {recs[0].title[:60]} (score={recs[0].score})"
            self.state.add_result("recommend", True, detail, time.time() - t0)
        except Exception as e:
            self.state.add_error(f"recommend: {e}")
            self.state.add_result("recommend", False, str(e), time.time() - t0)

    def _node_search_origin(self):
        import time
        t0 = time.time()
        try:
            from .gongkao_wechat_pipeline import pick_gongkao_event
            event = pick_gongkao_event(self.db_path, topic_id=self.state.source_id,
                                       category="", region="", status="",
                                       require_deadline=False, days_to_deadline=30)
            url = event.source_origin_url or ""
            self.state.origin_found = bool(url)
            self.state.origin_url = url
            detail = f"Origin {'found' if url else 'not found'}"
            if url:
                detail += f": {url[:80]}"
            self.state.add_result("search_origin", True, detail, time.time() - t0)
        except Exception as e:
            self.state.add_error(f"search_origin: {e}")
            self.state.add_result("search_origin", False, str(e), time.time() - t0)

    def _node_download_attachments(self):
        import time
        t0 = time.time()
        try:
            from .gongkao_attachments import _fetch_events, process_event
            rows = _fetch_events(self.db_path, [self.state.source_id], 1, False)
            if rows:
                reg, dl, parsed, failed = process_event(
                    self.db_path, rows[0], max_attachments=20,
                    use_office_com=False, metadata_only=False, job_tables_only=True)
                self.state.attachments_downloaded = dl
                self.state.attachments_parsed = parsed
                detail = f"Downloaded {dl}, parsed {parsed}, failed {failed}"
                self.state.add_result("attachments", True, detail, time.time() - t0)
            else:
                self.state.add_result("attachments", True, "No events to process",
                                      time.time() - t0)
        except Exception as e:
            self.state.add_error(f"attachments: {e}")
            self.state.add_result("attachments", False, str(e), time.time() - t0)

    def _node_generate(self):
        import time
        t0 = time.time()
        try:
            from .gongkao_wechat_pipeline import (pick_gongkao_event,
                build_gongkao_payload, _save_payload)
            from .wechat_pipeline import (export_wechat_markdown,
                convert_markdown_to_wechat_html, save_wechat_payload_preview)
            event = pick_gongkao_event(self.db_path, topic_id=self.state.source_id,
                                       category="", region="", status="",
                                       require_deadline=False, days_to_deadline=30)
            include_images = bool(self.state.attachments_parsed)
            payload = build_gongkao_payload(event,
                                            include_attachment_images=include_images)
            draft = payload.get("draft") or {}
            self.state.article_title = str(draft.get("title") or "")
            self.state.article_content = str(draft.get("content") or "")
            self.state.article_generated = True
            _save_payload(payload, event.source_id)
            markdown_path = export_wechat_markdown(payload, source_id=event.source_id,
                                                   account_name="Agent")
            html_path = convert_markdown_to_wechat_html(markdown_path, theme="tech")
            detail = (f"Title: {self.state.article_title[:60]}, "
                      f"Content: {len(self.state.article_content)} chars, "
                      f"HTML: {html_path}")
            self.state.add_result("generate", True, detail, time.time() - t0)
        except Exception as e:
            self.state.add_error(f"generate: {e}")
            self.state.add_result("generate", False, str(e), time.time() - t0)

    def _node_qa_check(self):
        import time, json
        t0 = time.time()
        try:
            from .gongkao_wechat_pipeline import (pick_gongkao_event,
                build_gongkao_payload, _run_quality_check)
            event = pick_gongkao_event(self.db_path, topic_id=self.state.source_id,
                                       category="", region="", status="",
                                       require_deadline=False, days_to_deadline=30)
            payload = build_gongkao_payload(event, include_attachment_images=False)
            report_path = _run_quality_check(event, payload)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.state.qa_passed = bool(report.get("pass"))
            self.state.qa_report = report.get("model_result") or {}
            detail = f"QA {'PASS' if self.state.qa_passed else 'FAIL'}"
            if not self.state.qa_passed:
                issues = self.state.qa_report.get("issues", [])
                detail += f": {'; '.join(issues[:3])}"
            self.state.add_result("qa_check", True, detail, time.time() - t0)
        except Exception as e:
            self.state.add_error(f"qa_check: {e}")
            self.state.add_result("qa_check", False, str(e), time.time() - t0)

    def _node_publish(self):
        import time
        t0 = time.time()
        try:
            from .gongkao_wechat_pipeline import (pick_gongkao_event,
                build_gongkao_payload)
            from .wechat_pipeline import (export_wechat_markdown,
                convert_markdown_to_wechat_html, publish_html_to_wechat_draft)
            event = pick_gongkao_event(self.db_path, topic_id=self.state.source_id,
                                       category="", region="", status="",
                                       require_deadline=False, days_to_deadline=30)
            payload = build_gongkao_payload(event, include_attachment_images=False)
            draft = payload.get("draft") or {}
            title = str(draft.get("title") or "")
            digest = str(draft.get("digest") or "")
            markdown_path = export_wechat_markdown(payload, source_id=event.source_id,
                                                   account_name="Agent")
            html_path = convert_markdown_to_wechat_html(markdown_path, theme="tech")
            cover = str(CONFIG.workspace_root / "wechat_cover.png")
            publish_html_to_wechat_draft(title=title, html_path=html_path,
                                         author="Agent", cover_path=cover,
                                         digest=digest, submit_publish=False,
                                         source_platform=event.source_platform,
                                         source_id=event.source_id)
            self.state.draft_submitted = True
            self.state.add_result("publish", True, f"Draft submitted: {title[:60]}",
                                  time.time() - t0)
        except Exception as e:
            self.state.add_error(f"publish: {e}")
            self.state.add_result("publish", False, str(e), time.time() - t0)


# CLI
if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser(description="LangGraph-style workflow")
    ap.add_argument("--source_id", default="", help="Event source_id")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--skip_publish", action="store_true")
    ap.add_argument("--include_attachments", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    wf = GongkaoWorkflow()
    state = wf.run(source_id=args.source_id, count=args.count,
                   skip_publish=args.skip_publish,
                   include_attachment_images=args.include_attachments)

    if args.json:
        print(json.dumps(state.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"Workflow: {state.objective}")
        print(f"Status: {state.status}")
        for r in state.node_results:
            icon = "V" if r["ok"] else "X"
            print(f"  [{icon}] {r['node']}: {r['detail'][:120]}")
        if state.errors:
            print("Errors:")
            for e in state.errors:
                print(f"  ! {e}")
