"""Task mode persistence and the instructions actually delivered to Pi."""

import asyncio
import json
import shutil

import pytest

from video_report_agent import cli
from video_report_agent.pi import SKILL, PiError, PiRunner
from video_report_agent.pipeline import create_run


@pytest.mark.parametrize("mode", ["standard", "brief"])
def test_create_run_saves_mode(tmp_path, mode):
    run = create_run(tmp_path, "BV1aTtb6uE7d", report_mode=mode)
    assert json.loads((run / "input.json").read_text())["report_mode"] == mode


@pytest.mark.parametrize("mode", ["auto", "", None, ["brief"]])
def test_invalid_mode_does_not_create_run(tmp_path, mode):
    with pytest.raises(ValueError, match="report_mode"):
        create_run(tmp_path, "BV1aTtb6uE7d", report_mode=mode)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("copied_skill", [False, True])
@pytest.mark.parametrize("mode", [None, "standard", "brief"])
def test_pi_uses_saved_mode_and_copies_only_selected_rules(
    tmp_path, monkeypatch, mode, copied_skill,
):
    run = create_run(tmp_path, "BV1aTtb6uE7d", report_mode=mode or "standard")
    if mode is None:
        metadata = json.loads((run / "input.json").read_text())
        metadata.pop("report_mode")
        (run / "input.json").write_text(json.dumps(metadata))
    original = (run / "input.json").read_bytes()
    # Old working directories may still contain resources staged by an earlier runner.
    (run / "modes").mkdir()
    (run / "assets").mkdir()
    for name in ("standard.md", "brief.md"):
        (run / "modes" / name).write_text("stale guide")
    for name in ("report-template.html", "brief-report-template.html"):
        (run / "assets" / name).write_text("stale template")
    (run / "transcript.md").write_text("Complete source text")
    executable = tmp_path / "fake-pi"
    executable.write_text('''#!/usr/bin/env python3
import json, pathlib, sys
prompt = json.loads(sys.stdin.readline())["message"]
metadata = json.loads(pathlib.Path("input.json").read_text())
mode = metadata.get("report_mode", "standard")
assert f"report_mode={mode}" in prompt
assert f"modes/{mode}.md" in prompt
assert sorted(p.name for p in pathlib.Path("modes").glob("*.md")) == [f"{mode}.md"]
template = "brief-report-template.html" if mode == "brief" else "report-template.html"
other = "report-template.html" if mode == "brief" else "brief-report-template.html"
assert f"assets/{template}" in prompt
assert f"assets/{other}" not in prompt
assert pathlib.Path("assets", template).is_file()
assert not pathlib.Path("assets", other).exists()
assert "stale template" not in pathlib.Path("assets", template).read_text()
pathlib.Path("report.html").write_text("<html><body>generated</body></html>")
print(json.dumps({"type":"message_end","message":{"role":"assistant","stopReason":"stop"}}))
print(json.dumps({"type":"agent_settled"}), flush=True)
''')
    executable.chmod(0o755)
    monkeypatch.setattr("video_report_agent.pi.shutil.which", lambda _: str(executable))
    monkeypatch.setattr("video_report_agent.pi.PI_AGENT_DIR", tmp_path / "pi-config")
    copied = tmp_path / "copied-skill"
    if copied_skill:
        shutil.copytree(SKILL, copied)
    asyncio.run(PiRunner(
        review=False, skill_dir=copied if copied_skill else None, isolation="local",
    ).run(run))
    assert (run / "input.json").read_bytes() == original


def test_cli_passes_mode(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr("sys.argv", [
        "video-report", "--runs", str(tmp_path), "generate", "BV1aTtb6uE7d",
        "--report-mode", "brief",
    ])

    def generate(run):
        captured.append(json.loads((run / "input.json").read_text())["report_mode"])
        return {"state": "RENDERED"}

    monkeypatch.setattr(cli, "generate", generate)
    assert cli.main() == 0
    assert captured == ["brief"]


def test_packaged_brief_never_falls_back_to_standard(tmp_path, monkeypatch):
    broken = tmp_path / "broken-skill"
    shutil.copytree(SKILL, broken)
    (broken / "assets/brief-report-template.html").unlink()
    monkeypatch.setattr("video_report_agent.pi.SKILL", broken)
    monkeypatch.setattr("video_report_agent.pi.shutil.which", lambda _: "unused")
    monkeypatch.setattr("video_report_agent.pi.PI_AGENT_DIR", tmp_path / "pi-config")
    run = create_run(tmp_path / "runs", "BV1aTtb6uE7d", report_mode="brief")
    (run / "transcript.md").write_text("Source")
    with pytest.raises(PiError, match="Selected template is missing"):
        asyncio.run(PiRunner(isolation="local").run(run))
    assert not (run / "assets/report-template.html").exists()
