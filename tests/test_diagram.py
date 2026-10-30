"""Isolated drawing, embedding, and usage; local Pi/stub checks make no paid calls."""

import asyncio
import json
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from video_report_agent.diagram import PLACEHOLDER, diagram_model_config, embed_diagram
from video_report_agent.inspect_report import inspect_report
from video_report_agent.pi import PiRunner
from video_report_agent.pipeline import create_run
from video_report_agent.task_container import snapshot_models, stage_inputs
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


def test_drawing_configuration_is_saved_at_creation_and_staged(tmp_path, monkeypatch):
    selection = {"provider": "drawing", "model": "drawing-model", "thinking": "high"}
    for key, value in selection.items():
        monkeypatch.setenv(f"DIAGRAM_{key.upper()}", value)
    run = create_run(tmp_path, "BV1aTtb6uE7d",
                     model_selection={"provider": "main", "model": "main"})
    monkeypatch.setenv("DIAGRAM_MODEL", "changed-after-queueing")
    (run / "transcript.md").write_text("source")
    staged = json.loads((stage_inputs(run) / "input.json").read_text())
    metadata = json.loads((run / "input.json").read_text())
    assert metadata["diagram_model_selection"] == staged["diagram_model_selection"] == selection
    assert metadata["model_selection"] == {"provider": "main", "model": "main"}
    inherit = create_run(tmp_path, "BV1aTtb6uE7d", diagram_model_selection={})
    assert "diagram_model_selection" not in json.loads((inherit / "input.json").read_text())
    disabled = create_run(tmp_path, "BV1aTtb6uE7d", diagrams=False)
    assert "diagram_model_selection" not in json.loads((disabled / "input.json").read_text())


@pytest.mark.parametrize("selection", [
    {"provider": "drawing"}, {"model": "model"}, {"thinking": "low"},
    {"provider": "drawing", "model": "model", "api_key": "secret"},
    {"provider": "drawing", "model": "model", "thinking": "invalid"},
    {"provider": "drawing", "model": None}, [],
])
def test_invalid_drawing_configuration_is_rejected(selection):
    with pytest.raises(ValueError):
        diagram_model_config(selection)


