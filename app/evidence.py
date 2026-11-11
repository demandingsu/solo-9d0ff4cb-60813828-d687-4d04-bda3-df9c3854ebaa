"""故障佐证编排：沿用有效截止时间 → 抓取故障公告/勘误 → 比对回执。

判定顺序：
1. 先用现有采集逻辑取得有效投标截止时间；采购状态待核实、已取消、未找到或
   截止时间缺失时，直接“无法判定”，绝不猜测截标时间。
2. 截标前（含截止时点）已有成功受理回执 → “已受理”。
3. 否则读取故障公告目录并解析全部详情及勘误。目录/详情抓取失败、勘误引用缺失、
   时段字段不完整等 → “无法判定”，不能当作“没有故障”。
4. 信息完整时，若截标前的失败尝试落在目标项目（勘误替换后）的故障时段内，
   且截标前没有成功受理回执 → “可佐证”；其余情况 → “无申诉依据”。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from app.collector import collect
from app.models import (
    EvidenceVerdict,
    FaultEvidenceRequest,
    FaultEvidenceResponse,
    FetchRequest,
    OutageNoticeSummary,
    OutageWindow,
    ProjectStatus,
    ReceiptOutcome,
    ReceiptRecord,
    SupportedAttempt,
    SourceLink,
    VERDICT_LABELS,
)
from app.outage_parser import (
    EffectiveOutage,
    OutageNotice,
    OutageParseError,
    build_effective_outages,
    parse_outage_detail,
)
from app.parser import extract_detail_links
from app.timeutil import attach_timezone, resolve_timezone

_MAX_CONCURRENCY = 5
_USER_AGENT = "TenderCollector/1.0 (+local procurement announcement API)"
_NAV_TEXT_RE = re.compile(r"^\s*(首页|主页|登录|注册|返回|返回列表|上一页|下一页|更多|联系我们)\s*$")
_CODE_NORM = str.maketrans({" ": "", "　": "", "_": "", "/": "", "\\": "", "-": ""})


def _normalize_code(code: str) -> str:
    return code.strip().lower().translate(_CODE_NORM)


def _add_source(
    sources: list[SourceLink], role: str, url: str, title: str | None = None
) -> None:
    if any(str(source.url) == str(url) for source in sources):
        return
    sources.append(SourceLink(role=role, url=url, title=title))


def _project_match(effective: EffectiveOutage, target_norm: str) -> bool | None:
    """True=命中，False=不相关，None=公告未给出可识别的受影响项目（信息不完整）。"""
    if effective.all_projects:
        return True
    if effective.projects is None or not effective.projects:
        return None
    for project in effective.projects:
        normalized = _normalize_code(project)
        if target_norm == normalized or target_norm in normalized:
            return True
    return False


def _find_window(occurred_at: datetime, windows: list[EffectiveOutage]) -> EffectiveOutage | None:
    complete = [
        window
        for window in windows
        if window.start is not None and window.end is not None
    ]
    for window in sorted(complete, key=lambda item: (item.start, item.end)):
        if window.start <= occurred_at <= window.end:
            return window
    return None


def _window_model(project_code: str, window: EffectiveOutage) -> OutageWindow:
    return OutageWindow(
        project_code=project_code,
        all_projects=window.all_projects,
        start=window.start,
        end=window.end,
        announcement_id=window.base.notice_id,
        source_url=window.base.url,
        source_urls=window.source_urls,
        errata_ids=window.errata_ids,
    )


@dataclass
class _OutageLoad:
    notices: list[OutageNotice] = field(default_factory=list)
    effective: list[EffectiveOutage] = field(default_factory=list)
    summaries: list[OutageNoticeSummary] = field(default_factory=list)
    sources: list[SourceLink] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    failed: bool = False


def _outage_reason_codes(errors: list[str]) -> list[str]:
    codes: list[str] = []
    for error in errors:
        if "引用编号不存在" in error or "缺少引用编号" in error:
            codes.append("errata_reference_missing")
        elif "发布时间" in error:
            codes.append("errata_publication_time_missing")
        elif "重复" in error:
            codes.append("duplicate_outage_id")
        elif "循环" in error:
            codes.append("errata_reference_cycle")
        elif "目录" in error:
            codes.append("outage_list_fetch_failed")
        elif "解析失败" in error:
            codes.append("outage_detail_parse_failed")
        elif "抓取失败" in error or "详情" in error:
            codes.append("outage_detail_fetch_failed")
        else:
            codes.append("outage_parse_error")
    return list(dict.fromkeys(codes))


async def _load_outages(dir_url: str, timeout: float, tz) -> _OutageLoad:
    result = _OutageLoad()
    _add_source(result.sources, "outage-list", dir_url, "平台故障公告目录")
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout), headers={"User-Agent": _USER_AGENT}, follow_redirects=True
    ) as client:
        try:
            directory_html = await _fetch(client, dir_url)
        except httpx.HTTPError as exc:
            result.failed = True
            result.reason_codes.append("outage_list_fetch_failed")
            result.warnings.append(f"故障公告目录抓取失败：{dir_url}（{exc.__class__.__name__}）")
            return result

        candidates = extract_detail_links(directory_html, dir_url)
        # 目录页常见的首页/返回等导航链接不是故障详情，避免被当成“信息不完整”
        candidates = [
            (url, text, context)
            for url, text, context in candidates
            if urlparse(url).path.rstrip("/") and not _NAV_TEXT_RE.match(text)
        ]
        if not candidates:
            result.warnings.append("故障公告目录未发现任何详情链接（按无故障公告处理）")
            return result

        semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

        async def fetch_one(url: str, link_text: str, context: str) -> tuple[str, str, str, str]:
            async with semaphore:
                try:
                    html = await _fetch(client, url)
                    return url, html, link_text, context
                except httpx.HTTPError:
                    return url, "", link_text, context

        fetched = await asyncio.gather(*(fetch_one(u, t, c) for u, t, c in candidates))

    errors: list[str] = []
    for url, html, _link_text, _context in fetched:
        if not html:
            errors.append(f"故障公告详情抓取失败：{url}")
            _add_source(result.sources, "outage-failed", url, "抓取失败-待核实")
            continue
        try:
            notice = parse_outage_detail(html, url, tz)
        except OutageParseError as exc:
            errors.append(f"故障公告详情解析失败：{url}（{exc}）")
            _add_source(result.sources, "outage-failed", url, "解析失败-待核实")
            continue

        result.notices.append(notice)
        result.summaries.append(
            OutageNoticeSummary(
                url=notice.url,
                title=notice.title,
                announcement_id=notice.notice_id,
                published_at=notice.published_at,
                is_errata=notice.is_errata,
                references=notice.references,
                affected_projects=notice.projects or [],
                all_projects=notice.all_projects,
                outage_start=notice.start,
                outage_end=notice.end,
                retracted=notice.retracted,
            )
        )
        _add_source(
            result.sources,
            "outage-errata" if notice.is_errata else "outage",
            notice.url,
            notice.title,
        )
        result.warnings.extend(notice.warnings)

    effective, structural_errors = build_effective_outages(result.notices)
    result.effective = effective
    errors.extend(structural_errors)
    if errors:
        result.failed = True
        result.warnings.extend(errors)
        result.reason_codes.extend(_outage_reason_codes(errors))
        result.reason_codes = list(dict.fromkeys(result.reason_codes))
    return result


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url)
    response.raise_for_status()
    return response.text


def _receipt_records(
    req: FaultEvidenceRequest, tz, warnings: list[str]
) -> list[ReceiptRecord]:
    records: list[ReceiptRecord] = []
    for receipt in req.receipts:
        aware = attach_timezone(receipt.occurred_at, tz)
        if receipt.occurred_at.tzinfo is None:
            warnings.append(
                f"回执 {receipt.receipt_no} 的时间未带时区，按 {req.deadline_timezone} 解释"
            )
        records.append(
            ReceiptRecord(
                receipt_no=receipt.receipt_no,
                outcome=receipt.outcome,
                occurred_at=aware,
            )
        )
    return sorted(records, key=lambda item: item.occurred_at)


def _base_response(
    req: FaultEvidenceRequest,
    *,
    verdict: EvidenceVerdict,
    sources: list[SourceLink],
    warnings: list[str],
    reason_codes: list[str],
    deadline: datetime | None,
    deadline_aware: datetime | None,
    receipts: list[ReceiptRecord],
    accepted_receipt: ReceiptRecord | None = None,
    supported_attempts: list[SupportedAttempt] | None = None,
    windows: list[OutageWindow] | None = None,
    outage_notices: list[OutageNoticeSummary] | None = None,
) -> FaultEvidenceResponse:
    return FaultEvidenceResponse(
        project_code=req.project_code,
        list_url=req.list_url,
        outage_dir_url=req.outage_dir_url,
        verdict=verdict,
        status=verdict,
        verdict_label=VERDICT_LABELS[verdict],
        appeal_basis=verdict == EvidenceVerdict.SUPPORTED,
        can_appeal=verdict == EvidenceVerdict.SUPPORTED,
        already_accepted=verdict == EvidenceVerdict.ACCEPTED,
        effective_deadline=deadline,
        effective_deadline_aware=deadline_aware,
        deadline_timezone=req.deadline_timezone,
        receipts=receipts,
        accepted_receipt=accepted_receipt,
        supported_attempts=supported_attempts or [],
        outage_windows=windows or [],
        outage_notices=outage_notices or [],
        sources=sources,
        warnings=list(dict.fromkeys(warnings)),
        reason_codes=list(dict.fromkeys(reason_codes)),
        fetched_at=datetime.now(timezone.utc),
    )


def _unable(
    req: FaultEvidenceRequest,
    *,
    sources: list[SourceLink],
    warnings: list[str],
    reason_codes: list[str],
    deadline: datetime | None = None,
    deadline_aware: datetime | None = None,
    receipts: list[ReceiptRecord] | None = None,
    outage_notices: list[OutageNoticeSummary] | None = None,
) -> FaultEvidenceResponse:
    warnings = [*warnings, "信息不完整或存在抓取失败，结论为无法判定；不能据此认定没有故障"]
    return _base_response(
        req,
        verdict=EvidenceVerdict.UNABLE_TO_DETERMINE,
        sources=sources,
        warnings=warnings,
        reason_codes=reason_codes,
        deadline=deadline,
        deadline_aware=deadline_aware,
        receipts=receipts or [],
        outage_notices=outage_notices,
    )


async def gather_fault_evidence(req: FaultEvidenceRequest) -> FaultEvidenceResponse:
    """执行完整故障佐证流程并返回结构化结论。"""
    tz = resolve_timezone(req.deadline_timezone)
    warnings: list[str] = []
    receipts = _receipt_records(req, tz, warnings)

    sources: list[SourceLink] = []
    _add_source(sources, "list", str(req.list_url), "采购公告列表页")

    # 1) 沿用现有采集结果中的有效截止时间
    try:
        collected = await collect(
            FetchRequest(
                list_url=req.list_url,
                project_code=req.project_code,
                timeout=req.timeout,
            )
        )
    except Exception as exc:  # HTTPException 或网络异常统一沉淀为“无法判定”
        status_code = getattr(exc, "status_code", None)
        reason = "procurement_list_fetch_failed" if status_code == 502 else "procurement_collect_failed"
        warnings.append(f"采购公告采集失败，无法取得有效截止时间：{getattr(exc, 'detail', exc)}")
        return _unable(
            req,
            sources=sources,
            warnings=warnings,
            reason_codes=[reason],
            receipts=receipts,
        )

    for source in collected.sources:
        _add_source(sources, source.role, str(source.url), source.title)

    deadline = collected.effective_deadline
    deadline_aware = attach_timezone(deadline, tz) if deadline is not None else None

    if collected.status != ProjectStatus.OPEN or deadline_aware is None:
        if collected.status == ProjectStatus.PENDING:
            reason = "deadline_pending"
            warnings.append("采购公告中的有效投标截止时间待核实，无法判定故障是否影响该项目")
        elif collected.status == ProjectStatus.NOT_FOUND:
            reason = "project_not_found"
            warnings.append("采购公告列表未发现该项目，无法取得有效投标截止时间")
        elif collected.status == ProjectStatus.CLOSED:
            reason = "deadline_unavailable"
            warnings.append("采购已取消，不存在可用于比对的有效投标截止时间")
        else:
            reason = "deadline_unavailable"
            warnings.append("未能取得有效投标截止时间，无法判定")
        warnings.extend(collected.warnings)
        return _unable(
            req,
            sources=sources,
            warnings=warnings,
            reason_codes=[reason],
            deadline=deadline,
            deadline_aware=deadline_aware,
            receipts=receipts,
        )

    # 2) 截标前（含截止时点）已有成功受理回执 → 已受理
    accepted = [
        receipt
        for receipt in receipts
        if receipt.outcome == ReceiptOutcome.ACCEPTED and receipt.occurred_at <= deadline_aware
    ]
    if accepted:
        earliest = min(accepted, key=lambda item: item.occurred_at)
        warnings.append(
            f"截标前已有成功受理回执（{earliest.receipt_no}），判定为已受理，无需故障佐证"
        )
        return _base_response(
            req,
            verdict=EvidenceVerdict.ACCEPTED,
            sources=sources,
            warnings=warnings,
            reason_codes=["accepted_receipt_before_deadline"],
            deadline=deadline,
            deadline_aware=deadline_aware,
            receipts=receipts,
            accepted_receipt=earliest,
        )

    # 3) 读取并解析故障公告及勘误
    outage = await _load_outages(str(req.outage_dir_url), req.timeout, tz)
    warnings.extend(outage.warnings)
    for source in outage.sources:
        _add_source(sources, source.role, str(source.url), source.title)

    if outage.failed:
        return _unable(
            req,
            sources=sources,
            warnings=warnings,
            reason_codes=outage.reason_codes or ["outage_data_incomplete"],
            deadline=deadline,
            deadline_aware=deadline_aware,
            receipts=receipts,
            outage_notices=outage.summaries,
        )

    # 4) 按勘误替换后的有效故障时段匹配目标项目
    target_norm = _normalize_code(req.project_code)
    windows: list[EffectiveOutage] = []
    ambiguous: list[str] = []
    incomplete: list[str] = []

    for effective in outage.effective:
        if effective.retracted:
            continue
        matched = _project_match(effective, target_norm)
        label = effective.base.notice_id or effective.base.url
        if matched is None:
            ambiguous.append(f"故障公告 {label} 未给出可识别的受影响项目，无法确认是否影响本项目")
            continue
        if not matched:
            continue
        if effective.incomplete or effective.start is None or effective.end is None:
            incomplete.append(f"故障公告 {label} 的故障时段无法完整解析，无法判定是否覆盖失败尝试")
            continue
        windows.append(effective)

    if ambiguous or incomplete:
        return _unable(
            req,
            sources=sources,
            warnings=[*warnings, *ambiguous, *incomplete],
            reason_codes=[
                *(["outage_scope_ambiguous"] if ambiguous else []),
                *(["outage_window_incomplete"] if incomplete else []),
            ],
            deadline=deadline,
            deadline_aware=deadline_aware,
            receipts=receipts,
            outage_notices=outage.summaries,
        )

    window_models = [_window_model(req.project_code, window) for window in windows]

    # 5) 只考虑截标前的失败尝试（含截止时点）
    failed_before = [
        receipt
        for receipt in receipts
        if receipt.outcome == ReceiptOutcome.FAILED and receipt.occurred_at <= deadline_aware
    ]

    supported: list[SupportedAttempt] = []
    for receipt in failed_before:
        matched_window = _find_window(receipt.occurred_at, windows)
        if matched_window is None:
            continue
        supported.append(
            SupportedAttempt(
                receipt_no=receipt.receipt_no,
                occurred_at=receipt.occurred_at,
                outage_start=matched_window.start,
                outage_end=matched_window.end,
                announcement_id=matched_window.base.notice_id,
                source_url=matched_window.base.url,
                source_urls=matched_window.source_urls,
                errata_ids=matched_window.errata_ids,
            )
        )

    ignored_acceptances = [
        receipt
        for receipt in receipts
        if receipt.outcome == ReceiptOutcome.ACCEPTED and receipt.occurred_at > deadline_aware
    ]
    if ignored_acceptances:
        warnings.append("存在截标后的成功受理回执，不作为“已受理”依据")

    if supported:
        warnings.append(
            "截标前存在落在故障时段内、且未被成功受理覆盖的失败尝试，可作为申诉佐证"
        )
        return _base_response(
            req,
            verdict=EvidenceVerdict.SUPPORTED,
            sources=sources,
            warnings=warnings,
            reason_codes=["failed_attempt_within_outage_before_deadline"],
            deadline=deadline,
            deadline_aware=deadline_aware,
            receipts=receipts,
            supported_attempts=supported,
            windows=window_models,
            outage_notices=outage.summaries,
        )

    # 6) 信息完整，但没有符合条件的失败尝试
    if not failed_before:
        reason = "no_failed_attempt_before_deadline"
        warnings.append("截标前没有提交失败回执，无申诉依据")
    elif not windows:
        reason = "no_outage_window_for_project"
        warnings.append("故障公告中未发现适用于本项目的故障时段，无申诉依据")
    else:
        reason = "failed_attempt_outside_outage_window"
        warnings.append("截标前的失败尝试均不在该项目故障时段内，无申诉依据")

    return _base_response(
        req,
        verdict=EvidenceVerdict.NO_EVIDENCE,
        sources=sources,
        warnings=warnings,
        reason_codes=[reason],
        deadline=deadline,
        deadline_aware=deadline_aware,
        receipts=receipts,
        windows=window_models,
        outage_notices=outage.summaries,
    )
