"""Pydantic 数据模型：请求 / 响应 / 内部记录。"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)


class ProjectStatus(str, Enum):
    """投标状态。

    OPEN         可投标（存在有效截止时间，且未取消）
    CLOSED       不可投标（公告明确取消采购）
    PENDING      待核实（抓取/解析失败，或无法得到有效截止时间，不能把旧时间当最终期限）
    NOT_FOUND    列表页未发现与该项目编号相关的同站点公告
    """

    OPEN = "open"
    CLOSED = "closed"
    PENDING = "pending"
    NOT_FOUND = "not_found"


class NoticeKind(str, Enum):
    ORIGINAL = "original"          # 原公告
    CORRECTION = "correction"      # 更正公告（含澄清、变更、延期）
    CANCELLATION = "cancellation"  # 取消公告（更正的一种，且明确取消采购）


class FetchRequest(BaseModel):
    """采集请求。"""

    list_url: HttpUrl = Field(..., description="采购公告列表页 URL")
    project_code: str = Field(..., min_length=1, description="项目编号，如 GSB-2026-001")
    timeout: float = Field(10.0, ge=1.0, le=60.0, description="单次请求超时时间（秒）")


class NoticeRecord(BaseModel):
    """单条公告详情的解析结果（按发布时间排序）。"""

    url: HttpUrl
    kind: NoticeKind
    title: str
    published_at: datetime | None = None
    deadline: datetime | None = Field(
        None, description="该公告声明的投标截止时间；未提及则为 None，合并时沿用已生效时间"
    )
    cancelled: bool = False
    cancellation_reason: str | None = None


class SourceLink(BaseModel):
    """来源链接（列表页 + 参与合并的详情页）。"""

    role: str  # list / original / correction / cancellation
    url: HttpUrl
    title: str | None = None


class FetchResponse(BaseModel):
    """采集响应。"""

    project_code: str
    list_url: HttpUrl
    status: ProjectStatus
    can_bid: bool = Field(..., description="当前是否可投标")
    effective_deadline: datetime | None = Field(
        None, description="有效投标截止时间；待核实/未找到时为 None，绝不返回旧时间冒充最终期限"
    )
    cancelled: bool = False
    notices: list[NoticeRecord] = Field(default_factory=list)
    sources: list[SourceLink] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    fetched_at: datetime


# --------------------------------------------------------------------------- #
# 故障佐证（投标失败申诉）模型
# --------------------------------------------------------------------------- #
class ReceiptOutcome(str, Enum):
    """企业回执的两种结果。"""

    FAILED = "failed"      # 提交失败/未被受理
    ACCEPTED = "accepted"  # 成功受理


_FAILED_ALIASES = {
    "failed",
    "failure",
    "fail",
    "error",
    "rejected",
    "reject",
    "submissionfailed",
    "submitfailed",
    "notaccepted",
    "unaccepted",
    "提交失败",
    "失败",
    "未受理",
    "提交不成功",
    "不受理",
    "拒绝",
}
_ACCEPTED_ALIASES = {
    "accepted",
    "accept",
    "success",
    "successful",
    "succeed",
    "received",
    "receipted",
    "受理成功",
    "成功受理",
    "已受理",
    "成功",
    "受理",
    "已接收",
    "接收成功",
    "提交成功",
}


def _strip_delimiters(value: str) -> str:
    return value.strip().lower().replace(" ", "").replace("_", "").replace("-", "")


def _normalize_outcome(value: Any) -> ReceiptOutcome:
    if isinstance(value, ReceiptOutcome):
        return value
    raw = str(value).strip()
    key = _strip_delimiters(raw)
    if (
        key in _FAILED_ALIASES
        or "提交失败" in raw
        or ("未" in raw and "受理" in raw)
        or (key and not key.startswith("not") and any(word in key for word in ("failed", "failure", "fail", "reject", "error")))
    ):
        return ReceiptOutcome.FAILED
    if (
        key in _ACCEPTED_ALIASES
        or "成功" in raw
        or "受理" in raw
        or "接收" in raw
        or (key and any(word in key for word in ("accepted", "accept", "success", "received", "receipted")))
    ):
        return ReceiptOutcome.ACCEPTED
    raise ValueError(f"无法识别的回执结果：{value!r}（应为提交失败或成功受理）")


class ReceiptInput(BaseModel):
    """企业回执输入：编号 + 结果 + 带时区时间。"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    receipt_no: str = Field(
        ...,
        validation_alias=AliasChoices(
            "receipt_no",
            "receipt_id",
            "receipt_number",
            "receiptNo",
            "receiptId",
            "receiptNumber",
            "number",
            "no",
            "回执编号",
            "编号",
        ),
        description="回执编号",
    )
    outcome: ReceiptOutcome = Field(
        ...,
        validation_alias=AliasChoices(
            "outcome",
            "status",
            "result",
            "state",
            "type",
            "submission_status",
            "receipt_type",
            "submissionResult",
            "receiptStatus",
            "回执结果",
            "结果",
            "状态",
        ),
        description="failed=提交失败；accepted=成功受理",
    )
    occurred_at: datetime = Field(
        ...,
        validation_alias=AliasChoices(
            "occurred_at",
            "occurredAt",
            "time",
            "timestamp",
            "at",
            "event_time",
            "eventTime",
            "happened_at",
            "submitted_at",
            "failed_at",
            "accepted_at",
            "created_at",
            "retrieved_at",
            "时间",
            "发生时间",
            "提交时间",
        ),
        description="回执发生时间（应带时区，如 2026-11-12T12:15:00+08:00）",
    )

    @field_validator("receipt_no", mode="before")
    @classmethod
    def _coerce_receipt_no(cls, value: Any) -> str:
        return str(value).strip()

    @field_validator("outcome", mode="before")
    @classmethod
    def _coerce_outcome(cls, value: Any) -> ReceiptOutcome:
        return _normalize_outcome(value)


