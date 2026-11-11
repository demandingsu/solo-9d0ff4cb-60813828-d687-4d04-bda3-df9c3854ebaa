# 采购公告采集与故障佐证 API

两个相互衔接的能力：

1. **公告采集**：传入采购公告列表页 URL 与项目编号，自动抓取同站点原公告与更正公告，
   从详情页提取发布时间、投标截止时间与取消采购信息，按发布时间合并后返回当前是否
   可投标、有效截止时间、项目状态与全部来源链接。
2. **故障佐证**：投标专员提交标书失败后，传入公告列表、项目编号、平台故障公告目录，
   以及企业对该项目的提交失败/成功受理回执，自动沿用采集结果中的有效截标时间，
   抓取故障详情及勘误并按引用编号 + 发布时间替换受影响项目与时段，判断是否存在
   可佐证的申诉依据。

## 业务规则

| 规则 | 实现位置 |
| --- | --- |
| 只采集列表页指向的**同站点**详情链接，外站链接、登录/导航链接自动忽略 | `app/parser.py::extract_detail_links` |
| 按**发布时间**从早到晚合并原公告与更正公告 | `app/merger.py::merge_notices` |
| 更正公告**未提及截止时间**时，沿用此前已生效的截止时间 | `app/merger.py` |
| 更正公告给出新截止时间（如延期）时，以新时间覆盖 | `app/merger.py` |
| 公告明确**取消采购** → 状态 `closed`、不可投标，不再返回有效截止时间（旧时间仅在公告明细中保留） | `app/merger.py` |
| **任一关联详情页**抓取/解析失败（404、超时、结构异常等）→ 状态 `pending` 待核实，`effective_deadline` 强制为 `null`，**绝不把旧时间当作最终期限** | `app/merger.py` + `app/collector.py` |
| 发布时间缺失、缺少原公告、全部公告均无截止时间等信息链不完整情况，同样标记 `pending` | `app/merger.py` |
| 故障佐证只使用采集结果中的**有效截标时间**；状态为待核实/已取消/未找到时直接“无法判定” | `app/evidence.py` |
| 故障公告勘误按**引用编号**定位原公告，并按**发布时间**排序应用；给出受影响项目/时段时**替换**原值，未给出的字段沿用原值 | `app/outage_parser.py::build_effective_outages` |
| 截标前（含截止时点）已有**成功受理回执** → 结论“已受理”，不再需要故障佐证 | `app/evidence.py` |
| 截标前的**失败尝试**落在目标项目（勘误替换后）的故障时段内，且截标前没有成功受理回执 → 结论“可佐证” | `app/evidence.py` |
| 故障详情抓取/解析失败、勘误引用缺失或发布时间缺失、故障时段字段不完整、受影响项目无法识别 → 结论“无法判定”，**不能当作没有故障** | `app/evidence.py` + `app/outage_parser.py` |

### 项目状态（`status`）

- `open`：可投标，`effective_deadline` 为合并后的有效截止时间；
- `closed`：采购已取消，`can_bid=false`，无有效截止时间；
- `pending`：待核实，存在关联页面失败或信息不完整，`effective_deadline=null`；
- `not_found`：列表页未发现与该项目编号相关的公告。

### 故障佐证结论（`verdict` / `status`，中文见 `verdict_label`）

| 结论 | 含义 | `appeal_basis` |
| --- | --- | --- |
| `supported`（可佐证） | 截标前存在落在故障时段内的失败尝试，且截标前没有成功受理回执 | `true` |
| `accepted`（已受理） | 截标前已有成功受理回执 | `false` |
| `no_evidence`（无申诉依据） | 故障信息完整，但没有符合“截标前 + 故障时段内 + 未被受理”条件的失败尝试 | `false` |
| `unable_to_determine`（无法判定） | 截止时间待核实、故障详情读取失败、勘误引用缺失或故障信息不完整 | `false` |

> `no_evidence` 只会在故障信息完整读取后给出；任何抓取/解析失败或信息缺项都会落到
> `unable_to_determine`，避免把“信息不全”误判成“平台没有故障”。

## 目录结构

