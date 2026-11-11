"""故障公告解析与勘误替换的单元测试（不依赖网络）。"""

from datetime import datetime, timedelta, timezone

from app.outage_parser import build_effective_outages, parse_outage_detail
from app.timeutil import resolve_timezone

TZ = timezone(timedelta(hours=8))


def _parse(html: str, url: str = "http://x/outage/a.html"):
    return parse_outage_detail(html, url, TZ)


BASE_HTML = """
<html><head><title>平台服务中断公告（FLT-2026-1112-01）</title></head><body>
<article>
  <h1>平台服务中断公告</h1>
  <ul>
    <li data-field="announcement-id" data-value="FLT-2026-1112-01">公告编号：FLT-2026-1112-01</li>
    <li data-field="published-at" data-value="2026-11-12 10:00">发布时间：2026-11-12 10:00</li>
    <li data-field="affected-projects" data-value="GSB-2026-001">影响项目：GSB-2026-001</li>
    <li data-field="outage-start" data-value="2026-11-12 09:30">故障开始时间：2026-11-12 09:30</li>
    <li data-field="outage-end" data-value="2026-11-12 12:00">恢复时间：2026-11-12 12:00</li>
  </ul>
</article>
</body></html>
"""

ERRATA_HTML = """
<html><head><title>恢复时间勘误公告（ERR-2026-1112-01）</title></head><body>
<article>
  <h1>恢复时间勘误公告</h1>
  <ul>
    <li data-field="announcement-id" data-value="ERR-2026-1112-01">公告编号：ERR-2026-1112-01</li>
    <li data-field="references" data-value="FLT-2026-1112-01">引用编号：FLT-2026-1112-01</li>
    <li data-field="published-at" data-value="2026-11-12 11:30">发布时间：2026-11-12 11:30</li>
    <li data-field="affected-projects" data-value="GSB-2026-099">影响项目：GSB-2026-099</li>
    <li data-field="outage-start" data-value="2026-11-12 09:30">故障开始时间：2026-11-12 09:30</li>
    <li data-field="outage-end" data-value="2026-11-12 12:30">恢复时间：2026-11-12 12:30</li>
  </ul>
</article>
</body></html>
"""

ERRATA_NO_REF_HTML = """
<html><head><title>勘误公告（未注明引用编号）</title></head><body>
<article><h1>勘误公告</h1>
<ul>
  <li data-field="announcement-id" data-value="ERR-X">公告编号：ERR-X</li>
  <li data-field="published-at" data-value="2026-11-12 11:30">发布时间：2026-11-12 11:30</li>
</ul>
</article></body></html>
"""


def test_parse_outage_detail_fields():
    notice = _parse(BASE_HTML)
    assert notice.notice_id == "FLT-2026-1112-01"
    assert notice.published_at == datetime(2026, 11, 12, 10, 0, tzinfo=TZ)
    assert notice.projects == ["GSB-2026-001"]
    assert notice.start == datetime(2026, 11, 12, 9, 30, tzinfo=TZ)
    assert notice.end == datetime(2026, 11, 12, 12, 0, tzinfo=TZ)
    assert notice.is_errata is False
    assert notice.window_incomplete is False


def test_errata_replaces_period_and_projects():
    notices = [_parse(BASE_HTML), _parse(ERRATA_HTML, "http://x/outage/errata.html")]
    effective, errors = build_effective_outages(notices)

    assert errors == []
    assert len(effective) == 1
    outage = effective[0]
    assert outage.projects == ["GSB-2026-099"]  # 勘误替换受影响项目
    assert outage.start == datetime(2026, 11, 12, 9, 30, tzinfo=TZ)  # 勘误替换时段
    assert outage.end == datetime(2026, 11, 12, 12, 30, tzinfo=TZ)
    assert [e.notice_id for e in outage.applied_errata] == ["ERR-2026-1112-01"]


def test_errata_inherits_unspecified_fields():
    errata = ERRATA_HTML.replace(
        '<li data-field="affected-projects" data-value="GSB-2026-099">影响项目：GSB-2026-099</li>',
        "",
    )
    notices = [_parse(BASE_HTML), _parse(errata, "http://x/outage/errata.html")]
    effective, errors = build_effective_outages(notices)

    assert errors == []
    assert effective[0].projects == ["GSB-2026-001"]  # 未给出项目则继承原公告