class ReceiptRecord(BaseModel):
    """规范化后的回执。"""

    receipt_no: str
    outcome: ReceiptOutcome
    occurred_at: datetime


def _make_receipt(item: Any, outcome: ReceiptOutcome) -> ReceiptInput:
    """把拆分列表中的回执补齐结果字段后转换为统一模型。"""
    if isinstance(item, ReceiptInput):
        return item.model_copy(update={"outcome": outcome})
    if isinstance(item, BaseModel):
        data = item.model_dump()
    elif isinstance(item, dict):
        data = dict(item)
    else:
        raise ValueError("回执必须是对象")
    data["outcome"] = outcome
    return ReceiptInput.model_validate(data)


class FaultEvidenceRequest(BaseModel):
    """故障佐证请求。"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    list_url: HttpUrl = Field(
        ...,
        validation_alias=AliasChoices(
            "list_url",
            "listUrl",
            "procurement_list_url",
            "procurementListUrl",
            "announcement_list_url",
            "采购公告列表URL",
            "公告列表URL",
        ),
        description="采购公告列表页 URL（用于沿用有效投标截止时间）",
    )
    project_code: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices(
            "project_code",
            "projectCode",
            "project_no",
            "projectNo",
            "project_id",
            "projectId",
            "项目编号",
        ),
        description="项目编号",
    )
    outage_dir_url: HttpUrl = Field(
        ...,
        validation_alias=AliasChoices(
            "outage_dir_url",
            "outageDirUrl",
            "outage_list_url",
            "outageListUrl",
            "fault_list_url",
            "faultListUrl",
            "fault_dir_url",
            "faultDirUrl",
            "incident_list_url",
            "incidentListUrl",
            "outage_url",
            "outageUrl",
            "故障公告目录URL",
            "故障公告目录",
        ),
        description="平台故障公告目录页 URL",
    )
    receipts: list[ReceiptInput] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "receipts",
            "submission_receipts",
            "receipt_records",
            "回执",
            "回执列表",
        ),
        description="该企业对目标项目的提交失败/成功受理回执列表",
    )
    failed_receipts: list[Any] | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "failed_receipts",
            "failed_attempts",
            "submission_failures",
            "提交失败回执",
        ),
        exclude=True,
        description="兼容按结果拆分的输入：提交失败回执",
    )
    success_receipts: list[Any] | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "success_receipts",
            "accepted_receipts",
            "successful_receipts",
            "成功受理回执",
        ),
        exclude=True,
        description="兼容按结果拆分的输入：成功受理回执",
    )
    timeout: float = Field(10.0, ge=1.0, le=60.0, description="单次请求超时时间（秒）")
    deadline_timezone: str = Field(
        "Asia/Shanghai",
        validation_alias=AliasChoices(
            "deadline_timezone",
            "deadlineTimezone",
            "timezone",
            "tz",
            "deadline_tz",
            "deadlineTz",
            "截止时间时区",
        ),
        description="采购公告中不带时区的截止时间/故障时段所采用的时区，默认 Asia/Shanghai",
    )

    @field_validator("project_code", mode="before")
    @classmethod
    def _strip_project_code(cls, value: Any) -> str:
        return str(value).strip()

    @field_validator("receipts", mode="before")
    @classmethod
    def _coerce_receipts(cls, value: Any) -> Any:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("receipts 必须是回执对象数组或其 JSON 字符串") from exc
        if isinstance(value, dict):
            return [value]
        return value

    @model_validator(mode="after")
    def _merge_split_receipts(self) -> "FaultEvidenceRequest":
        """兼容按结果拆分传入的 ``failed_receipts`` / ``success_receipts``。"""
        merged = list(self.receipts)
        for item in self.failed_receipts or []:
            merged.append(_make_receipt(item, ReceiptOutcome.FAILED))
        for item in self.success_receipts or []:
            merged.append(_make_receipt(item, ReceiptOutcome.ACCEPTED))
        self.receipts = merged
        self.failed_receipts = None
        self.success_receipts = None
        return self


class EvidenceVerdict(str, Enum):
    """故障佐证结论。"""

    SUPPORTED = "supported"                    # 可佐证（存在窗口内失败、且截标前未受理）
    ACCEPTED = "accepted"                      # 已受理（截标前已有成功回执）
    NO_EVIDENCE = "no_evidence"                # 无申诉依据（信息完整，但没有符合条件的失败尝试）
    UNABLE_TO_DETERMINE = "unable_to_determine"  # 无法判定（信息不完整/抓取失败/截止时间待核实）


VERDICT_LABELS: dict[EvidenceVerdict, str] = {
    EvidenceVerdict.SUPPORTED: "可佐证",
    EvidenceVerdict.ACCEPTED: "已受理",
    EvidenceVerdict.NO_EVIDENCE: "无申诉依据",
    EvidenceVerdict.UNABLE_TO_DETERMINE: "无法判定",
}


class OutageNoticeSummary(BaseModel):
    """解析到的故障公告/勘误摘要。"""

    url: HttpUrl
    title: str
    announcement_id: str | None = None
    published_at: datetime | None = None
    is_errata: bool = False
    references: list[str] = Field(default_factory=list)
    affected_projects: list[str] = Field(default_factory=list)
    all_projects: bool = False
    outage_start: datetime | None = None
    outage_end: datetime | None = None
    retracted: bool = False


class OutageWindow(BaseModel):
    """勘误替换后，目标项目实际适用的故障时段。"""

    project_code: str
    all_projects: bool = False
    start: datetime
    end: datetime
    announcement_id: str | None = None
    source_url: HttpUrl
    source_urls: list[HttpUrl] = Field(default_factory=list)
    errata_ids: list[str] = Field(default_factory=list)


class SupportedAttempt(BaseModel):
    """可佐证的失败尝试。"""

    receipt_no: str
    occurred_at: datetime
    outage_start: datetime
    outage_end: datetime
    announcement_id: str | None = None
    source_url: HttpUrl
    source_urls: list[HttpUrl] = Field(default_factory=list)
    errata_ids: list[str] = Field(default_factory=list)


class FaultEvidenceResponse(BaseModel):
    """故障佐证响应。"""

    project_code: str
    list_url: HttpUrl
    outage_dir_url: HttpUrl
    verdict: EvidenceVerdict = Field(..., description="故障佐证结论")
    status: EvidenceVerdict = Field(..., description="与 verdict 相同，便于沿用 status 字段的调用方")
    verdict_label: str = Field(..., description="结论中文标签：可佐证/已受理/无申诉依据/无法判定")
    appeal_basis: bool = Field(False, description="是否存在可佐证的申诉依据")
    can_appeal: bool = Field(False, description="与 appeal_basis 相同，便于按申诉语义调用")
    already_accepted: bool = Field(False, description="截标前是否已有成功受理回执")
    effective_deadline: datetime | None = Field(
        None, description="沿用采购公告采集结果的有效截止时间（公告本地墙上时间）"
    )
    effective_deadline_aware: datetime | None = Field(
        None, description="按 deadline_timezone 解释后的有效截止时间"
    )
    deadline_timezone: str = "Asia/Shanghai"
    receipts: list[ReceiptRecord] = Field(default_factory=list)
    accepted_receipt: ReceiptRecord | None = None
    supported_attempts: list[SupportedAttempt] = Field(default_factory=list)
    outage_windows: list[OutageWindow] = Field(default_factory=list)
    outage_notices: list[OutageNoticeSummary] = Field(default_factory=list)
    sources: list[SourceLink] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    fetched_at: datetime
