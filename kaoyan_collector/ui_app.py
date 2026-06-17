from __future__ import annotations

import argparse
import html
import json
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from .agent_tools import list_agent_tools
from .config import CONFIG
from .gongkao_recommender import recommend_events
from .schema import init_db


GONGKAOLEIDA_CATEGORY_OPTIONS = {
    "事业单位": "sydw",
    "公务员": "gwy",
    "国企": "guoqi",
    "教师": "teacher",
    "医疗": "medical",
    "选调": "xuandiao",
    "全部": "all",
}
FENBI_CATEGORY_OPTIONS = {
    "公务员": "1",
    "国考": "0",
    "省考": "1",
    "事业单位": "4",
    "国企": "9",
    "教师": "10",
    "医疗": "11",
    "选调": "3",
    "全部": "",
}


DEFAULT_COVER = CONFIG.workspace_root / "wechat_cover.png"
if not DEFAULT_COVER.exists():
    DEFAULT_COVER = CONFIG.workspace_root / "考试通知.png"
_TASKS: dict[str, dict[str, Any]] = {}
_TASK_LOCK = threading.Lock()


def _json_response(handler: BaseHTTPRequestHandler, data: dict[str, Any], status: int = 200) -> None:
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _text_response(handler: BaseHTTPRequestHandler, text: str, content_type: str = "text/html; charset=utf-8") -> None:
    payload = text.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _run_module(module: str, args: list[str], *, timeout: int = 600) -> dict[str, Any]:
    command = [sys.executable, "-m", module, *args]
    started = time.time()
    result = subprocess.run(
        command,
        cwd=str(CONFIG.workspace_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    output = "\n".join(part for part in [result.stdout, result.stderr] if part).strip()
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "command": " ".join(command),
        "elapsed_seconds": round(time.time() - started, 2),
        "output": output,
    }


def _utc_now() -> str:
    return datetime.utcnow().isoformat()


def _create_agent_trace(task_id: str, label: str, module: str, args: list[str], command: list[str]) -> tuple[int, int]:
    now = _utc_now()
    input_json = json.dumps(
        {
            "task_id": task_id,
            "module": module,
            "args": args,
            "command": command,
        },
        ensure_ascii=False,
    )
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO agent_runs(task_id, objective, status, trigger_source, input_json, started_at)
            VALUES (?, ?, 'running', 'ui_task', ?, ?)
            """,
            (task_id, label, input_json, now),
        )
        run_id = int(cursor.lastrowid)
        step_cursor = conn.execute(
            """
            INSERT INTO agent_steps(run_id, step_index, tool_name, tool_args_json, status, started_at)
            VALUES (?, 1, ?, ?, 'running', ?)
            """,
            (
                run_id,
                module,
                json.dumps({"args": args, "command": command}, ensure_ascii=False),
                now,
            ),
        )
        step_id = int(step_cursor.lastrowid)
        conn.commit()
    return run_id, step_id


def _finish_agent_trace(
    task_id: str,
    *,
    status: str,
    ok: bool,
    output: str = "",
    error: str = "",
    elapsed_seconds: float = 0,
) -> None:
    finished_at = _utc_now()
    observation = (output or "")[-12000:]
    with _connect() as conn:
        run = conn.execute("SELECT id FROM agent_runs WHERE task_id = ?", (task_id,)).fetchone()
        if run is None:
            return
        run_id = int(run["id"])
        conn.execute(
            """
            UPDATE agent_runs
            SET status = ?, final_output = ?, error_message = ?, finished_at = ?, elapsed_seconds = ?
            WHERE id = ?
            """,
            (status, observation, error, finished_at, elapsed_seconds, run_id),
        )
        conn.execute(
            """
            UPDATE agent_steps
            SET status = ?, observation = ?, error_message = ?, finished_at = ?, elapsed_seconds = ?
            WHERE run_id = ? AND step_index = 1
            """,
            ("completed" if ok else "failed", observation, error, finished_at, elapsed_seconds, run_id),
        )
        conn.commit()


def _append_task_output(task_id: str, text: str) -> None:
    if not text:
        return
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return
        task["output"] = (str(task.get("output") or "") + text)[-40000:]
        task["updated_at"] = _utc_now()


def _start_module_task(label: str, module: str, args: list[str], *, timeout: int = 900) -> dict[str, Any]:
    task_id = uuid.uuid4().hex[:12]
    command = [sys.executable, "-m", module, *args]
    agent_run_id = 0
    agent_step_id = 0
    try:
        agent_run_id, agent_step_id = _create_agent_trace(task_id, label, module, args, command)
    except Exception as exc:
        print(f"[agent-trace] create failed: {exc}", flush=True)
    task = {
        "id": task_id,
        "agent_run_id": agent_run_id,
        "agent_step_id": agent_step_id,
        "label": label,
        "module": module,
        "args": args,
        "timeout": timeout,
        "status": "running",
        "ok": None,
        "returncode": None,
        "command": " ".join(command),
        "output": "",
        "error": "",
        "started_at": _utc_now(),
        "updated_at": _utc_now(),
        "finished_at": "",
        "elapsed_seconds": 0,
    }
    with _TASK_LOCK:
        _TASKS[task_id] = task

    thread = threading.Thread(
        target=_run_task_worker,
        args=(task_id, command, timeout),
        daemon=True,
    )
    thread.start()
    return {"ok": True, "task_id": task_id, "task": task}


def _run_task_worker(task_id: str, command: list[str], timeout: int) -> None:
    started = time.time()
    try:
        process = subprocess.Popen(
            command,
            cwd=str(CONFIG.workspace_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        try:
            output_parts: list[str] = []
            while True:
                if time.time() - started > timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                if process.stdout is None:
                    break
                line = process.stdout.readline()
                if line:
                    output_parts.append(line)
                    _append_task_output(task_id, line)
                    continue
                if process.poll() is not None:
                    remainder = process.stdout.read() if process.stdout else ""
                    if remainder:
                        output_parts.append(remainder)
                        _append_task_output(task_id, remainder)
                    break
                time.sleep(0.1)
            output = "".join(output_parts)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
            _append_task_output(task_id, output or "")
            elapsed = round(time.time() - started, 2)
            error = f"任务超时，已停止。timeout={timeout}s"
            _finish_agent_trace(
                task_id,
                status="failed",
                ok=False,
                output=output or "",
                error=error,
                elapsed_seconds=elapsed,
            )
            with _TASK_LOCK:
                task = _TASKS[task_id]
                task.update(
                    {
                        "status": "failed",
                        "ok": False,
                        "returncode": -1,
                        "error": error,
                        "finished_at": _utc_now(),
                        "updated_at": _utc_now(),
                        "elapsed_seconds": elapsed,
                    }
                )
            return
        with _TASK_LOCK:
            task = _TASKS[task_id]
            ok = process.returncode == 0
            elapsed = round(time.time() - started, 2)
            error = "" if ok else f"进程退出码：{process.returncode}"
            _finish_agent_trace(
                task_id,
                status="completed" if ok else "failed",
                ok=ok,
                output=output,
                error=error,
                elapsed_seconds=elapsed,
            )
            task.update(
                {
                    "status": "completed" if ok else "failed",
                    "ok": ok,
                    "returncode": process.returncode,
                    "finished_at": _utc_now(),
                    "updated_at": _utc_now(),
                    "elapsed_seconds": elapsed,
                }
            )
    except Exception as exc:
        elapsed = round(time.time() - started, 2)
        _finish_agent_trace(
            task_id,
            status="failed",
            ok=False,
            output="",
            error=str(exc),
            elapsed_seconds=elapsed,
        )
        with _TASK_LOCK:
            task = _TASKS.get(task_id)
            if task is not None:
                task.update(
                    {
                        "status": "failed",
                        "ok": False,
                        "returncode": -1,
                        "error": str(exc),
                        "finished_at": _utc_now(),
                        "updated_at": _utc_now(),
                        "elapsed_seconds": elapsed,
                    }
                )


def _get_task(task_id: str) -> dict[str, Any] | None:
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        return dict(task) if task else None


def _recent_tasks(limit: int = 20) -> list[dict[str, Any]]:
    with _TASK_LOCK:
        tasks = sorted(_TASKS.values(), key=lambda item: str(item.get("started_at") or ""), reverse=True)
        return [dict(task) for task in tasks[:limit]]


def _retry_task(task_id: str) -> dict[str, Any]:
    task = _get_task(task_id)
    if task is None:
        return {"ok": False, "error": "任务不存在"}
    module = str(task.get("module") or "")
    args = task.get("args") or []
    if not module or not isinstance(args, list):
        return {"ok": False, "error": "这个任务没有可重跑的命令信息"}
    label = "重跑-" + str(task.get("label") or module)
    return _start_module_task(label, module, [str(arg) for arg in args], timeout=int(task.get("timeout") or 900))


def _connect() -> sqlite3.Connection:
    init_db(CONFIG.database_path)
    conn = sqlite3.connect(CONFIG.database_path)
    conn.row_factory = sqlite3.Row
    return conn


def _repair_registration_statuses_now() -> int:
    today = datetime.now().date().isoformat()
    updated = 0
    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE gongkao_events
            SET status = '报名结束'
            WHERE coalesce(registration_deadline, '') <> ''
              AND registration_deadline < ?
              AND status <> '报名结束'
            """,
            (today,),
        )
        updated += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        cursor = conn.execute(
            """
            UPDATE gongkao_events
            SET status = '即将开始'
            WHERE coalesce(registration_start, '') <> ''
              AND registration_start > ?
              AND status <> '即将开始'
              AND (
                  coalesce(registration_deadline, '') = ''
                  OR registration_deadline >= ?
              )
            """,
            (today, today),
        )
        updated += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        cursor = conn.execute(
            """
            UPDATE gongkao_events
            SET status = '正在报名'
            WHERE coalesce(registration_deadline, '') <> ''
              AND registration_deadline >= ?
              AND (
                  coalesce(registration_start, '') = ''
                  OR registration_start <= ?
              )
              AND status <> '正在报名'
            """,
            (today, today),
        )
        updated += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        conn.commit()
    return updated


def _query_events(params: dict[str, list[str]]) -> list[dict[str, Any]]:
    _repair_registration_statuses_now()
    clauses = ["1=1"]
    values: list[Any] = []

    category = (params.get("category") or [""])[0].strip()
    source = (params.get("source") or [""])[0].strip()
    status = (params.get("status") or [""])[0].strip()
    origin_status = (params.get("origin_status") or [""])[0].strip()
    keyword = (params.get("keyword") or [""])[0].strip()
    limit_raw = (params.get("limit") or ["50"])[0].strip()
    try:
        limit = max(1, min(int(limit_raw), 300))
    except ValueError:
        limit = 50

    if category:
        clauses.append("category = ?")
        values.append(category)
    if source:
        clauses.append("source_platform = ?")
        values.append(source)
    if status:
        clauses.append("status = ?")
        values.append(status)
    if origin_status:
        clauses.append("origin_search_status = ?")
        values.append(origin_status)
    if keyword:
        clauses.append("(title LIKE ? OR org_name LIKE ? OR region LIKE ?)")
        like = f"%{keyword}%"
        values.extend([like, like, like])

    query = f"""
    SELECT
        source_platform,
        source_id,
        title,
        region,
        category,
        coalesce(fenbi_exam_type_id, '') AS fenbi_exam_type_id,
        coalesce(fenbi_exam_type_name, category, '') AS fenbi_exam_type_name,
        org_name,
        job_count,
        registration_deadline,
        coalesce(registration_deadline_time, registration_deadline, '') AS registration_deadline_time,
        status,
        coalesce(source_origin_url, '') AS source_origin_url,
        coalesce(origin_search_status, 'pending') AS origin_search_status,
        coalesce(origin_search_attempts, 0) AS origin_search_attempts,
        (
            SELECT count(*)
            FROM gongkao_event_attachments a
            WHERE a.event_source_platform = gongkao_events.source_platform
              AND a.event_source_id = gongkao_events.source_id
        ) AS attachment_count,
        (
            SELECT count(*)
            FROM gongkao_event_attachments a
            WHERE a.event_source_platform = gongkao_events.source_platform
              AND a.event_source_id = gongkao_events.source_id
              AND coalesce(a.download_status, '') = 'downloaded'
        ) AS attachment_downloaded_count,
        (
            SELECT count(*)
            FROM gongkao_event_attachments a
            WHERE a.event_source_platform = gongkao_events.source_platform
              AND a.event_source_id = gongkao_events.source_id
              AND coalesce(a.parse_status, '') = 'parsed'
        ) AS attachment_parsed_count,
        imported_at
    FROM gongkao_events
    WHERE {' AND '.join(clauses)}
    ORDER BY
        CASE WHEN coalesce(source_origin_url, '') <> '' THEN 0 ELSE 1 END,
        CASE WHEN coalesce(registration_deadline, '') = '' THEN 1 ELSE 0 END,
        registration_deadline ASC,
        imported_at DESC
    LIMIT ?
    """
    values.append(limit)

    with _connect() as conn:
        rows = [dict(row) for row in conn.execute(query, values).fetchall()]
    for row in rows:
        deadline = str(row.get("registration_deadline") or "").strip()
        deadline_time = str(row.get("registration_deadline_time") or "").strip()
        row["deadline_date_display"] = deadline or deadline_time[:10]
        row["deadline_countdown"] = _deadline_countdown(row["deadline_date_display"])
    return rows