def test_errata_without_reference_is_structural_error():
    notices = [_parse(BASE_HTML), _parse(ERRATA_NO_REF_HTML, "http://x/outage/errata.html")]
    _, errors = build_effective_outages(notices)
    assert any("缺少引用编号" in error for error in errors)


def test_errata_reference_missing_is_structural_error():
    errata = ERRATA_HTML.replace("FLT-2026-1112-01", "FLT-NOT-FOUND")
    # 替换后公告自身的 announcement-id 也会变化，恢复它以便只让引用失效
    errata = errata.replace(
        'data-value="FLT-NOT-FOUND">公告编号：FLT-NOT-FOUND',
        'data-value="ERR-2026-1112-01">公告编号：ERR-2026-1112-01',
    )
    notices = [_parse(BASE_HTML), _parse(errata, "http://x/outage/errata.html")]
    _, errors = build_effective_outages(notices)
    assert any("引用编号不存在" in error for error in errors)


def test_platform_wide_outage_matches_any_project():
    html = BASE_HTML.replace(
        'data-value="GSB-2026-001">影响项目：GSB-2026-001',
        'data-value="全平台所有项目">影响项目：全平台所有项目',
    )
    notice = _parse(html)
    assert notice.all_projects is True
    assert notice.projects == []


def test_window_duration_fallback():
    html = """
    <html><head><title>平台服务中断公告</title></head><body><article><h1>平台服务中断公告</h1>
    <ul>
      <li data-field="announcement-id" data-value="FLT-D">公告编号：FLT-D</li>
      <li data-field="published-at" data-value="2026-11-12 10:00">发布时间：2026-11-12 10:00</li>
      <li data-field="affected-projects" data-value="GSB-2026-001">影响项目：GSB-2026-001</li>
      <li data-field="outage-start" data-value="2026-11-12 09:30">故障开始时间：2026-11-12 09:30</li>
    </ul>
    <p>故障持续约 40 分钟，现已恢复。</p>
    </article></body></html>
    """
    notice = _parse(html)
    assert notice.start == datetime(2026, 11, 12, 9, 30, tzinfo=TZ)
    assert notice.end == datetime(2026, 11, 12, 10, 10, tzinfo=TZ)
    assert notice.window_incomplete is False


def test_explicit_timezone_offset_is_respected():
    html = BASE_HTML.replace("2026-11-12 09:30", "2026-11-12T01:30:00+00:00")
    notice = _parse(html)
    assert notice.start == datetime(2026, 11, 12, 1, 30, tzinfo=timezone.utc)


def test_errata_may_replace_only_one_window_endpoint():
    errata = ERRATA_HTML.replace(
        '<li data-field="outage-start" data-value="2026-11-12 09:30">故障开始时间：2026-11-12 09:30</li>',
        "",
    )
    notices = [_parse(BASE_HTML), _parse(errata, "http://x/outage/errata.html")]
    effective, errors = build_effective_outages(notices)

    assert errors == []
    outage = effective[0]
    # 开始时间由原公告继承，恢复时间由勘误替换
    assert outage.start == datetime(2026, 11, 12, 9, 30, tzinfo=TZ)
    assert outage.end == datetime(2026, 11, 12, 12, 30, tzinfo=TZ)
    assert outage.incomplete is False


def test_errata_reference_can_be_found_in_title_wording():
    html = """
    <html><head><title>关于 FLT-2026-1112-01 的恢复时间勘误公告</title></head><body>
    <article><h1>关于 FLT-2026-1112-01 的恢复时间勘误公告</h1>
    <ul>
      <li data-field="announcement-id" data-value="ERR-TITLE">公告编号：ERR-TITLE</li>
      <li data-field="published-at" data-value="2026-11-12 11:30">发布时间：2026-11-12 11:30</li>
      <li data-field="outage-end" data-value="2026-11-12 12:30">恢复时间：2026-11-12 12:30</li>
    </ul>
    </article></body></html>
    """
    notice = _parse(html, "http://x/outage/errata.html")
    assert notice.references == ["FLT-2026-1112-01"]
    assert notice.is_errata is True


def test_default_timezone_resolution():
    assert resolve_timezone("+08:00").utcoffset(None) == timedelta(hours=8)
    assert resolve_timezone("UTC").utcoffset(None) == timedelta(0)
