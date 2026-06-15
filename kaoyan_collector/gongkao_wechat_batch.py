from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from .config import CONFIG


def _run_one(source_id: str, args: argparse.Namespace) -> tuple[bool, int]:
    command = [
        sys.executable,
        "-B",
        "-m",
        "kaoyan_collector.gongkao_wechat_pipeline",
        "--topic_id",
        source_id,
        "--days_to_deadline",
        str(args.days_to_deadline),
        "--author",
        args.author,
    ]
    if args.wechat_cover:
        command.extend(["--wechat_cover", args.wechat_cover])
    if args.skip_publish:
        command.append("--skip_publish")
    if args.submit_publish:
        command.append("--submit_publish")
    if args.skip_quality_check:
        command.append("--skip_quality_check")
    if args.include_attachment_images:
        command.append("--include_attachment_images")

    print("=" * 72, flush=True)
    print(f"[batch] 开始处理公告: {source_id}", flush=True)
    print(f"[batch] 命令: {' '.join(command)}", flush=True)
    started = time.time()
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
    if process.stdout is not None:
        for line in process.stdout:
            print(line, end="", flush=True)
    returncode = process.wait()
    elapsed = round(time.time() - started, 2)
    ok = returncode == 0
    print(f"[batch] 公告 {source_id} {'完成' if ok else '失败'}，耗时 {elapsed} 秒，returncode={returncode}", flush=True)
    return ok, returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch generate/publish WeChat drafts for selected gongkao events.")
    parser.add_argument("--topic_ids", required=True, help="Comma-separated source_id list.")
    parser.add_argument("--days_to_deadline", type=int, default=30)
    parser.add_argument("--author", default="岸上信息站")
    parser.add_argument(
        "--wechat_cover",
        default=str(CONFIG.workspace_root / "wechat_cover.png")
        if (CONFIG.workspace_root / "wechat_cover.png").exists()
        else str(CONFIG.workspace_root / "考试通知.png"),
    )
    parser.add_argument("--skip_publish", action="store_true", help="Only generate previews.")
    parser.add_argument("--submit_publish", action="store_true", help="Submit drafts for live publish after creation.")
    parser.add_argument("--skip_quality_check", action="store_true")
    parser.add_argument("--include_attachment_images", action="store_true")
    args = parser.parse_args()

    source_ids = [item.strip() for item in args.topic_ids.split(",") if item.strip()]
    if not source_ids:
        raise SystemExit("未选择任何公告。")

    print(f"[batch] 共选择 {len(source_ids)} 条公告。", flush=True)
    succeeded: list[str] = []
    failed: list[str] = []
    for index, source_id in enumerate(source_ids, 1):
        print(f"[batch] 进度 {index}/{len(source_ids)}", flush=True)
        ok, _ = _run_one(source_id, args)
        if ok:
            succeeded.append(source_id)
        else:
            failed.append(source_id)

    print("=" * 72, flush=True)
    print(f"[batch] 批量任务结束：成功 {len(succeeded)} 条，失败 {len(failed)} 条。", flush=True)
    if succeeded:
        print("[batch] 成功ID: " + ", ".join(succeeded), flush=True)
    if failed:
        print("[batch] 失败ID: " + ", ".join(failed), flush=True)
    raise SystemExit(0 if not failed else 1)


if __name__ == "__main__":
    main()
