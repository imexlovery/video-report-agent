"""The single style pass preserves report structure and retains safe local edits."""
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from video_report_agent import style_edit
from video_report_agent.pi import PiRunner
from video_report_agent.usage import call_costs

REPORT = '''<!doctype html><html><head><style>.x{color:red}</style></head><body>
<h2>标题</h2><p data-source-units="unit-000001">讲述者强调，先检查两次。</p>
<p data-source-units="unit-000002">仅在密封时升温，压强才会增大 <strong>3</strong>%。</p>
<p>公式 <code>x=2</code>，<a href="https://example.test/">来源</a>。</p>
<figure><svg><text>图示</text></svg></figure><script>const x=1;</script>
</body></html>'''


def prepare(tmp_path):
    (tmp_path / "report.html").write_text(REPORT)
    (tmp_path / "transcript.md").write_text(
        "[unit-000001 | 0–1s] 先检查两次。\n[unit-000002 | 1–2s] 仅在密封时。\n")
    (tmp_path / "input.json").write_text('{}')
    return PiRunner(provider="fixture", model="fixture", isolation="local")


def test_merge_restores_only_block_with_changed_number():
    candidate = REPORT.replace("讲述者强调，", "").replace(">3</strong>", ">4</strong>")
    result, checks = style_edit.merge_style_change(REPORT, candidate)
    assert "先检查两次" in result and "讲述者强调" not in result
    assert ">3</strong>" in result and ">4</strong>" not in result
    assert checks["numeric_or_formula_blocks_restored"] == 1
    assert checks["semantic_review"] == "not_automatically_verified"


def test_merge_protects_titles_links_diagrams_and_rejects_changed_markup():
    candidate = REPORT.replace("讲述者强调，", "").replace("标题", "新标题")
    candidate = candidate.replace("图示", "新图示").replace(">来源</a>", ">其他</a>")
    result, checks = style_edit.merge_style_change(REPORT, candidate)
    assert "讲述者强调" not in result
    assert "新标题" not in result and "新图示" not in result and ">其他</a>" not in result
    assert checks["protected_text_slots_restored"] == 3
    candidate = candidate.replace("unit-000001", "unit-000099").replace(
        'href="https://example.test/"', 'href="https://other.test/"')
    result, checks = style_edit.merge_style_change(REPORT, candidate)
    assert "unit-000099" not in result and "https://other.test/" not in result
    assert "讲述者强调" in result
    assert checks["attribute_blocks_restored"] == 1


def test_parent_source_changes_restore_descendants_but_not_other_blocks():
    before = ('<div data-source-units="unit-1"><p>第一段。</p></div>'
              '<p data-source-units="unit-2">讲述者提到，第二段。</p>')
    candidate = (before.replace("unit-1", "unit-3").replace("第一段", "其他段")
                 .replace("讲述者提到，", ""))
    result, _ = style_edit.merge_style_change(before, candidate)
    assert "unit-3" not in result and "其他段" not in result
    assert "第一段" in result and "讲述者提到" not in result


def test_inline_structure_change_restores_only_affected_block():
    before = ('<section><p>讲述者指出，第一段。</p>'
              '<p>讲述者指出，第二段。</p></section>')
    candidate = before.replace('第一段', '<strong>第一段</strong>').replace('讲述者指出，', '')
    result, checks = style_edit.merge_style_change(before, candidate)
    assert '<p>讲述者指出，第一段。</p>' in result
    assert '<p>第二段。</p>' in result
    assert '<strong>' not in result
    assert checks["structure_blocks_restored"] == 1


def test_inline_recovery_preserves_sibling_container_prose():
    before = ('<div>讲者说，组件说明。<p>原句。</p></div>'
              '<p>讲者说，另一段。</p>')
    candidate = (before.replace('讲者说，', '')
                 .replace('原句', '<em>原句</em>'))
    result, _ = style_edit.merge_style_change(before, candidate)
    assert '<div>组件说明。<p>原句。</p></div>' in result
    assert '<p>另一段。</p>' in result


