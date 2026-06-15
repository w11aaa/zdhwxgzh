from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class ChromeProfile:
    directory: str  # e.g. "Default", "Profile 1"
    name: str  # display name in Chrome UI


@dataclass
class ChromeProfilesDetection:
    user_data_dir: str
    profiles: List[ChromeProfile]
    default_profile_directory: str = "Default"


def _candidate_user_data_dirs() -> List[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        return [
            home / "Library" / "Application Support" / "Google" / "Chrome",
            home / "Library" / "Application Support" / "Chromium",
        ]
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or ""
        if local:
            return [
                Path(local) / "Google" / "Chrome" / "User Data",
                Path(local) / "Chromium" / "User Data",
            ]
        return []
    # linux/others
    return [
        home / ".config" / "google-chrome",
        home / ".config" / "chromium",
    ]


def _load_local_state(user_data_dir: Path) -> Dict[str, object]:
    p = user_data_dir / "Local State"
    if not p.exists() or not p.is_file():
        return {}
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def detect_chrome_profiles(user_data_dir: str = "") -> Optional[ChromeProfilesDetection]:
    """Detect Chrome user profiles under a given user data directory.

    Returns None if no Chrome user data dir is found.
    """
    target_dir: Optional[Path] = None
    if str(user_data_dir or "").strip():
        p = Path(os.path.expanduser(str(user_data_dir).strip()))
        if p.exists() and p.is_dir():
            target_dir = p
    else:
        for cand in _candidate_user_data_dirs():
            if cand.exists() and cand.is_dir():
                target_dir = cand
                break

    if not target_dir:
        return None

    local_state = _load_local_state(target_dir)
    profile_obj = local_state.get("profile") if isinstance(local_state, dict) else {}
    if not isinstance(profile_obj, dict):
        profile_obj = {}

    info_cache = profile_obj.get("info_cache") if isinstance(profile_obj.get("info_cache"), dict) else {}
    if not isinstance(info_cache, dict):
        info_cache = {}

    # Determine default profile directory from chrome's "last active"/order lists.
    default_dir = ""
    try:
        last_active = profile_obj.get("last_active_profiles")
        if isinstance(last_active, list) and last_active:
            default_dir = str(last_active[0] or "").strip()
    except Exception:
        default_dir = ""
    if not default_dir:
        try:
            order = profile_obj.get("profiles_order")
            if isinstance(order, list) and order:
                default_dir = str(order[0] or "").strip()
        except Exception:
            default_dir = ""

    # Collect profile directories from local state cache and disk.
    dirs = set()
    for k in list(info_cache.keys()):
        if str(k).strip():
            dirs.add(str(k).strip())

    try:
        for child in target_dir.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            if name == "Default" or re.fullmatch(r"Profile\\s+\\d+", name):
                dirs.add(name)
    except Exception:
        pass

    profiles: List[ChromeProfile] = []
    for d in sorted(dirs):
        dir_path = target_dir / d
        if not dir_path.exists() or not dir_path.is_dir():
            continue
        display = d
        try:
            cached = info_cache.get(d)
            if isinstance(cached, dict):
                display = str(cached.get("name") or display).strip() or display
        except Exception:
            display = d
        profiles.append(ChromeProfile(directory=d, name=display))

    if not profiles:
        return None

    if not default_dir:
        default_dir = "Default" if any(p.directory == "Default" for p in profiles) else profiles[0].directory

    return ChromeProfilesDetection(
        user_data_dir=str(target_dir),
        profiles=profiles,
        default_profile_directory=default_dir,
    )

