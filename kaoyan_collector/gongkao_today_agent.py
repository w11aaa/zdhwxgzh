from __future__ import annotations

import argparse
import subprocess
import sys
import time

from .config import CONFIG
from .gongkao_recommender import recommend_events


def _run_command(command: list[str], *, timeout: int) -> int:
    print("[today-agent] 命令: " + " ".join(command), flush=True)
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
            if time.time() - started > timeout:
                process.kill()
                print(f"[today-agent] 子任务超时，已停止 timeout={timeout}s", flush=True)
                return -1
    returncode = process.wait()
    print(f"[today-agent] 子任务结束 returncode={returncode}", flush=True)
    return returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan and generate today's recommended WeChat drafts.")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--days_to_deadline", type=int, default=30)
    parser.add_argument("--author", default="岸上信息站")
    parser.add_argument("--wechat_cover", default=str(CONFIG.workspace_root / "wechat_cover.png"))
    parser.add_argument("--skip_publish", action="store_true", help="Only generate previews, do not submit drafts.")
    parser.add_argument("--include_attachment_images", action="store_true")
    parser.add_argument("--skip_attachment_download", action="store_true")
    args = parser.parse_args()

    count = max(1, min(args.count, 10))
    print("=" * 72, flush=True)
    print(f"[today-agent] 目标：选择今天最适合发布的 {count} 条公告并生成公众号{'预览' if args.skip_publish else '草稿'}。", flush=True)
    recommendations = recommend_events(limit=count, include_published=False, status="正在报名")
    if not recommendations:
        print("[today-agent] 没有找到可推荐的正在报名公告。", flush=True)
        return

    selected_ids = [item.source_id for item in recommendations]
    print("[today-agent] 推荐结果：", flush=True)
    for index, item in enumerate(recommendations, 1):
        print(f"[today-agent] {index}. score={item.score} id={item.source_id} title={item.title}", flush=True)
        print(f"[today-agent]    理由：{'；'.join(item.reasons)}", flush=True)

    if args.include_attachment_images and not args.skip_attachment_download:
        print("=" * 72, flush=True)
        print("[today-agent] 步骤1：下载解析岗位表候选附件。", flush=True)
        attachment_cmd = [
            sys.executable,
            "-B",
            "-m",
            "kaoyan_collector.gongkao_attachments",
            "--topic_ids",
            ",".join(selected_ids),
            "--max_attachments",
            "20",
            "--job_tables_only",
        ]
        attachment_code = _run_command(attachment_cmd, timeout=max(600, count * 180))
        if attachment_code != 0:
            print("[today-agent] 岗位表附件步骤有失败，继续生成草稿，但文章可能不包含岗位表图片。", flush=True)

    print("=" * 72, flush=True)
    print("[today-agent] 步骤2：批量生成公众号内容。", flush=True)
    batch_cmd = [
        sys.executable,
        "-B",
        "-m",
        "kaoyan_collector.gongkao_wechat_batch",
        "--topic_ids",
        ",".join(selected_ids),
        "--days_to_deadline",
        str(args.days_to_deadline),
        "--author",
        args.author,
    ]
    if args.wechat_cover:
        batch_cmd.extend(["--wechat_cover", args.wechat_cover])
    if args.skip_publish:
        batch_cmd.append("--skip_publish")
    if args.include_attachment_images:
        batch_cmd.append("--include_attachment_images")

    returncode = _run_command(batch_cmd, timeout=max(900, count * 300))
    if returncode != 0:
        raise SystemExit(returncode)

    print("=" * 72, flush=True)
    print("[today-agent] 今日草稿 Agent 任务完成。", flush=True)


if __name__ == "__main__":
    main()