def test_inline_recovery_handles_repeated_container_structures():
    before = ''.join(f'<div>原开{i}<p>原段{i}</p>原尾{i}</div>' for i in range(4))
    candidate = before.replace('原开', '新开').replace('原尾', '新尾')
    candidate = candidate.replace('原段1', '<em>原段1</em>')
    result, checks = style_edit.merge_style_change(before, candidate)
    assert '<em>' not in result and '<p>原段1</p>' in result
    assert '新开0' in result and '新尾3' in result
    assert checks['structure_blocks_restored'] == 1


def test_inline_change_does_not_bypass_parent_source_protection():
    before = ('<div data-source-units="unit-1"><p>原内容。</p></div>'
              '<p>讲述者指出，第二段。</p><p>讲述者说，第三段。</p>')
    candidate = (before.replace('unit-1', 'unit-3').replace('原内容', '换内容')
                 .replace('第二段', '<strong>第二段</strong>')
                 .replace('讲述者说，', ''))
    result, _ = style_edit.merge_style_change(before, candidate)
    assert 'unit-1' in result and '原内容' in result and 'unit-3' not in result
    assert '<strong>' not in result and '讲述者说' not in result


@pytest.mark.parametrize("before,after", [
    ("每天服用一片", "每天服用两片"),
    ("甲组20%，乙组30%", "甲组30%，乙组20%"),
    ("剂量为100毫克", "剂量为100微克"),
    ("累计观看10万", "累计观看10亿"),
])
def test_numeric_changes_are_restored_by_block(before, after):
    original = f"<p>{before}</p><p>讲述者指出，另一段。</p>"
    candidate = original.replace(before, after).replace("讲述者指出，", "")
    result, checks = style_edit.merge_style_change(original, candidate)
    assert before in result and "讲述者指出" not in result
    assert checks["numeric_or_formula_blocks_restored"] == 1


def test_component_text_can_change_without_modifying_markup():
    original = '<div class="brief-fact"><span>视频称，内容清楚。</span></div>'
    candidate = original.replace("视频称，", "")
    assert style_edit.merge_style_change(original, candidate)[0] == candidate


def test_natural_rephrasing_not_rejected_as_changed_number_or_condition():
    before = "<p>一个人讲述一个假设，这两种含义由一套系统解释。</p>"
    after = "<p>假设两种含义由系统解释。</p>"
    result, checks = style_edit.merge_style_change(before, after)
    assert result == after
    assert checks["numeric_or_formula_blocks_restored"] == 0


def test_meta_source_is_preserved_but_viewpoint_note_may_be_edited():
    before = '<p class="meta">原作者<a href="/v">视频</a><br>依据转写整理；正文我指作者。</p>'
    after = before.replace("原作者", "其他人").replace("；正文我指作者", "")
    result, _ = style_edit.merge_style_change(before, after)
    assert result == before.replace("；正文我指作者", "")


def test_edit_keeps_original_candidate_and_partial_delivery(tmp_path, monkeypatch):
    runner = prepare(tmp_path)
    candidate = REPORT.replace("讲述者强调，", "").replace(">3</strong>", ">4</strong>")

    async def change(_run, stage, *_args):
        (stage / "report.html").write_text(candidate)

    monkeypatch.setattr(style_edit, "_execute", change)
    result = style_edit.edit_report(tmp_path, runner)
    assert result["status"] == "applied"
    assert not (tmp_path / "inspection").exists()
    assert result["numeric_or_formula_blocks_restored"] == 1
    assert (tmp_path / "style/before.html").read_text() == REPORT
    assert (tmp_path / "style/candidate.html").read_text() == candidate
    delivered = (tmp_path / "style/delivered.html").read_text()
    assert delivered == (tmp_path / "report.html").read_text()
    assert "讲述者强调" not in delivered and ">3</strong>" in delivered
    assert ">4</strong>" not in delivered


def test_interrupted_edit_keeps_candidate_and_original(tmp_path):
    prepare(tmp_path)
    style = tmp_path / "style"
    style.mkdir()
    (style / "before.html").write_text(REPORT)
    (style / "result.json").write_text('{"status":"fallback","reason":"interrupted"}')
    stage = tmp_path / ".style-work"
    stage.mkdir()
    (stage / "report.html").write_text(REPORT.replace("讲述者强调，", ""))
    assert style_edit.recover_interrupted_style(tmp_path)
    assert (style / "candidate.html").read_text() != REPORT
    assert (style / "delivered.html").read_text() == REPORT
    assert (tmp_path / "report.html").read_text() == REPORT


