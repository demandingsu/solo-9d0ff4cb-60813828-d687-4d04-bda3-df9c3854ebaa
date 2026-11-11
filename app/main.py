"""FastAPI 入口：公告采集 API + 故障佐证 API。

端点：
* GET  /                         演示页（可直接在浏览器发起采集/佐证）
* POST /api/v1/collect           传入列表页 URL 与项目编号，返回合并后的投标状态
* GET  /api/v1/collect           同上的查询参数形式，便于浏览器/curl 快速演示
* POST /api/v1/fault-evidence    输入公告列表、故障目录与回执，判断是否存在申诉依据
* GET  /api/v1/fault-evidence    同上的查询参数形式（receipts 为 JSON 数组字符串）
* GET  /healthz                  健康检查
"""

from __future__ import annotations

import json
import os

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from app.collector import collect
from app.evidence import gather_fault_evidence
from app.models import (
    FaultEvidenceRequest,
    FaultEvidenceResponse,
    FetchRequest,
    FetchResponse,
)

_SAMPLE_BASE = os.environ.get("SAMPLE_SITE_URL", "http://localhost:8080").rstrip("/")
_SAMPLE_LIST_URL = os.environ.get("SAMPLE_LIST_URL", f"{_SAMPLE_BASE}/list.html")
_SAMPLE_OUTAGE_LIST_URL = os.environ.get(
    "SAMPLE_OUTAGE_LIST_URL", f"{_SAMPLE_BASE}/outage/list.html"
)

app = FastAPI(
    title="采购公告采集与故障佐证 API",
    description=(
        "抓取同站点原公告与更正公告，按发布时间合并，输出投标状态与有效截止时间；"
        "并可在提交失败后，结合平台故障公告与勘误判断是否存在申诉依据。"
    ),
    version="1.1.0",
)


