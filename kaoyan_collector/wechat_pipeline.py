from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from urllib.request import urlopen
from pathlib import Path
from typing import Any

from .config import CONFIG
from .schema import init_db


def _wechat_skills_root() -> Path:
    return CONFIG.workspace_root / "wechat_article_skills"


def _wechat_formatter_script() -> Path:
    return _wechat_skills_root() / "wechat-article-formatter" / "scripts" / "markdown_to_html.py"


def _wechat_publisher_script() -> Path:
    return _wechat_skills_root() / "wechat-draft-publisher" / "publisher.py"


def _wechat_output_dir() -> Path:
    path = CONFIG.project_root / "wechat_outputs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _detect_public_ip() -> str:
    try:
        with urlopen("https://api.ipify.org", timeout=8) as response:
            return response.read().decode("utf-8").strip()
    except Exception:
        return ""


def _extract_textual_draft_fields(payload: dict[str, Any]) -> tuple[str, str]:
    draft = payload.get("draft") or {}
    title = str(draft.get("title") or "").strip()
    content = str(draft.get("content") or "").strip()
    raw_text = str(draft.get("raw_text") or "").strip()
    raw_json = draft.get("raw_json") or {}

    parsed: dict[str, Any] | None = None
    for candidate in (raw_json if isinstance(raw_json, dict) else None,):
        if isinstance(candidate, dict) and candidate:
            parsed = candidate
            break

    if parsed is None:
        for candidate_text in (raw_text, content):
            if not candidate_text.startswith("{"):
                continue
            try:
                maybe = json.loads(candidate_text)
                if isinstance(maybe, dict):
                    parsed = maybe
                    break
            except Exception:
                continue

    if parsed is None:
        text_candidates = [raw_text, content]
        for candidate_text in text_candidates:
            if not candidate_text:
                continue
            title_match = re.search(r'"title"\s*:\s*"([^"]+)"', candidate_text, flags=re.S)
            content_match = re.search(
                r'"full_content"\s*:\s*"(?P<body>.+?)"\s*(?:,\s*"content_pages"|,\s*"raw_json"|,\s*"hashtags"|}\s*$)',
                candidate_text,
                flags=re.S,
            )
            extracted_title = title_match.group(1).strip() if title_match else ""
            extracted_content = content_match.group("body").strip() if content_match else ""
            if extracted_title:
                title = extracted_title.encode("utf-8").decode("unicode_escape") if "\\u" in extracted_title else extracted_title
            if extracted_content:
                extracted_content = extracted_content.replace("\\n", "\n").replace("\\r", "\r").replace('\\"', '"')
                content = extracted_content
            if extracted_title or extracted_content:
                break

    if parsed:
        try:
            better_title = str(parsed.get("title") or "").strip()
            better_content = str(parsed.get("full_content") or "").strip()
            if better_title:
                title = better_title
            if better_content:
                content = better_content
        except Exception:
            pass

    return title, content


def _build_wechat_markdown(payload: dict[str, Any], *, account_name: str = "研路笔记") -> str:
    source_topic = payload.get("source_topic") or {}
    draft = payload.get("draft") or {}

    title, content = _extract_textual_draft_fields(payload)
    preamble = draft.get("wechat_preamble") or []
    footer = draft.get("wechat_footer") or []
    hashtags = draft.get("hashtags") or []
    if not isinstance(hashtags, list):
        hashtags = []
    if not isinstance(preamble, list):
        preamble = []
    if not isinstance(footer, list):
        footer = []

    source_platform = str(source_topic.get("platform") or "").strip()
    source_keyword = str(source_topic.get("source_keyword") or "").strip()
    source_title = str(source_topic.get("title") or "").strip()
    source_author = str(source_topic.get("author_name") or "").strip()
    suppress_source_meta = bool(draft.get("suppress_source_meta"))

    sections: list[str] = []
    if not suppress_source_meta:
        sections.append(f"> 来源方向：{source_platform} / {source_keyword}".strip(" /"))
        if source_title:
            sections.append(f"> 参考选题：{source_title}")
        if source_author:
            sections.append(f"> 整理账号：{account_name} | 参考作者：{source_author}")
        sections.append("")

    clean_preamble = [str(item).strip() for item in preamble if str(item).strip()]
    if clean_preamble:
        sections.extend(clean_preamble)
        sections.append("")

    normalized = content.replace("\r\n", "\n").strip()
    if normalized:
        sections.append(normalized)
        sections.append("")

    if hashtags:
        clean_tags = [str(tag).strip() for tag in hashtags if str(tag).strip()]
        if clean_tags:
            sections.append("## 相关话题")
            sections.append("")
            sections.append(" ".join(clean_tags))
            sections.append("")

    clean_footer = [str(item).strip() for item in footer if str(item).strip()]
    if clean_footer:
        sections.append("## 说明")
        sections.append("")
        sections.extend(clean_footer)
        sections.append("")

    return "\n".join(sections).strip() + "\n"


def export_wechat_markdown(
    payload: dict[str, Any],
    *,
    source_id: str,
    account_name: str = "研路笔记",
) -> Path:
    output_dir = _wechat_output_dir()
    md_path = output_dir / f"wechat_{source_id}.md"
    markdown_text = _build_wechat_markdown(payload, account_name=account_name)
    md_path.write_text(markdown_text, encoding="utf-8")
    return md_path


