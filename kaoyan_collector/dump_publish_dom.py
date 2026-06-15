from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
XHS_DATA_DIR = Path.home() / ".xhs_system"
DEFAULT_PROFILE_DIR = XHS_DATA_DIR / "chrome_user_data"
OUTPUT_DIR = WORKSPACE_ROOT / "kaoyan_collector" / "dom_dumps"


async def _launch_browser(profile_dir: Path):
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    context = await playwright.chromium.launch_persistent_context(
        str(profile_dir),
        channel="chrome",
        headless=False,
        args=["--start-maximized"],
        viewport={"width": 1440, "height": 960},
    )
    page = context.pages[0] if context.pages else await context.new_page()
    return playwright, context, page


async def _collect_dom_snapshot(page):
    return await page.evaluate(
        """
        () => {
          const rectObj = (el) => {
            try {
              const r = el.getBoundingClientRect();
              return { x: r.x, y: r.y, w: r.width, h: r.height };
            } catch (e) {
              return null;
            }
          };

          const textOf = (el) => ((el?.innerText || el?.textContent || '').trim());

          const tabs = Array.from(document.querySelectorAll('.creator-tab, [role="tab"], button, a, div'))
            .filter((el) => {
              const txt = textOf(el);
              return txt && /图文|上传图文|视频|上传视频|发布笔记/.test(txt);
            })
            .slice(0, 50)
            .map((el) => ({
              tag: el.tagName,
              className: el.className,
              text: textOf(el),
              rect: rectObj(el),
            }));

          const fileInputs = Array.from(document.querySelectorAll('input[type="file"]'))
            .slice(0, 20)
            .map((el) => ({
              className: el.className,
              accept: el.getAttribute('accept') || '',
              multiple: !!el.multiple,
              disabled: !!el.disabled,
              files: el.files ? el.files.length : 0,
              rect: rectObj(el),
            }));

          const uploadButtons = Array.from(document.querySelectorAll('button, div, span, label, a'))
            .filter((el) => {
              const txt = textOf(el);
              return txt && /上传图片|上传视频|图文|视频|发布笔记/.test(txt);
            })
            .slice(0, 50)
            .map((el) => ({
              tag: el.tagName,
              className: el.className,
              text: textOf(el),
              rect: rectObj(el),
            }));

          return {
            url: location.href,
            title: document.title,
            bodyTextPreview: textOf(document.body).slice(0, 4000),
            tabs,
            fileInputs,
            uploadButtons,
            html: document.documentElement.outerHTML,
          };
        }
        """
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Dump runtime DOM info from Xiaohongshu publish page.")
    parser.add_argument(
        "--profile-dir",
        default=str(DEFAULT_PROFILE_DIR),
        help="Chrome persistent profile directory to reuse.",
    )
    parser.add_argument(
        "--url",
        default="https://creator.xiaohongshu.com/new/home",
        help="Initial URL to open before manual navigation.",
    )
    args = parser.parse_args()

    profile_dir = Path(os.path.expanduser(args.profile_dir)).resolve()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    playwright, context, page = await _launch_browser(profile_dir)
    try:
        print(f"Using profile: {profile_dir}")
        print(f"Opening: {args.url}")
        await page.goto(args.url, wait_until="domcontentloaded", timeout=30_000)
        print("请在打开的 Chrome 窗口里手动进入“小红书图文发布页”，确认页面上能看到“上传图片”区域。")
        input("准备好后回到终端按回车，我会抓取当前页 DOM 信息...")

        snapshot = await _collect_dom_snapshot(page)
        ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())

        json_path = OUTPUT_DIR / f"publish_dom_{ts}.json"
        html_path = OUTPUT_DIR / f"publish_dom_{ts}.html"

        html = snapshot.pop("html", "")
        json_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        html_path.write_text(str(html), encoding="utf-8")

        print(f"DOM JSON saved: {json_path}")
        print(f"DOM HTML saved: {html_path}")
        print(f"Current URL: {snapshot.get('url')}")
    finally:
        await context.close()
        await playwright.stop()


if __name__ == "__main__":
    asyncio.run(main())