def test_interrupted_inspection_does_not_replace_original_candidate(tmp_path):
    prepare(tmp_path)
    style = tmp_path / "style"
    style.mkdir()
    (style / "before.html").write_text(REPORT)
    (style / "result.json").write_text('{"status":"fallback","reason":"interrupted"}')
    (style / "candidate.html").write_text("original model candidate")
    stage = tmp_path / ".style-work"
    stage.mkdir()
    (stage / "report.html").write_text("partially merged candidate")
    assert style_edit.recover_interrupted_style(tmp_path)
    assert (style / "candidate.html").read_text() == "original model candidate"
    assert (style / "delivered.html").read_text() == REPORT


@pytest.mark.skipif(shutil.which("pi") is None, reason="Pi is not installed")
@pytest.mark.parametrize("has_routing", [False, True])
def test_real_pi_edit_session_with_source_lookup_and_usage(tmp_path, monkeypatch, has_routing):
    class Handler(BaseHTTPRequestHandler):
        calls = 0

        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            Handler.calls += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if Handler.calls == 1:
                tool, args = "read_source_units", {"ids": ["unit-000001"]}
            elif Handler.calls == 2:
                tool, args = "edit", {"path": "report.html", "edits": [{
                    "oldText": "讲述者强调，先检查两次。", "newText": "先检查两次。"}]}
            else:
                tool, args = None, None
            delta = ({"tool_calls": [{"index": 0, "id": f"call-{Handler.calls}",
                                      "type": "function", "function": {"name": tool,
                                      "arguments": json.dumps(args, ensure_ascii=False)}}]}
                     if tool else {"content": "Done"})
            for payload in (
                {"id": "stub", "object": "chat.completion.chunk", "created": 1,
                 "model": "style-stub", "choices": [{"index": 0, "delta": delta,
                 "finish_reason": None}]},
                {"id": "stub", "object": "chat.completion.chunk", "created": 1,
                 "model": "style-stub", "choices": [{"index": 0, "delta": {},
                 "finish_reason": "tool_calls" if tool else "stop"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}},
            ):
                self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = tmp_path / "config"
    config.mkdir()
    (config / "models.json").write_text(json.dumps({"providers": {"style-local": {
        "baseUrl": f"http://127.0.0.1:{server.server_port}/v1", "api": "openai-completions",
        "apiKey": "local-only", "models": [{"id": "style-stub", "name": "Style stub",
        "reasoning": False, "input": ["text"], "contextWindow": 128000, "maxTokens": 4096,
        "cost": {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0}}]}}}))
    run = tmp_path / "run"
    run.mkdir()
    prepare(run)
    if has_routing:
        (run / "model-attempts.jsonl").write_text(
            '{"kind":"request_started","attempt":5,"model":"main"}\n')
    monkeypatch.setattr("video_report_agent.pi.PI_AGENT_DIR", config)
    monkeypatch.setattr(style_edit, "PI_AGENT_DIR", config)
    try:
        runner = PiRunner(provider="style-local", model="style-stub", api_key="local-only",
                          thinking="off", timeout=30, isolation="local")
        result = style_edit.edit_report(run, runner)
        assert result["status"] == "applied", result
        assert "讲述者强调" not in (run / "report.html").read_text()
        events = [json.loads(line) for line in (run / "pi.events.jsonl").read_text().splitlines()]
        assert any(e.get("toolName") == "read_source_units" for e in events)
        assert all(e.get("_agent_role") == "style" for e in events)
        assert call_costs(run, {"transcript_reused_from": "fixture"}, None)["llm_calls"] == 3
        attempts = run / "model-attempts.jsonl"
        if has_routing:
            records = [json.loads(line) for line in attempts.read_text().splitlines()]
            assert [e["attempt"] for e in records if e["kind"] == "request_finished"] == [6, 7, 8]
            assert all(e.get("agent_role") == "style" for e in records
                       if e["kind"] == "request_started" and e["attempt"] > 5)
        else:
            assert not attempts.exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
