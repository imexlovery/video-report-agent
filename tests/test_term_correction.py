"""Synthetic offline checks; no live model calls or semantic-quality claims."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from video_report_agent.pi import PiError, PiRunner
from video_report_agent.pipeline import create_run
from video_report_agent.reuse import cache_key
from video_report_agent.storage import Store
from video_report_agent.term_correction import (
    CORRECTED_TRANSCRIPT,
    SOURCE_CONTEXT,
    TERM_DOCUMENTS,
    TERM_REVIEW,
    finish_lookup,
    prepare_term_correction,
    record_term_review,
    start_fetch,
    start_lookup,
)


@pytest.fixture
def workspace(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "download").mkdir()
    (run / "download/source.info.json").write_text(json.dumps({
        "title": "old title", "description": "薄肌、训练与健康", "uploader": "示例作者",
        "unused": "not included in source context",
    }))
    (run / "input.json").write_text(json.dumps({"term_correction": True}))
    (run / "transcript.md").write_text(
        "# 训练术语示例\n\n"
        "[unit-000001 | 0.000–5.000s] 💪开始练搏击，深蹲和硬拉。\n"
        "[unit-000002 | 5.000–10.000s] 搏击比赛的拳击规则。\n"
        "[unit-000003 | 10.000–15.000s] cloud帮我整理资料。\n"
        "[unit-000004 | 15.000–20.000s] 每天25克，我没有修改目标。\n"
    )
    (run / "asr.json").write_text('{"raw":"fixture"}')
    (run / "canonical-transcript.jsonl").write_text('{"original":"fixture"}\n')
    prepare_term_correction(run, {"title": "训练术语示例", "url": "https://example.org/video"})
    return run


def review(run):
    return json.loads((run / TERM_REVIEW).read_text())


def entry(unit_id="unit-000001", before="搏击", after="薄肌", **changes):
    text = {"unit-000001": "💪开始练搏击，深蹲和硬拉。",
            "unit-000003": "cloud帮我整理资料。",
            "unit-000004": "每天25克，我没有修改目标。"}[unit_id]
    start = text.index(before)
    return {
        "unit_id": unit_id, "start": start, "end": start + len(before),
        "before": before, "after": after, "kind": "term", "status": "supported",
        "evidence": [{"source_id": "metadata:description", "quote": "薄肌、训练与健康"}],
        "reason": "简介明确使用候选术语，局部内容涉及力量训练。",
        "confidence": "high", **changes,
    }


def test_metadata_patch_preserves_raw_files_ids_and_other_occurrences(workspace):
    raw = {name: (workspace / name).read_bytes()
           for name in ("transcript.md", "asr.json", "canonical-transcript.jsonl")}
    result = record_term_review(workspace, [entry()])
    assert result["supported"] == 1
    corrected = (workspace / CORRECTED_TRANSCRIPT).read_text()
    assert "💪开始练薄肌，深蹲和硬拉。" in corrected
    assert "[unit-000002 | 5.000–10.000s] 搏击比赛" in corrected
    assert "每天25克，我没有修改目标。" in corrected
    assert all((workspace / name).read_bytes() == body for name, body in raw.items())
    context = json.loads((workspace / SOURCE_CONTEXT).read_text())
    assert context["title"] == "训练术语示例"
    assert "unused" not in context


@pytest.mark.parametrize("changes,match", [
    ({"evidence": []}, "require source evidence"),
    ({"evidence": [{"source_id": "invented", "quote": "薄肌"}]}, "Evidence must quote"),
    ({"evidence": [{"source_id": "metadata:description", "quote": "模型认为是薄肌"}]},
     "Evidence must quote"),
    ({"after": "另一个词"}, "must appear"),
    ({"before": "错误定位"}, "Original text mismatch"),
    ({"kind": "opinion"}, "Only names and terms"),
    ({"end": 1000}, "Invalid character range"),
])
def test_unbacked_or_invalid_patch_is_not_applied(workspace, changes, match):
    original_review = (workspace / TERM_REVIEW).read_bytes()
    original_corrected = (workspace / CORRECTED_TRANSCRIPT).read_bytes()
    with pytest.raises(ValueError, match=match):
        record_term_review(workspace, [{**entry(), **changes}])
    assert (workspace / TERM_REVIEW).read_bytes() == original_review
    assert (workspace / CORRECTED_TRANSCRIPT).read_bytes() == original_corrected


@pytest.mark.parametrize("before,after", [("25", "30"), ("没有", "已经")])
def test_numbers_and_negations_cannot_use_term_patch(workspace, before, after):
    with pytest.raises(ValueError, match="outside term correction scope"):
        record_term_review(workspace, [entry("unit-000004", before, after)])


def test_candidate_retained_and_revised_judgment_rebuilds_from_original(workspace):
    record_term_review(workspace, [entry(status="candidate", evidence=[])])
    assert (workspace / CORRECTED_TRANSCRIPT).read_bytes() == (
        workspace / "transcript.md").read_bytes()
    record_term_review(workspace, [entry()])
    record_term_review(workspace, [entry(status="unresolved", evidence=[], reason="发现同名歧义")])
    assert len(review(workspace)["entries"]) == 3
    assert (workspace / CORRECTED_TRANSCRIPT).read_bytes() == (
        workspace / "transcript.md").read_bytes()


def test_lookup_budget_persists_and_context_is_local(workspace):
    for _ in range(3):
        lookup = start_lookup(workspace, "作者使用的训练术语", ["unit-000001"])
        assert "薄肌、训练与健康" in lookup["context"]
        assert "unit-000003" in lookup["context"]
        assert "unit-000004" not in lookup["context"]
        finish_lookup(workspace, lookup["id"], {"status": "failed", "error_code": "http_402"})
    prepare_term_correction(workspace, {})
    with pytest.raises(ValueError, match="budget exhausted"):
        start_lookup(workspace, "one more", ["unit-000001"])
    assert len(review(workspace)["lookups"]) == 3


def test_fetch_uses_saved_source_and_shares_lookup_budget(workspace):
    lookup = start_lookup(workspace, "cloud 编程工具", ["unit-000003"])
    saved = finish_lookup(workspace, lookup["id"], {
        "status": "completed", "sources": [{"title": "产品介绍", "url": "https://example.org/docs"}],
    })
    fetched = start_fetch(workspace, saved["sources"][0]["id"])
    assert fetched["operation"] == "fetch"
    assert fetched["url"] == "https://example.org/docs"
    assert fetched["unit_ids"] == ["unit-000003"]
    assert fetched["parent_source_id"] == saved["sources"][0]["id"]
    body = finish_lookup(workspace, fetched["id"], {
        "status": "completed", "sources": [{
            "title": "", "url": fetched["url"], "content": "# Claude Code\n开发工具文档。",
        }],
    })
    record_term_review(workspace, [entry("unit-000003", "cloud", "Claude Code", evidence=[{
        "source_id": body["sources"][0]["id"], "quote": "# Claude Code",
    }])])
    assert "Claude Code帮我" in (workspace / CORRECTED_TRANSCRIPT).read_text()
    with pytest.raises(ValueError, match="source ID already returned"):
        start_fetch(workspace, "https://unseen.example.org/")
    start_lookup(workspace, "第三次请求", ["unit-000003"])
    with pytest.raises(ValueError, match="budget exhausted"):
        start_fetch(workspace, saved["sources"][0]["id"])


def test_search_summary_is_not_direct_evidence(workspace):
    lookup = start_lookup(workspace, "cloud 编程工具", ["unit-000003"])
    saved = finish_lookup(workspace, lookup["id"], {
        "status": "completed", "sources": [{
            "title": "产品介绍", "url": "https://example.org/docs",
            "summary": "Claude Code", "snippet": "Claude Code",
        }],
    })
    assert saved["sources"][0]["summary"] == "Claude Code"
    with pytest.raises(ValueError, match="Evidence must quote"):
        record_term_review(workspace, [entry("unit-000003", "cloud", "Claude Code", evidence=[{
            "source_id": saved["sources"][0]["id"], "quote": "Claude Code",
        }])])


def test_search_page_content_can_support_patch(workspace):
    lookup = start_lookup(workspace, "cloud 编程工具", ["unit-000003"])
    saved = finish_lookup(workspace, lookup["id"], {
        "status": "completed", "sources": [{
            "title": "产品介绍", "url": "https://example.org/docs",
            "content": "Claude Code 是开发工具。",
        }],
    })
    record_term_review(workspace, [entry("unit-000003", "cloud", "Claude Code", evidence=[{
        "source_id": saved["sources"][0]["id"], "quote": "Claude Code 是开发工具。",
    }])])
    assert "Claude Code帮我" in (workspace / CORRECTED_TRANSCRIPT).read_text()


def test_fetched_content_is_bounded_and_marked(workspace):
    lookup = start_lookup(workspace, "cloud", ["unit-000003"])
    saved = finish_lookup(workspace, lookup["id"], {
        "status": "completed", "sources": [{
            "title": "文档", "url": "https://example.org/docs", "content": "a" * 12001,
        }],
    })
    assert len(saved["sources"][0]["content"]) == 12000
    assert saved["sources"][0]["content_truncated"] is True


@pytest.mark.parametrize("status,accepted", [("completed", True), ("no_sources", False),
                                            ("failed", False), ("incomplete", True)])
def test_returned_sources_support_patches_independently_of_analysis(workspace, status, accepted):
    lookup = start_lookup(workspace, "AI产品 cloud 名称", ["unit-000003"])
    saved = finish_lookup(workspace, lookup["id"], {
        "status": status, "analysis": "辅助模型的解释不是直接证据", "elapsed_ms": 10,
        "sources": [{"title": "Claude 官方文档", "url": "https://example.org/docs"}],
        "usage": {"input_tokens": 100, "output_tokens": 10, "plugins": {"search": {"count": 1}}},
        "headers": {"Authorization": "fixture-secret-not-to-persist"},
    })
    patch = entry("unit-000003", "cloud", "Claude", kind="product", evidence=[{
        "source_id": saved["sources"][0]["id"], "quote": "Claude 官方文档",
    }])
    if accepted:
        record_term_review(workspace, [patch])
        assert "Claude帮我" in (workspace / CORRECTED_TRANSCRIPT).read_text()
    else:
        with pytest.raises(ValueError, match="Evidence must quote"):
            record_term_review(workspace, [patch])
    assert "fixture-secret-not-to-persist" not in (workspace / TERM_REVIEW).read_text()


def test_flag_defaults_off_and_term_documents_capture_without_cache_identity_change(tmp_path):
    run = create_run(tmp_path / "runs", "BV1aTtb6uE7d", term_correction=True)
    store = Store(run.parent / "state.sqlite3", initialize=False)
    metadata = store.run(run.name)["input"]
    assert metadata["term_correction"] is True
    assert cache_key("transcript", metadata) == cache_key(
        "transcript", {**metadata, "term_correction": False})
    plain = create_run(run.parent, "BV1aTtb6uE7d")
    assert store.run(plain.name)["input"]["term_correction"] is False
    execution = store.claim_run(run.name)
    (run / "transcript.md").write_text("[unit-1 | 0–1s] 原文")
    prepare_term_correction(run, metadata)
    store.capture(run, execution)
    assert set(TERM_DOCUMENTS) <= store.documents(run.name).keys()
    for name in TERM_DOCUMENTS:
        (run / name).unlink()
    store.export(run.name)
    assert all((run / name).exists() for name in TERM_DOCUMENTS)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("legacy_permission", [None, "0", "1"])
def test_pi_tools_prompt_and_model_are_run_scoped(
    workspace, tmp_path, monkeypatch, enabled, legacy_permission,
):
    executable = tmp_path / "fake-pi"
    executable.write_text(
        "#!/usr/bin/env python3\nimport json,sys\njson.loads(sys.stdin.readline())\n"
        'print(json.dumps({"type":"message_end","message":{"role":"assistant",'
        '"stopReason":"stop"}}))\n'
        'print(json.dumps({"type":"agent_settled"}),flush=True)\n'
    )
    executable.chmod(0o755)
    monkeypatch.setattr("video_report_agent.pi.shutil.which", lambda _: str(executable))
    monkeypatch.setattr("video_report_agent.pi.PI_AGENT_DIR", tmp_path / "pi-config")
    if legacy_permission is not None:
        monkeypatch.setenv("PI_TERM_EVIDENCE_ALLOW_PAID_SEARCH", legacy_permission)
    else:
        monkeypatch.delenv("PI_TERM_EVIDENCE_ALLOW_PAID_SEARCH", raising=False)
    (workspace / "input.json").write_text(json.dumps({"term_correction": enabled}))
    with pytest.raises(PiError, match="without report.html"):
        asyncio.run(PiRunner(provider="fixture", model="report-model", thinking="medium",
                             review=True, isolation="local").run(workspace))
    invocation = json.loads((workspace / "invocation.json").read_text())
    command, prompt = invocation["command"], invocation["prompt"]
    assert command[command.index("--model") + 1] == "report-model"
    assert command[command.index("--thinking") + 1] == "medium"
    assert ("lookup_term" in command[command.index("--tools") + 1]) is enabled
    assert ("fetch_term_source" in command[command.index("--tools") + 1]) is enabled
    assert ("record_term_review" in command[command.index("--tools") + 1]) is enabled
    assert ("source-context.json" in prompt) is enabled
    if enabled:
        assert (workspace / "term_correction.ts").is_file()
        assert "可按需调用 lookup_term" in prompt
        assert "本次未启用付费联网取证" not in prompt
    else:
        assert not (workspace / "term_correction.ts").exists()
        assert "不要读取、改写或自行生成视频简介" in prompt


def test_docker_staging_patch_and_export(workspace, tmp_path, monkeypatch):
    from video_report_agent import pi

    monkeypatch.delenv("PI_TERM_EVIDENCE_ALLOW_PAID_SEARCH", raising=False)
    monkeypatch.setattr(pi, "PI_AGENT_DIR", tmp_path / "pi-config")
    monkeypatch.setattr(pi, "snapshot_models", lambda config, *_: config.mkdir())
    fake = tmp_path / "fake_pi.py"
    fake.write_text(
        "import json,os,sys,signal\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "from video_report_agent.term_correction import record_term_review\n"
        "os.chdir(sys.argv[1]); json.loads(sys.stdin.readline())\n"
        f"record_term_review(Path.cwd(), {repr([entry()])})\n"
        "Path('report.html').write_text('<html><body>{{VIDEO_DESCRIPTION}}薄肌</body></html>')\n"
        "print(json.dumps({'type':'message_end','message':{'role':'assistant','stopReason':'stop'}}))\n"
        "print(json.dumps({'type':'agent_settled'}),flush=True)\n"
        "sys.stdin.read()\n"
    )

    def launch(stage, config, command, deadline):
        assert "/workspace/term_correction.ts" in command
        assert "record_term_review" in command[command.index("--tools") + 1]
        assert json.loads((stage / SOURCE_CONTEXT).read_text())["description"] == "薄肌、训练与健康"
        assert not (stage / "asr.json").exists()
        return [sys.executable, str(fake), str(stage)]

    monkeypatch.setattr(pi, "launch_command", launch)
    original = (workspace / "transcript.md").read_text()
    assert asyncio.run(PiRunner(isolation="docker", review=False).run(workspace)).is_file()
    assert "练薄肌" in (workspace / CORRECTED_TRANSCRIPT).read_text()
    assert review(workspace)["entries"][0]["status"] == "supported"
    assert (workspace / "transcript.md").read_text() == original
    assert not (workspace / ".generation-config").exists()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("legacy_permission", ["0", "1"])
def test_docker_search_credentials_are_scoped_and_not_in_command(
    workspace, tmp_path, monkeypatch, enabled, legacy_permission,
):
    from video_report_agent import task_container

    (workspace / "input.json").write_text(json.dumps({"term_correction": enabled}))
    monkeypatch.setenv("PI_TERM_EVIDENCE_ALLOW_PAID_SEARCH", legacy_permission)
    monkeypatch.setenv("DEEPSEEK_SEARCH_API_KEY", "private-search-fixture")
    monkeypatch.delenv("PI_TASK_RUNS_VOLUME", raising=False)
    monkeypatch.setattr(task_container, "require_cleaner", lambda _: None)
    monkeypatch.setattr(task_container.os, "getuid", lambda: 1000)
    stage = task_container.stage_inputs(workspace)
    config = tmp_path / "config"
    config.mkdir()
    command = task_container.launch_command(stage, config, ["pi"], float("inf"))
    assert "private-search-fixture" not in " ".join(command)
    assert "PI_TERM_EVIDENCE_ALLOW_PAID_SEARCH" not in " ".join(command)
    key = config / "term-search-key"
    assert key.exists() is enabled
    if key.exists():
        assert key.read_text() == "private-search-fixture"
        assert key.stat().st_mode & 0o777 == 0o600


def test_cli_passes_explicit_flag(tmp_path, monkeypatch):
    from video_report_agent import cli

    captured = {}
    monkeypatch.setattr(cli, "load_dotenv", lambda *args: None)
    def generate(run, **kwargs):
        captured.update(json.loads((run / "input.json").read_text()))
        return {"state": "RENDERED"}
    monkeypatch.setattr(cli, "generate", generate)
    monkeypatch.setattr(sys, "argv", ["video-report", "--runs", str(tmp_path / "runs"),
                                     "generate", "BV1aTtb6uE7d", "--term-correction"])
    assert cli.main() == 0
    assert captured["term_correction"] is True


def test_extension_deepseek_search_fetch_and_tools_with_fake_http(workspace):
    node, pi = shutil.which("node"), shutil.which("pi")
    if not node or not pi:
        pytest.skip("Installed Pi and Node needed for offline extension loading")
    pi_root = next((p for p in Path(pi).resolve().parents
                    if (p / "dist/core/extensions/jiti-loader.js").is_file()), None)
    if not pi_root:
        pytest.skip("Pi extension loader not available")
    harness = Path(__file__).with_name("term_tools_fixture.mjs")
    env = {**os.environ, "VIDEO_REPORT_PYTHON": sys.executable,
           "TERM_TEST_PI_ROOT": str(pi_root), "TERM_TEST_RUN": str(workspace),
           "TERM_TEST_EXTENSION": str(Path(__file__).parents[1] /
               "src/video_report_agent/term_correction.ts")}
    result = subprocess.run([node, str(harness)], env=env, capture_output=True, text=True,
                            timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