```
.
├── app/
│   ├── main.py          # FastAPI 入口、路由、浏览器演示页
│   ├── collector.py     # 公告采集编排：列表发现 → 并发抓取 → 归属判定 → 合并
│   ├── parser.py        # HTML 解析：同站链接发现、三层字段提取、取消识别
│   ├── merger.py        # 公告业务合并规则（时间继承、延期覆盖、取消、待核实）
│   ├── evidence.py      # 故障佐证编排：截止时间沿用 → 故障/勘误 → 回执比对
│   ├── outage_parser.py # 故障公告解析与勘误替换
│   ├── timeutil.py      # 时区解析、带时区时间、时间范围与时长识别
│   └── models.py        # 请求/响应 Pydantic 模型
├── sample-site/         # 本地示例站点（nginx 静态托管）
│   ├── list.html        # 采购公告列表页（含外站/登录干扰链接）
│   ├── detail/          # 3 个采购项目的原公告/更正/取消详情页
│   └── outage/          # 故障公告目录与详情（含勘误、坏详情、缺引用样例）
├── nginx/default.conf   # 示例站点 nginx 配置（缺失页面返回真实 404）
├── tests/               # 解析/合并/故障佐证单测 + 本地静态服务器端到端测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| 公告采集与故障佐证 API | http://localhost:8000 | Swagger 文档：http://localhost:8000/docs |
| 浏览器演示页 | http://localhost:8000/ | 填表即可发起采集或故障佐证 |
| 示例采购公告站点 | http://localhost:8080/list.html | nginx 托管的静态采购公告 |
| 示例故障公告目录 | http://localhost:8080/outage/list.html | 含故障详情与勘误样例 |

> Docker Compose 中 API 容器通过服务名 `sample-site` 访问示例站点，演示页默认值已由
> `SAMPLE_LIST_URL` / `SAMPLE_OUTAGE_LIST_URL` 环境变量注入；浏览器只把 URL 交给 API，
> 因此不会受宿主机端口映射影响。直接 `curl` 时也请使用 `http://sample-site/...` 形式
> （在容器内），或使用 `docker compose exec`。

停止：`docker compose down`。

## API 用法

### POST（推荐）

```bash
# Docker Compose：API 容器内可解析 sample-site 服务名
curl -X POST http://localhost:8000/api/v1/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url":"http://sample-site/list.html","project_code":"GSB-2026-001"}'

# 本地非 Docker：示例站点由 python -m http.server 8080 托管
# -d '{"list_url":"http://localhost:8080/list.html","project_code":"GSB-2026-001"}'
```

### GET（便于浏览器快速验证）

```bash
curl "http://localhost:8000/api/v1/collect?list_url=http://sample-site/list.html&project_code=GSB-2026-001"
```

响应关键字段：

```json
{
  "project_code": "GSB-2026-001",
  "status": "open",
  "can_bid": true,
  "effective_deadline": "2026-11-15T14:00:00",
  "cancelled": false,
  "notices": [ { "kind": "original", "published_at": "...", "deadline": "..." } ],
  "sources": [ { "role": "list", "url": "..." }, { "role": "correction", "url": "..." } ],
  "warnings": []
}
```

## 故障佐证 API

### POST `/api/v1/fault-evidence`（推荐）

```bash
curl -X POST http://localhost:8000/api/v1/fault-evidence \
  -H 'Content-Type: application/json' \
  -d '{
    "list_url": "http://sample-site/list.html",
    "project_code": "GSB-2026-001",
    "outage_dir_url": "http://sample-site/outage/list.html",
    "receipts": [
      {
        "receipt_no": "RCPT-2026-001-01",
        "outcome": "failed",
        "occurred_at": "2026-11-12T12:15:00+08:00"
      }
    ]
  }'
```

请求字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `list_url` | 是 | 采购公告列表页 URL；用于沿用采集结果中的有效截标时间 |
| `project_code` | 是 | 项目编号 |
| `outage_dir_url` | 是 | 平台故障公告目录页 URL（别名 `outage_list_url` / `fault_list_url`） |
| `receipts` | 否 | 回执数组，每项含 `receipt_no`、`outcome`（`failed`/`accepted`）、带时区的 `occurred_at` |
| `timeout` | 否 | 单次抓取超时秒数，默认 10 |
| `deadline_timezone` | 否 | 公告中不带时区的截止时间/故障时段所采用的时区，默认 `Asia/Shanghai`；也可传 `+08:00`、`UTC` 等 |

