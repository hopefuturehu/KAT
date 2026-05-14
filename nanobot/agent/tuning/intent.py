"""Shared intent and target-system detection for tuning flows."""

from __future__ import annotations

import re

TARGET_SYSTEM_PATTERNS = {
    "redis": re.compile(r"\bredis\b", re.IGNORECASE | re.ASCII),
    "mysql": re.compile(r"\bmysql\b", re.IGNORECASE | re.ASCII),
}
TUNING_KEYWORDS = (
    "tune", "tuning", "optimize", "optimization",
    "throughput", "latency", "qps", "rps", "performance",
    "调优", "调参", "优化", "性能", "吞吐", "延迟",
)
RETRY_KEYWORDS = (
    "continue", "retry", "rerun", "run again", "try again",
    "继续", "重试", "再试", "重新跑", "重新执行",
)
ESCAPE_KEYWORDS = (
    "cancel tuning", "stop tuning", "abort tuning",
    "取消调优", "停止调优", "退出调优",
    "not tuning", "no tuning",
)
PROFILE_SKIP_KEYWORDS = {
    "none", "skip", "manual", "new", "no", "不用", "不使用", "跳过", "重新配置", "手动填写",
}


def detect_target_system(message: str) -> str | None:
    normalized = message.strip().lower()
    if not normalized:
        return None
    for system, pattern in TARGET_SYSTEM_PATTERNS.items():
        if pattern.search(normalized):
            return system
    return None


def looks_like_tuning_request(message: str) -> bool:
    normalized = message.strip().lower()
    if not normalized:
        return False
    if detect_target_system(normalized) is None:
        return False
    return any(keyword in normalized for keyword in TUNING_KEYWORDS)


def looks_like_retry_request(message: str) -> bool:
    normalized = message.strip().lower()
    return any(keyword in normalized for keyword in RETRY_KEYWORDS)


def looks_like_escape_request(message: str) -> bool:
    normalized = message.strip().lower()
    return any(keyword in normalized for keyword in ESCAPE_KEYWORDS)
