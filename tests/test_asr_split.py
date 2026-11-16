import json
import shutil
import threading
import wave
from types import SimpleNamespace

import pytest

from video_report_agent.asr import AsrRun, normalize_asr_segments
from video_report_agent.audio import midpoint_pause_ms, split_pcm_audio
from video_report_agent.media_config import QWEN_FILETRANS_MODEL
from video_report_agent.paraformer import CloudAsrError, ParaformerBackend
from video_report_agent.pipeline import write_json


def pcm(path, seconds=8, quiet=None):
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        for second in range(seconds):
            sample = b"\0\0" if quiet and quiet[0] <= second < quiet[1] else b"\0\x20"
            audio.writeframes(sample * 16000)
    return path


def result(label, *, missing_usage=False, dropped=False):
    rows = [{"start": .1, "end": .5, "text": label,
             "words": [{"word": label, "start": .1, "end": .5}]}]
    if dropped:
        rows.insert(0, {"start": 0, "end": .05, "text": ""})
    normalized = normalize_asr_segments(rows)
    return AsrRun(
        engine="paraformer", backend="paraformer", provider="dashscope",
        model="paraformer-v2", language="zh", elapsed_ms=10,
        segments=normalized.segments, raw_result={"raw_segment_count": len(rows)},
        usage={} if missing_usage else {
            "content_duration_ms": 400, "service_usage": {"duration": 1},
        },
        dropped_empty_raw_segment_ordinals=normalized.dropped_empty_raw_segment_ordinals,
    )


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    audio = pcm(tmp_path / "audio.wav")
    monkeypatch.setattr("video_report_agent.paraformer.probe_audio",
                        lambda _: SimpleNamespace(duration_ms=3_600_000))
    monkeypatch.setattr("video_report_agent.paraformer.midpoint_pause_ms", lambda *_: 4000)
    return audio, tmp_path / "asr-task.json"


@pytest.mark.parametrize("duration,pause,expected", [
    (3_599_999, 4000, 1), (3_600_000, None, 1),
    (3_600_000, 4000, 2), (10_800_000, 4000, 2),
])
def test_threshold_no_hard_cut_and_no_recursive_split(configured, monkeypatch,
                                                     duration, pause, expected):
    audio, checkpoint = configured
    searches, calls = [], []
    monkeypatch.setattr("video_report_agent.paraformer.probe_audio",
                        lambda _: SimpleNamespace(duration_ms=duration))

    def find(*args):
        searches.append(args)
        return pause

    def recognize(self, path, language):
        calls.append(path)
        return result(path.name)

    monkeypatch.setattr("video_report_agent.paraformer.midpoint_pause_ms", find)
    monkeypatch.setattr(ParaformerBackend, "_single_run", recognize)
    ParaformerBackend(task_path=checkpoint).transcribe(audio)
    assert len(calls) == expected
    assert len(searches) == (duration >= 3_600_000)
    if expected == 1:
        assert calls == [audio]
        assert not checkpoint.exists()
    assert not list(audio.parent.glob(".asr-parts-*"))


@pytest.mark.parametrize(
    "model,missing_usage,dropped",
    [("paraformer-v2", False, False), ("paraformer-v2", True, True),
     (QWEN_FILETRANS_MODEL, False, False), (QWEN_FILETRANS_MODEL, True, False)],
)
def test_parallel_merge_offsets_usage_provenance_and_cleanup(
    configured, monkeypatch, model, missing_usage, dropped,
):
    audio, checkpoint = configured
    barrier = threading.Barrier(2)

    def recognize(self, path, language):
        assert json.loads(checkpoint.read_text())["chunks"]
        write_json(self.task_path, {"task_id": path.stem})
        barrier.wait(timeout=5)  # Sequential execution would fail.
        recognized = result(path.stem, missing_usage=missing_usage, dropped=dropped)
        if model == QWEN_FILETRANS_MODEL and not missing_usage:
            recognized.usage["service_usage"].update(input_tokens=20, output_tokens=4)
        return recognized

    monkeypatch.setattr(ParaformerBackend, "_single_run", recognize)
    language = "auto" if model == QWEN_FILETRANS_MODEL else "zh"
    merged = ParaformerBackend(model=model, task_path=checkpoint).transcribe(audio, language)
    assert [s.start_ms for s in merged.segments] == [100, 4100]
    assert [s.words[0]["start"] for s in merged.segments] == [.1, 4.1]
    assert [s.text for s in merged.segments] == ["part-0", "part-1"]
    assert [s.ordinal for s in merged.segments] == ([1, 3] if dropped else [0, 1])
    assert merged.dropped_empty_raw_segment_ordinals == ((0, 2) if dropped else ())
    assert merged.to_dict()["raw_segment_count"] == (4 if dropped else 2)
    assert merged.usage["content_duration_ms"] == (None if missing_usage else 800)
    assert merged.usage["service_usage"]["duration"] == (None if missing_usage else 2)
    if model == QWEN_FILETRANS_MODEL:
        assert merged.usage["service_usage"]["input_tokens"] == (None if missing_usage else 40)
        assert merged.usage["service_usage"]["output_tokens"] == (None if missing_usage else 8)
    assert [c["offset_ms"] for c in merged.raw_result["chunks"]] == [0, 4000]
    assert len(list(audio.parent.glob("asr-task.part-*.json"))) == 2
    assert not list(audio.parent.glob(".asr-parts-*"))


@pytest.mark.parametrize("stage", ["poll", "submit_unknown"])
def test_partial_failure_preserves_ids_and_never_resubmits(configured, monkeypatch, stage):
    audio, checkpoint = configured
    barrier = threading.Barrier(2)
    calls = []

    def recognize(self, path, language):
        calls.append(path.name)
        if path.stem == "part-1" or stage == "poll":
            write_json(self.task_path, {"task_id": path.stem})
        barrier.wait(timeout=5)
        if path.stem == "part-0":
            raise CloudAsrError(stage, "controlled", "part-0" if stage == "poll" else None)
        return result("sibling")

    monkeypatch.setattr(ParaformerBackend, "_single_run", recognize)
    backend = ParaformerBackend(task_path=checkpoint)
    with pytest.raises(CloudAsrError) as error:
        backend.transcribe(audio)
    assert error.value.stage == stage
    assert json.loads((audio.parent / "asr-task.part-1.json").read_text())["task_id"] == "part-1"
    audio.unlink()
    with pytest.raises(CloudAsrError, match="已有云端任务"):
        backend.transcribe(audio)
    assert sorted(calls) == ["part-0.wav", "part-1.wav"]
    assert not list(audio.parent.glob(".asr-parts-*"))


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires FFmpeg")
@pytest.mark.parametrize("quiet,expected", [(None, None), ((59, 61), 60_000)])
def test_real_silence_detection(tmp_path, quiet, expected):
    audio = pcm(tmp_path / "signal.wav", seconds=120, quiet=quiet)
    split = midpoint_pause_ms(audio, 120_000)
    if expected is None:
        assert split is None
    else:
        assert abs(split - expected) <= 1


def test_pcm_split_is_exact_and_contiguous(tmp_path):
    audio = pcm(tmp_path / "input.wav", quiet=(3, 5))
    parts = split_pcm_audio(audio, tmp_path, 4123)
    def frames(path):
        with wave.open(str(path)) as stream:
            return stream.readframes(stream.getnframes())
    assert frames(parts[0]) + frames(parts[1]) == frames(audio)
    assert len(frames(parts[0])) == 4123 * 16 * 2