def convert_markdown_to_wechat_html(
    markdown_path: Path,
    *,
    theme: str = "tech",
) -> Path:
    formatter_script = _wechat_formatter_script()
    if not formatter_script.exists():
        raise FileNotFoundError(f"未找到微信公众号 formatter 脚本: {formatter_script}")

    html_path = markdown_path.with_name(markdown_path.stem + "_formatted.html")
    command = [
        sys.executable,
        str(formatter_script),
        "--input",
        str(markdown_path),
        "--output",
        str(html_path),
        "--theme",
        theme,
    ]
    subprocess.run(command, check=True, cwd=str(CONFIG.workspace_root))
    return html_path


def publish_html_to_wechat_draft(
    *,
    title: str,
    html_path: Path,
    author: str = "研路笔记",
    cover_path: str = "",
    digest: str = "",
    submit_publish: bool = False,
    source_platform: str = "",
    source_id: str = "",
) -> None:
    publisher_script = _wechat_publisher_script()
    if not publisher_script.exists():
        raise FileNotFoundError(f"未找到微信公众号草稿发布脚本: {publisher_script}")
    normalized_cover_path = ""
    if cover_path.strip():
        normalized_cover = Path(cover_path.strip())
        if not normalized_cover.is_absolute():
            normalized_cover = (CONFIG.workspace_root / normalized_cover).resolve()
        if not normalized_cover.exists():
            raise FileNotFoundError(f"微信公众号封面图不存在或路径无效: {normalized_cover}")
        normalized_cover_path = str(normalized_cover)

    command = [
        sys.executable,
        str(publisher_script),
        "--title",
        title,
        "--content",
        str(html_path),
        "--author",
        author,
    ]
    if normalized_cover_path:
        command.extend(["--cover", normalized_cover_path])
    if digest.strip():
        command.extend(["--digest", digest.strip()])
    if submit_publish:
        command.append("--submit_publish")

    result = subprocess.run(
        command,
        check=False,
        cwd=str(publisher_script.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    _record_wechat_publish_result(
        title=title,
        author=author,
        html_path=html_path,
        cover_path=normalized_cover_path or cover_path,
        submit_publish=submit_publish,
        source_platform=source_platform,
        source_id=source_id,
        returncode=result.returncode,
        output=combined,
    )
    if result.returncode == 0:
        return

    if "40164" in combined or "IP地址不在白名单中" in combined:
        public_ip = _detect_public_ip()
        extra = f"\n当前公网出口 IP: {public_ip}" if public_ip else ""
        raise RuntimeError(f"微信公众号接口仍被 IP 白名单拦截，请检查白名单是否已生效。{extra}")

    raise RuntimeError(
        "微信公众号草稿发布失败。\n"
        f"命令: {' '.join(command)}\n"
        f"输出:\n{combined}"
    )


def _record_wechat_publish_result(
    *,
    title: str,
    author: str,
    html_path: Path,
    cover_path: str,
    submit_publish: bool,
    source_platform: str,
    source_id: str,
    returncode: int,
    output: str,
) -> None:
    init_db(CONFIG.database_path)
    media_ids = re.findall(r"media_id:\s*([A-Za-z0-9_=-]+)", output or "")
    draft_media_id = media_ids[-1] if media_ids else ""
    publish_match = re.search(r"publish_id:\s*([A-Za-z0-9_=-]+)", output or "")
    publish_id = publish_match.group(1) if publish_match else ""
    created_at = datetime.utcnow().isoformat()
    action = "submit_publish" if submit_publish else "draft"
    if returncode == 0 and submit_publish:
        status = "submitted"
    elif returncode == 0:
        status = "draft_created"
    elif draft_media_id and submit_publish:
        status = "draft_created_publish_failed"
    else:
        status = "failed"
    error_message = "" if returncode == 0 else _trim_error(output)

    with sqlite3.connect(CONFIG.database_path) as conn:
        conn.execute(
            """
            INSERT INTO wechat_publish_records(
                source_platform, source_id, title, author, action, status,
                media_id, publish_id, html_path, cover_path, submit_publish,
                raw_output, error_message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_platform,
                source_id,
                title,
                author,
                action,
                status,
                draft_media_id,
                publish_id,
                str(html_path),
                cover_path,
                1 if submit_publish else 0,
                output,
                error_message,
                created_at,
                created_at,
            ),
        )
        conn.commit()


def _trim_error(output: str) -> str:
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if "错误" in line or "失败" in line or "errcode" in line:
            return line[:500]
    return (output or "")[:500]


def save_wechat_payload_preview(payload: dict[str, Any], *, source_id: str) -> Path:
    output_dir = _wechat_output_dir()
    preview_path = output_dir / f"wechat_{source_id}_payload.json"
    preview_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return preview_path


def get_wechat_publish_fields(payload: dict[str, Any]) -> tuple[str, str]:
    title, content = _extract_textual_draft_fields(payload)
    digest = content[:120].replace("\r", " ").replace("\n", " ").strip()
    return title, digest