def _deadline_countdown(deadline: str) -> str:
    deadline = str(deadline or "").strip()[:10]
    if not deadline:
        return ""
    try:
        days = (datetime.strptime(deadline, "%Y-%m-%d").date() - datetime.now().date()).days
    except Exception:
        return ""
    if days < 0:
        return f"已截止{abs(days)}天"
    if days == 0:
        return "今日截止"
    return f"{days}天后"


def _db_stats() -> dict[str, Any]:
    if not CONFIG.database_path.exists():
        return {"exists": False}
    _repair_registration_statuses_now()
    with _connect() as conn:
        total = conn.execute("SELECT count(*) FROM gongkao_events").fetchone()[0]
        found = conn.execute("SELECT count(*) FROM gongkao_events WHERE coalesce(source_origin_url, '') <> ''").fetchone()[0]
        pending = conn.execute(
            "SELECT count(*) FROM gongkao_events WHERE coalesce(origin_search_status, 'pending') = 'pending'"
        ).fetchone()[0]
        not_found = conn.execute(
            "SELECT count(*) FROM gongkao_events WHERE coalesce(origin_search_status, '') = 'not_found'"
        ).fetchone()[0]
    return {
        "exists": True,
        "path": str(CONFIG.database_path),
        "total": total,
        "origin_found": found,
        "origin_pending": pending,
        "origin_not_found": not_found,
    }


def _dashboard_data() -> dict[str, Any]:
    stats = _db_stats()
    with _connect() as conn:
        active = conn.execute("SELECT count(*) FROM gongkao_events WHERE status='正在报名'").fetchone()[0]
        upcoming = conn.execute("SELECT count(*) FROM gongkao_events WHERE status='即将开始'").fetchone()[0]
        ended = conn.execute("SELECT count(*) FROM gongkao_events WHERE status='报名结束'").fetchone()[0]
        with_origin = conn.execute("SELECT count(*) FROM gongkao_events WHERE coalesce(source_origin_url, '') <> ''").fetchone()[0]
        attachment_events = conn.execute(
            "SELECT count(distinct event_source_id) FROM gongkao_event_attachments"
        ).fetchone()[0]
        drafts = conn.execute(
            "SELECT count(*) FROM wechat_publish_records WHERE status IN ('draft_created', 'draft_created_publish_failed')"
        ).fetchone()[0]
        published = conn.execute(
            "SELECT count(*) FROM wechat_publish_records WHERE status IN ('submitted', 'published')"
        ).fetchone()[0]
        agent_runs = conn.execute("SELECT count(*) FROM agent_runs").fetchone()[0]
        agent_failed = conn.execute("SELECT count(*) FROM agent_runs WHERE status='failed'").fetchone()[0]
    recommendations = recommend_events(limit=5, include_published=False, status="正在报名")
    return {
        "stats": stats,
        "active": active,
        "upcoming": upcoming,
        "ended": ended,
        "with_origin": with_origin,
        "attachment_events": attachment_events,
        "drafts": drafts,
        "published": published,
        "agent_runs": agent_runs,
        "agent_failed": agent_failed,
        "recommendations": [item.to_dict() for item in recommendations],
    }


def _status_label(row: dict[str, Any]) -> tuple[str, str]:
    if row.get("source_origin_url"):
        return "已找到原公告", "ok"
    status = str(row.get("origin_search_status") or "pending")
    attempts = int(row.get("origin_search_attempts") or 0)
    if status == "not_found":
        return "未找到并封存", "bad"
    if status == "searched":
        return f"搜索中 {attempts}/5", "warn"
    if status == "found":
        return "已找到原公告", "ok"
    return "待搜索", "muted"