兼容性输入：回执编号/结果/时间可使用 `receipt_id`、`status`、`result`、`time`、
`timestamp` 等常见别名；也可把回执拆成顶层 `failed_receipts` 与 `success_receipts`
（每项只需编号和时间，结果由所在列表决定）。

响应关键字段：

```json
{
  "project_code": "GSB-2026-001",
  "verdict": "supported",
  "status": "supported",
  "verdict_label": "可佐证",
  "appeal_basis": true,
  "already_accepted": false,
  "effective_deadline": "2026-11-15T14:00:00",
  "effective_deadline_aware": "2026-11-15T14:00:00+08:00",
  "deadline_timezone": "Asia/Shanghai",
  "supported_attempts": [
    {
      "receipt_no": "RCPT-2026-001-01",
      "occurred_at": "2026-11-12T12:15:00+08:00",
      "outage_start": "2026-11-12T09:30:00+08:00",
      "outage_end": "2026-11-12T12:30:00+08:00",
      "announcement_id": "FLT-2026-1112-01",
      "errata_ids": ["ERR-2026-1112-01"],
      "source_url": "http://sample-site/outage/detail/flt-20261112-01.html",
      "source_urls": ["..."]
    }
  ],
  "outage_windows": [ { "start": "...", "end": "...", "errata_ids": ["..."] } ],
  "outage_notices": [ { "announcement_id": "...", "is_errata": false } ],
  "sources": [ { "role": "outage-list", "url": "..." }, { "role": "outage-errata", "url": "..." } ],
  "reason_codes": ["failed_attempt_within_outage_before_deadline"],
  "warnings": []
}
```

### GET（便于快速验证）

```bash
curl -G http://localhost:8000/api/v1/fault-evidence \
  --data-urlencode "list_url=http://sample-site/list.html" \
  --data-urlencode "project_code=GSB-2026-001" \
  --data-urlencode "outage_dir_url=http://sample-site/outage/list.html" \
  --data-urlencode 'receipts=[{"receipt_no":"RCPT-2026-001-01","outcome":"failed","occurred_at":"2026-11-12T12:15:00+08:00"}]'
```

### 判定规则

1. **先沿用采集结果**：调用现有采集逻辑取得 `effective_deadline`。若采购状态为
   `pending`/`closed`/`not_found` 或没有有效截止时间，直接返回“无法判定”，不猜测截标时间。
2. **先看成功受理**：截标前（含截止时点）存在 `accepted` 回执 → “已受理”。
3. **解析故障公告与勘误**：目录中每条详情都必须能读取；任一详情 404/超时/解析失败、
   勘误缺引用编号或引用目标不存在、勘误缺发布时间、受影响项目或时段字段不完整 →
   “无法判定”。
4. **勘误替换**：勘误按引用编号定位原故障公告，并按发布时间顺序应用；勘误给出的
   受影响项目和时段替换原值，未给出的字段继承原值。最终按项目编号（或“全平台”）
   匹配目标项目。
5. **失败尝试比对**：只看截标前（含截止时点）的 `failed` 回执；若其发生时间落在
   勘误替换后的故障时段内，且截标前没有 `accepted` 回执 → “可佐证”，返回回执编号、
   故障时段及故障公告/勘误来源。
6. **其余情况**：信息完整但没有符合条件的失败尝试 → “无申诉依据”；截标后的成功
   回执不会覆盖截标前的失败尝试。

## 示例站点内置的三个演示项目

| 项目编号 | 场景 | 预期结果 |
| --- | --- | --- |
| `GSB-2026-001` | 原公告 → 澄清更正（**未提截止时间，沿用原时间**）→ 延期更正（**新时间覆盖**） | `open`，有效截止时间 `2026-11-15 14:00` |
| `GSB-2026-002` | 原公告 → 取消公告（预算收回） | `closed`，不可投标，无有效截止时间 |
| `GSB-2026-003` | 原公告 → 更正页**已下线（404）** | `pending` 待核实，`effective_deadline=null`，来源中列出失败链接 |

