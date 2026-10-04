"""UTC 时间的解析与规范化。

数据库内统一存储为固定 27 字符的 UTC 字符串：``YYYY-MM-DDTHH:MM:SS.ffffffZ``。
该格式按字典序比较与按时间先后比较结果一致，因此区间过滤可直接用字符串完成。
"""
from __future__ import annotations

from datetime import datetime, timezone

CANONICAL_LEN = 27


class TimeParseError(ValueError):
    """时间字符串无法解析为带时区的 ISO-8601 时刻。"""


def now_canonical() -> str:
    """服务端当前时刻（UTC，微秒精度）。更正/发布时间只允许来自这里。"""
    return to_canonical(datetime.now(timezone.utc))


def to_canonical(dt: datetime) -> str:
    """把 timezone-aware datetime 转成 UTC 规范字符串。"""
    if dt.tzinfo is None:
        raise TimeParseError("时间必须带时区信息（例如末尾加 Z 或 +08:00）")
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def parse_canonical(value: str, field: str = "time") -> str:
    """解析外部传入的 ISO-8601 时间并规范化为 UTC 字符串。

    接受 ``Z`` 后缀和任意 UTC 偏移；拒绝无时区、不可解析、非有限时间。
    """
    if not isinstance(value, str) or not value.strip():
        raise TimeParseError(f"{field} 不能为空，需为 ISO-8601 带时区时间")
    text = value.strip()
    try:
        text = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise TimeParseError(f"{field} 不是合法的 ISO-8601 时间: {value!r}")
    if dt.tzinfo is None:
        raise TimeParseError(f"{field} 必须带时区信息（例如末尾加 Z 或 +08:00）")
    return to_canonical(dt)
