"""Embed the isolated drawing result without asking the report agent to copy SVG."""

import html
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

PLACEHOLDER = "<!-- video-report-diagram -->"
SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)


def diagram_model_config(selection: dict | None = None) -> dict | None:
    """Snapshot an optional drawing model; credentials remain in Pi configuration."""
    if selection is None:
        selection = {
            key: os.getenv(f"DIAGRAM_{key.upper()}", "")
            for key in ("provider", "model", "thinking")
        }
    if not isinstance(selection, dict) or set(selection) - {"provider", "model", "thinking"}:
        raise ValueError("绘图模型配置只接受 provider、model 和 thinking")
    if any(not isinstance(value, str) for value in selection.values()):
        raise ValueError("绘图模型配置必须是字符串")
    configured = {key: value for key, value in selection.items() if value.strip()}
    if not configured:
        return None
    if not configured.get("provider") or not configured.get("model"):
        raise ValueError("独立绘图模型须同时配置 provider 和 model")
    if configured.get("thinking") not in {
        None, "off", "minimal", "low", "medium", "high", "xhigh", "max",
    }:
        raise ValueError("无效的绘图思考强度")
    return configured


def embed_diagram(report: Path, run: Path) -> None:
    content = report.read_text()
    directory = run / "assets" / "diagram"
    if PLACEHOLDER not in content:
        if (directory / "request.json").exists() and 'data-report-diagram="true"' not in content:
            raise ValueError("Requested diagram is missing from the report")
        return
    if content.count(PLACEHOLDER) != 1:
        raise ValueError("Expected one diagram position")
    request = json.loads((directory / "request.json").read_text())
    svg = ET.fromstring((directory / "diagram.svg").read_text())
    if svg.tag != f"{{{SVG_NS}}}svg" or not svg.get("viewBox"):
        raise ValueError("Diagram must be SVG with a viewBox")
    # Inline SVG must remain self-contained and must not style the surrounding report.
    for node in svg.iter():
        if node.tag.rsplit("}", 1)[-1].lower() in {"script", "style", "foreignobject", "image"}:
            raise ValueError("Unsupported diagram element")
        for key, value in node.attrib.items():
            key = key.rsplit("}", 1)[-1].lower()
            if key.startswith("on") or (key == "href" and not value.startswith("#")):
                raise ValueError("Diagram must not contain active or external resources")
            if any(not ref.strip(" \t'\"").startswith("#")
                   for ref in re.findall(r"url\(([^)]+)\)", value, re.I)):
                raise ValueError("Diagram must not contain external resources")
    ids = {node.get("id"): "report-diagram-" + node.get("id")
           for node in svg.iter() if node.get("id")}
    for node in svg.iter():
        for key, value in list(node.attrib.items()):
            if key == "id":
                node.set(key, ids[value])
            else:
                value = re.sub(
                    r"url\(\s*(['\"]?)#([^)'\"\s]+)\1\s*\)",
                    lambda match: f"url(#{ids.get(match[2], match[2])})", value,
                )
                if value.startswith("#") and value[1:] in ids:
                    value = "#" + ids[value[1:]]
                node.set(key, value)
    svg.set("width", "100%")
    svg.attrib.pop("height", None)
    svg.set("style", "display:block;width:100%;height:auto")
    sources = html.escape(" ".join(request["source_units"]), quote=True)
    figure = (f'<figure data-report-diagram="true" data-source-units="{sources}">'
              f'{ET.tostring(svg, encoding="unicode")}<figcaption>'
              f'{html.escape(request["caption"])}</figcaption></figure>')
    report.write_text(content.replace(PLACEHOLDER, figure))
