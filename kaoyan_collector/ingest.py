from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import CONFIG
from .normalize import normalize_content
from .store import ContentStore
from .topic_filter import evaluate_relevance


def discover_latest_content_file(raw_root: Path, platform: str) -> Path:
    contents_dir = raw_root / platform / "jsonl"
    if not contents_dir.exists():
        raise FileNotFoundError(f"Raw content directory not found: {contents_dir}")

    candidates = sorted(contents_dir.glob("*_contents_*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No content jsonl files found for platform {platform} in {contents_dir}")
    return candidates[0]


def ingest_file(file_path: Path, platform: str, store: ContentStore) -> int:
    count = 0
    with file_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            normalized = normalize_content(platform, raw)
            if not normalized.get("source_id"):
                continue
            relevance = evaluate_relevance(
                str(normalized.get("title") or ""),
                str(normalized.get("content") or ""),
                str(normalized.get("source_keyword") or ""),
            )
            normalized.update(
                {
                    "is_relevant": relevance.is_relevant,
                    "relevance_score": relevance.relevance_score,
                    "relevance_label": relevance.relevance_label,
                    "relevance_reason": relevance.relevance_reason,
                }
            )
            store.upsert_content_item(normalized)
            count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Import MediaCrawler jsonl output into unified SQLite.")
    parser.add_argument("--platform", required=True, choices=CONFIG.supported_platforms)
    parser.add_argument("--file", help="Optional explicit jsonl content file path.")
    parser.add_argument("--db", default=str(CONFIG.database_path), help="SQLite database path.")
    args = parser.parse_args()

    store = ContentStore(Path(args.db))
    file_path = Path(args.file) if args.file else discover_latest_content_file(CONFIG.raw_data_root, args.platform)
    imported = ingest_file(file_path, args.platform, store)
    print(f"Imported {imported} items from {file_path}")


if __name__ == "__main__":
    main()
