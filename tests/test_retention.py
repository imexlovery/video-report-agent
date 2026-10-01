import json
import os
import time

import pytest

from video_report_agent import pipeline
from video_report_agent.retention import cleanup_cancelled_run, cleanup_media
from video_report_agent.transcript_foundation import FUSION_VERSION, NORMALIZER_VERSION


def make_run(root, name, state="RENDERED", age=0):
    run = root / name
    (run / "download").mkdir(parents=True)
    (run / "assets").mkdir()
    for name in (
        "download/source.mp4", "download/source.m4a", "audio.wav", "report.html", "transcript.md",
        "download/source.info.json", "download/source.zh.srt", "assets/image.png", "failure.log",
    ):
        (run / name).write_text("keep or remove")
    status = run / "status.json"
    status.write_text(json.dumps({"state": state}))
    timestamp = time.time() - age * 86400
    os.utime(status, (timestamp, timestamp))
    return run


def complete_transcript(run):
    metadata = {"video_id": "test", "asr_backend": "test", "asr_model": "test"}
    pipeline.write_json(run / "input.json", metadata)
    pipeline.write_json(run / "asr.json", {"segments": [
        {"ordinal": 0, "start_ms": 0, "end_ms": 1000, "text": "text", "words": []},
    ]})
    (run / "canonical-transcript.jsonl").write_text(json.dumps({
        "unit_id": "unit-1", "start_ms": 0, "end_ms": 1000, "canonical_text": "text",
    }) + "\n")
    pipeline.write_json(run / "transcript-manifest.json", {
        "status": "READY", "video_id": "test", "transcript_mode": "asr-only",
        "normalizer_version": NORMALIZER_VERSION, "fusion_version": FUSION_VERSION,
        "canonical_unit_count": 1,
        "artifacts": {"canonical_transcript": "canonical-transcript.jsonl"},
    })


@pytest.mark.parametrize("state", ["RENDERED", "FAILED", "CANCELLED"])
def test_media_older_than_seven_days_removed_and_transcript_preserved(tmp_path, monkeypatch, state):
    monkeypatch.delenv("MEDIA_KEEP_LAST", raising=False)
    monkeypatch.delenv("MEDIA_MAX_AGE_DAYS", raising=False)
    run = make_run(tmp_path, "complete", state, age=8)
    complete_transcript(run)
    cleanup_media(tmp_path)
    for name in ("download/source.mp4", "download/source.m4a", "audio.wav"):
        assert not (run / name).exists()
    for name in (
        "report.html", "transcript.md", "asr.json", "canonical-transcript.jsonl",
        "transcript-manifest.json", "input.json", "download/source.info.json",
        "download/source.zh.srt", "assets/image.png", "failure.log", "status.json",
    ):
        assert (run / name).exists()
    cleanup_media(tmp_path)


@pytest.mark.parametrize("state", ["RENDERED", "FAILED", "CANCELLED"])
def test_recent_media_kept_even_with_complete_transcript(tmp_path, monkeypatch, state):
    monkeypatch.delenv("MEDIA_KEEP_LAST", raising=False)
    monkeypatch.delenv("MEDIA_MAX_AGE_DAYS", raising=False)
    runs = [make_run(tmp_path, str(i), state, age=6) for i in range(25)]
    for run in runs:
        complete_transcript(run)
    cleanup_media(tmp_path)
    assert all((run / "download/source.m4a").exists() for run in runs)
    assert all((run / "audio.wav").exists() for run in runs)


def test_active_media_and_disabled_age_limit_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_KEEP_LAST", "0")
    monkeypatch.setenv("MEDIA_MAX_AGE_DAYS", "7")
    active = make_run(tmp_path, "active", "TRANSCRIBING", age=400)
    old = make_run(tmp_path, "old", age=8)
    monkeypatch.setenv("MEDIA_MAX_AGE_DAYS", "0")
    cleanup_media(tmp_path)
    assert (old / "audio.wav").exists()
    monkeypatch.setenv("MEDIA_MAX_AGE_DAYS", "7")
    cleanup_media(tmp_path)
    assert (active / "audio.wav").exists()
    assert not (old / "audio.wav").exists()


