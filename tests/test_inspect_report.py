import json

from video_report_agent.inspect_report import inspect_report


def test_browser_finds_defects_and_rechecks_current_report(tmp_path):
    (tmp_path / "transcript.md").write_text("[unit-1 | 0–1s] 内容")
    (tmp_path / "report.html").write_text('''<html><body>
      <p data-source-units="missing" style="width:1400px">正文</p>
      <div style="height:10px;overflow:hidden">第一行<br>第二行<br>第三行</div>
      <img src="missing.png">
      <details><summary>来源</summary><div style="width:2000px">折叠内容</div></details>
    </body></html>''')
    first = inspect_report(tmp_path, "before")
    assert first["status"] == "checked", first.get("error")
    kinds = {f["kind"] for f in first["findings"]}
    assert {"horizontal_overflow", "invalid_source_ids", "possible_vertical_clipping",
            "broken_image", "resource_or_script_error"} <= kinds
    assert not any(f.get("text") == "折叠内容" for f in first["findings"])
    (tmp_path / "report.html").write_text(
        '<html><body><p data-source-units="unit-1">修复后的正文</p></body></html>'
    )
    second = inspect_report(tmp_path, "after")
    assert second["status"] == "checked"
    assert second["findings"] == []
    assert (tmp_path / "inspection/before/report.html").read_text() != (
        tmp_path / "inspection/after/report.html").read_text()
    assert len(second["screenshots"]) == 3


def test_browser_failure_is_not_a_pass(tmp_path, monkeypatch):
    (tmp_path / "report.html").write_text("<html><body>test</body></html>")
    (tmp_path / "transcript.md").write_text("test")

    def fail(*args, **kwargs):
        raise RuntimeError("browser unavailable")

    monkeypatch.setattr("video_report_agent.inspect_report.render_report_image", fail)
    result = inspect_report(tmp_path)
    assert result["status"] == "error"
    assert "findings" not in result
    assert json.loads((tmp_path / "inspection/final/result.json").read_text())["status"] == "error"


def test_inspection_only_fills_preview_not_agent_file(tmp_path):
    original = ('<html><body>{{VIDEO_DESCRIPTION}}'
                '<p data-source-units="unit-1">正文</p></body></html>')
    (tmp_path / "report.html").write_text(original)
    (tmp_path / "transcript.md").write_text("[unit-1 | 0–1s] 正文")
    (tmp_path / "download").mkdir()
    (tmp_path / "download/source.info.json").write_text('{"description":"简介"}')
    result = inspect_report(tmp_path)
    assert result["status"] == "checked"
    assert (tmp_path / "report.html").read_text() == original
    assert (tmp_path / "inspection/final/source.html").read_text() == original
    preview = (tmp_path / "inspection/final/report.html").read_text()
    assert "{{VIDEO_DESCRIPTION}}" not in preview
    assert "<details><summary>视频简介（展开）</summary>" in preview
    assert "data-video-description" not in preview

    # A completed review can leave the filled report in place; the final inspect
    # must fill the existing boundary again without duplicating the disclosure.
    (tmp_path / "report.html").write_text(preview)
    final = inspect_report(tmp_path, "after-review")
    assert final["status"] == "checked"
    final_preview = (tmp_path / "inspection/after-review/report.html").read_text()
    assert final_preview == preview
    assert final_preview.count("视频简介（展开）") == 1


def test_visual_references_use_the_observation_manifest(tmp_path):
    (tmp_path / "transcript.md").write_text("[u1 | 0–1s] 内容")
    (tmp_path / "visual").mkdir()
    (tmp_path / "visual/observations.jsonl").write_text('{"frame_id":"frame-000001"}\n')
    (tmp_path / "report.html").write_text('<html><body><p data-source-units="u1" '
        'data-source-frames="frame-000001">图文内容</p>'
        '<p data-source-frames="missing">错误引用</p></body></html>')
    result = inspect_report(tmp_path)
    assert result["frame_id_count"] == 1
    assert result["frame_reference_elements"] == 2
    assert [x["invalid_ids"] for x in result["findings"]
            if x["kind"] == "invalid_frame_ids"] == [["missing"]]
