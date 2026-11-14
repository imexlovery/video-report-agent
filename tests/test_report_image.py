import struct
from pathlib import Path

from video_report_agent.report_image import render_report_image

TEMPLATE = (
    Path(__file__).parents[1]
    / "src/video_report_agent/skills/video-report/assets/report-template.html"
)


def test_desktop_full_page_and_cached_image(tmp_path):
    (tmp_path / "report.html").write_text('''<!doctype html><style>
      body {margin:0; height:3200px; background:#fff}
      @media(max-width:900px) {body {height:100px}}
    </style><h1>桌面长图</h1><p style="position:absolute;top:3100px">页面底部</p>''')
    output = render_report_image(tmp_path)
    content = output.read_bytes()
    assert content[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", content[16:24])
    assert width == 920
    assert height >= 3200
    (tmp_path / "report.html").unlink()
    assert render_report_image(tmp_path).read_bytes() == content


def test_old_wide_image_is_regenerated(tmp_path):
    (tmp_path / "report.html").write_text("<html><body>报告</body></html>")
    output = tmp_path / "report.png"
    output.write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 1440, 20)
    )
    content = render_report_image(tmp_path).read_bytes()
    width = struct.unpack(">I", content[16:20])[0]
    assert width == 920


def test_long_image_hides_standard_desktop_navigation(tmp_path):
    rendered = TEMPLATE.read_text()
    values = {
        "TITLE": "Fixture",
        "SUBTITLE": "",
        "LEAD": "Long report",
        "ATTRIBUTION": "Fixture source",
        "VIDEO_DESCRIPTION": "",
        "BODY": (
            '<nav class="report-nav" aria-label="章节导航">'
            '<span class="report-nav-title">章节 · 2</span>'
            '<a href="#s1">现象</a><a href="#s2">无状态</a></nav>'
            '<section id="s1"><h2><span class="num">1</span>'
            '<span class="section-title">现象</span></h2><p>正文</p></section>'
        ),
        "SOURCES": "Fixture source mapping",
    }
    for name, value in values.items():
        rendered = rendered.replace("{{" + name + "}}", value)
    (tmp_path / "report.html").write_text(rendered)

    observed = {}

    def check(page, _loading_errors):
        assert page.locator(".report-theme-toggle").evaluate("el => !el.hidden")
        assert not page.locator(".report-theme-toggle").is_visible()
        observed["display"] = page.locator(".report-nav").evaluate(
            "el => getComputedStyle(el).display"
        )
        observed["paper_width"] = page.locator(".paper").evaluate(
            "el => Math.round(el.getBoundingClientRect().width)"
        )

    render_report_image(tmp_path, on_page=check)
    assert observed == {"display": "none", "paper_width": 860}
