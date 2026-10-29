from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

TEMPLATE = (
    Path(__file__).parents[1]
    / "src/video_report_agent/skills/video-report/assets/report-template.html"
)


def test_video_description_placeholder_reuses_source_details_contract():
    template = TEMPLATE.read_text()

    assert "{{VIDEO_DESCRIPTION}}" in template
    assert "<details><summary>来源说明</summary>{{SOURCES}}</details>" in template
    assert (
        ".video-description-text{white-space:pre-wrap;overflow-wrap:anywhere;"
        "word-break:break-word}"
    ) in template


def test_footer_keeps_report_label_left_and_domain_right():
    template = TEMPLATE.read_text()

    assert "footer{display:flex;align-items:baseline;justify-content:space-between;" in template
    assert (
        '<footer><span>视频报告·精读</span>'
        '<a class="footer-link" href="https://vreport.tri4t.xyz/">'
        "vreport.tri4t.xyz</a></footer>"
    ) in template
    assert ".footer-link,.footer-link:hover{color:inherit;text-decoration:underline}" in template


def test_header_contract_keeps_summary_and_full_source_metadata():
    template = TEMPLATE.read_text()

    assert '<p class="lead">{{LEAD}}</p>' in template
    assert '<p class="meta">{{ATTRIBUTION}}</p>' in template
    assert "导语固定保留" in template
    assert "平台；UP 主；《原视频完整标题》；可点击的原视频裸 URL" in template
    assert ".meta a{color:inherit;text-decoration:underline}" in template


def test_template_keeps_paper_white_and_canvas_light_gray():
    template = TEMPLATE.read_text()

    assert "--paper:#fff;--canvas:#f2f5f7;" in template


def test_standard_navigation_lives_outside_paper_and_hides_for_long_image():
    template = TEMPLATE.read_text()

    assert ".report-nav{counter-reset:report-nav;position:fixed;" in template
    assert "left:calc(50vw - 724px);" in template
    assert ".report-nav-title{display:block;" in template
    assert ".report-nav>a::before{content:counter(report-nav,decimal-leading-zero);" in template
    assert "@media(max-width:1194px){.report-nav{display:none}}" in template
    assert "@media print{.report-nav{display:none}}" in template
    assert '<nav class="report-nav" aria-label="章节导航">' in template
    assert '<span class="report-nav-title">目录</span>' in template


@pytest.mark.parametrize("width", [390, 1440])
def test_bar_component_renders_visible_fills_at_declared_ratios(width):
    """Check the actual span geometry, including CSS cascade and mobile layout."""
    template = TEMPLATE.read_text()
    body = '<div class="bars">' + ''.join(
        f'<div class="bar"><div class="bar-head"><span class="bar-name">Item</span>'
        f'<span class="bar-value">{percent}</span></div>'
        f'<span class="bar-track"><span class="bar-fill" style="width:{percent}%">'
        '</span></span></div>'
        for percent in (25, 100)
    ) + '</div>'
    for name, value in {
        "TITLE": "Bar fixture", "SUBTITLE": "", "LEAD": "Comparable values",
        "ATTRIBUTION": "Synthetic source", "BODY": body,
        "SOURCES": "", "VIDEO_DESCRIPTION": "",
    }.items():
        template = template.replace("{{" + name + "}}", value)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 900})
            page.route("**/*", lambda route: route.abort())
            page.set_content(template)
            bars = page.locator(".bar-track").evaluate_all('''tracks => tracks.map(track => {
                const fill = track.querySelector('.bar-fill');
                const style = getComputedStyle(fill);
                return {track: track.getBoundingClientRect().toJSON(),
                    fill: fill.getBoundingClientRect().toJSON(),
                    display: style.display, visibility: style.visibility,
                    opacity: style.opacity, background: style.backgroundColor};
            })''')
            assert len(bars) == 2
            for bar, ratio in zip(bars, (0.25, 1)):
                assert bar["display"] == "block"
                assert bar["visibility"] == "visible" and bar["opacity"] == "1"
                assert bar["background"] != "rgba(0, 0, 0, 0)"
                assert bar["track"]["width"] > 0 and bar["track"]["height"] > 0
                assert bar["fill"]["width"] == pytest.approx(bar["track"]["width"] * ratio, abs=1)
                assert bar["fill"]["height"] == pytest.approx(bar["track"]["height"], abs=0.1)
        finally:
            browser.close()


def test_brief_template_fills_shared_delivery_contract(tmp_path):
    from video_report_agent.report_content import fill_video_description

    brief = TEMPLATE.with_name("brief-report-template.html").read_text()
    rendered = brief
    for name, value in {
        "TITLE": "Fixture", "SUBTITLE": "", "LEAD": "Source overview",
        "ATTRIBUTION": "Fixture source", "BODY": "<section>Body</section>",
        "SOURCES": "Fixture source mapping",
    }.items():
        rendered = rendered.replace("{{" + name + "}}", value)
    rendered = rendered.split("<!-- BRIEF_EXAMPLES_START")[0]
    report = tmp_path / "report.html"
    report.write_text(rendered)
    (tmp_path / "download").mkdir()
    (tmp_path / "download/source.info.json").write_text('{"description":"Original <source>"}')
    fill_video_description(report, tmp_path)
    html = report.read_text()
    assert "{{" not in html
    assert "Original &lt;source&gt;" in html
    assert "视频报告·速览" in html
    assert 'href="https://vreport.tri4t.xyz/"' in html
    assert 'class="brief-paper"' in html
