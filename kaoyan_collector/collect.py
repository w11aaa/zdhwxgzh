from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .config import CONFIG
from .store import ContentStore


def run_platform_collection(
    *,
    platform: str,
    keywords: str,
    crawler_max_notes_count: int,
    get_comment: bool,
    headless: bool,
    login_type: str,
    crawler_type: str,
    python_executable: str,
) -> int:
    raw_root = CONFIG.raw_data_root
    raw_root.mkdir(parents=True, exist_ok=True)

    store = ContentStore(CONFIG.database_path)
    run_id = store.create_crawl_run(
        platform=platform,
        keywords=keywords,
        crawler_type=crawler_type,
        save_data_path=str(raw_root),
        status="running",
    )

    command = [
        python_executable,
        "main.py",
        "--platform",
        platform,
        "--lt",
        login_type,
        "--type",
        crawler_type,
        "--keywords",
        keywords,
        "--crawler_max_notes_count",
        str(crawler_max_notes_count),
        "--get_comment",
        "true" if get_comment else "false",
        "--save_data_option",
        "jsonl",
        "--save_data_path",
        str(raw_root),
        "--headless",
        "true" if headless else "false",
    ]

    try:
        completed = subprocess.run(
            command,
            cwd=CONFIG.media_crawler_root,
            check=True,
        )
        store.finish_crawl_run(run_id, status="success")
        return completed.returncode
    except subprocess.CalledProcessError as exc:
        store.finish_crawl_run(run_id, status="failed", error_message=str(exc))
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Run MediaCrawler and store raw results for CS kaoyan topics.")
    parser.add_argument("--platform", required=True, choices=CONFIG.supported_platforms)
    parser.add_argument("--keywords", default=",".join(CONFIG.default_keywords))
    parser.add_argument("--crawler_max_notes_count", type=int, default=30)
    parser.add_argument("--get_comment", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--login_type", default="qrcode")
    parser.add_argument("--crawler_type", default="search")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()

    run_platform_collection(
        platform=args.platform,
        keywords=args.keywords,
        crawler_max_notes_count=args.crawler_max_notes_count,
        get_comment=args.get_comment,
        headless=args.headless,
        login_type=args.login_type,
        crawler_type=args.crawler_type,
        python_executable=args.python,
    )


if __name__ == "__main__":
    main()
