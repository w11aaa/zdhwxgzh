from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import CONFIG


def _load_content_manager():
    repo_root = CONFIG.workspace_root
    publisher_root = repo_root / "xhs_ai_publisher"
    if str(publisher_root) not in sys.path:
        sys.path.insert(0, str(publisher_root))

    from src.core.content_manager import ContentManager  # type: ignore

    return ContentManager


def _read_draft(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"草稿文件不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_tags(draft_payload: dict) -> list[str]:
    raw_tags = draft_payload.get("draft", {}).get("hashtags") or []
    tags: list[str] = []
    if isinstance(raw_tags, str):
        raw_tags = [item.strip() for item in raw_tags.split() if item.strip()]

    if isinstance(raw_tags, list):
        for tag in raw_tags:
            text = str(tag or "").strip()
            if not text:
                continue
            if not text.startswith("#"):
                text = f"#{text}"
            tags.append(text)
    return tags


def import_draft(draft_path: Path) -> tuple[str, Path]:
    payload = _read_draft(draft_path)
    draft = payload.get("draft") or {}
    title = str(draft.get("title") or "").strip()
    content = str(draft.get("content") or "").strip()
    tags = _extract_tags(payload)

    if not title:
        raise ValueError("草稿缺少标题")
    if not content:
        raise ValueError("草稿缺少正文")

    ContentManager = _load_content_manager()
    manager = ContentManager()
    content_id = manager.create_content(title, content, tags)
    content_item = manager.get_content(content_id)
    if not content_item:
        raise RuntimeError("导入草稿后未能读取到内容项")

    content_file = manager.content_file
    return content_id, content_file


def main() -> None:
    parser = argparse.ArgumentParser(description="Import generated draft into xhs_ai_publisher content storage.")
    parser.add_argument("--draft", required=True, help="Draft JSON file path.")
    args = parser.parse_args()

    content_id, content_file = import_draft(Path(args.draft).resolve())
    print(f"Imported draft as content_id={content_id}")
    print(f"Content storage file: {content_file}")


if __name__ == "__main__":
    main()