@pytest.mark.skipif(shutil.which("pi") is None, reason="Pi is not installed")
@pytest.mark.parametrize("enabled,cancel,auth", [
    (True, False, "runtime_key"), (False, False, "runtime_key"),
    (True, True, "runtime_key"), (True, False, "stored_key"),
    (True, False, "oauth"),
    (True, False, "oauth_to_key"), (True, False, "key_to_oauth"),
    (True, False, "key_to_key"), (True, True, "key_to_oauth"),
    (True, False, "key_to_same_provider"),
    (True, False, "legacy_no_input"),
])
def test_real_pi_isolated_diagram_and_default_interface(
    tmp_path, monkeypatch, enabled, cancel, auth, *, isolation="local", evidence=None,
):
    requests = []
    authorizations = []
    child_started, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            authorizations.append(self.headers.get("Authorization"))
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
                        "model": body["model"], "choices": [
                            {"index": 0, "delta": change, "finish_reason": finish}]}
                if finish:
                    data["usage"] = {"prompt_tokens": 10, "completion_tokens": 20,
                                     "total_tokens": 30}
                self.wfile.write(("data: " + json.dumps(data) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("0.0.0.0" if isolation == "docker" else "127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = tmp_path / "config"
    config.mkdir()
    # Keep native Codex OAuth validation, with completions served only by the local fixture.
    independent = "_to_" in auth
    provider = "openai-codex" if auth in {"oauth", "oauth_to_key"} else "diagram-local"
    key = "synthetic-oauth-access" if provider == "openai-codex" else "ephemeral-key"
    drawing_provider = ("openai-codex" if auth == "key_to_oauth" else "drawing-local"
                        ) if independent else provider
    if auth == "key_to_same_provider":
        drawing_provider = provider
    drawing_model = "drawing-stub" if independent else "diagram-stub"
    drawing_key = "separate-drawing-key" if independent else key
    if drawing_provider == provider:
        drawing_key = key
    models, credentials = {}, {}
    host = "host.docker.internal" if isolation == "docker" else "127.0.0.1"
    roles = [(provider, "diagram-stub", key)]
    if independent:
        roles.append((drawing_provider, drawing_model, drawing_key))
    for role_provider, role_model, role_key in roles:
        model_config = models.setdefault(role_provider, {
            "baseUrl": f"http://{host}:{server.server_port}/v1", "api": "openai-completions",
            "models": [],
        })
        model_config["models"].append({
            "id": role_model, "name": "Fixture", "reasoning": independent,
            "input": ["text"], "contextWindow": 128000, "maxTokens": 4096,
            "cost": {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0},
        })
        if role_provider == "openai-codex":
            credentials[role_provider] = {
                "type": "oauth", "access": role_key, "refresh": "synthetic-refresh",
                "expires": int(time.time() * 1000) + 3_600_000, "accountId": "synthetic-account",
            }
        elif auth == "stored_key" or (
            independent and role_provider == drawing_provider and drawing_provider != provider
        ):
            credentials[role_provider] = {"type": "api_key", "key": role_key}
    (config / "models.json").write_text(json.dumps({"providers": models}))
    (config / "auth.json").write_text(json.dumps(credentials))
    monkeypatch.setattr("video_report_agent.pi.PI_AGENT_DIR", config)
    run = create_run(tmp_path / "runs", "BV1aTtb6uE7d", diagrams=enabled,
                     diagram_model_selection={"provider": drawing_provider, "model": drawing_model,
                                              "thinking": "high"} if independent else {})
    if enabled:
        # Existing callers omit the flag; omission must also enable the tool.
        metadata = json.loads((run / "input.json").read_text())
        metadata.pop("diagrams")
        (run / "input.json").write_text(json.dumps(metadata))
    (run / "transcript.md").write_text(
        "[unit-000001 | 0–1s] 必要原文\n[unit-000002 | 1–2s] FULL_TRANSCRIPT_ONLY\n"
    )
    if auth == "legacy_no_input":
        (run / "input.json").unlink()
    try:
        runner = PiRunner(provider=provider, model="diagram-stub",
                          api_key=key if provider not in credentials else None,
                          thinking="off", timeout=90 if isolation == "docker" else 40,
                          isolation=isolation)
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
        assert authorizations == [f"Bearer {drawing_key if not body.get('tools') else key}"
                                  for body in requests]
        assert json.loads((config / "auth.json").read_text()) == credentials
        if auth == "key_to_key" and isolation == "local":
            stage = stage_inputs(run)
            snapshot = run / ".generation-config"
            snapshot_models(snapshot, config, [
                {"provider": provider, "model": "diagram-stub", "api_key": key},
                {"provider": drawing_provider, "model": drawing_model},
            ])
            assert json.loads((snapshot / "auth.json").read_text()) == {
                provider: {"type": "api_key", "key": key},
                drawing_provider: {"type": "api_key", "key": drawing_key},
            }
            staged = (stage / "input.json").read_text()
            assert key not in staged and drawing_key not in staged
        if enabled:
            assert 'data-report-diagram="true"' in report.read_text()
            assert len([e for e in drawing if e["type"] == "message_end"]) == 1
            child = next(body for body in requests if not body.get("tools"))
            assert child["model"] == drawing_model
            assert next(e for e in drawing if e["type"] == "request_start")["provider"] == (
                drawing_provider
            )
            if independent:
                assert child["reasoning_effort"] == "high"
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
        if evidence is not None:
            evidence.update(
                isolation=json.loads((run / "invocation.json").read_text())["isolation"],
                independent=independent,
                requests=[{"role": "main" if body.get("tools") else "drawing",
                           "model": body["model"],
                           "credential": "main" if header == f"Bearer {key}" else "drawing"}
                          for body, header in zip(requests, authorizations, strict=True)],
                diagram_embedded='data-report-diagram="true"' in report.read_text(),
                run=str(run),
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