_DEMO_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>采购公告采集与故障佐证 API 演示</title>
<style>
body { font-family: system-ui, "PingFang SC", "Microsoft YaHei", sans-serif; margin: 2rem; max-width: 980px; }
input, select { width: 32rem; max-width: 80vw; padding: .4rem; }
button { padding: .45rem 1.2rem; cursor: pointer; }
pre { background: #f4f4f6; padding: 1rem; border-radius: 8px; overflow:auto; max-height: 55vh; }
.hint { color: #666; font-size: .9rem; }
section { border-top: 1px solid #ddd; margin-top: 2rem; padding-top: 1rem; }
</style>
</head>
<body>
<h1>采购公告采集与故障佐证 API 演示</h1>
<p class="hint">示例站点由 docker compose 中的 nginx 提供；也可用本地静态服务器托管 sample-site。</p>

<section>
<h2>1. 采集公告并合并截止时间</h2>
<form id="f">
  <p><label>列表页 URL <input id="list_url" value="__LIST_URL__"></label></p>
  <p><label>项目编号 <input id="project_code" value="GSB-2026-001"></label></p>
  <button type="submit">采集并合并</button>
</form>
<pre id="out">响应将显示在这里……</pre>
</section>

<section>
<h2>2. 故障佐证（提交失败后判断申诉依据）</h2>
<form id="ef">
  <p><label>采购公告列表 URL <input id="e_list_url" value="__LIST_URL__"></label></p>
  <p><label>项目编号 <input id="e_project_code" value="GSB-2026-001"></label></p>
  <p><label>故障公告目录 URL <input id="outage_dir_url" value="__OUTAGE_URL__"></label></p>
  <p><label>回执编号 <input id="receipt_no" value="RCPT-2026-001-01"></label></p>
  <p><label>回执结果
    <select id="outcome">
      <option value="failed">提交失败</option>
      <option value="accepted">成功受理</option>
    </select></label></p>
  <p><label>回执时间（带时区）<input id="occurred_at" value="2026-11-12T12:15:00+08:00"></label></p>
  <button type="submit">判断故障佐证</button>
</form>
<pre id="eout">响应将显示在这里……</pre>
</section>

<script>
async function postJson(url, payload, out) {
  out.textContent = '请求中……';
  try {
    const resp = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    out.textContent = JSON.stringify(await resp.json(), null, 2);
  } catch (err) {
    out.textContent = '请求失败：' + err;
  }
}

document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  await postJson('/api/v1/collect', {
    list_url: document.getElementById('list_url').value,
    project_code: document.getElementById('project_code').value
  }, document.getElementById('out'));
});

document.getElementById('ef').addEventListener('submit', async (e) => {
  e.preventDefault();
  await postJson('/api/v1/fault-evidence', {
    list_url: document.getElementById('e_list_url').value,
    project_code: document.getElementById('e_project_code').value,
    outage_dir_url: document.getElementById('outage_dir_url').value,
    receipts: [{
      receipt_no: document.getElementById('receipt_no').value,
      outcome: document.getElementById('outcome').value,
      occurred_at: document.getElementById('occurred_at').value
    }]
  }, document.getElementById('eout'));
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def demo_page() -> str:
    return (
        _DEMO_PAGE.replace("__LIST_URL__", _SAMPLE_LIST_URL)
        .replace("__OUTAGE_URL__", _SAMPLE_OUTAGE_LIST_URL)
    )


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/collect", response_model=FetchResponse, tags=["collect"])
async def collect_post(req: FetchRequest) -> FetchResponse:
    return await collect(req)


@app.get("/api/v1/collect", response_model=FetchResponse, tags=["collect"])
async def collect_get(
    list_url: str = Query(..., description="采购公告列表页 URL"),
    project_code: str = Query(..., description="项目编号"),
    timeout: float = Query(10.0, ge=1.0, le=60.0),
) -> FetchResponse:
    return await collect(FetchRequest(list_url=list_url, project_code=project_code, timeout=timeout))


# --------------------------------------------------------------------------- #
# 故障佐证 API
# --------------------------------------------------------------------------- #
@app.post("/api/v1/fault-evidence", response_model=FaultEvidenceResponse, tags=["evidence"])
@app.post(
    "/api/v1/outage-evidence",
    response_model=FaultEvidenceResponse,
    include_in_schema=False,
)
@app.post("/api/v1/evidence", response_model=FaultEvidenceResponse, include_in_schema=False)
@app.post(
    "/api/v1/faults/evidence",
    response_model=FaultEvidenceResponse,
    include_in_schema=False,
)
@app.post(
    "/api/v1/appeal-evidence",
    response_model=FaultEvidenceResponse,
    include_in_schema=False,
)
async def fault_evidence_post(req: FaultEvidenceRequest) -> FaultEvidenceResponse:
    return await gather_fault_evidence(req)


@app.get("/api/v1/fault-evidence", response_model=FaultEvidenceResponse, tags=["evidence"])
@app.get(
    "/api/v1/outage-evidence",
    response_model=FaultEvidenceResponse,
    include_in_schema=False,
)
@app.get("/api/v1/evidence", response_model=FaultEvidenceResponse, include_in_schema=False)
async def fault_evidence_get(
    list_url: str = Query(..., description="采购公告列表页 URL"),
    project_code: str = Query(..., description="项目编号"),
    outage_dir_url: str | None = Query(None, description="故障公告目录 URL"),
    outage_list_url: str | None = Query(None, description="故障公告目录 URL（别名）"),
    receipts: str = Query("[]", description="回执 JSON 数组字符串"),
    deadline_timezone: str = Query("Asia/Shanghai", description="公告截止时间所采用的时区"),
    timeout: float = Query(10.0, ge=1.0, le=60.0),
) -> FaultEvidenceResponse:
    directory = outage_dir_url or outage_list_url
    if not directory:
        raise HTTPException(status_code=422, detail="必须提供 outage_dir_url 或 outage_list_url")
    try:
        parsed_receipts = json.loads(receipts)
        request = FaultEvidenceRequest(
            list_url=list_url,
            project_code=project_code,
            outage_dir_url=directory,
            receipts=parsed_receipts,
            deadline_timezone=deadline_timezone,
            timeout=timeout,
        )
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await gather_fault_evidence(request)
