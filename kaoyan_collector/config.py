from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CollectorConfig:
    workspace_root: Path = Path(__file__).resolve().parent.parent
    media_crawler_root: Path = workspace_root / "MediaCrawler"
    project_root: Path = workspace_root / "kaoyan_collector"
    raw_data_root: Path = project_root / "raw_data"
    database_path: Path = project_root / "data" / "kaoyan_content.db"
    default_platforms: tuple[str, ...] = ("xhs", "dy", "bili", "wb", "zhihu", "tieba", "ks")
    default_keywords: tuple[str, ...] = (
        "计算机考研",
        "408",
        "王道考研",
        "计算机考研择校",
        "计算机考研经验",
    )
    supported_platforms: tuple[str, ...] = field(
        default=("xhs", "dy", "bili", "wb", "zhihu", "tieba", "ks")
    )


CONFIG = CollectorConfig()
