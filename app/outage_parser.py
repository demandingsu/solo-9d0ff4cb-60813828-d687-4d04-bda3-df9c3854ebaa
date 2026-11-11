"""故障公告解析：目录发现、详情字段提取与勘误替换。

一条故障公告通常包含：
* 公告编号（供勘误引用）；
* 发布时间；
* 受影响项目（项目编号，或“全平台/全部项目”）；
* 故障时段（开始时间、恢复/结束时间）。

勘误公告通过“引用编号”指向原故障公告，并以其自身的发布时间参与排序；
勘误给出的受影响项目/时段会**替换**被引用公告的对应字段。引用缺失、
重复编号、循环引用或发布时间缺失都会被视为“无法判定”，而不是“没有故障”。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

from bs4 import BeautifulSoup

from app.parser import _clean, _labeled_value, _soup
from app.timeutil import find_datetimes, parse_aware_datetime, parse_duration

class OutageParseError(ValueError):
    """故障详情页缺少标题等必要结构。"""


# --------------------------------------------------------------------------- #
# 字段标记与标签
# --------------------------------------------------------------------------- #
_NOTICE_ID_FIELDS = (
    "announcement-id",
    "notice-id",
    "fault-id",
    "outage-id",
    "notice-no",
    "announcement-no",
    "fault-no",
    "id",
    "故障公告编号",
    "公告编号",
)
_NOTICE_ID_LABELS = ("公告编号", "故障公告编号", "故障编号", "通知编号", "公告ID", "编号")
_REFERENCE_FIELDS = (
    "references",
    "reference",
    "reference-id",
    "ref-id",
    "references-id",
    "引用编号",
    "原公告编号",
)
_REFERENCE_LABELS = ("引用编号", "引用公告编号", "原公告编号", "被引用公告编号", "勘误引用")
_PUBLISH_FIELDS = ("published-at", "publish-time", "publish-at", "发布时间")
_PUBLISH_LABELS = ("发布时间", "发布日期", "公告时间", "公示时间")
_PROJECT_FIELDS = (
    "affected-projects",
    "affected-project-codes",
    "project-codes",
    "projects",
    "project-code",
    "affected-project",
    "影响项目",
)
_PROJECT_LABELS = (
    "影响项目",
    "受影响项目",
    "涉及项目",
    "影响范围",
    "适用项目",
    "涉及项目编号",
    "项目编号",
    "采购编号",
)
_START_FIELDS = (
    "outage-start",
    "fault-start",
    "fault-start-time",
    "start-time",
    "start",
    "故障开始时间",
    "故障发生时间",
)
_START_LABELS = (
    "故障开始时间",
    "故障发生时间",
    "中断开始时间",
    "影响开始时间",
    "开始时间",
)
_END_FIELDS = (
    "outage-end",
    "fault-end",
    "fault-end-time",
    "restored-at",
    "restored-time",
    "recovery-time",
    "end-time",
    "end",
    "恢复时间",
    "故障结束时间",
)
_END_LABELS = (
    "故障结束时间",
    "恢复时间",
    "服务恢复时间",
    "影响结束时间",
    "结束时间",
    "恢复",
)
_PERIOD_FIELDS = (
    "outage-period",
    "fault-period",
    "outage-time",
    "fault-time",
    "downtime",
    "impact-period",
    "affected-period",
    "period",
    "故障时段",
    "影响时段",
)
_PERIOD_LABELS = (
    "影响时段",
    "故障时段",
    "中断时段",
    "影响时间",
    "故障时间",
    "故障起止时间",
    "影响期间",
    "影响区间",
)
_RETRACTED_FIELDS = ("retracted", "withdrawn", "revoked", "撤销", "作废")

_ERRATA_TITLE_KEYWORDS = ("勘误", "更正", "修正", "补充公告", "变更公告", "澄清公告")
_WILDCARD_HINTS = (
    "全部项目",
    "所有项目",
    "全平台",
    "全站",
    "全部供应商",
    "所有供应商",
    "所有投标人",
    "不特定项目",
)
_RETRACT_HINTS = (
    "撤销该故障公告",
    "撤销本公告",
    "原公告作废",
    "公告作废",
    "本次公告作废",
    "误报",
    "不实信息",
    "未发生故障",
    "不存在故障",
    "取消本次故障公告",
)
_RETRACT_PATTERN = re.compile(
    r"(?:撤销|作废|撤回|取消)[^。；;\n]{0,20}(?:故障公告|本公告|该公告|本次公告)"
)
_CODE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9._/-]*[A-Za-z0-9]|[0-9][A-Za-z0-9._/-]*[A-Za-z0-9]")
# 时段文本中省略日期的第二个时刻，如“2026-11-12 09:30 至 12:00”
_TIME_ONLY_AFTER_SEPARATOR_RE = re.compile(
    r"(?:至|到|~|～|—|－|-)\s*(\d{1,2})\s*[:：时]\s*(\d{1,2})(?:\s*[:：分]\s*(\d{1,2}))?"
)


# --------------------------------------------------------------------------- #
# 通用取值
# --------------------------------------------------------------------------- #
def _data_value(soup: BeautifulSoup, names: tuple[str, ...]) -> str | None:
    for name in names:
        node = soup.find(attrs={"data-field": name})
        if node is None:
            continue
        value = node.get("data-value")
        if value is not None:
            return str(value).strip()
        return _clean(node.get_text())
    return None


def _article_attr(soup: BeautifulSoup, names: tuple[str, ...]) -> str | None:
    """兼容把字段写在 ``<article data-...>`` 上的页面。"""
    for name in names:
        for tag in soup.find_all(["article", "main", "section"]):
            value = tag.get(name)
            if value:
                return str(value).strip()
    return None


def _value(soup: BeautifulSoup, data_names: tuple[str, ...], labels: tuple[str, ...]) -> str | None:
    raw = _data_value(soup, data_names) or _article_attr(soup, data_names) or _labeled_value(soup, labels)
    return _clean(raw) if raw else None


def _split_tokens(raw: str | None, *, keep_prose: bool = False) -> list[str]:
    """从字段值中提取编号 token；保留出现顺序并去重。"""
    if not raw:
        return []
    text = _clean(raw)
    tokens: list[str] = [m.group(0) for m in _CODE_TOKEN_RE.finditer(text)]
    if not tokens and keep_prose:
        for part in re.split(r"[，,、;；\s]+", text):
            part = part.strip("（）()【】[]。.:：")
            if part and part not in {"详见", "见", "参考"}:
                tokens.append(part)
    result: list[str] = []
    for token in tokens:
        token = token.strip("（）()【】[]。.:：")
        if token and token not in result:
            result.append(token)
    return result


_REFERENCE_IN_TEXT_PATTERNS = (
    re.compile(
        r"(?:关于|针对|对)\s*([A-Za-z0-9][A-Za-z0-9._/-]*[A-Za-z0-9])\s*(?:公告|的|进行|作出)"
    ),
    re.compile(
        r"(?:引用编号|引用公告编号|原公告编号|被引用公告编号)\s*[:：]?\s*"
        r"([A-Za-z0-9][A-Za-z0-9._/-]*[A-Za-z0-9])"
    ),
)


def _extract_references(
    soup: BeautifulSoup,
    title: str,
    body_text: str,
    own_id: str | None,
    *,
    allow_text_fallback: bool,
) -> list[str]:
    raw = _value(soup, _REFERENCE_FIELDS, _REFERENCE_LABELS)
    tokens = _split_tokens(raw, keep_prose=True)
    if tokens:
        return tokens
    if not allow_text_fallback:
        return []

    # 结构化字段/标签都没有时，从“关于 XXX 公告”“对 XXX 进行勘误”等正文措辞补足
    fallback: list[str] = []
    for pattern in _REFERENCE_IN_TEXT_PATTERNS:
        for match in pattern.finditer(f"{title} {body_text}"):
            token = match.group(1)
            if token and token not in fallback:
                fallback.append(token)
    if own_id:
        fallback = [token for token in fallback if token != own_id]
    return fallback


def _extract_projects(soup: BeautifulSoup, body_text: str) -> tuple[list[str] | None, bool]:
    """返回 ``(项目编号列表, 是否全平台)``；无法确定时列表为 None。"""
    raw = _value(soup, _PROJECT_FIELDS, _PROJECT_LABELS)
    if raw:
        lowered = raw.lower()
        if any(hint.lower() in lowered for hint in _WILDCARD_HINTS) or "*" in raw:
            return [], True
        tokens = _split_tokens(raw, keep_prose=False)
        if tokens:
            return tokens, False
        return None, False

    # 没有受影响项目字段时，正文中明确的“全平台/所有项目”仍可识别为全平台
    if any(hint in body_text for hint in _WILDCARD_HINTS):
        return [], True
    return None, False


def _extract_published_at(soup: BeautifulSoup, tz: tzinfo) -> datetime | None:
    raw = _value(soup, _PUBLISH_FIELDS, _PUBLISH_LABELS)
    return parse_aware_datetime(raw, tz)


def _infer_end_time(period_text: str, start: datetime | None) -> datetime | None:
    """从“至 12:00/12时00分”这类省略日期的写法推断结束时刻。"""
    if start is None:
        return None
    for match in _TIME_ONLY_AFTER_SEPARATOR_RE.finditer(period_text):
        hour = int(match.group(1))
        minute = int(match.group(2))
        second = int(match.group(3) or 0)
        if hour > 23 or minute > 59 or second > 59:
            continue
        candidate = start.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if candidate < start:
            candidate += timedelta(days=1)  # 跨零点
        return candidate
    return None


def _extract_window(
    soup: BeautifulSoup, body_text: str, tz: tzinfo
) -> tuple[datetime | None, datetime | None, bool, bool]:
    """返回 ``(开始, 结束, 是否有时间字段文本, 是否字段格式不完整)``。

    只有一端时间而另一端未给出时，不算“格式不完整”：勘误可以只修正恢复时间，
    缺失的一端由被引用公告继承；最终是否成对由合并阶段检查。
    """
    start_raw = _value(soup, _START_FIELDS, _START_LABELS)
    end_raw = _value(soup, _END_FIELDS, _END_LABELS)
    period_raw = _value(soup, _PERIOD_FIELDS, _PERIOD_LABELS)

    start = parse_aware_datetime(start_raw, tz)
    end = parse_aware_datetime(end_raw, tz)

    incomplete = False
    if start_raw and start is None:
        incomplete = True
    if end_raw and end is None:
        incomplete = True

    if period_raw:
        points = find_datetimes(period_raw, tz)
        if len(points) >= 2:
            start, end = points[0], points[1]
        elif len(points) == 1:
            if start is None and end is None:
                start = points[0]
            elif start is None:
                start = points[0]
            elif end is None:
                end = points[0]
        else:
            incomplete = True

        if end is None:
            end = _infer_end_time(period_raw, start)

    has_window_text = bool(start_raw or end_raw or period_raw)
    duration = parse_duration(body_text)

    if start is not None and end is None and duration is not None:
        end = start + duration
    elif end is not None and start is None and duration is not None:
        start = end - duration

    if start is not None and end is not None and start > end:
        incomplete = True

    return start, end, has_window_text, incomplete


def _extract_retracted(soup: BeautifulSoup, title: str, body_text: str) -> bool:
    raw = _value(soup, _RETRACTED_FIELDS, ("撤销", "作废"))
    if raw is not None:
        flag = raw.strip().lower()
        if flag in {"true", "1", "yes", "是", "已撤销", "作废"}:
            return True
    haystack = f"{title} {body_text}"
    return any(hint in haystack for hint in _RETRACT_HINTS) or bool(_RETRACT_PATTERN.search(haystack))


# --------------------------------------------------------------------------- #
# 详情页
# --------------------------------------------------------------------------- #
@dataclass
class OutageNotice:
    url: str
    title: str
    notice_id: str | None
    published_at: datetime | None
    is_errata: bool
    references: list[str]
    projects: list[str] | None
    all_projects: bool
    start: datetime | None
    end: datetime | None
    has_window_text: bool
    window_incomplete: bool
    retracted: bool
    warnings: list[str] = field(default_factory=list)


def parse_outage_detail(html: str, url: str, tz: tzinfo) -> OutageNotice:
    """解析单条故障公告/勘误公告详情页。"""
    soup = _soup(html)
    title_node = soup.find("title")
    h1 = soup.find("h1")
    title = _clean(h1.get_text() if h1 else (title_node.get_text() if title_node else ""))
    if not title:
        raise OutageParseError(f"故障公告缺少标题：{url}")

    body_text = _clean(soup.get_text(" "))
    notice_id = _value(soup, _NOTICE_ID_FIELDS, _NOTICE_ID_LABELS)
    errata_title = any(keyword in title for keyword in _ERRATA_TITLE_KEYWORDS)
    references = _extract_references(
        soup, title, body_text, notice_id, allow_text_fallback=errata_title
    )
    published_at = _extract_published_at(soup, tz)
    projects, all_projects = _extract_projects(soup, body_text)
    start, end, has_window_text, window_incomplete = _extract_window(soup, body_text, tz)
    retracted = _extract_retracted(soup, title, body_text)

    is_errata = bool(references) or errata_title

    warnings: list[str] = []
    if is_errata and not references:
        warnings.append("勘误公告未给出引用编号")
    if is_errata and published_at is None:
        warnings.append("勘误公告缺少发布时间，无法确定替换顺序")
    if has_window_text and window_incomplete:
        warnings.append("故障时段字段存在但无法完整解析")

    return OutageNotice(
        url=url,
        title=title,
        notice_id=notice_id,
        published_at=published_at,
        is_errata=is_errata,
        references=references,
        projects=projects,
        all_projects=all_projects,
        start=start,
        end=end,
        has_window_text=has_window_text,
        window_incomplete=window_incomplete,
        retracted=retracted,
        warnings=warnings,
    )


# --------------------------------------------------------------------------- #
# 勘误替换
# --------------------------------------------------------------------------- #
@dataclass
class ScopeState:
    projects: list[str] | None = None
    all_projects: bool = False
    start: datetime | None = None
    end: datetime | None = None
    retracted: bool = False
    incomplete: bool = False
    applied_errata: list[OutageNotice] = field(default_factory=list)


@dataclass
class EffectiveOutage:
    base: OutageNotice
    projects: list[str] | None
    all_projects: bool
    start: datetime | None
    end: datetime | None
    retracted: bool
    incomplete: bool
    applied_errata: list[OutageNotice]

    @property
    def source_urls(self) -> list[str]:
        urls = [self.base.url]
        for errata in self.applied_errata:
            if errata.url not in urls:
                urls.append(errata.url)
        return urls

    @property
    def errata_ids(self) -> list[str]:
        return [e.notice_id for e in self.applied_errata if e.notice_id]


def _initial_state(notice: OutageNotice) -> ScopeState:
    return ScopeState(
        projects=list(notice.projects) if notice.projects is not None else None,
        all_projects=notice.all_projects,
        start=notice.start,
        end=notice.end,
        retracted=notice.retracted,
        incomplete=notice.window_incomplete,
    )


def _apply_errata_state(target: ScopeState, source: ScopeState, errata: OutageNotice) -> None:
    """把勘误解析出的有效字段替换到目标状态；未给出的字段沿用目标值。"""
    if source.retracted:
        target.retracted = True

    if source.all_projects:
        target.projects = []
        target.all_projects = True
    elif source.projects is not None:
        target.projects = list(source.projects)
        target.all_projects = False

    window_changed = False
    if source.start is not None:
        target.start = source.start
        window_changed = True
    if source.end is not None:
        target.end = source.end
        window_changed = True

    if window_changed:
        if target.start is not None and target.end is not None and target.start <= target.end:
            # 勘误明确给出了可用时段；若其自身其他时间字段仍无法解析，则保持待核实
            target.incomplete = source.incomplete
        else:
            target.incomplete = True
    elif source.incomplete:
        target.incomplete = True

    if errata not in target.applied_errata:
        target.applied_errata.append(errata)
    for downstream in source.applied_errata:
        if downstream not in target.applied_errata:
            target.applied_errata.append(downstream)


def build_effective_outages(
    notices: list[OutageNotice],
) -> tuple[list[EffectiveOutage], list[str]]:
    """应用勘误，返回每条原故障公告的有效受影响范围与结构性问题。

    结构性问题（引用不存在、勘误缺引用/发布时间、编号重复、循环引用）会原样
    返回，调用方必须按“无法判定”处理，不能当作没有故障。
    """
    errors: list[str] = []

    if notices and not any(not notice.is_errata for notice in notices):
        errors.append("故障公告目录仅有勘误公告，缺少被引用的原故障公告")

    by_id: dict[str, OutageNotice] = {}
    for notice in notices:
        if not notice.notice_id:
            continue
        if notice.notice_id in by_id:
            errors.append(f"故障公告编号重复：{notice.notice_id}（{notice.url}）")
            continue
        by_id[notice.notice_id] = notice

    errata_by_ref: dict[str, list[OutageNotice]] = {}
    for notice in notices:
        if not notice.is_errata:
            continue
        if not notice.references:
            errors.append(f"勘误公告缺少引用编号：{notice.url}")
            continue
        if notice.published_at is None:
            errors.append(f"勘误公告缺少发布时间：{notice.url}")
        for reference in notice.references:
            if reference not in by_id:
                errors.append(f"勘误引用编号不存在：{reference}（{notice.url}）")
                continue
            errata_by_ref.setdefault(reference, []).append(notice)

    cache: dict[str, ScopeState] = {}

    def resolve(notice: OutageNotice, stack: tuple[str, ...]) -> ScopeState:
        if notice.notice_id and notice.notice_id in cache:
            return cache[notice.notice_id]
        if notice.notice_id and notice.notice_id in stack:
            raise ValueError(f"勘误引用形成循环：{notice.notice_id}")

        state = _initial_state(notice)
        if notice.notice_id:
            related = sorted(
                errata_by_ref.get(notice.notice_id, []),
                key=lambda item: item.published_at or datetime.min,
            )
            for errata in related:
                try:
                    errata_state = resolve(errata, stack + (notice.notice_id,))
                except ValueError as exc:
                    errors.append(str(exc))
                    state.incomplete = True
                    continue
                _apply_errata_state(state, errata_state, errata)

        if notice.notice_id:
            cache[notice.notice_id] = state
        return state

    effective: list[EffectiveOutage] = []
    for notice in notices:
        if notice.is_errata:
            continue
        try:
            state = resolve(notice, ())
        except ValueError as exc:
            errors.append(str(exc))
            state = _initial_state(notice)
            state.incomplete = True

        effective.append(
            EffectiveOutage(
                base=notice,
                projects=state.projects,
                all_projects=state.all_projects,
                start=state.start,
                end=state.end,
                retracted=state.retracted,
                incomplete=state.incomplete,
                applied_errata=state.applied_errata,
            )
        )

    # 去重后返回，避免同一勘误错误重复出现
    unique_errors = list(dict.fromkeys(errors))
    return effective, unique_errors
