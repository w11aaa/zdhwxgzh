from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime
from typing import Dict, Optional, Tuple

from PyQt5.QtCore import QThread, pyqtSignal
from playwright.sync_api import sync_playwright

from src.core.services.chrome_profile_service import detect_chrome_profiles
from src.core.services.user_service import user_service


class ChromeSessionImportThread(QThread):
    """Import XHS login state from system Chrome profile into app storage files.

    Notes:
    - Requires user to completely quit Chrome once (profile lock).
    - Only exports xiaohongshu.com related cookies/storage to protect privacy.
    """

    progress = pyqtSignal(str)
    finished = pyqtSignal(dict)  # {user_id, cookies_file, storage_state_file, profile_directory, user_data_dir}
    error = pyqtSignal(str)

    def __init__(
        self,
        *,
        phone: str,
        chrome_user_data_dir: str = "",
        chrome_profile_directory: str = "",
        timeout_s: int = 300,
    ):
        super().__init__()
        self.phone = str(phone or "").strip()
        self.chrome_user_data_dir = str(chrome_user_data_dir or "").strip()
        self.chrome_profile_directory = str(chrome_profile_directory or "").strip()
        try:
            self.timeout_s = int(timeout_s or 300)
        except Exception:
            self.timeout_s = 300
        self.timeout_s = max(60, min(1800, self.timeout_s))

    @staticmethod
    def _is_chrome_running() -> bool:
        """Best-effort check to avoid Chrome profile lock errors."""
        try:
            if sys.platform == "darwin":
                r = subprocess.run(
                    ["pgrep", "-x", "Google Chrome"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                return r.returncode == 0
            if sys.platform == "win32":
                r = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return "chrome.exe" in (r.stdout or "").lower()
            # linux/others
            r = subprocess.run(
                ["pgrep", "-x", "chrome"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return r.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _ensure_user_for_phone(phone: str) -> int:
        phone = str(phone or "").strip()
        if not phone:
            raise ValueError("手机号不能为空")

        current_user = user_service.get_user_by_phone(phone)
        if current_user:
            user_service.switch_user(current_user.id)
            return int(current_user.id)

        normalized_phone = "".join([c for c in phone if c.isdigit()]) or phone
        username_base = f"user_{normalized_phone}"
        username = username_base
        suffix = 1
        while user_service.get_user_by_username(username):
            username = f"{username_base}_{suffix}"
            suffix += 1

        created = user_service.create_user(
            username=username,
            phone=phone,
            display_name=phone,
            set_current=True,
        )
        return int(created.id)

    @staticmethod
    def _get_user_storage_dir(user_id: Optional[int]) -> str:
        base_dir = os.path.join(os.path.expanduser("~"), ".xhs_system")
        if not user_id:
            return base_dir
        return os.path.join(base_dir, "users", str(int(user_id)))

    @staticmethod
    def _is_xhs_cookie(cookie: Dict[str, object]) -> bool:
        try:
            domain = str(cookie.get("domain") or "")
            url = str(cookie.get("url") or "")
            return ("xiaohongshu.com" in domain) or ("xiaohongshu.com" in url)
        except Exception:
            return False

    @staticmethod
    def _is_xhs_origin(origin: str) -> bool:
        return "xiaohongshu.com" in str(origin or "")

    @staticmethod
    def _check_creator_logged_in(context) -> bool:
        try:
            resp = context.request.get(
                "https://creator.xiaohongshu.com/api/galaxy/user/info",
                timeout=10_000,
            )
            status = int(getattr(resp, "status", 0) or 0)
            try:
                resp.dispose()
            except Exception:
                pass
            return status == 200
        except Exception:
            return False

    @staticmethod
    def _backup_file(path: str) -> None:
        try:
            if not path or not os.path.exists(path) or os.path.getsize(path) <= 0:
                return
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            bak = f"{path}.bak_{ts}"
            with open(path, "rb") as rf, open(bak, "wb") as wf:
                wf.write(rf.read())
        except Exception:
            pass

    @staticmethod
    def _copy_profile_to_temp(*, user_data_dir: str, profile_dir: str) -> str:
        """Copy a system Chrome profile to a non-default user-data-dir for Playwright.

        Chrome disallows remote debugging with the default user data directory. We work
        around it by copying the selected profile (and Local State) into a temporary dir.
        """
        src_root = os.path.abspath(os.path.expanduser(str(user_data_dir or "").strip()))
        src_profile = os.path.join(src_root, str(profile_dir or "Default").strip() or "Default")
        if not os.path.isdir(src_root):
            raise RuntimeError(f"Chrome 用户数据目录不存在: {src_root}")
        if not os.path.isdir(src_profile):
            raise RuntimeError(f"Chrome Profile 目录不存在: {src_profile}")

        temp_root = tempfile.mkdtemp(prefix="xhs_chrome_import_")
        # Copy Local State for cookie encryption key.
        try:
            local_state_src = os.path.join(src_root, "Local State")
            if os.path.exists(local_state_src):
                shutil.copy2(local_state_src, os.path.join(temp_root, "Local State"))
        except Exception:
            # Not fatal; Chrome may still start and recreate.
            pass

        # Copy the profile directory.
        dst_profile = os.path.join(temp_root, os.path.basename(src_profile))
        shutil.copytree(src_profile, dst_profile, dirs_exist_ok=True)
        return temp_root

    def run(self) -> None:
        temp_user_data_dir = ""
        try:
            if not self.phone:
                raise ValueError("手机号不能为空")

            self.progress.emit("准备导入系统 Chrome 登录态...")
            if self._is_chrome_running():
                raise RuntimeError("检测到 Chrome 正在运行，请先完全退出 Chrome（Cmd+Q）后再导入。")

            user_id = self._ensure_user_for_phone(self.phone)
            user_dir = self._get_user_storage_dir(user_id)
            os.makedirs(user_dir, exist_ok=True)

            cookies_file = os.path.join(user_dir, "xiaohongshu_cookies.json")
            storage_state_file = os.path.join(user_dir, "xiaohongshu_storage_state.json")

            detected = detect_chrome_profiles(self.chrome_user_data_dir)
            if not detected:
                raise RuntimeError("未检测到系统 Chrome 用户数据目录。请确认已安装 Chrome，或在 .env 手动配置 XHS_CHROME_USER_DATA_DIR。")

            chrome_user_data_dir = str(detected.user_data_dir)
            profile_dir = self.chrome_profile_directory or detected.default_profile_directory or "Default"

            self.progress.emit("正在复制 Chrome Profile（仅临时使用，导入完成会自动清理）...")
            temp_user_data_dir = self._copy_profile_to_temp(user_data_dir=chrome_user_data_dir, profile_dir=profile_dir)

            self.progress.emit(f"打开临时 Chrome Profile：{profile_dir}（请勿手动关闭弹窗，导入完成会自动退出）")

            with sync_playwright() as p:
                last_err = None
                launch_args = ["--start-maximized"]
                if profile_dir and not any(a.startswith("--profile-directory=") for a in launch_args):
                    launch_args.append(f"--profile-directory={profile_dir}")

                for attempt in (
                    {"channel": "chrome"},
                    {},
                ):
                    try:
                        ctx = p.chromium.launch_persistent_context(
                            temp_user_data_dir,
                            headless=False,
                            timeout=120_000,
                            args=launch_args,
                            **attempt,
                        )
                        break
                    except Exception as e:
                        last_err = e
                        ctx = None
                if not ctx:
                    raise RuntimeError(f"启动系统 Chrome 失败: {last_err}")

                try:
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()

                    # Touch key origins so storage_state includes localStorage for them.
                    try:
                        page.goto("https://creator.xiaohongshu.com/new/home", wait_until="domcontentloaded", timeout=30_000)
                        time.sleep(1.2)
                    except Exception:
                        pass
                    try:
                        page.goto("https://www.xiaohongshu.com/", wait_until="domcontentloaded", timeout=30_000)
                        time.sleep(1.2)
                    except Exception:
                        pass

                    if not self._check_creator_logged_in(ctx):
                        self.progress.emit("未检测到登录态，请在打开的 Chrome 窗口完成扫码/风控验证（最多等待 5 分钟）...")
                        deadline = time.time() + float(self.timeout_s)
                        while time.time() < deadline:
                            if self._check_creator_logged_in(ctx):
                                break
                            time.sleep(3)

                    if not self._check_creator_logged_in(ctx):
                        raise RuntimeError("导入失败：仍未检测到创作者中心登录态（可能未登录/风控未完成）。")

                    raw_state = ctx.storage_state()
                finally:
                    try:
                        ctx.close()
                    except Exception:
                        pass

            # Privacy: keep only xiaohongshu.com related state.
            cookies = []
            try:
                cookies = list((raw_state or {}).get("cookies") or [])
            except Exception:
                cookies = []
            cookies = [c for c in cookies if isinstance(c, dict) and self._is_xhs_cookie(c)]

            origins = []
            try:
                origins = list((raw_state or {}).get("origins") or [])
            except Exception:
                origins = []
            kept_origins = []
            for o in origins:
                if not isinstance(o, dict):
                    continue
                origin = str(o.get("origin") or "").strip()
                if not origin or not self._is_xhs_origin(origin):
                    continue
                kept_origins.append(o)

            state = {"cookies": cookies, "origins": kept_origins}

            self._backup_file(cookies_file)
            self._backup_file(storage_state_file)
            with open(cookies_file, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False)
            with open(storage_state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)

            self.progress.emit("✅ 导入完成：已保存登录态文件（下一步点“登录”尝试复用）")
            self.finished.emit(
                {
                    "user_id": user_id,
                    "cookies_file": cookies_file,
                    "storage_state_file": storage_state_file,
                    "profile_directory": profile_dir,
                    "user_data_dir": chrome_user_data_dir,
                }
            )
        except Exception as e:
            # Emit a concise message for UI, but keep a full traceback for debugging.
            msg = str(e)
            try:
                tb = traceback.format_exc()
                msg = msg.strip() or tb.strip()
                # Attach a short hint if we hit Chrome's default profile restriction.
                if "DevTools remote debugging requires a non-default data directory" in tb:
                    msg = (
                        "Chrome 默认用户数据目录不允许远程调试，导致导入失败。\n"
                        "已实现“复制 Profile 到临时目录再导入”的方案；请重试。\n\n"
                        + msg
                    )
            except Exception:
                pass
            self.error.emit(msg)
        finally:
            if temp_user_data_dir:
                try:
                    shutil.rmtree(temp_user_data_dir, ignore_errors=True)
                except Exception:
                    pass