def test_cancel_keeps_reusable_inputs_and_removes_partial_outputs(tmp_path):
    run = pipeline.create_run(tmp_path, "BV1cZ8x6sEhF")
    metadata = json.loads((run / "input.json").read_text())
    (run / "download").mkdir()
    (run / "download/source.m4a").write_bytes(b"media")
    pipeline.write_json(
        run / "download/source.info.json",
        {"title": "Test", "uploader": "UP", "duration": 60},
    )
    (run / "download/source.m4a.part").write_bytes(b"partial")
    unit = {"unit_id": "unit-1", "start_ms": 0, "end_ms": 1000, "canonical_text": "text"}
    (run / "canonical-transcript.jsonl").write_text(json.dumps(unit) + "\n")
    (run / "source-text-events.jsonl").write_text("{}\n")
    pipeline.write_json(
        run / "asr.json",
        {
            "segments": [
                {"ordinal": 0, "start_ms": 0, "end_ms": 1000, "text": "text", "words": []}
            ]
        },
    )
    (run / "transcript.md").write_text("text")
    pipeline.write_json(
        run / "transcript-manifest.json",
        {
            "status": "READY",
            "video_id": metadata["video_id"],
            "transcript_mode": "asr-only",
            "normalizer_version": NORMALIZER_VERSION,
            "fusion_version": FUSION_VERSION,
            "canonical_unit_count": 1,
            "artifacts": {
                "canonical_transcript": "canonical-transcript.jsonl",
                "source_text_events": "source-text-events.jsonl",
                "transcript_manifest": "transcript-manifest.json",
            },
        },
    )
    for name in ("audio.wav", "report.html", "report.png", "pi.events.jsonl", "asr-task.json"):
        (run / name).write_text("partial")
    (run / "sessions").mkdir()
    (run / "sessions/partial.jsonl").write_text("partial")

    pipeline.write_json(run / "status.json", {"state": "CANCELLED"})
    cleanup_cancelled_run(run)

    for name in (
        "download/source.m4a", "audio.wav", "download/source.info.json", "asr.json",
        "canonical-transcript.jsonl", "source-text-events.jsonl",
        "transcript-manifest.json", "transcript.md", "input.json", "status.json",
    ):
        assert (run / name).exists()
    for name in (
        "download/source.m4a.part", "report.html", "report.png",
        "pi.events.jsonl", "asr-task.json", "sessions",
    ):
        assert not (run / name).exists()


def test_cancel_removes_incomplete_download_and_transcript(tmp_path):
    run = pipeline.create_run(tmp_path, "BV1cZ8x6sEhF")
    (run / "download").mkdir()
    (run / "download/source.m4a.part").write_bytes(b"partial")
    (run / "asr.json").write_text("{}")
    (run / "canonical-transcript.jsonl").write_text("")
    (run / "audio.wav").write_bytes(b"partial")

    cleanup_cancelled_run(run)

    assert not (run / "download").exists()
    assert not (run / "asr.json").exists()
    assert not (run / "canonical-transcript.jsonl").exists()
    assert not (run / "audio.wav").exists()
    assert (run / "input.json").exists()
    assert (run / "status.json").exists()


def test_cancel_keeps_completed_asr_before_transcript_is_ready(tmp_path):
    run = pipeline.create_run(tmp_path, "BV1cZ8x6sEhF")
    (run / "download").mkdir()
    (run / "download/source.m4a").write_bytes(b"media")
    pipeline.write_json(run / "download/source.info.json", {"title": "Test", "uploader": "UP", "duration": 60})
    (run / "audio.wav").write_bytes(b"audio")
    pipeline.write_json(run / "status.json", {"state": "CANCELLED"})
    pipeline.write_json(
        run / "asr.json",
        {
            "segments": [
                {"ordinal": 0, "start_ms": 0, "end_ms": 1000, "text": "text", "words": []}
            ]
        },
    )
    (run / "canonical-transcript.jsonl").write_text("")

    cleanup_cancelled_run(run)

    assert (run / "download/source.m4a").exists()
    assert (run / "audio.wav").exists()
    assert (run / "asr.json").exists()
    assert not (run / "canonical-transcript.jsonl").exists()
