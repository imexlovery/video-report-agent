import json

import pytest

from video_report_agent.inspect_report import content_candidates, inspect_report


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


def blocks(*texts):
    return [{"text": text, "section": f"问题{i}", "selector": f"p:nth-of-type({i})",
             "source_units": f"u{i}", "tag": "p"} for i, text in enumerate(texts, 1)]


@pytest.mark.parametrize("text,expected", [
    ("讲者提到，只有密封时，升温才会使容器内压强增大。", "possible_third_person_narration"),
    ("视频中提到，只有密封时才成立。", "possible_third_person_narration"),
    ("视频叙述了迁移中的事故。", "possible_third_person_narration"),
    ("在视频作者的说明中，两种像沿同一视线排列。", "possible_third_person_narration"),
    ("本报告介绍容器内压强的变化。", "possible_meta_narration"),
    ("只有密封时，升温才会使容器内压强增大。", None),
    ("代表甲提出方案，乙方负责人乙接受了其中两项条件。", None),
    ("李明回忆，王宁曾转述项目负责人陈原的建议，陈原未参加当时的试验。", None),
    ("仅在同一硬件和相同负载下，响应时间才从约20毫秒降至15毫秒。", None),
])
def test_surface_checks_cover_direct_teaching_entities_and_conditions(text, expected):
    findings = content_candidates(blocks(text))
    assert [f["kind"] for f in findings] == ([expected] if expected else [])


def test_necessary_attribution_is_a_review_candidate_not_a_failure_or_edit():
    text = "视频作者林青试用两周后认为更方便，但没有计时；能否推广到其他团队仍不确定。"
    material = blocks(text)
    findings = content_candidates(material, "林青")
    assert {f["kind"] for f in findings} == {
        "possible_author_as_subject", "possible_third_person_narration"}
    assert findings[0]["certainty"] == "candidate"
    assert findings[0]["source_units"] == "u1"
    assert "不机械删词" in findings[0]["hint"]
    assert material[0]["text"] == text
    # A quoted phrase can legitimately contain the pattern; it is not a semantic verdict.
    quoted = content_candidates(blocks('原文用“作者指出”引出陈原的意见。'))
    assert quoted[0]["certainty"] == "candidate"


def test_repeated_discussion_locates_both_sections_without_erasing_conditions():
    sentence = "只有在同一硬件和相同负载条件下，平均响应时间才从约20毫秒降至15毫秒。"
    findings = content_candidates(blocks(sentence, "新增说明。" + sentence))
    assert len(findings) == 1
    finding = findings[0]
    assert finding["kind"] == "possible_repeated_text"
    assert finding["section"] == "问题2"
    assert finding["previous"]["section"] == "问题1"
    assert finding["previous"]["selector"] != finding["selector"]
    assert content_candidates(blocks(sentence, sentence.replace("15毫秒", "16毫秒"))) == []
    assert content_candidates(blocks(sentence, sentence.replace("才从", "也未从"))) == []
    # Repetition inside a paragraph is also visible, without counting a single sentence twice.
    assert len(content_candidates(blocks(sentence + sentence))) == 1
    assert content_candidates(blocks(sentence)) == []


def test_browser_content_locations_exclude_sources_and_include_components(tmp_path):
    repeated = "只有在同一硬件和相同负载条件下，平均响应时间才从约20毫秒降至15毫秒。"
    (tmp_path / "transcript.md").write_text("[u1 | 0–1s] source")
    (tmp_path / "input.json").write_text('{"uploader":"林青"}')
    (tmp_path / "report.html").write_text(f'''<html><head><meta charset="utf-8"></head><body>
      <header><h1>响应时间</h1><p class="meta">林青；本文</p></header>
      <nav><h2>本报告目录</h2>{repeated}</nav>
      <details open><summary>视频简介（展开）</summary><p>林青指出，本报告</p></details>
      <section data-source-units="u1"><h2>一、测量条件</h2><h3>固定负载</h3>
        <p>讲者\n<strong>提到</strong>，本\n<strong>报告</strong>介绍测量条件。</p>
        <p>{repeated}</p>
        <div hidden><p>林青认为，本文</p></div>
        <div style="display:none"><p>作者指出，本报告</p></div>
      </section>
      <section data-source-units="u1"><h2>二、结果</h2>
        <div class="card"><p>{repeated}</p></div>
        <svg width="400" height="80"><text x="5" y="30">本图展示响应时间</text></svg>
        <p>视频作者林青试用两周后感觉更方便，但未计时。</p>
      </section>
      <details open><summary>来源说明</summary><p>林青；本报告</p></details>
      <footer>林青；本报告</footer>
    </body></html>''')
    result = inspect_report(tmp_path)
    assert result["status"] == "checked", result.get("error")
    assert result["semantic_review"] == "not_performed"
    candidates = [f for f in result["findings"] if f.get("certainty") == "candidate"]
    assert len(candidates) == 6
    assert candidates[0]["section"] == "一、测量条件"
    assert candidates[0]["subsection"] == "固定负载"
    assert candidates[0]["source_units"] == "u1"
    assert all(f["selector"].startswith("body > ") for f in candidates)
    assert any(f["tag"] == "text" for f in candidates)
    assert sum(f["kind"] == "possible_author_as_subject" for f in candidates) == 1
    duplicate = next(f for f in candidates if f["kind"] == "possible_repeated_text")
    assert duplicate["section"] == "二、结果"
    assert duplicate["previous"]["section"] == "一、测量条件"
