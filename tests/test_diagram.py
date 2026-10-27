"""Isolated drawing, embedding, and usage; local Pi/stub checks make no paid calls."""

import asyncio
import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from video_report_agent.diagram import PLACEHOLDER, embed_diagram
from video_report_agent.inspect_report import inspect_report
from video_report_agent.pi import PiRunner
from video_report_agent.pipeline import create_run
from video_report_agent.task_container import stage_inputs
from video_report_agent.usage import call_costs

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 200">'
       '<defs><marker id="arrow"><path d="M0 0 L4 2 L0 4"/></marker></defs>'
       '<path d="M10 50 L200 50" marker-end="url(#arrow)"/>'
       '<text x="20" y="100">局部关系</text></svg>')


@pytest.mark.parametrize("reference", ["url(#arrow)", "url('#arrow')", 'url( "#arrow" )'])
def test_embedding_and_missing_position(tmp_path, reference):
    diagram = tmp_path / "assets/diagram"
    diagram.mkdir(parents=True)
    (diagram / "request.json").write_text(json.dumps({
        "source_units": ["unit-000001"], "caption": "来源 <一>",
    }))
    (diagram / "diagram.svg").write_text(
        SVG.replace("url(#arrow)", reference.replace('"', "&quot;"))
    )
    report = tmp_path / "report.html"
    report.write_text(f"<html><body>before{PLACEHOLDER}after</body></html>")
    embed_diagram(report, tmp_path)
    result = report.read_text()
    assert result.index("before") < result.index("<svg") < result.index("after")
    assert 'data-source-units="unit-000001"' in result
    assert "来源 &lt;一&gt;" in result
    assert 'id="report-diagram-arrow"' in result
    assert 'url(#report-diagram-arrow)' in result
    assert PLACEHOLDER not in result
    embed_diagram(report, tmp_path)
    assert report.read_text() == result
    report.write_text("<html><body>forgot the diagram</body></html>")
    with pytest.raises(ValueError, match="missing"):
        embed_diagram(report, tmp_path)


@pytest.mark.parametrize("element", [
    '<script>alert(1)</script>', '<style>body{display:none}</style>',
    '<image href="https://example.test/a.png"/>', '<path onload="alert(1)"/>',
    '<path fill="url(https://example.test/a.svg#x)"/>',
])
def test_svg_cannot_load_resources_or_change_the_report(tmp_path, element):
    diagram = tmp_path / "assets/diagram"
    diagram.mkdir(parents=True)
    (diagram / "request.json").write_text('{"source_units": ["u1"], "caption": "test"}')
    (diagram / "diagram.svg").write_text(SVG.replace("</svg>", element + "</svg>"))
    report = tmp_path / "report.html"
    report.write_text(PLACEHOLDER)
    with pytest.raises(ValueError):
        embed_diagram(report, tmp_path)


def test_inspection_previews_embedded_diagram_without_rewriting_parent(tmp_path):
    diagram = tmp_path / "assets/diagram"
    diagram.mkdir(parents=True)
    (diagram / "request.json").write_text('{"source_units": ["u1"], "caption": "test"}')
    (diagram / "diagram.svg").write_text(SVG)
    (tmp_path / "transcript.md").write_text("[u1 | 0–1s] source")
    report = tmp_path / "report.html"
    source = f"<html><body>{PLACEHOLDER}</body></html>"
    report.write_text(source)
    result = inspect_report(tmp_path)
    assert result["status"] == "checked"
    assert result["diagram_count"] == 1
    assert result["findings"] == []
    assert report.read_text() == source
    assert 'data-report-diagram="true"' in (tmp_path / "inspection/final/report.html").read_text()


@pytest.mark.parametrize("enabled", [True, False])
def test_diagram_flag_is_saved_and_staged(tmp_path, enabled):
    run = create_run(tmp_path, "BV1aTtb6uE7d", diagrams=enabled)
    assert json.loads((run / "input.json").read_text())["diagrams"] is enabled
    (run / "transcript.md").write_text("source")
    stage = stage_inputs(run)
    assert json.loads((stage / "input.json").read_text())["diagrams"] is enabled


