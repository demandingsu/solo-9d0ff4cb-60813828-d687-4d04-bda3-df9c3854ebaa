"""时间工具：时区解析、带时区时间解析、时间范围与时长识别。

采购公告页上的截止时间通常只有“墙上时间”（不带时区），而企业回执按要求带有
时区。为了让二者可以比较，本模块把公告时间解释为 ``deadline_timezone`` 指定
的时区（默认 Asia/Shanghai / UTC+8），回执时间若已经带时区则保留原时区。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo

from app.parser import parse_datetime

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - 取决于运行环境
    ZoneInfo = None  # type: ignore[assignment]


_DEFAULT_OFFSET = timedelta(hours=8)
_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?(?::?(\d{2}))?$", re.I)
_ISO_WITH_OFFSET_RE = re.compile(
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:\d{2}|[+-]\d{4})"
)
_NUMERIC_DT_SCAN_RE = re.compile(
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r"(?:[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}|[+-]\d{4})?)?"
)
_CN_DT_SCAN_RE = re.compile(
    r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"(?:\s*\d{1,2}\s*[:：时]\s*\d{1,2}\s*分?(?:\s*\d{1,2}\s*秒)?)?"
)
_DURATION_RE = re.compile(
    r"(?:持续|中断|用时|历时|影响)(?:约|大约|近)?\s*"
    r"((?:\d+(?:\.\d+)?\s*(?:小时|h|H))?(?:\s*\d+(?:\.\d+)?\s*(?:分钟|分|min))?)",
    re.I,
)
_DURATION_HOURS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:小时|h|H)", re.I)
_DURATION_MINUTES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:分钟|分|min)", re.I)


def resolve_timezone(value: str | None) -> tzinfo:
    """把时区名称/固定偏移解析为 ``tzinfo``；无法识别时回退 UTC+8。"""
    if not value:
        return timezone(_DEFAULT_OFFSET)
    text = value.strip()
    if not text:
        return timezone(_DEFAULT_OFFSET)

    if text.upper() in {"Z", "UTC", "GMT", "ETC/UTC", "UCT"}:
        return timezone.utc

    match = _OFFSET_RE.match(text)
    if match:
        sign = -1 if match.group(1) == "-" else 1
        hours = int(match.group(2))
        minutes = int(match.group(3) or 0)
        seconds = int(match.group(4) or 0)
        return timezone(sign * timedelta(hours=hours, minutes=minutes, seconds=seconds))

    if ZoneInfo is not None:
        try:
            return ZoneInfo(text)
        except Exception:
            pass

    # 中国采购平台常见同义写法；中国不实行夏令时，固定 +8 是安全的回退
    if text.upper() in {"CST", "PRC", "ASIA/SHANGHAI", "ASIA/CHONGQING"}:
        return timezone(_DEFAULT_OFFSET)
    return timezone(_DEFAULT_OFFSET)


def attach_timezone(value: datetime, tz: tzinfo) -> datetime:
    """无时区时间按给定业务时区解释；已带时区的时间原样返回。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value


def parse_aware_datetime(text: str | None, tz: tzinfo) -> datetime | None:
    """解析带/不带时区的日期时间。

    页面显式给出 ``+08:00`` / ``Z`` 等偏移时以页面为准；否则按 ``tz`` 解释。
    """
    if not text:
        return None

    iso_match = _ISO_WITH_OFFSET_RE.search(text)
    if iso_match:
        raw = iso_match.group(0).replace("/", "-").replace(" ", "T")
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        # +0800 → +08:00
        if re.search(r"[+-]\d{4}$", raw):
            raw = raw[:-5] + raw[-5:-2] + ":" + raw[-2:]
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass

    parsed = parse_datetime(text)
    if parsed is None:
        return None
    return attach_timezone(parsed, tz)


def find_datetimes(text: str | None, tz: tzinfo) -> list[datetime]:
    """返回文本中出现的全部日期时间（按出现位置排序）。"""
    if not text:
        return []

    found: list[tuple[int, datetime]] = []
    for pattern in (_NUMERIC_DT_SCAN_RE, _CN_DT_SCAN_RE):
        for match in pattern.finditer(text):
            value = parse_aware_datetime(match.group(0), tz)
            if value is not None:
                found.append((match.start(), value))

    found.sort(key=lambda item: item[0])
    return [value for _, value in found]


def parse_duration(text: str | None) -> timedelta | None:
    """识别“持续约 1 小时 30 分钟”“中断 45 分钟”等时长描述。"""
    if not text:
        return None
    for match in _DURATION_RE.finditer(text):
        fragment = match.group(1)
        if not fragment or not any(ch.isdigit() for ch in fragment):
            continue
        hours_match = _DURATION_HOURS_RE.search(fragment)
        minutes_match = _DURATION_MINUTES_RE.search(fragment)
        hours = float(hours_match.group(1)) if hours_match else 0.0
        minutes = float(minutes_match.group(1)) if minutes_match else 0.0
        if hours == 0.0 and minutes == 0.0:
            continue
        return timedelta(hours=hours, minutes=minutes)
    return None
