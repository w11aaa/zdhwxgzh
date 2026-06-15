from __future__ import annotations

import argparse
from pathlib import Path

from .collect import run_platform_collection
from .config import CONFIG
from .ingest import discover_latest_content_file, ingest_file
from .store import ContentStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect and import kaoyan content from multiple platforms.")
    parser.add_argument("--platforms", default=",".join(CONFIG.default_platforms))
    parser.add_argument("--keywords", default=",".join(CONFIG.default_keywords))
    parser.add_argument("--crawler_max_notes_count", type=int, default=30)
    parser.add_argument("--get_comment", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--login_type", default="qrcode")
    parser.add_argument("--crawler_type", default="search")
    parser.add_argument("--python", default="python")
    parser.add_argument("--db", default=str(CONFIG.database_path))
    args = parser.parse_args()

    platforms = [item.strip() for item in args.platforms.split(",") if item.strip()]
    store = ContentStore(Path(args.db))

    for platform in platforms:
        run_platform_collection(
            platform=platform,
            keywords=args.keywords,
            crawler_max_notes_count=args.crawler_max_notes_count,
            get_comment=args.get_comment,
            headless=args.headless,
            login_type=args.login_type,
            crawler_type=args.crawler_type,
            python_executable=args.python,
        )
        file_path = discover_latest_content_file(CONFIG.raw_data_root, platform)
        imported = ingest_file(file_path, platform, store)
        print(f"[{platform}] imported {imported} items from {file_path.name}")


if __name__ == "__main__":
    main()
