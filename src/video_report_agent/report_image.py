"""Render the desktop report as a full-page PNG without a model call."""

import struct
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.sync_api import sync_playwright

REPORT_CSP = (
    "sandbox allow-scripts; default-src 'none'; "
    "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "img-src 'self' data:; font-src data:"
)
REPORT_IMAGE_WIDTH = 920


def _has_current_width(output: Path) -> bool:
    """Avoid stale wide screenshots while keeping the image generation cached."""
    try:
        header = output.read_bytes()[:24]
        width = struct.unpack(">I", header[16:20])[0]
    except (OSError, struct.error):
        return False
    return header[:8] == b"\x89PNG\r\n\x1a\n" and width == REPORT_IMAGE_WIDTH


def render_report_image(
    run: Path, *, refresh: bool = False, output_name: str = "report.png", on_page=None,
    report_file: Path | None = None,
) -> Path:
    run = run.resolve()
    output = run / output_name
    if not refresh and output.is_file() and _has_current_width(output):
        return output
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": REPORT_IMAGE_WIDTH, "height": 900},
                                    device_scale_factor=1, service_workers="block", color_scheme="light")
            loading_errors = []
            page.on("requestfailed", lambda request: loading_errors.append(request.url))
            page.on("pageerror", lambda error: loading_errors.append(str(error)))

            def serve(route):
                url = urlsplit(route.request.url)
                target = (run / unquote(url.path).lstrip("/")).resolve()
                allowed = target == run / "report.html" or target.is_relative_to(run / "assets")
                if url.netloc == "report.local" and allowed and target.is_file():
                    served = target
                    if target == run / "report.html" and report_file:
                        served = report_file
                    route.fulfill(path=str(served), headers={"Content-Security-Policy": REPORT_CSP})
                else:
                    route.abort()

            page.route("**/*", serve)
            page.goto("http://report.local/report.html", wait_until="load")
            page.add_style_tag(content=".report-theme-toggle { visibility: hidden }")
            page.evaluate("""async () => {
                await document.fonts.ready;
                await Promise.all([...document.images].map(image => {
                    image.loading = 'eager';
                    return image.decode().catch(() => {});
                }));
            }""")
            if on_page is not None:
                on_page(page, loading_errors)
            # Capture in memory so a failed capture never leaves a partial PNG.
            content = page.screenshot(full_page=True, animations="disabled", timeout=60000)
            output.write_bytes(content)
        finally:
            browser.close()
    return output