另外列表页还包含：外站链接、登录导航链接、其他项目公告（`GSB-2026-099`）、
无关项目的坏链接（`GSB-2025-888`），用于验证同站过滤、项目归属判定与失败隔离。

### 示例故障公告目录

| 目录 URL | 场景 | 预期结论 |
| --- | --- | --- |
| `/outage/list.html` | `FLT-2026-1112-01` 故障时段 09:30–12:00，勘误 `ERR-2026-1112-01` 将恢复时间替换为 12:30 | 001 在 12:15 的失败 → `supported`；001 截标前有成功回执 → `accepted`；001 12:45 失败 → `no_evidence`；099 无故障 → `no_evidence` |
| `/outage/broken/list.html` | 目录中的详情页已下线（404） | `unable_to_determine` |
| `/outage/errata-no-ref/list.html` | 勘误公告漏写引用编号 | `unable_to_determine` |
| `/outage/errata-dangling/list.html` | 勘误引用的公告编号不存在 | `unable_to_determine` |
| `/outage/errata-project/list.html` | 勘误把受影响项目由 001 替换为 099 | 001 → `no_evidence`；099 → `supported` |

所有样例均位于 `sample-site/outage/`，由 nginx 或本地 `python -m http.server`
离线托管，不依赖任何外网服务。

## 详情页解析策略（三层回退）

1. **结构化标记**：`data-field="published-at|deadline|cancelled|..."` 元素或
   `<meta property="article:published_time">`；
2. **标签匹配**：`发布时间 / 投标截止时间 / 响应文件递交截止时间 / 取消原因` 等
   中文标签，兼容 `th/td`、`dt/dd`、`li/p` 等布局，支持
   `2026-11-10 09:30`、`2026年11月15日 14时00分` 等常见写法；
3. **正文正则兜底**：在含“截止/递交/提交/受理”等时限语义的块级元素中识别日期，
   并排除“发布时间”句、资质业绩说明中的历史日期（如“近三年（2023年1月1日起）”）。

取消采购识别支持 `data-field="cancelled"` 标记、取消原因标签以及
“本项目取消采购 / 终止本次招标”等显式句式。

> 接入真实采购平台时，若页面结构不同，优先在详情页模板中补 `data-field` 标记，
> 或在 `app/parser.py` 顶部的标签/关键词元组中登记该平台的字段叫法。

## 本地开发与测试（不使用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 终端 A：托管示例公告页（含故障公告目录）
cd sample-site && python -m http.server 8080
# 终端 B：启动 API
uvicorn app.main:app --reload --port 8000

# 测试（自带静态服务器，无需外部依赖/外网）
pytest -q
```

本地运行时，把示例 URL 写成 `http://localhost:8080/list.html` 和
`http://localhost:8080/outage/list.html`；Docker Compose 中则使用
`http://sample-site/...`（API 容器内解析）。

## 设计说明

- 公告列表页抓取失败直接返回 `502`；详情页失败不影响整体响应，而是按业务规则沉淀为
  `pending` + 失败来源链接，便于投标专员人工核实。
- 故障佐证不会把“无法读取/信息缺项”当成“没有故障”：故障目录或任一详情读取失败、
  勘误引用异常、受影响项目/时段无法解析，均返回 `unable_to_determine` 并附
  `reason_codes` 与来源链接，便于人工复核。
- 故障公告中的时间默认按 `deadline_timezone`（默认 Asia/Shanghai / UTC+8）解释；
  页面显式带 `+08:00`/`Z` 等偏移时以页面为准，企业回执则按其自带时区与截止时间
  比较，避免跨时区误判。
- 关联归属判定结合详情页项目编号与列表行上下文（含编号列），
  即使详情页下线（只能看到列表行）也能正确判定失败是否与目标项目相关；
  无关项目的坏链接不会污染查询结果。
- 详情页/故障详情并发抓取（上限 5），统一超时、自定义 UA、跟随重定向。