def _events_page_html(params: dict[str, list[str]]) -> str:
    events = _query_events(params)
    keyword = html.escape((params.get("keyword") or [""])[0])
    category = html.escape((params.get("category") or [""])[0])
    status = html.escape((params.get("status") or [""])[0])
    origin_status = html.escape((params.get("origin_status") or [""])[0])
    source = html.escape((params.get("source") or [""])[0])
    limit = html.escape((params.get("limit") or ["100"])[0])
    rows: list[str] = []
    for item in events:
        label, cls = _status_label(item)
        origin_url = str(item.get("source_origin_url") or "")
        origin_link = (
            f'<a href="{html.escape(origin_url)}" target="_blank" rel="noreferrer">打开原文</a>'
            if origin_url
            else '<span class="muted">无</span>'
        )
        attachment_count = int(item.get("attachment_count") or 0)
        attachment_downloaded_count = int(item.get("attachment_downloaded_count") or 0)
        attachment_parsed_count = int(item.get("attachment_parsed_count") or 0)
        attachment_label = (
            f"{attachment_downloaded_count}/{attachment_count} 下载，{attachment_parsed_count} 解析"
            if attachment_count
            else "未处理"
        )
        rows.append(
            "<tr>"
            f"<td><input class=\"row-check\" type=\"checkbox\" value=\"{html.escape(str(item.get('source_id') or ''))}\"></td>"
            f"<td>{html.escape(str(item.get('source_platform') or ''))}</td>"
            f"<td><code>{html.escape(str(item.get('source_id') or ''))}</code></td>"
            f"<td class=\"title\">{html.escape(str(item.get('title') or ''))}</td>"
            f"<td>{html.escape(str(item.get('region') or ''))}</td>"
            f"<td>{html.escape(str(item.get('fenbi_exam_type_name') or item.get('category') or ''))}</td>"
            f"<td>{html.escape(str(item.get('status') or ''))}</td>"
            f"<td>{html.escape(str(item.get('deadline_date_display') or ''))}</td>"
            f"<td>{html.escape(str(item.get('deadline_countdown') or ''))}</td>"
            f"<td>{html.escape(str(item.get('job_count') or ''))}</td>"
            f"<td><span class=\"badge {cls}\">{html.escape(label)}</span></td>"
            f"<td>{html.escape(str(item.get('origin_search_attempts') or 0))}</td>"
            f"<td>{origin_link}</td>"
            f"<td>{html.escape(attachment_label)}</td>"
            f"<td class=\"ops\">"
            f"<button onclick=\"copyId('{html.escape(str(item.get('source_id') or ''))}')\">复制ID</button>"
            f"<button onclick=\"wechatAction('{html.escape(str(item.get('source_id') or ''))}', true, false)\">预览</button>"
            f"<button onclick=\"wechatAction('{html.escape(str(item.get('source_id') or ''))}', false, false)\">发草稿</button>"
            f"<button onclick=\"attachmentAction(['{html.escape(str(item.get('source_id') or ''))}'])\">附件</button>"
            f"</td>"
            "</tr>"
        )
    source_options = ["", "fenbi", "gongkaoleida"]
    category_options = ["", "事业单位", "公务员", "省考", "国考", "国企", "教师", "医疗", "选调"]
    origin_options = ["", "pending", "searched", "found", "not_found"]
    status_options = ["", "即将开始", "正在报名", "报名结束"]

    def select_options(options: list[str], selected: str) -> str:
        rendered = []
        for option in options:
            label = option or "全部"
            chosen = " selected" if option == selected else ""
            rendered.append(f'<option value="{html.escape(option)}"{chosen}>{html.escape(label)}</option>')
        return "\n".join(rendered)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>公告库 - 考公信息自动化控制台</title>
  <style>
    body {{ margin: 0; background: #f6f7f9; color: #1f2937; font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif; font-size: 14px; }}
    header {{ background: #111827; color: white; padding: 18px 28px; display: flex; align-items: center; justify-content: space-between; gap: 16px; }}
    h1 {{ margin: 0; font-size: 20px; }}
    main {{ padding: 22px 28px 36px; max-width: 1560px; margin: 0 auto; }}
    a {{ color: #1f7a5a; text-decoration: none; }}
    .panel {{ background: white; border: 1px solid #dfe3ea; border-radius: 8px; padding: 16px; }}
    .filters {{ display: grid; grid-template-columns: 1.6fr repeat(5, minmax(120px, 1fr)) auto; gap: 10px; align-items: end; }}
    label {{ display: block; color: #6b7280; margin: 0 0 5px; }}
    input, select {{ width: 100%; height: 36px; border: 1px solid #dfe3ea; border-radius: 6px; padding: 0 10px; background: white; }}
    button {{ height: 36px; border: 0; border-radius: 6px; padding: 0 12px; background: #1f7a5a; color: white; cursor: pointer; font-weight: 600; }}
    button.secondary {{ background: #374151; }}
    button.warning {{ background: #b45309; }}
    button[disabled] {{ opacity: .55; cursor: wait; }}
    .summary {{ margin: 14px 0; color: #6b7280; display: flex; justify-content: space-between; gap: 12px; }}
    table {{ width: 100%; border-collapse: collapse; background: white; border: 1px solid #dfe3ea; border-radius: 8px; overflow: hidden; }}
    th, td {{ border-bottom: 1px solid #dfe3ea; padding: 9px 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f1f5f9; color: #475569; white-space: nowrap; position: sticky; top: 0; z-index: 1; }}
    tr:hover td {{ background: #f8fafc; }}
    code {{ font-family: Consolas, "Courier New", monospace; }}
    .title {{ min-width: 360px; max-width: 560px; line-height: 1.5; }}
    .table-wrap {{ max-height: calc(100vh - 220px); overflow: auto; border-radius: 8px; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; white-space: nowrap; background: #e5e7eb; color: #374151; }}
    .badge.ok {{ background: #dcfce7; color: #166534; }}
    .badge.warn {{ background: #fef3c7; color: #92400e; }}
    .badge.bad {{ background: #fee2e2; color: #991b1b; }}
    .badge.muted {{ background: #e5e7eb; color: #6b7280; }}
    .muted {{ color: #6b7280; }}
    .ops {{ display: flex; gap: 6px; flex-wrap: wrap; min-width: 190px; }}
    .ops button {{ height: 30px; padding: 0 9px; }}
    .batch-bar {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 12px 0; }}
    .batch-bar .muted {{ margin-left: auto; }}
    .inline-check {{ display: inline-flex; align-items: center; gap: 6px; color: #374151; }}
    .inline-check input {{ width: 16px; height: 16px; padding: 0; }}
    .task-status {{ border: 1px solid #dfe3ea; border-radius: 8px; padding: 10px 12px; margin: 12px 0; background: #f8fafc; }}
    .task-status.running {{ border-color: #fbbf24; background: #fffbeb; }}
    .task-status.completed {{ border-color: #86efac; background: #f0fdf4; }}
    .task-status.failed {{ border-color: #fecaca; background: #fef2f2; color: #991b1b; }}
    .log {{ background: #0b1020; color: #d1e7ff; border-radius: 8px; padding: 12px; white-space: pre-wrap; font-family: Consolas, "Courier New", monospace; max-height: 260px; overflow: auto; }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>公告库</h1>
      <div class="muted">查看已入库公告、原公告搜索状态和发布候选</div>
    </div>
    <div>
      <a href="/"><button class="secondary">返回控制台</button></a>
    </div>
  </header>
  <main>
    <section class="panel">
      <form class="filters" method="get" action="/events">
        <div>
          <label>关键词</label>
          <input name="keyword" value="{keyword}" placeholder="标题、单位、地区">
        </div>
        <div>
          <label>来源</label>
          <select name="source">{select_options(source_options, source)}</select>
        </div>
        <div>
          <label>分类</label>
          <select name="category">{select_options(category_options, category)}</select>
        </div>
        <div>
          <label>报名状态</label>
          <select name="status">{select_options(status_options, status)}</select>
        </div>
        <div>
          <label>原公告状态</label>
          <select name="origin_status">{select_options(origin_options, origin_status)}</select>
        </div>
        <div>
          <label>显示数量</label>
          <input name="limit" type="number" min="1" max="300" value="{limit or '100'}">
        </div>
        <button type="submit">筛选</button>
      </form>
    </section>
    <div class="summary">
      <span>当前显示 {len(events)} 条</span>
      <span>状态说明：待搜索 / 搜索中 n/5 / 已找到原公告 / 未找到并封存</span>
    </div>
    <div class="batch-bar">
      <button class="secondary" onclick="toggleAllRows(true)">全选当前页</button>
      <button class="secondary" onclick="toggleAllRows(false)">取消选择</button>
      <button class="secondary" onclick="batchWechat(true, false)">批量生成预览</button>
      <button onclick="batchWechat(false, false)">批量提交草稿</button>
      <button class="warning" onclick="batchWechat(false, true)">批量直接发布</button>
      <button class="secondary" onclick="batchAttachments()">批量下载解析附件</button>
      <button class="secondary" onclick="scanAllAttachments()">扫描全库附件链接</button>
      <button class="secondary" onclick="batchJobTableAttachments()">批量下载岗位表</button>
      <label class="inline-check"><input id="includeAttachmentImages" type="checkbox">加入附件岗位表图片</label>
      <span id="selectedCount" class="muted">已选择 0 条</span>
    </div>
    <div id="taskStatus" class="task-status">当前没有运行中的任务。</div>
    <div id="log" class="log">公告库已就绪。可以直接在表格右侧生成预览或提交草稿。</div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>选择</th><th>来源</th><th>ID</th><th>标题</th><th>地区</th><th>粉笔类型</th><th>报名状态</th>
            <th>截止日期</th><th>倒计时</th><th>人数</th><th>原公告状态</th><th>搜索次数</th><th>原文</th><th>附件</th><th>操作</th>
          </tr>
        </thead>
        <tbody>{''.join(rows) if rows else '<tr><td colspan="15" class="muted">暂无匹配公告</td></tr>'}</tbody>
      </table>
    </div>
  </main>
  <script>
    let activeTaskId = "";
    let lastOutputLength = 0;
    let taskTimer = null;

    function copyId(id) {{
      navigator.clipboard.writeText(id).then(() => alert("已复制 source_id: " + id));
    }}

    function selectedIds() {{
      return Array.from(document.querySelectorAll(".row-check:checked"))
        .map(el => el.value)
        .filter(Boolean);
    }}

    function updateSelectedCount() {{
      document.getElementById("selectedCount").textContent = "已选择 " + selectedIds().length + " 条";
    }}

    function toggleAllRows(checked) {{
      for (const el of document.querySelectorAll(".row-check")) {{
        el.checked = !!checked;
      }}
      updateSelectedCount();
    }}

    function log(text) {{
      const el = document.getElementById("log");
      const time = new Date().toLocaleTimeString();
      el.textContent += "\\n\\n[" + time + "] " + text;
      el.scrollTop = el.scrollHeight;
    }}

    async function api(path, options) {{
      const res = await fetch(path, options);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || JSON.stringify(data));
      return data;
    }}

    function setTaskStatus(task) {{
      const el = document.getElementById("taskStatus");
      if (!task) {{
        el.className = "task-status";
        el.textContent = "当前没有运行中的任务。";
        return;
      }}
      el.className = "task-status " + task.status;
      const elapsed = task.elapsed_seconds ? `，耗时 ${{task.elapsed_seconds}} 秒` : "";
      const error = task.error ? `，错误：${{task.error}}` : "";
      el.textContent = `${{task.label || "任务"}}：${{task.status}}${{elapsed}}${{error}}`;
    }}

    async function wechatAction(sourceId, skipPublish, submitPublish) {{
      const payload = {{
        topic_id: sourceId,
        days_to_deadline: 30,
        author: "岸上信息站",
        cover_path: {json.dumps(str(DEFAULT_COVER), ensure_ascii=False)},
        skip_publish: !!skipPublish,
        submit_publish: !!submitPublish,
        include_attachment_images: !!document.getElementById("includeAttachmentImages")?.checked
      }};
      const data = await api("/api/wechat", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify(payload)
      }});
      activeTaskId = data.task_id;
      lastOutputLength = 0;
      setTaskStatus(data.task);
      log("任务已启动：" + (data.task ? data.task.label : "公众号任务") + "\\n公告ID：" + sourceId + "\\n任务ID：" + activeTaskId);
      pollTask();
    }}

    async function batchWechat(skipPublish, submitPublish) {{
      const ids = selectedIds();
      if (!ids.length) {{
        log("请先勾选要批量处理的公告。");
        return;
      }}
      const actionName = submitPublish ? "批量直接发布" : (skipPublish ? "批量生成预览" : "批量提交草稿");
      if (!skipPublish && !confirm(actionName + " " + ids.length + " 条公告？")) {{
        return;
      }}
      const payload = {{
        topic_ids: ids,
        days_to_deadline: 30,
        author: "岸上信息站",
        cover_path: {json.dumps(str(DEFAULT_COVER), ensure_ascii=False)},
        skip_publish: !!skipPublish,
        submit_publish: !!submitPublish,
        include_attachment_images: !!document.getElementById("includeAttachmentImages")?.checked
      }};
      const data = await api("/api/wechat_batch", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify(payload)
      }});
      activeTaskId = data.task_id;
      lastOutputLength = 0;
      setTaskStatus(data.task);
      log("批量任务已启动：" + actionName + "\\n数量：" + ids.length + "\\n任务ID：" + activeTaskId);
      pollTask();
    }}

    async function attachmentAction(ids, options) {{
      const cleanIds = (ids || []).filter(Boolean);
      if (!cleanIds.length) {{
        log("请先选择要下载解析附件的公告。");
        return;
      }}
      const payload = Object.assign({{topic_ids: cleanIds, max_attachments: 10}}, options || {{}});
      const data = await api("/api/attachments", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify(payload)
      }});
      activeTaskId = data.task_id;
      lastOutputLength = 0;
      setTaskStatus(data.task);
      log("附件任务已启动\\n数量：" + cleanIds.length + "\\n任务ID：" + activeTaskId);
      pollTask();
    }}

    async function batchAttachments() {{
      const ids = selectedIds();
      await attachmentAction(ids);
    }}

    async function batchJobTableAttachments() {{
      const ids = selectedIds();
      if (!ids.length) {{
        log("请先勾选要下载岗位表附件的公告。");
        return;
      }}
      await attachmentAction(ids, {{job_tables_only: true, max_attachments: 20}});
    }}

    async function scanAllAttachments() {{
      if (!confirm("扫描全库公告里的附件链接？这个动作只登记链接，不下载文件。")) {{
        return;
      }}
      const data = await api("/api/attachments_scan_all", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{limit: 5000}})
      }});
      activeTaskId = data.task_id;
      lastOutputLength = 0;
      setTaskStatus(data.task);
      log("全库附件扫描已启动\\n任务ID：" + activeTaskId);
      pollTask();
    }}

    async function pollTask() {{
      if (!activeTaskId) return;
      if (taskTimer) clearTimeout(taskTimer);
      try {{
        const data = await api(`/api/task?id=${{encodeURIComponent(activeTaskId)}}`);
        const task = data.task;
        setTaskStatus(task);
        const output = task.output || "";
        if (output.length > lastOutputLength) {{
          log(output.slice(lastOutputLength).trim());
          lastOutputLength = output.length;
        }}
        if (task.status === "running") {{
          taskTimer = setTimeout(pollTask, 1200);
          return;
        }}
        log(task.ok ? "任务完成。" : "任务失败，请查看上方输出。");
      }} catch (err) {{
        log("任务状态读取失败：" + (err && err.message ? err.message : err));
      }}
    }}

    for (const el of document.querySelectorAll(".row-check")) {{
      el.addEventListener("change", updateSelectedCount);
    }}
    updateSelectedCount();
  </script>
</body>
</html>"""


def _check_wechat_token() -> dict[str, Any]:
    token_result = _get_wechat_access_token()
    if token_result.get("access_token"):
        return {"ok": True, "message": f"access_token 可用，有效期 {token_result.get('expires_in')} 秒"}
    return {"ok": False, "message": token_result.get("error") or json.dumps(token_result, ensure_ascii=False)}


def _get_wechat_access_token() -> dict[str, Any]:
    config_path = Path.home() / ".wechat-publisher" / "config.json"
    if not config_path.exists():
        return {"error": f"未找到微信配置: {config_path}"}
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        appid = str(cfg.get("appid") or "")
        secret = str(cfg.get("appsecret") or "")
        url = (
            "https://api.weixin.qq.com/cgi-bin/token?"
            f"grant_type=client_credential&appid={appid}&secret={secret}"
        )
        with urlopen(url, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        if data.get("access_token"):
            return data
        return {"error": json.dumps(data, ensure_ascii=False), "raw": data}
    except Exception as exc:
        return {"error": str(exc)}


def _wechat_post(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    token_result = _get_wechat_access_token()
    token = token_result.get("access_token")
    if not token:
        return {"ok": False, "error": token_result.get("error") or "无法获取 access_token"}
    url = f"https://api.weixin.qq.com/cgi-bin/{endpoint}?access_token={token}"
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if data.get("errcode") not in (None, 0):
        return {"ok": False, "error": json.dumps(data, ensure_ascii=False), "raw": data}
    return {"ok": True, "data": data}


def _fetch_wechat_remote(kind: str, *, offset: int, count: int) -> dict[str, Any]:
    endpoint = "draft/batchget" if kind == "drafts" else "freepublish/batchget"
    return _wechat_post(endpoint, {"offset": offset, "count": count, "no_content": 0})


def _query_wechat_records(kind: str = "", limit: int = 100) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    values: list[Any] = []
    if kind == "drafts":
        clauses.append("status IN ('draft_created', 'draft_created_publish_failed')")
    elif kind == "published":
        clauses.append("status IN ('submitted', 'published')")
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, source_id, title, author, action, status, media_id, publish_id,
                   article_id, html_path, cover_path, error_message, created_at, updated_at
            FROM wechat_publish_records
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()
    return [dict(row) for row in rows]


def _wechat_time(value: Any) -> str:
    try:
        ts = int(value)
    except Exception:
        return ""
    if ts <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _remote_article_rows(kind: str, result: dict[str, Any]) -> tuple[str, str]:
    if not result.get("ok"):
        error = html.escape(str(result.get("error") or "未知错误"))
        return f'<div class="notice bad">远端列表获取失败：{error}</div>', ""
    data = result.get("data") or {}
    items = data.get("item") or []
    rows: list[str] = []
    for item in items:
        content = item.get("content") or {}
        news_items = content.get("news_item") or []
        first = news_items[0] if news_items else {}
        title = first.get("title") or item.get("title") or ""
        author = first.get("author") or ""
        digest = first.get("digest") or ""
        url = first.get("url") or first.get("content_source_url") or ""
        media_or_article = item.get("media_id") or item.get("article_id") or item.get("publish_id") or ""
        update_time = _wechat_time(item.get("update_time") or content.get("update_time"))
        link = f'<a href="{html.escape(str(url))}" target="_blank" rel="noreferrer">打开</a>' if url else '<span class="muted">无</span>'
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(media_or_article))}</code></td>"
            f"<td class=\"title\">{html.escape(str(title))}</td>"
            f"<td>{html.escape(str(author))}</td>"
            f"<td class=\"digest\">{html.escape(str(digest))}</td>"
            f"<td>{html.escape(update_time)}</td>"
            f"<td>{link}</td>"
            "</tr>"
        )
    summary = (
        f"远端总数：{html.escape(str(data.get('total_count', '')))}，"
        f"本次返回：{html.escape(str(data.get('item_count', len(items))))}"
    )
    if not rows:
        rows.append('<tr><td colspan="6" class="muted">远端暂无数据或未返回列表</td></tr>')
    return f'<div class="notice">{summary}</div>', "".join(rows)


def _local_record_rows(records: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for record in records:
        cls = "ok" if record.get("status") in ("draft_created", "submitted", "published") else "warn"
        if str(record.get("status") or "").endswith("failed"):
            cls = "bad"
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(record.get('created_at') or ''))}</td>"
            f"<td><code>{html.escape(str(record.get('source_id') or ''))}</code></td>"
            f"<td class=\"title\">{html.escape(str(record.get('title') or ''))}</td>"
            f"<td><span class=\"badge {cls}\">{html.escape(str(record.get('status') or ''))}</span></td>"
            f"<td><code>{html.escape(str(record.get('media_id') or ''))}</code></td>"
            f"<td><code>{html.escape(str(record.get('publish_id') or ''))}</code></td>"
            f"<td class=\"digest\">{html.escape(str(record.get('error_message') or ''))}</td>"
            "</tr>"
        )
    return "".join(rows) if rows else '<tr><td colspan="7" class="muted">暂无本地记录</td></tr>'


def _wechat_list_page_html(kind: str, params: dict[str, list[str]]) -> str:
    page_title = "草稿箱列表" if kind == "drafts" else "已发布列表"
    endpoint_label = "draft/batchget" if kind == "drafts" else "freepublish/batchget"
    try:
        offset = max(0, int((params.get("offset") or ["0"])[0]))
    except ValueError:
        offset = 0
    try:
        count = max(1, min(int((params.get("count") or ["20"])[0]), 20))
    except ValueError:
        count = 20
    remote = _fetch_wechat_remote(kind, offset=offset, count=count)
    remote_summary, remote_rows = _remote_article_rows(kind, remote)
    local_rows = _local_record_rows(_query_wechat_records(kind, limit=100))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{page_title} - 考公信息自动化控制台</title>
  <style>
    body {{ margin: 0; background: #f6f7f9; color: #1f2937; font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif; font-size: 14px; }}
    header {{ background: #111827; color: white; padding: 18px 28px; display: flex; justify-content: space-between; align-items: center; gap: 16px; }}
    h1 {{ margin: 0; font-size: 20px; }}
    main {{ padding: 22px 28px 36px; max-width: 1560px; margin: 0 auto; }}
    a {{ color: #1f7a5a; text-decoration: none; }}
    button {{ height: 36px; border: 0; border-radius: 6px; padding: 0 12px; background: #374151; color: white; cursor: pointer; font-weight: 600; }}
    .panel {{ background: white; border: 1px solid #dfe3ea; border-radius: 8px; padding: 16px; margin-bottom: 18px; }}
    .filters {{ display: flex; gap: 10px; align-items: end; flex-wrap: wrap; }}
    label {{ display: block; color: #6b7280; margin: 0 0 5px; }}
    input {{ height: 36px; border: 1px solid #dfe3ea; border-radius: 6px; padding: 0 10px; }}
    table {{ width: 100%; border-collapse: collapse; background: white; border: 1px solid #dfe3ea; border-radius: 8px; overflow: hidden; }}
    th, td {{ border-bottom: 1px solid #dfe3ea; padding: 9px 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f1f5f9; color: #475569; white-space: nowrap; }}
    tr:hover td {{ background: #f8fafc; }}
    code {{ font-family: Consolas, "Courier New", monospace; word-break: break-all; }}
    .title {{ min-width: 360px; max-width: 560px; line-height: 1.5; }}
    .digest {{ max-width: 420px; color: #6b7280; line-height: 1.5; }}
    .notice {{ background: #eef6f1; border: 1px solid #cde9d8; color: #145f45; padding: 10px 12px; border-radius: 8px; margin: 12px 0; }}
    .notice.bad {{ background: #fef2f2; border-color: #fecaca; color: #991b1b; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; white-space: nowrap; background: #e5e7eb; color: #374151; }}
    .badge.ok {{ background: #dcfce7; color: #166534; }}
    .badge.warn {{ background: #fef3c7; color: #92400e; }}
    .badge.bad {{ background: #fee2e2; color: #991b1b; }}
    .muted {{ color: #6b7280; }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>{page_title}</h1>
      <div class="muted">远端接口：{endpoint_label}；下方同时显示本地操作记录</div>
    </div>
    <div>
      <a href="/"><button>返回控制台</button></a>
      <a href="/events"><button>公告库</button></a>
      <a href="/wechat/drafts"><button>草稿箱</button></a>
      <a href="/wechat/published"><button>已发布</button></a>
    </div>
  </header>
  <main>
    <section class="panel">
      <form class="filters" method="get">
        <div><label>offset</label><input name="offset" type="number" min="0" value="{offset}"></div>
        <div><label>count</label><input name="count" type="number" min="1" max="20" value="{count}"></div>
        <button type="submit">刷新远端列表</button>
      </form>
      {remote_summary}
      <table>
        <thead><tr><th>ID</th><th>标题</th><th>作者</th><th>摘要</th><th>更新时间</th><th>链接</th></tr></thead>
        <tbody>{remote_rows}</tbody>
      </table>
    </section>
    <section class="panel">
      <h2>本地操作记录</h2>
      <table>
        <thead><tr><th>时间</th><th>公告ID</th><th>标题</th><th>状态</th><th>media_id</th><th>publish_id</th><th>错误</th></tr></thead>
        <tbody>{local_rows}</tbody>
      </table>
    </section>
  </main>
</body>
</html>"""


def _query_agent_runs(limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                r.id,
                r.task_id,
                r.objective,
                r.status,
                r.trigger_source,
                r.started_at,
                r.finished_at,
                r.elapsed_seconds,
                r.error_message,
                s.tool_name,
                s.tool_args_json,
                s.status AS step_status,
                s.observation,
                s.error_message AS step_error
            FROM agent_runs r
            LEFT JOIN agent_steps s ON s.run_id = r.id AND s.step_index = 1
            ORDER BY r.started_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def _agent_runs_page_html(params: dict[str, list[str]]) -> str:
    try:
        limit = max(1, min(int((params.get("limit") or ["50"])[0]), 200))
    except ValueError:
        limit = 50
    rows: list[str] = []
    for run in _query_agent_runs(limit):
        status = str(run.get("status") or "")
        cls = "ok" if status == "completed" else ("bad" if status == "failed" else "warn")
        tool_args = str(run.get("tool_args_json") or "{}")
        observation = str(run.get("observation") or "")
        if len(observation) > 900:
            observation = observation[:900] + "\n..."
        error = str(run.get("error_message") or run.get("step_error") or "")
        error_html = f'<div class="err">{html.escape(error)}</div>' if error else ""
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(run.get('id') or ''))}</code><br><span class=\"muted\">{html.escape(str(run.get('task_id') or ''))}</span></td>"
            f"<td>{html.escape(str(run.get('started_at') or ''))}<br><span class=\"muted\">耗时 {html.escape(str(run.get('elapsed_seconds') or ''))} 秒</span></td>"
            f"<td class=\"title\">{html.escape(str(run.get('objective') or ''))}</td>"
            f"<td><span class=\"badge {cls}\">{html.escape(status)}</span><br><span class=\"muted\">step: {html.escape(str(run.get('step_status') or ''))}</span></td>"
            f"<td><code>{html.escape(str(run.get('tool_name') or ''))}</code><details><summary>参数</summary><pre>{html.escape(tool_args)}</pre></details></td>"
            f"<td><pre>{html.escape(observation)}</pre>{error_html}</td>"
            "</tr>"
        )
    body_rows = "".join(rows) if rows else '<tr><td colspan="6" class="muted">暂无 Agent 运行记录</td></tr>'
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agent 运行记录</title>
  <style>
    body {{ margin:0; background:#f6f7f9; color:#1f2937; font-family:"Microsoft YaHei","Segoe UI",Arial,sans-serif; font-size:14px; }}
    header {{ background:#111827; color:#fff; padding:18px 28px; display:flex; justify-content:space-between; align-items:center; gap:16px; }}
    h1 {{ font-size:20px; margin:0; }}
    main {{ padding:22px 28px 36px; max-width:1500px; margin:0 auto; }}
    button {{ height:36px; border:0; border-radius:6px; padding:0 12px; background:#374151; color:#fff; cursor:pointer; font-weight:600; }}
    .panel {{ background:#fff; border:1px solid #dfe3ea; border-radius:8px; padding:16px; }}
    .filters {{ display:flex; gap:10px; align-items:end; margin-bottom:14px; }}
    label {{ display:block; color:#6b7280; margin-bottom:5px; }}
    input {{ height:36px; border:1px solid #dfe3ea; border-radius:6px; padding:0 10px; }}
    table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid #dfe3ea; }}
    th, td {{ border-bottom:1px solid #dfe3ea; padding:9px 8px; text-align:left; vertical-align:top; }}
    th {{ background:#f1f5f9; color:#475569; white-space:nowrap; }}
    .title {{ max-width:260px; line-height:1.5; }}
    .muted {{ color:#6b7280; }}
    .badge {{ display:inline-block; padding:2px 7px; border-radius:999px; background:#e5e7eb; color:#374151; }}
    .badge.ok {{ background:#dcfce7; color:#166534; }}
    .badge.warn {{ background:#fef3c7; color:#92400e; }}
    .badge.bad {{ background:#fee2e2; color:#991b1b; }}
    pre {{ max-width:560px; max-height:220px; overflow:auto; margin:6px 0 0; padding:8px; background:#0b1020; color:#d1e7ff; border-radius:6px; white-space:pre-wrap; font-family:Consolas,"Courier New",monospace; font-size:12px; }}
    .err {{ margin-top:6px; color:#991b1b; line-height:1.5; }}
    details summary {{ cursor:pointer; color:#2563eb; margin-top:4px; }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Agent 运行记录</h1>
      <div class="muted">记录 UI 触发的任务、工具调用、输出摘要和错误信息</div>
    </div>
    <div>
      <a href="/"><button>返回控制台</button></a>
      <a href="/events"><button>公告库</button></a>
    </div>
  </header>
  <main>
    <section class="panel">
      <form class="filters" method="get">
        <div><label>显示数量</label><input name="limit" type="number" min="1" max="200" value="{limit}"></div>
        <button type="submit">刷新</button>
      </form>
      <table>
        <thead><tr><th>Run</th><th>时间</th><th>目标</th><th>状态</th><th>工具</th><th>观察结果</th></tr></thead>
        <tbody>{body_rows}</tbody>
      </table>
    </section>
  </main>
</body>
</html>"""


def _agent_tools_page_html() -> str:
    rows: list[str] = []
    for tool in list_agent_tools():
        review = "需要" if tool.get("human_review") else "不需要"
        risk = str(tool.get("risk_level") or "")
        cls = "bad" if risk == "high" else ("warn" if risk == "medium" else "ok")
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(tool.get('name') or ''))}</code></td>"
            f"<td><code>{html.escape(str(tool.get('module') or ''))}</code></td>"
            f"<td>{html.escape(str(tool.get('description') or ''))}</td>"
            f"<td>{html.escape(', '.join(tool.get('inputs') or []))}</td>"
            f"<td>{html.escape(', '.join(tool.get('outputs') or []))}</td>"
            f"<td><span class=\"badge {cls}\">{html.escape(risk)}</span></td>"
            f"<td>{review}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agent 工具注册表</title>
  <style>
    body {{ margin:0; background:#f6f7f9; color:#1f2937; font-family:"Microsoft YaHei","Segoe UI",Arial,sans-serif; font-size:14px; }}
    header {{ background:#111827; color:#fff; padding:18px 28px; display:flex; justify-content:space-between; align-items:center; gap:16px; }}
    h1 {{ font-size:20px; margin:0; }}
    main {{ padding:22px 28px 36px; max-width:1500px; margin:0 auto; }}
    button {{ height:36px; border:0; border-radius:6px; padding:0 12px; background:#374151; color:#fff; cursor:pointer; font-weight:600; }}
    .panel {{ background:#fff; border:1px solid #dfe3ea; border-radius:8px; padding:16px; }}
    table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid #dfe3ea; }}
    th, td {{ border-bottom:1px solid #dfe3ea; padding:9px 8px; text-align:left; vertical-align:top; line-height:1.5; }}
    th {{ background:#f1f5f9; color:#475569; white-space:nowrap; }}
    .muted {{ color:#6b7280; }}
    .badge {{ display:inline-block; padding:2px 7px; border-radius:999px; background:#e5e7eb; color:#374151; }}
    .badge.ok {{ background:#dcfce7; color:#166534; }}
    .badge.warn {{ background:#fef3c7; color:#92400e; }}
    .badge.bad {{ background:#fee2e2; color:#991b1b; }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Agent 工具注册表</h1>
      <div class="muted">把现有脚本能力整理成 Agent 可调用工具，标注输入、输出和风险等级</div>
    </div>
    <div>
      <a href="/"><button>返回控制台</button></a>
      <a href="/agent/runs"><button>运行记录</button></a>
    </div>
  </header>
  <main>
    <section class="panel">
      <table>
        <thead><tr><th>工具</th><th>模块</th><th>说明</th><th>输入</th><th>输出</th><th>风险</th><th>人工确认</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </section>
  </main>
</body>
</html>"""


def _dashboard_page_html() -> str:
    data = _dashboard_data()
    stats = data["stats"]
    recs = data["recommendations"]
    cards = [
        ("公告总数", stats.get("total", 0)),
        ("正在报名", data.get("active", 0)),
        ("即将开始", data.get("upcoming", 0)),
        ("报名结束", data.get("ended", 0)),
        ("已找到原公告", data.get("with_origin", 0)),
        ("有附件公告", data.get("attachment_events", 0)),
        ("草稿记录", data.get("drafts", 0)),
        ("已发布记录", data.get("published", 0)),
    ]
    card_html = "".join(
        f'<div class="card"><span>{html.escape(str(label))}</span><b>{html.escape(str(value))}</b></div>'
        for label, value in cards
    )
    rec_rows = []
    for item in recs:
        reasons = "；".join(item.get("reasons") or [])
        origin = f'<a href="{html.escape(str(item.get("source_origin_url") or ""))}" target="_blank" rel="noreferrer">原文</a>' if item.get("source_origin_url") else '<span class="muted">无</span>'
        rec_rows.append(
            "<tr>"
            f"<td><span class=\"badge {'ok' if item.get('score', 0) >= 70 else 'warn' if item.get('score', 0) >= 50 else 'bad'}\">{html.escape(str(item.get('score') or 0))}</span></td>"
            f"<td class=\"title\">{html.escape(str(item.get('title') or ''))}</td>"
            f"<td>{html.escape(str(item.get('region') or ''))}</td>"
            f"<td>{html.escape(str(item.get('deadline_countdown') or ''))}</td>"
            f"<td>{html.escape(str(item.get('category') or ''))}</td>"
            f"<td>{html.escape(str(item.get('job_count') or 0))}</td>"
            f"<td>{origin}</td>"
            f"<td class=\"reason\">{html.escape(reasons)}</td>"
            "</tr>"
        )
    rec_table = "".join(rec_rows) if rec_rows else '<tr><td colspan="8" class="muted">暂无推荐数据</td></tr>'
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>考公 Agent Dashboard</title>
  <style>
    body {{ margin:0; background:#f6f7f9; color:#1f2937; font-family:"Microsoft YaHei","Segoe UI",Arial,sans-serif; font-size:14px; }}
    header {{ background:#111827; color:#fff; padding:18px 28px; display:flex; justify-content:space-between; align-items:center; gap:16px; }}
    h1 {{ font-size:20px; margin:0; }}
    main {{ padding:22px 28px 36px; max-width:1500px; margin:0 auto; }}
    .actions {{ display:flex; gap:8px; flex-wrap:wrap; }}
    button {{ height:36px; border:0; border-radius:6px; padding:0 12px; background:#374151; color:#fff; cursor:pointer; font-weight:600; }}
    .panel {{ background:#fff; border:1px solid #dfe3ea; border-radius:8px; padding:16px; margin-bottom:18px; }}
    .cards {{ display:grid; grid-template-columns: repeat(4, minmax(140px, 1fr)); gap:12px; }}
    .card {{ background:#fff; border:1px solid #dfe3ea; border-radius:8px; padding:14px; }}
    .card span {{ color:#6b7280; display:block; }}
    .card b {{ display:block; font-size:22px; margin-top:6px; }}
    table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid #dfe3ea; }}
    th, td {{ border-bottom:1px solid #dfe3ea; padding:9px 8px; text-align:left; vertical-align:top; line-height:1.5; }}
    th {{ background:#f1f5f9; color:#475569; white-space:nowrap; }}
    .title {{ max-width:320px; }}
    .reason {{ max-width:420px; color:#374151; }}
    .muted {{ color:#6b7280; }}
    .badge {{ display:inline-block; padding:2px 7px; border-radius:999px; background:#e5e7eb; color:#374151; }}
    .badge.ok {{ background:#dcfce7; color:#166534; }}
    .badge.warn {{ background:#fef3c7; color:#92400e; }}
    .badge.bad {{ background:#fee2e2; color:#991b1b; }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>考公 Agent Dashboard</h1>
      <div class="muted">用于面试展示的总览页：推荐选题、公告状态、附件覆盖和 Agent 运行情况</div>
    </div>
    <div class="actions">
      <a href="/"><button>控制台</button></a>
      <a href="/events"><button>公告库</button></a>
      <a href="/agent/runs"><button>运行记录</button></a>
      <a href="/agent/tools"><button>工具注册表</button></a>
    </div>
  </header>
  <main>
    <section class="panel">
      <div class="cards">{card_html}</div>
    </section>
    <section class="panel">
      <h2>今日推荐 Top 5</h2>
      <table>
        <thead><tr><th>分数</th><th>标题</th><th>地区</th><th>倒计时</th><th>类型</th><th>人数</th><th>原文</th><th>理由</th></tr></thead>
        <tbody>{rec_table}</tbody>
      </table>
    </section>
    <section class="panel">
      <h2>系统状态</h2>
      <div class="muted">Agent 运行总数：{html.escape(str(data.get('agent_runs', 0)))}</div>
      <div class="muted">失败运行：{html.escape(str(data.get('agent_failed', 0)))}</div>
    </section>
  </main>
</body>
</html>"""


def _home_html() -> str:
    cover = html.escape(str(DEFAULT_COVER))
    db_path = html.escape(str(CONFIG.database_path))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>考公信息自动化控制台</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1f2937;
      --muted: #6b7280;
      --line: #dfe3ea;
      --accent: #1f7a5a;
      --accent-strong: #145f45;
      --danger: #b42318;
      --warning: #a15c00;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
      font-size: 14px;
      letter-spacing: 0;
    }}
    header {{
      background: #111827;
      color: #fff;
      padding: 18px 28px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
    }}
    header h1 {{ font-size: 20px; margin: 0; font-weight: 650; }}
    header p {{ margin: 4px 0 0; color: #cbd5e1; }}
    main {{ padding: 22px 28px 36px; max-width: 1480px; margin: 0 auto; }}
    .grid {{ display: grid; grid-template-columns: 360px 1fr; gap: 18px; align-items: start; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}
    .panel h2 {{ font-size: 16px; margin: 0 0 14px; }}
    .row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
    label {{ display: block; color: var(--muted); margin: 8px 0 5px; }}
    input, select {{
      width: 100%;
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 10px;
      background: #fff;
      color: var(--text);
    }}
    .checkline {{ display: flex; align-items: center; gap: 8px; margin: 12px 0; color: var(--text); }}
    .checkline input {{ width: 16px; height: 16px; }}
    button {{
      height: 36px;
      border: 0;
      border-radius: 6px;
      padding: 0 12px;
      background: var(--accent);
      color: white;
      cursor: pointer;
      font-weight: 600;
    }}
    button:hover {{ background: var(--accent-strong); }}
    button.secondary {{ background: #374151; }}
    button.warning {{ background: var(--warning); }}
    button.danger {{ background: var(--danger); }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }}
    .stats {{ display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 10px; margin-bottom: 16px; }}
    .stat {{ background: #fff; border: 1px solid var(--line); border-radius: 8px; padding: 12px; }}
    .stat b {{ display: block; font-size: 22px; margin-top: 4px; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 9px 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f1f5f9; color: #475569; white-space: nowrap; }}
    tr:hover td {{ background: #f8fafc; }}
    tr.selected td {{ background: #e8f5ef; }}
    .title-cell {{ max-width: 420px; line-height: 1.5; }}
    .badge {{ display: inline-block; padding: 2px 7px; border-radius: 999px; background: #e5e7eb; color: #374151; white-space: nowrap; }}
    .badge.ok {{ background: #dcfce7; color: #166534; }}
    .badge.warn {{ background: #fef3c7; color: #92400e; }}
    .muted {{ color: var(--muted); }}
    .log {{
      width: 100%;
      min-height: 220px;
      max-height: 420px;
      overflow: auto;
      background: #0b1020;
      color: #d1e7ff;
      border-radius: 8px;
      padding: 12px;
      white-space: pre-wrap;
      font-family: Consolas, "Courier New", monospace;
      line-height: 1.5;
    }}
    .task-status {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      margin-bottom: 10px;
      background: #f8fafc;
      color: #334155;
      line-height: 1.5;
    }}
    .task-status.running {{ border-color: #fbbf24; background: #fffbeb; }}
    .task-status.completed {{ border-color: #86efac; background: #f0fdf4; }}
    .task-status.failed {{ border-color: #fecaca; background: #fef2f2; color: #991b1b; }}
    button[disabled] {{ opacity: .55; cursor: wait; }}
    .task-table {{ width: 100%; font-size: 12px; }}
    .task-table td, .task-table th {{ padding: 7px 6px; }}
    .task-table button {{ height: 28px; padding: 0 8px; }}
    .path {{ font-family: Consolas, "Courier New", monospace; font-size: 12px; color: #475569; word-break: break-all; }}
    .progress-box {{ display: none; margin-top: 12px; }}
    .progress-track {{ width: 100%; height: 10px; border-radius: 999px; background: #e5e7eb; overflow: hidden; }}
    .progress-fill {{ width: 0%; height: 100%; background: var(--accent); transition: width .25s ease; }}
    .progress-text {{ margin-top: 6px; font-size: 12px; color: #475569; line-height: 1.5; }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>考公信息自动化控制台</h1>
      <p>采集粉笔/公考雷达、管理公告库、生成公众号预览、提交草稿箱</p>
    </div>
    <div class="actions" style="margin:0;">
      <a href="/events"><button class="secondary">打开公告库</button></a>
      <a href="/dashboard"><button class="secondary">Dashboard</button></a>
      <a href="/agent/runs"><button class="secondary">Agent 运行记录</button></a>
      <a href="/agent/tools"><button class="secondary">Agent 工具</button></a>
      <a href="/wechat/drafts"><button class="secondary">草稿箱列表</button></a>
      <a href="/wechat/published"><button class="secondary">已发布列表</button></a>
      <button class="secondary" onclick="refreshAll()">刷新状态</button>
    </div>
  </header>
  <main>
    <section class="stats" id="stats"></section>
    <div class="grid">
      <aside>
        <section class="panel">
          <h2>信息采集</h2>
          <label>数据源</label>
          <select id="collectSource">
            <option value="fenbi" selected>粉笔（推荐，无需登录）</option>
            <option value="gongkaoleida">公考雷达</option>
          </select>
          <div class="row">
            <div>
              <label>分类</label>
              <select id="collectCategory">
                <option>事业单位</option><option>公务员</option><option>省考</option><option>国考</option><option>国企</option>
                <option>教师</option><option>医疗</option><option>选调</option><option>全部</option>
              </select>
            </div>
            <div>
              <label>页码</label>
              <input id="collectPage" type="number" value="1" min="1">
            </div>
          </div>
          <label>采集数量</label>
          <input id="collectMax" type="number" value="10" min="1" max="100">
          <label>一键补全每组上限</label>
          <input id="backfillMax" type="number" value="50" min="1" max="300">
          <p class="muted">默认用粉笔采集，信息更完整且无需登录；公考雷达保留为备用来源。</p>
          <div class="actions">
            <button id="collectButton" onclick="collectEvents(false)">快速采集入库</button>
            <button id="collectSearchButton" class="warning" onclick="collectEvents(true)">采集并搜索原公告</button>
            <button id="backfillButton" class="secondary" onclick="backfillActiveFenbi()">一键补全有效考试</button>
          </div>
          <div class="progress-box" id="progressBox">
            <div class="progress-track"><div class="progress-fill" id="progressFill"></div></div>
            <div class="progress-text" id="progressText">等待任务开始</div>
          </div>
        </section>

        <section class="panel" style="margin-top:18px;">
          <h2>公众号发布</h2>
          <label>公告 source_id</label>
          <input id="topicId" placeholder="从右侧表格选择，或手动输入">
          <div class="row">
            <div>
              <label>截止天数</label>
              <input id="daysToDeadline" type="number" value="30" min="0">
            </div>
            <div>
              <label>作者</label>
              <input id="author" value="岸上信息站">
            </div>
          </div>
          <label>封面图</label>
          <input id="coverPath" value="{cover}">
          <label class="checkbox-line"><input id="includeAttachmentImagesHome" type="checkbox"> 文章中加入附件岗位表图片</label>
          <div class="actions">
            <button class="secondary" onclick="generatePreview()">生成预览</button>
            <button onclick="publishDraft()">提交草稿箱</button>
            <button class="warning" onclick="submitPublish()">尝试直接发布</button>
          </div>
        </section>

        <section class="panel" style="margin-top:18px;">
          <h2>今日 Agent</h2>
          <div class="row">
            <div>
              <label>草稿数量</label>
              <input id="todayDraftCount" type="number" value="3" min="1" max="10">
            </div>
            <div>
              <label>截止天数</label>
              <input id="todayDaysToDeadline" type="number" value="30" min="1" max="90">
            </div>
          </div>
          <label class="checkbox-line"><input id="todayIncludeAttachmentImages" type="checkbox" checked> 自动下载岗位表并插入图片</label>
          <label class="checkbox-line"><input id="todaySkipPublish" type="checkbox"> 只生成预览，不提交草稿箱</label>
          <p class="muted">Agent 会自动选择推荐分最高的正在报名公告，记录选择理由和工具调用轨迹。</p>
          <div class="actions">
            <button id="todayAgentButton" onclick="runTodayAgent()">一键生成今日草稿</button>
            <a href="/dashboard"><button class="secondary" type="button">打开 Dashboard</button></a>
          </div>
        </section>

        <section class="panel" style="margin-top:18px;">
          <h2>系统检测</h2>
          <p class="path">数据库：{db_path}</p>
          <p class="path">默认封面：{cover}</p>
          <div class="actions">
            <button class="secondary" onclick="checkWechat()">检测微信 Token</button>
            <button class="secondary" onclick="refreshEvents()">刷新公告列表</button>
          </div>
        </section>

        <section class="panel" style="margin-top:18px;">
          <h2>最近任务</h2>
          <div style="overflow:auto;">
            <table class="task-table">
              <thead><tr><th>任务</th><th>状态</th><th>耗时</th><th>操作</th></tr></thead>
              <tbody id="taskRows"><tr><td colspan="4" class="muted">暂无任务</td></tr></tbody>
            </table>
          </div>
        </section>
      </aside>

      <section>
        <section class="panel">
          <h2>公告库</h2>
          <div class="row">
            <div>
              <label>关键词</label>
              <input id="filterKeyword" placeholder="标题、单位、地区">
            </div>
            <div>
              <label>原公告状态</label>
              <select id="filterOrigin">
                <option value="">全部</option><option value="pending">pending</option>
                <option value="searched">searched</option><option value="found">found</option>
                <option value="not_found">not_found</option>
              </select>
            </div>
          </div>
          <div class="actions">
            <button class="secondary" onclick="refreshEvents()">查询</button>
          </div>
          <div style="overflow:auto; margin-top:12px;">
            <table>
              <thead>
                <tr>
                  <th>来源</th><th>ID</th><th>标题</th><th>地区</th><th>粉笔类型</th><th>截止日期</th><th>倒计时</th>
                  <th>状态</th><th>原公告</th><th>次数</th><th>操作</th>
                </tr>
              </thead>
              <tbody id="eventRows"></tbody>
            </table>
          </div>
        </section>

        <section class="panel" style="margin-top:18px;">
          <h2>运行日志</h2>
          <div class="actions" style="margin-top:0; margin-bottom:10px;">
            <button class="secondary" onclick="clearLog()">清空日志</button>
            <button class="secondary" onclick="refreshTasks()">刷新任务</button>
          </div>
          <div id="taskStatus" class="task-status">当前没有运行中的任务。</div>
          <div id="log" class="log">控制台已就绪。</div>
        </section>
      </section>
    </div>
  </main>
<script>
let selectedId = "";
let activeTaskId = "";
let lastOutputLength = 0;
let taskTimer = null;

function log(text) {{
  const el = document.getElementById("log");
  const time = new Date().toLocaleTimeString();
  el.textContent += "\\n\\n[" + time + "] " + text;
  el.scrollTop = el.scrollHeight;
}}

function clearLog() {{
  document.getElementById("log").textContent = "日志已清空。";
}}

async function api(path, options) {{
  const res = await fetch(path, options);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || JSON.stringify(data));
  return data;
}}

function resultText(data) {{
  return (data.ok ? "成功" : "失败") + "\\n命令：" + (data.command || "") +
    "\\n耗时：" + (data.elapsed_seconds || 0) + " 秒\\n\\n" + (data.output || data.message || "");
}}

function setButtonBusy(buttonId, busy) {{
  for (const id of String(buttonId || "").split(",")) {{
    const button = document.getElementById(id.trim());
    if (button) {{
      button.disabled = !!busy;
    }}
  }}
}}

function setTaskStatus(task) {{
  const el = document.getElementById("taskStatus");
  if (!task) {{
    el.className = "task-status";
    el.textContent = "当前没有运行中的任务。";
    return;
  }}
  el.className = "task-status " + task.status;
  const elapsed = task.elapsed_seconds ? `，耗时 ${{task.elapsed_seconds}} 秒` : "";
  const error = task.error ? `，错误：${{task.error}}` : "";
  el.textContent = `${{task.label || "任务"}}：${{task.status}}${{elapsed}}${{error}}`;
}}

function resetProgress(text) {{
  const box = document.getElementById("progressBox");
  const fill = document.getElementById("progressFill");
  const label = document.getElementById("progressText");
  if (!box || !fill || !label) return;
  box.style.display = "block";
  fill.style.width = "0%";
  label.textContent = text || "任务准备中";
}}

function updateProgressFromOutput(output) {{
  const box = document.getElementById("progressBox");
  const fill = document.getElementById("progressFill");
  const label = document.getElementById("progressText");
  if (!box || !fill || !label || !output) return;
  const matches = [...String(output).matchAll(/PROGRESS\\s+(\\d+)\\/(\\d+)\\s*([^\\n]*)/g)];
  if (!matches.length) return;
  const latest = matches[matches.length - 1];
  const current = Number(latest[1] || 0);
  const total = Math.max(1, Number(latest[2] || 1));
  const message = latest[3] || "";
  const pct = Math.max(0, Math.min(100, Math.round(current * 100 / total)));
  box.style.display = "block";
  fill.style.width = pct + "%";
  label.textContent = `${{pct}}%（${{current}}/${{total}}）${{message ? " - " + message : ""}}`;
}}

async function startTask(endpoint, payload, buttonId) {{
  setButtonBusy(buttonId, true);
  const data = await api(endpoint, {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify(payload)
  }});
  activeTaskId = data.task_id;
  lastOutputLength = 0;
  setTaskStatus(data.task);
  log("任务已启动：" + (data.task ? data.task.label : endpoint) + "\\n任务ID：" + activeTaskId);
  pollTask(buttonId);
}}

async function pollTask(buttonId) {{
  if (!activeTaskId) return;
  if (taskTimer) clearTimeout(taskTimer);
  try {{
    const data = await api(`/api/task?id=${{encodeURIComponent(activeTaskId)}}`);
    const task = data.task;
    setTaskStatus(task);
    const output = task.output || "";
    updateProgressFromOutput(output);
    if (output.length > lastOutputLength) {{
      log(output.slice(lastOutputLength).trim());
      lastOutputLength = output.length;
    }}
    if (task.status === "running") {{
      taskTimer = setTimeout(() => pollTask(buttonId), 1200);
      return;
    }}
    setButtonBusy(buttonId, false);
    log(task.ok ? "任务完成。" : "任务失败，请查看上方输出。");
    await refreshAll();
  }} catch (err) {{
    setButtonBusy(buttonId, false);
    log("任务状态读取失败：" + (err && err.message ? err.message : err));
  }}
}}

async function refreshTasks() {{
  const data = await api("/api/tasks");
  const tasks = data.tasks || [];
  if (!tasks.length) {{
    const tbody = document.getElementById("taskRows");
    if (tbody) {{
      tbody.innerHTML = '<tr><td colspan="4" class="muted">暂无任务</td></tr>';
    }}
    log("暂无任务历史。");
    return;
  }}
  renderTaskRows(tasks);
  const lines = tasks.slice(0, 8).map(task => `${{task.started_at}} | ${{task.label}} | ${{task.status}} | ${{task.elapsed_seconds || 0}}s`);
  log("最近任务：\\n" + lines.join("\\n"));
}}

function renderTaskRows(tasks) {{
  const tbody = document.getElementById("taskRows");
  if (!tbody) return;
  tbody.innerHTML = "";
  for (const task of tasks.slice(0, 8)) {{
    const tr = document.createElement("tr");
    const badgeClass = task.status === "completed" ? "ok" : "warn";
    tr.innerHTML = `
      <td>${{escapeHtml(task.label || "")}}</td>
      <td><span class="badge ${{badgeClass}}">${{escapeHtml(task.status || "")}}</span></td>
      <td>${{task.elapsed_seconds || 0}}s</td>
      <td><button class="secondary" onclick="retryTask('${{escapeJs(task.id)}}')">重跑</button></td>
    `;
    tbody.appendChild(tr);
  }}
}}

async function retryTask(taskId) {{
  try {{
    log("准备重跑任务：" + taskId);
    const data = await api("/api/retry_task", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{task_id: taskId}})
    }});
    activeTaskId = data.task_id;
    lastOutputLength = 0;
    setTaskStatus(data.task);
    log("重跑任务已启动：\\n任务ID：" + activeTaskId);
    pollTask("");
  }} catch (err) {{
    log("重跑失败：" + (err && err.message ? err.message : err));
  }}
}}

async function refreshStats() {{
  const data = await api("/api/stats");
  const stats = data.stats || {{}};
  document.getElementById("stats").innerHTML = `
    <div class="stat"><span class="muted">公告总数</span><b>${{stats.total ?? 0}}</b></div>
    <div class="stat"><span class="muted">已找到原公告</span><b>${{stats.origin_found ?? 0}}</b></div>
    <div class="stat"><span class="muted">待搜索</span><b>${{stats.origin_pending ?? 0}}</b></div>
    <div class="stat"><span class="muted">搜索失败封存</span><b>${{stats.origin_not_found ?? 0}}</b></div>
  `;
}}

async function refreshEvents() {{
  const keyword = encodeURIComponent(document.getElementById("filterKeyword").value.trim());
  const origin = encodeURIComponent(document.getElementById("filterOrigin").value);
  const data = await api(`/api/events?limit=80&keyword=${{keyword}}&origin_status=${{origin}}`);
  const tbody = document.getElementById("eventRows");
  tbody.innerHTML = "";
  for (const row of data.events) {{
    const tr = document.createElement("tr");
    tr.dataset.id = row.source_id;
    if (row.source_id === selectedId) tr.classList.add("selected");
    const hasOrigin = !!row.source_origin_url;
    tr.innerHTML = `
      <td>${{escapeHtml(row.source_platform || "")}}</td>
      <td>${{escapeHtml(row.source_id || "")}}</td>
      <td class="title-cell">${{escapeHtml(row.title || "")}}</td>
      <td>${{escapeHtml(row.region || "")}}</td>
      <td>${{escapeHtml(row.fenbi_exam_type_name || row.category || "")}}</td>
      <td>${{escapeHtml(row.deadline_date_display || row.registration_deadline || "")}}</td>
      <td>${{escapeHtml(row.deadline_countdown || "")}}</td>
      <td><span class="badge">${{escapeHtml(row.status || "")}}</span></td>
      <td><span class="badge ${{hasOrigin ? "ok" : "warn"}}">${{hasOrigin ? "有" : escapeHtml(row.origin_search_status || "pending")}}</span></td>
      <td>${{row.origin_search_attempts ?? 0}}</td>
      <td><button class="secondary" onclick="selectEvent('${{escapeJs(row.source_id)}}')">选择</button></td>
    `;
    tbody.appendChild(tr);
  }}
  log("公告列表已刷新，共 " + data.events.length + " 条。");
}}

function selectEvent(id) {{
  selectedId = id;
  document.getElementById("topicId").value = id;
  for (const tr of document.querySelectorAll("#eventRows tr")) {{
    tr.classList.toggle("selected", tr.dataset.id === id);
  }}
  log("已选择公告：" + id);
}}

async function collectEvents(useOriginSearch) {{
  try {{
    log(useOriginSearch ? "开始采集并搜索原公告，请稍等..." : "开始快速采集入库，请稍等...");
    const payload = {{
      source: document.getElementById("collectSource").value,
      category: document.getElementById("collectCategory").value,
      page: Number(document.getElementById("collectPage").value || 1),
      max_items: Number(document.getElementById("collectMax").value || 20),
      use_origin_search: !!useOriginSearch
    }};
    await startTask("/api/collect", payload, "collectButton,collectSearchButton");
  }} catch (err) {{
    setButtonBusy("collectButton", false);
    setButtonBusy("collectSearchButton", false);
    log("采集失败：" + (err && err.message ? err.message : err));
  }}
}}

async function backfillActiveFenbi() {{
  try {{
    resetProgress("准备按粉笔考试类型补全有效公告");
    log("开始一键补全：按粉笔考试类型逐个采集即将开始和正在报名，全国范围。");
    const payload = {{
      max_items: Number(document.getElementById("backfillMax").value || 50)
    }};
    await startTask("/api/fenbi_backfill_active", payload, "collectButton,collectSearchButton,backfillButton");
  }} catch (err) {{
    setButtonBusy("collectButton", false);
    setButtonBusy("collectSearchButton", false);
    setButtonBusy("backfillButton", false);
    log("一键补全失败：" + (err && err.message ? err.message : err));
  }}
}}

async function runTodayAgent() {{
  try {{
    log("今日 Agent 开始规划：自动选题、岗位表处理、生成公众号草稿。");
    const payload = {{
      count: Number(document.getElementById("todayDraftCount").value || 3),
      days_to_deadline: Number(document.getElementById("todayDaysToDeadline").value || 30),
      author: document.getElementById("author").value.trim(),
      cover_path: document.getElementById("coverPath").value.trim(),
      include_attachment_images: !!document.getElementById("todayIncludeAttachmentImages").checked,
      skip_publish: !!document.getElementById("todaySkipPublish").checked
    }};
    await startTask("/api/today_agent", payload, "todayAgentButton");
  }} catch (err) {{
    setButtonBusy("todayAgentButton", false);
    log("今日 Agent 启动失败：" + (err && err.message ? err.message : err));
  }}
}}

async function generatePreview() {{
  await runWechat(false, false);
}}

async function publishDraft() {{
  await runWechat(false, true);
}}

async function submitPublish() {{
  await runWechat(true, true);
}}

async function runWechat(submitPublish, doPublish) {{
  const topicId = document.getElementById("topicId").value.trim();
  if (!topicId) {{
    log("请先选择或输入公告 source_id。");
    return;
  }}
  log(submitPublish ? "开始创建草稿并尝试直接发布..." : (doPublish ? "开始提交草稿箱..." : "开始生成预览..."));
  const payload = {{
    topic_id: topicId,
    days_to_deadline: Number(document.getElementById("daysToDeadline").value || 30),
    author: document.getElementById("author").value.trim(),
    cover_path: document.getElementById("coverPath").value.trim(),
    skip_publish: !doPublish,
    submit_publish: submitPublish,
    include_attachment_images: !!document.getElementById("includeAttachmentImagesHome").checked
  }};
  await startTask("/api/wechat", payload, "");
}}

async function checkWechat() {{
  const data = await api("/api/check_wechat");
  log((data.ok ? "微信 Token 检测成功：" : "微信 Token 检测失败：") + data.message);
}}

async function refreshAll() {{
  await refreshStats();
  await refreshEvents();
  const data = await api("/api/tasks");
  renderTaskRows(data.tasks || []);
}}

function escapeHtml(value) {{
  return String(value).replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
}}

function escapeJs(value) {{
  const slash = String.fromCharCode(92);
  return String(value).split(slash).join(slash + slash).replace(/'/g, slash + "'");
}}

refreshAll().catch(err => log("初始化失败：" + err.message));
</script>
</body>
</html>"""


# ── 微信 AI 客服回调处理 ──────────────────────────────────────

def _handle_wechat_callback_get(handler, params):
    """微信服务器配置验证（GET 请求）。"""
    from .wechat_ai_service import verify_signature
    signature = (params.get("signature") or [""])[0]
    timestamp = (params.get("timestamp") or [""])[0]
    nonce = (params.get("nonce") or [""])[0]
    echostr = (params.get("echostr") or [""])[0]

    if verify_signature(signature, timestamp, nonce):
        print(f"[wechat] 签名验证成功", flush=True)
        _text_response(handler, echostr, content_type="text/plain; charset=utf-8")
    else:
        print(f"[wechat] 签名验证失败", flush=True)
        handler.send_response(403)
        handler.end_headers()


def _handle_wechat_callback_post(handler):
    """处理用户消息推送（POST 请求）。"""
    from .wechat_ai_service import parse_message, build_text_reply

    content_length = int(handler.headers.get("Content-Length", "0") or 0)
    raw_body = handler.rfile.read(content_length).decode("utf-8") if content_length > 0 else ""

    print(f"[wechat] 收到消息: {raw_body[:200]}...", flush=True)

    try:
        msg = parse_message(raw_body)
        from_user = msg.get("FromUserName", "")
        to_user = msg.get("ToUserName", "")
        msg_type = msg.get("MsgType", "text")
        content = msg.get("Content", "")

        print(f"[wechat] 用户 {from_user} 发送: [{msg_type}] {content[:60]}", flush=True)

        # ── Demo: 固定回复 ──
        reply_content = "好的，收到"
        reply_xml = build_text_reply(to_user, from_user, reply_content)

        payload = reply_xml.encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/xml; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    except Exception as e:
        print(f"[wechat] 处理失败: {e}", flush=True)
        handler.send_response(200)
        handler.end_headers()
        handler.wfile.write(b"success")


def _handle_chat_stream(handler, params):
    """SSE streaming chat endpoint."""
    from .chat_service import generate_chat_stream
    msg = (params.get("message") or [""])[0]
    sid = (params.get("session") or ["default"])[0]
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    try:
        for event in generate_chat_stream(msg, sid):
            handler.wfile.write(event.encode("utf-8"))
            handler.wfile.flush()
    except Exception as e:
        handler.wfile.write(f'data: {{"type":"error","content":"{e}"}}\n\n'.encode())


def _chat_page_html() -> str:
    return r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Chat - Gongkao Agent</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Microsoft YaHei",sans-serif;background:#111827;color:#e5e7eb;height:100vh;display:flex;flex-direction:column}
header{background:#1f2937;padding:12px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #374151}
header h1{font-size:18px;color:#f9fafb}
header a,header button{color:#60a5fa;text-decoration:none;font-size:14px;background:none;border:none;cursor:pointer}
.chat-container{flex:1;overflow-y:auto;padding:16px 20px;display:flex;flex-direction:column;gap:12px}
.message{max-width:85%;padding:10px 14px;border-radius:12px;line-height:1.6;font-size:14px;white-space:pre-wrap;word-break:break-word;animation:fadeIn .3s}
.message.user{align-self:flex-end;background:#2563eb;color:#fff;border-bottom-right-radius:4px}
.message.assistant{align-self:flex-start;background:#374151;color:#e5e7eb;border-bottom-left-radius:4px}
.message.system{align-self:center;background:transparent;color:#9ca3af;font-size:12px;padding:4px 8px}
.input-area{background:#1f2937;padding:12px 20px;display:flex;gap:8px;border-top:1px solid #374151}
.input-area input{flex:1;padding:10px 14px;border-radius:20px;border:1px solid #4b5563;background:#374151;color:#f9fafb;font-size:14px;outline:none}
.input-area input:focus{border-color:#60a5fa}
.input-area button{padding:10px 20px;border-radius:20px;border:none;background:#2563eb;color:#fff;cursor:pointer;font-weight:600;font-size:14px}
.input-area button:hover{background:#1d4ed8}
.input-area button:disabled{opacity:.5;cursor:wait}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.tool-badge{display:inline-block;background:#1e40af;color:#93c5fd;padding:2px 8px;border-radius:10px;font-size:11px;margin:4px 2px}
.loading{display:inline-block;width:8px;height:8px;border-radius:50%;background:#9ca3af;animation:bounce 1.4s infinite;margin:0 2px}
.loading:nth-child(2){animation-delay:.2s}.loading:nth-child(3){animation-delay:.4s}
@keyframes bounce{0%,80%,100%{transform:scale(0)}40%{transform:scale(1)}}
</style>
</head>
<body>
<header>
  <h1>AI Chat</h1>
  <div>
    <a href="/">Dashboard</a> |
    <a href="/dashboard">Stats</a> |
    <button onclick="newSession()" style="color:#60a5fa;cursor:pointer;font-size:14px">New Chat</button>
  </div>
</header>
<div class="chat-container" id="chat"></div>
<div class="input-area">
  <input id="input" placeholder="Ask about gongkao..." onkeydown="if(event.key==='Enter')send()" autofocus>
  <button id="sendBtn" onclick="send()">Send</button>
</div>
<script>
let sessionId = localStorage.getItem('chat_session') || ('s'+Date.now());
localStorage.setItem('chat_session', sessionId);
let isStreaming = false;

function newSession(){
  sessionId = 's'+Date.now();
  localStorage.setItem('chat_session', sessionId);
  document.getElementById('chat').innerHTML = '';
  addMsg('system','New conversation');
}

function addMsg(role, content, streaming){
  const chat = document.getElementById('chat');
  let el = chat.querySelector('.msg-streaming');
  if(streaming && el){el.innerHTML=content;chat.scrollTop=chat.scrollHeight;return el}
  el = document.createElement('div');
  el.className = 'message '+role+(streaming?' msg-streaming':'');
  el.innerHTML = content;
  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
  return el;
}

async function send(){
  if(isStreaming) return;
  const input = document.getElementById('input');
  const msg = input.value.trim();
  if(!msg) return;
  input.value = ''; input.disabled = true;
  document.getElementById('sendBtn').disabled = true;
  isStreaming = true;

  addMsg('user', escapeHtml(msg));
  const adiv = addMsg('assistant', '<span class="loading"></span><span class="loading"></span><span class="loading"></span>', true);
  let fullReply = '';

  try{
    const resp = await fetch('/api/chat/stream?message='+encodeURIComponent(msg)+'&session='+sessionId);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while(true){
      const {done, value} = await reader.read();
      if(done) break;
      buffer += decoder.decode(value, {stream:true});
      const lines = buffer.split('\\n');
      buffer = lines.pop() || '';
      for(const line of lines){
        if(!line.startsWith('data: ')) continue;
        try{
          const data = JSON.parse(line.slice(6));
          if(data.type==='thinking'||data.type==='tool_call'){
            addMsg('system','<span class="tool-badge">'+escapeHtml(data.content||(data.tool+' results:'+data.count))+'</span>');
          }else if(data.type==='reply'){
            fullReply = data.content;
            adiv.innerHTML = escapeHtml(fullReply);
            adiv.classList.remove('msg-streaming');
          }else if(data.type==='done'){
            adiv.classList.remove('msg-streaming');
            if(!fullReply) adiv.innerHTML = 'No response. Try asking about exam types or regions.';
          }
        }catch(e){}
      }
    }
  }catch(e){
    adiv.innerHTML = 'Network error: '+escapeHtml(e.message);
    adiv.classList.remove('msg-streaming');
  }

  input.disabled = false;
  document.getElementById('sendBtn').disabled = false;
  isStreaming = false;
  input.focus();
}

function escapeHtml(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
</script>
</body>
</html>'''


class GongkaoUiHandler(BaseHTTPRequestHandler):
    server_version = "GongkaoUi/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[ui] {self.address_string()} - {format % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            _text_response(self, _home_html())
            return
        if parsed.path == "/dashboard":
            _text_response(self, _dashboard_page_html())
            return
        if parsed.path == "/events":
            _text_response(self, _events_page_html(parse_qs(parsed.query)))
            return
        if parsed.path == "/agent/runs":
            _text_response(self, _agent_runs_page_html(parse_qs(parsed.query)))
            return
        if parsed.path == "/agent/tools":
            _text_response(self, _agent_tools_page_html())
            return
        if parsed.path == "/chat":
            _text_response(self, _chat_page_html())
            return
        if parsed.path.startswith("/api/chat/stream"):
            _handle_chat_stream(self, parse_qs(parsed.query))
            return
        if parsed.path == "/wechat/callback":
            _handle_wechat_callback_get(self, parse_qs(parsed.query))
            return
        if parsed.path == "/wechat/drafts":
            _text_response(self, _wechat_list_page_html("drafts", parse_qs(parsed.query)))
            return
        if parsed.path == "/wechat/published":
            _text_response(self, _wechat_list_page_html("published", parse_qs(parsed.query)))
            return
        if parsed.path == "/api/stats":
            _json_response(self, {"ok": True, "stats": _db_stats()})
            return
        if parsed.path == "/api/dashboard":
            _json_response(self, {"ok": True, "dashboard": _dashboard_data()})
            return
        if parsed.path == "/api/recommendations":
            params = parse_qs(parsed.query)
            try:
                limit = max(1, min(int((params.get("limit") or ["10"])[0]), 50))
            except ValueError:
                limit = 10
            include_published = (params.get("include_published") or ["0"])[0] in {"1", "true", "yes"}
            category = (params.get("category") or [""])[0]
            region = (params.get("region") or [""])[0]
            status = (params.get("status") or ["正在报名"])[0]
            items = recommend_events(
                limit=limit,
                include_published=include_published,
                status=status,
                category=category,
                region=region,
            )
            _json_response(self, {"ok": True, "recommendations": [item.to_dict() for item in items]})
            return
        if parsed.path == "/api/events":
            params = parse_qs(parsed.query)
            _json_response(self, {"ok": True, "events": _query_events(params)})
            return
        if parsed.path == "/api/check_wechat":
            result = _check_wechat_token()
            _json_response(self, result)
            return
        if parsed.path == "/api/wechat/autoreply/diagnose":
            from .wechat_ai_service import diagnose_autoreply_conflict
            report = diagnose_autoreply_conflict(our_ai_enabled=True)
            _json_response(self, {"ok": True, "diagnosis": report})
            return
        if parsed.path == "/api/task":
            task_id = (parse_qs(parsed.query).get("id") or [""])[0]
            task = _get_task(task_id)
            if task is None:
                _json_response(self, {"ok": False, "error": "任务不存在"}, status=404)
                return
            _json_response(self, {"ok": True, "task": task})
            return
        if parsed.path == "/api/tasks":
            _json_response(self, {"ok": True, "tasks": _recent_tasks()})
            return
        _json_response(self, {"ok": False, "error": "Not found"}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        # 微信回调 POST（优先处理，不需要 JSON body）
        if parsed.path == "/wechat/callback":
            _handle_wechat_callback_post(self)
            return
        body = _read_json_body(self)
        try:
            if parsed.path == "/api/collect":
                source = str(body.get("source") or "fenbi").strip()
                category_label = str(body.get("category") or "事业单位")
                if source == "gongkaoleida":
                    category = GONGKAOLEIDA_CATEGORY_OPTIONS.get(category_label, "sydw")
                    args = [
                        "--category",
                        category,
                        "--max_items",
                        str(int(body.get("max_items") or 20)),
                        "--page",
                        str(int(body.get("page") or 1)),
                    ]
                    if bool(body.get("use_origin_search")):
                        args.append("--use_origin_search")
                    task = _start_module_task("采集公考雷达", "kaoyan_collector.gongkaoleida_crawler", args, timeout=900)
                else:
                    category = FENBI_CATEGORY_OPTIONS.get(category_label)
                    if category is None:
                        category = category_label if category_label.isdigit() else "4"
                    args = [
                        "--category",
                        category,
                        "--max_items",
                        str(int(body.get("max_items") or 20)),
                        "--page",
                        str(int(body.get("page") or 1)),
                    ]
                    task = _start_module_task("采集粉笔", "kaoyan_collector.fenbi_crawler", args, timeout=900)
                _json_response(
                    self,
                    task,
                )
                return

            if parsed.path == "/api/fenbi_backfill_active":
                args = [
                    "--backfill_active",
                    "--max_items",
                    str(int(body.get("max_items") or 50)),
                ]
                task = _start_module_task(
                    "一键补全粉笔有效考试",
                    "kaoyan_collector.fenbi_crawler",
                    args,
                    timeout=3600,
                )
                _json_response(self, task)
                return

            if parsed.path == "/api/today_agent":
                count = max(1, min(int(body.get("count") or 3), 10))
                args = [
                    "--count",
                    str(count),
                    "--days_to_deadline",
                    str(int(body.get("days_to_deadline") or 30)),
                    "--author",
                    str(body.get("author") or "岸上信息站").strip(),
                ]
                cover = str(body.get("cover_path") or "").strip()
                if cover:
                    args.extend(["--wechat_cover", cover])
                if bool(body.get("skip_publish")):
                    args.append("--skip_publish")
                if bool(body.get("include_attachment_images")):
                    args.append("--include_attachment_images")
                label = f"今日公众号草稿 Agent（{count}条）"
                _json_response(
                    self,
                    _start_module_task(label, "kaoyan_collector.gongkao_today_agent", args, timeout=max(1200, count * 420)),
                )
                return

            if parsed.path == "/api/wechat":
                args = [
                    "--topic_id",
                    str(body.get("topic_id") or "").strip(),
                    "--days_to_deadline",
                    str(int(body.get("days_to_deadline") or 30)),
                    "--author",
                    str(body.get("author") or "岸上信息站").strip(),
                ]
                cover = str(body.get("cover_path") or "").strip()
                if cover:
                    args.extend(["--wechat_cover", cover])
                if bool(body.get("skip_publish")):
                    args.append("--skip_publish")
                if bool(body.get("submit_publish")):
                    args.append("--submit_publish")
                if bool(body.get("include_attachment_images")):
                    args.append("--include_attachment_images")
                label = "尝试直接发布" if bool(body.get("submit_publish")) else ("生成公众号预览" if bool(body.get("skip_publish")) else "提交公众号草稿")
                _json_response(
                    self,
                    _start_module_task(label, "kaoyan_collector.gongkao_wechat_pipeline", args, timeout=900),
                )
                return

            if parsed.path == "/api/wechat_batch":
                topic_ids_raw = body.get("topic_ids") or []
                if isinstance(topic_ids_raw, str):
                    topic_ids = [item.strip() for item in topic_ids_raw.split(",") if item.strip()]
                elif isinstance(topic_ids_raw, list):
                    topic_ids = [str(item).strip() for item in topic_ids_raw if str(item).strip()]
                else:
                    topic_ids = []
                if not topic_ids:
                    _json_response(self, {"ok": False, "error": "请至少选择一条公告。"}, status=400)
                    return
                if len(topic_ids) > 50:
                    _json_response(self, {"ok": False, "error": "单次批量最多选择 50 条。"}, status=400)
                    return
                args = [
                    "--topic_ids",
                    ",".join(topic_ids),
                    "--days_to_deadline",
                    str(int(body.get("days_to_deadline") or 30)),
                    "--author",
                    str(body.get("author") or "岸上信息站").strip(),
                ]
                cover = str(body.get("cover_path") or "").strip()
                if cover:
                    args.extend(["--wechat_cover", cover])
                if bool(body.get("skip_publish")):
                    args.append("--skip_publish")
                if bool(body.get("submit_publish")):
                    args.append("--submit_publish")
                if bool(body.get("include_attachment_images")):
                    args.append("--include_attachment_images")
                label = "批量直接发布" if bool(body.get("submit_publish")) else ("批量生成公众号预览" if bool(body.get("skip_publish")) else "批量提交公众号草稿")
                _json_response(
                    self,
                    _start_module_task(label, "kaoyan_collector.gongkao_wechat_batch", args, timeout=max(900, len(topic_ids) * 240)),
                )
                return

            if parsed.path == "/api/attachments":
                topic_ids_raw = body.get("topic_ids") or []
                if isinstance(topic_ids_raw, str):
                    topic_ids = [item.strip() for item in topic_ids_raw.split(",") if item.strip()]
                elif isinstance(topic_ids_raw, list):
                    topic_ids = [str(item).strip() for item in topic_ids_raw if str(item).strip()]
                else:
                    topic_ids = []
                if not topic_ids:
                    _json_response(self, {"ok": False, "error": "请至少选择一条公告。"}, status=400)
                    return
                if len(topic_ids) > 100:
                    _json_response(self, {"ok": False, "error": "单次附件处理最多选择 100 条。"}, status=400)
                    return
                args = [
                    "--topic_ids",
                    ",".join(topic_ids),
                    "--max_attachments",
                    str(int(body.get("max_attachments") or 10)),
                ]
                if bool(body.get("job_tables_only")):
                    args.append("--job_tables_only")
                label = f"下载岗位表附件（{len(topic_ids)}条公告）" if bool(body.get("job_tables_only")) else f"下载解析附件（{len(topic_ids)}条公告）"
                _json_response(
                    self,
                    _start_module_task(label, "kaoyan_collector.gongkao_attachments", args, timeout=max(900, len(topic_ids) * 180)),
                )
                return

            if parsed.path == "/api/attachments_scan_all":
                limit = int(body.get("limit") or 5000)
                limit = max(1, min(limit, 10000))
                args = [
                    "--limit",
                    str(limit),
                    "--max_attachments",
                    "0",
                    "--metadata_only",
                ]
                _json_response(
                    self,
                    _start_module_task("扫描全库附件链接", "kaoyan_collector.gongkao_attachments", args, timeout=1800),
                )
                return

            if parsed.path == "/api/retry_task":
                task_id = str(body.get("task_id") or "").strip()
                result = _retry_task(task_id)
                _json_response(self, result, status=200 if result.get("ok") else 400)
                return

            _json_response(self, {"ok": False, "error": "Not found"}, status=404)
        except Exception as exc:
            _json_response(self, {"ok": False, "error": str(exc)}, status=500)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local web UI for gongkao automation.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), GongkaoUiHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"考公信息自动化控制台已启动: {url}")
    print("按 Ctrl+C 停止服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
