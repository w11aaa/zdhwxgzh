from __future__ import annotations

from dataclasses import dataclass


POSITIVE_KEYWORDS: tuple[tuple[str, int], ...] = (
    ("计算机考研", 6),
    ("408", 4),
    ("考研", 3),
    ("上岸", 3),
    ("择校", 3),
    ("王道", 3),
    ("专业课", 2),
    ("复试", 2),
    ("初试", 2),
    ("经验贴", 2),
    ("调剂", 2),
    ("计科", 2),
    ("计算机专业", 2),
    ("软件工程", 2),
    ("数据结构", 2),
    ("操作系统", 2),
    ("组成原理", 2),
    ("计算机网络", 2),
)

NEGATIVE_KEYWORDS: tuple[tuple[str, int], ...] = (
    ("一口价", -6),
    ("新房", -5),
    ("楼盘", -5),
    ("平米", -5),
    ("户型", -5),
    ("装修", -4),
    ("二手房", -5),
    ("房价", -5),
    ("租房", -4),
    ("探店", -3),
    ("穿搭", -3),
    ("美甲", -3),
    ("口红", -3),
    ("发型", -3),
)


@dataclass(frozen=True)
class RelevanceResult:
    is_relevant: int
    relevance_score: int
    relevance_label: str
    relevance_reason: str


def evaluate_relevance(title: str, content: str, source_keyword: str) -> RelevanceResult:
    haystack = " ".join(part for part in (title, content, source_keyword) if part).lower()

    score = 0
    positive_hits: list[str] = []
    negative_hits: list[str] = []

    for keyword, weight in POSITIVE_KEYWORDS:
        if keyword.lower() in haystack:
            score += weight
            positive_hits.append(keyword)

    for keyword, weight in NEGATIVE_KEYWORDS:
        if keyword.lower() in haystack:
            score += weight
            negative_hits.append(keyword)

    if positive_hits and score >= 4:
        label = "relevant"
        is_relevant = 1
    elif positive_hits and score >= 1:
        label = "review"
        is_relevant = 0
    else:
        label = "noise"
        is_relevant = 0

    reason_parts: list[str] = []
    if positive_hits:
        reason_parts.append("positive:" + ",".join(positive_hits[:8]))
    if negative_hits:
        reason_parts.append("negative:" + ",".join(negative_hits[:8]))
    if not reason_parts:
        reason_parts.append("no_topic_signal")

    return RelevanceResult(
        is_relevant=is_relevant,
        relevance_score=score,
        relevance_label=label,
        relevance_reason="; ".join(reason_parts),
    )