@pytest.mark.skipif(shutil.which("pi") is None, reason="Pi is not installed")
@pytest.mark.parametrize("enabled,cancel", [(True, False), (False, False), (True, True)])
def test_real_pi_isolated_diagram_and_default_interface(tmp_path, monkeypatch, enabled, cancel):
    requests = []
    child_started, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            child = not body.get("tools")
            if child:
                child_started.set()
                if cancel:
                    release.wait(10)
                delta, reason = {"content": SVG}, "stop"
            elif not any(m.get("role") == "tool" for m in body["messages"]):
                if enabled:
                    name, args = "generate_diagram", {
                        "goal": "解释局部关系", "context": "两者的方向", "caption": "关系示意",
                        "source_units": ["unit-000001"],
                    }
                else:
                    name, args = "write", {"path": "report.html",
                                          "content": "<html><body>text only</body></html>"}
                delta = {"tool_calls": [{"index": 0, "id": "first", "type": "function",
                          "function": {"name": name, "arguments": json.dumps(args)}}]}
                reason = "tool_calls"
            elif enabled and not any(
                call.get("function", {}).get("name") == "read"
                for message in body["messages"] for call in message.get("tool_calls", [])
            ):
                delta = {"tool_calls": [{"index": 0, "id": "probe", "type": "function",
                         "function": {"name": "read", "arguments": json.dumps({
                             "path": "assets/diagram/diagram.svg",
                         })}}]}
                reason = "tool_calls"
            elif enabled and not any(
                call.get("function", {}).get("name") == "write"
                for message in body["messages"] for call in message.get("tool_calls", [])
            ):
                delta = {"tool_calls": [{"index": 0, "id": "write", "type": "function",
                         "function": {"name": "write", "arguments": json.dumps({
                             "path": "report.html",
                             "content": f"<html><body>{PLACEHOLDER}</body></html>",
                         })}}]}
                reason = "tool_calls"
            else:
                delta, reason = {"content": "Done"}, "stop"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for change, finish in [(delta, None), ({}, reason)]:
                data = {"id": "stub", "object": "chat.completion.chunk", "created": 1,
                        "model": "diagram-stub", "choices": [
                            {"index": 0, "delta": change, "finish_reason": finish}]}
                if finish:
                    data["usage"] = {"prompt_tokens": 10, "completion_tokens": 20,
                                     "total_tokens": 30}
                self.wfile.write(("data: " + json.dumps(data) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = tmp_path / "config"
    config.mkdir()
    (config / "models.json").write_text(json.dumps({"providers": {"diagram-local": {
        "baseUrl": f"http://127.0.0.1:{server.server_port}/v1", "api": "openai-completions",
        "models": [{"id": "diagram-stub", "name": "Fixture", "reasoning": False,
                    "input": ["text"], "contextWindow": 128000, "maxTokens": 4096,
                    "cost": {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0}}],
    }}}))
    monkeypatch.setattr("video_report_agent.pi.PI_AGENT_DIR", config)
    run = create_run(tmp_path / "runs", "BV1aTtb6uE7d", diagrams=enabled)
    if enabled:
        # Existing callers omit the flag; omission must also enable the tool.
        metadata = json.loads((run / "input.json").read_text())
        metadata.pop("diagrams")
        (run / "input.json").write_text(json.dumps(metadata))
    (run / "transcript.md").write_text(
        "[unit-000001 | 0–1s] 必要原文\n[unit-000002 | 1–2s] FULL_TRANSCRIPT_ONLY\n"
    )
    try:
        runner = PiRunner(provider="diagram-local", model="diagram-stub", api_key="ephemeral-key",
                          thinking="off", timeout=40, isolation="local")
        if cancel:
            async def interrupt():
                task = asyncio.create_task(runner.run(run))
                try:
                    async with asyncio.timeout(15):
                        while not child_started.is_set():
                            await asyncio.sleep(0.05)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                finally:
                    release.set()
            asyncio.run(interrupt())
            processes = subprocess.check_output(["ps", "-axo", "command="], text=True)
            assert str(run / "assets/diagram/sessions") not in processes
            assert not (run / ".diagram-config").exists()
            assert not (run / "report.html").exists()
            return
        report = asyncio.run(runner.run(run))
        events = [json.loads(line) for line in (run / "pi.events.jsonl").read_text().splitlines()]
        drawing = [e for e in events if e.get("_agent_role") == "diagram"]
        assert call_costs(run, {}, None)["llm_calls"] == (5 if enabled else 2)
        if enabled:
            assert 'data-report-diagram="true"' in report.read_text()
            assert len([e for e in drawing if e["type"] == "message_end"]) == 1
            child = next(body for body in requests if not body.get("tools"))
            assert "FULL_TRANSCRIPT_ONLY" not in json.dumps(child)
            assert "必要原文" in json.dumps(child, ensure_ascii=False)
            assert len(list((run / "assets/diagram/sessions").glob("*.jsonl"))) == 1
            # Parent receives a marker, never child SVG or session history as tool output.
            parent = [body for body in requests if body.get("tools")]
            assert all("<svg" not in json.dumps(body, ensure_ascii=False) for body in parent)
            assert any("图示源码和会话由独立工具管理" in json.dumps(body, ensure_ascii=False)
                       for body in parent)
        else:
            assert not drawing
            assert all(t["function"]["name"] != "generate_diagram"
                       for body in requests for t in body.get("tools", []))
        assert not (run / ".diagram-config").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
