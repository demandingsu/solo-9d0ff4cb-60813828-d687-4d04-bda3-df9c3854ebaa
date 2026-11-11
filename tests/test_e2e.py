"""端到端测试：对 sample-site 启动本地静态服务器，通过 FastAPI 跑完整采集链路。"""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample-site"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # 静默测试输出
        pass


@pytest.fixture(scope="module")
def static_server():
    handler = functools.partial(_QuietHandler, directory=str(SAMPLE_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield f"http://127.0.0.1:{port}/list.html"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _collect(client, list_url, code):
    resp = client.post(
        "/api/v1/collect",
        json={"list_url": list_url, "project_code": code, "timeout": 5},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_project_001_merge_corrections_and_deadline_extension(client, static_server):
    data = _collect(client, static_server, "GSB-2026-001")

    assert data["status"] == "open"
    assert data["can_bid"] is True
    assert data["effective_deadline"].startswith("2026-11-15T14:00:00")
    assert data["cancelled"] is False

    # 三条公告按发布时间排序：原公告 → 澄清(未改期) → 延期
    kinds = [n["kind"] for n in data["notices"]]
    assert kinds == ["original", "correction", "correction"]
    published = [n["published_at"] for n in data["notices"]]
    assert published == sorted(published)

    # 澄清公告未提截止时间 → 沿用原时间；延期公告覆盖
    assert data["notices"][1]["deadline"] is None
    assert data["notices"][2]["deadline"].startswith("2026-11-15T14:00:00")

    roles = {s["role"] for s in data["sources"]}
    assert {"list", "original", "correction"} <= roles


def test_project_002_cancelled_is_not_biddable(client, static_server):
    data = _collect(client, static_server, "GSB-2026-002")

    assert data["status"] == "closed"
    assert data["can_bid"] is False
    assert data["cancelled"] is True
    # 取消后不存在“有效截止时间”，旧时间只能在 notices 明细中追溯
    assert data["effective_deadline"] is None
    assert any("取消" in w for w in data["warnings"])
    assert [n["kind"] for n in data["notices"]] == ["original", "cancellation"]
    assert data["notices"][0]["deadline"].startswith("2026-11-12T14:30:00")


def test_project_003_failed_detail_forces_pending_without_deadline(client, static_server):
    data = _collect(client, static_server, "GSB-2026-003")

    assert data["status"] == "pending"
    assert data["can_bid"] is False
    # 关键约束：有关联页面抓取失败时，绝不能把原公告的旧截止时间返回为最终期限
    assert data["effective_deadline"] is None
    assert any("抓取失败" in w for w in data["warnings"])
    assert any(s["role"] == "failed" for s in data["sources"])


def test_unknown_project_returns_not_found(client, static_server):
    data = _collect(client, static_server, "GSB-2099-NOPE")
    assert data["status"] == "not_found"
    assert data["effective_deadline"] is None
    # 无关项目的坏链接不应被计入本项目的关联失败
    assert not any(s["role"] == "failed" for s in data["sources"])


def test_external_and_nav_links_ignored(client, static_server):
    data = _collect(client, static_server, "GSB-2026-001")
    all_urls = [s["url"] for s in data["sources"]] + [n["url"] for n in data["notices"]]
    assert not any("example.gov" in u for u in all_urls)
    assert not any("login" in u for u in all_urls)


def test_get_endpoint_works_the_same(client, static_server):
    resp = client.get(
        "/api/v1/collect",
        params={"list_url": static_server, "project_code": "GSB-2026-001"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "open"


def test_bad_list_url_returns_502(client):
    resp = client.post(
        "/api/v1/collect",
        json={"list_url": "http://127.0.0.1:1/never-listens.html", "project_code": "X", "timeout": 2},
    )
    assert resp.status_code == 502


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


# --------------------------------------------------------------------------- #
# 故障佐证 API
# --------------------------------------------------------------------------- #
def _evidence(client, list_url, code, outage_path, receipts):
    resp = client.post(
        "/api/v1/fault-evidence",
        json={
            "list_url": list_url,
            "project_code": code,
            "outage_dir_url": outage_path,
            "receipts": receipts,
            "timeout": 5,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _outage_url(static_server: str, path: str) -> str:
    return static_server.replace("/list.html", path)


def test_fault_evidence_supported_after_errata_extends_outage(client, static_server):
    data = _evidence(
        client,
        static_server,
        "GSB-2026-001",
        _outage_url(static_server, "/outage/list.html"),
        [
            {
                "receipt_no": "RCPT-001",
                "outcome": "failed",
                "occurred_at": "2026-11-12T12:15:00+08:00",
            }
        ],
    )

    assert data["verdict"] == "supported"
    assert data["status"] == "supported"
    assert data["verdict_label"] == "可佐证"
    assert data["appeal_basis"] is True
    # 原公告 12:00 结束，勘误延长到 12:30，因此 12:15 的失败尝试可佐证
    assert data["supported_attempts"][0]["receipt_no"] == "RCPT-001"
    assert data["supported_attempts"][0]["outage_end"].startswith("2026-11-12T12:30:00")
    assert data["supported_attempts"][0]["errata_ids"] == ["ERR-2026-1112-01"]
    assert any("err" in s["url"] for s in data["sources"] if s["role"] == "outage-errata")


def test_fault_evidence_accepted_when_success_before_deadline(client, static_server):
    data = _evidence(
        client,
        static_server,
        "GSB-2026-001",
        _outage_url(static_server, "/outage/list.html"),
        [
            {
                "receipt_no": "RCPT-FAIL",
                "outcome": "failed",
                "occurred_at": "2026-11-12T12:15:00+08:00",
            },
            {
                "receipt_no": "RCPT-OK",
                "outcome": "accepted",
                "occurred_at": "2026-11-13T10:00:00+08:00",
            },
        ],
    )

    assert data["verdict"] == "accepted"
    assert data["verdict_label"] == "已受理"
    assert data["already_accepted"] is True
    assert data["appeal_basis"] is False
    assert data["accepted_receipt"]["receipt_no"] == "RCPT-OK"
    assert data["supported_attempts"] == []


def test_fault_evidence_ignores_success_after_deadline(client, static_server):
    data = _evidence(
        client,
        static_server,
        "GSB-2026-001",
        _outage_url(static_server, "/outage/list.html"),
        [
            {
                "receipt_no": "RCPT-FAIL",
                "outcome": "failed",
                "occurred_at": "2026-11-12T12:15:00+08:00",
            },
            {
                "receipt_no": "RCPT-LATE",
                "outcome": "accepted",
                "occurred_at": "2026-11-15T15:00:00+08:00",
            },
        ],
    )

    assert data["verdict"] == "supported"
    assert data["already_accepted"] is False
    assert [a["receipt_no"] for a in data["supported_attempts"]] == ["RCPT-FAIL"]
    assert any("截标后" in w for w in data["warnings"])


def test_fault_evidence_understands_timezone_aware_receipts(client, static_server):
    # 04:15Z == 12:15+08:00，落在故障时段内
    data = _evidence(
        client,
        static_server,
        "GSB-2026-001",
        _outage_url(static_server, "/outage/list.html"),
        [
            {
                "receipt_no": "RCPT-UTC",
                "outcome": "failed",
                "occurred_at": "2026-11-12T04:15:00Z",
            }
        ],
    )
    assert data["verdict"] == "supported"
    assert data["supported_attempts"][0]["occurred_at"].startswith("2026-11-12T04:15:00")


def test_fault_evidence_no_evidence_when_attempt_outside_window(client, static_server):
    data = _evidence(
        client,
        static_server,
        "GSB-2026-001",
        _outage_url(static_server, "/outage/list.html"),
        [
            {
                "receipt_no": "RCPT-OUTSIDE",
                "outcome": "failed",
                "occurred_at": "2026-11-12T12:45:00+08:00",
            }
        ],
    )
    assert data["verdict"] == "no_evidence"
    assert data["verdict_label"] == "无申诉依据"
    assert data["appeal_basis"] is False


def test_fault_evidence_project_without_outage_has_no_evidence(client, static_server):
    data = _evidence(
        client,
        static_server,
        "GSB-2026-099",
        _outage_url(static_server, "/outage/list.html"),
        [
            {
                "receipt_no": "RCPT-099",
                "outcome": "failed",
                "occurred_at": "2026-11-12T12:15:00+08:00",
            }
        ],
    )
    assert data["verdict"] == "no_evidence"
    assert data["reason_codes"] == ["no_outage_window_for_project"]


def test_fault_evidence_pending_deadline_is_undeterminable(client, static_server):
    data = _evidence(
        client,
        static_server,
        "GSB-2026-003",
        _outage_url(static_server, "/outage/list.html"),
        [
            {
                "receipt_no": "RCPT-003",
                "outcome": "failed",
                "occurred_at": "2026-11-08T22:00:00+08:00",
            }
        ],
    )
    assert data["verdict"] == "unable_to_determine"
    assert data["verdict_label"] == "无法判定"
    assert data["effective_deadline"] is None
    assert data["reason_codes"] == ["deadline_pending"]


def test_fault_evidence_cancelled_project_is_undeterminable(client, static_server):
    data = _evidence(
        client,
        static_server,
        "GSB-2026-002",
        _outage_url(static_server, "/outage/list.html"),
        [
            {
                "receipt_no": "RCPT-002",
                "outcome": "failed",
                "occurred_at": "2026-11-04T11:00:00+08:00",
            }
        ],
    )
    assert data["verdict"] == "unable_to_determine"
    assert data["effective_deadline"] is None


def test_fault_evidence_detail_404_is_undeterminable(client, static_server):
    data = _evidence(
        client,
        static_server,
        "GSB-2026-001",
        _outage_url(static_server, "/outage/broken/list.html"),
        [
            {
                "receipt_no": "RCPT-404",
                "outcome": "failed",
                "occurred_at": "2026-11-12T12:15:00+08:00",
            }
        ],
    )
    assert data["verdict"] == "unable_to_determine"
    assert "outage_detail_fetch_failed" in data["reason_codes"]
    assert any(s["role"] == "outage-failed" for s in data["sources"])


def test_fault_evidence_errata_without_reference_is_undeterminable(client, static_server):
    data = _evidence(
        client,
        static_server,
        "GSB-2026-001",
        _outage_url(static_server, "/outage/errata-no-ref/list.html"),
        [
            {
                "receipt_no": "RCPT-ER1",
                "outcome": "failed",
                "occurred_at": "2026-11-12T12:15:00+08:00",
            }
        ],
    )
    assert data["verdict"] == "unable_to_determine"
    assert "errata_reference_missing" in data["reason_codes"]


def test_fault_evidence_dangling_errata_reference_is_undeterminable(client, static_server):
    data = _evidence(
        client,
        static_server,
        "GSB-2026-001",
        _outage_url(static_server, "/outage/errata-dangling/list.html"),
        [
            {
                "receipt_no": "RCPT-ER2",
                "outcome": "failed",
                "occurred_at": "2026-11-12T12:15:00+08:00",
            }
        ],
    )
    assert data["verdict"] == "unable_to_determine"
    assert "errata_reference_missing" in data["reason_codes"]
    assert any("引用编号不存在" in w for w in data["warnings"])


def test_fault_evidence_errata_replaces_affected_project(client, static_server):
    # 原公告写 GSB-2026-001，勘误替换为 GSB-2026-099
    data_001 = _evidence(
        client,
        static_server,
        "GSB-2026-001",
        _outage_url(static_server, "/outage/errata-project/list.html"),
        [
            {
                "receipt_no": "RCPT-001",
                "outcome": "failed",
                "occurred_at": "2026-11-12T12:15:00+08:00",
            }
        ],
    )
    assert data_001["verdict"] == "no_evidence"

    data_099 = _evidence(
        client,
        static_server,
        "GSB-2026-099",
        _outage_url(static_server, "/outage/errata-project/list.html"),
        [
            {
                "receipt_no": "RCPT-099",
                "outcome": "failed",
                "occurred_at": "2026-11-12T12:15:00+08:00",
            }
        ],
    )
    assert data_099["verdict"] == "supported"
    assert data_099["supported_attempts"][0]["errata_ids"] == ["ERR-2026-1112-31"]


def test_fault_evidence_get_endpoint(client, static_server):
    import json

    receipts = json.dumps(
        [
            {
                "receipt_no": "RCPT-GET",
                "outcome": "failed",
                "occurred_at": "2026-11-12T12:15:00+08:00",
            }
        ]
    )
    resp = client.get(
        "/api/v1/fault-evidence",
        params={
            "list_url": static_server,
            "project_code": "GSB-2026-001",
            "outage_dir_url": _outage_url(static_server, "/outage/list.html"),
            "receipts": receipts,
            "timeout": 5,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "supported"
