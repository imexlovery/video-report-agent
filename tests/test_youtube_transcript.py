import json
from io import BytesIO
from types import SimpleNamespace

import pytest

from video_report_agent import pipeline, youtube_subtitles
from video_report_agent.asr import AsrRun
from video_report_agent.ingest import UrlIngestError, validate_source_url
from video_report_agent.media_config import QWEN_FILETRANS_MODEL
from video_report_agent.schemas import AsrSegment

ID = "vid_0000001"
WATCH = f"https://www.youtube.com/watch?v={ID}"


@pytest.mark.parametrize("url", [f"https://youtu.be/{ID}?si=track", f"{WATCH}&list=PLx", WATCH])
def test_single_video_link_has_one_canonical_identity(url):
    source = validate_source_url(url, allow_youtube=True)
    assert source.video_id == f"youtube-{ID}"
    assert source.canonical_url == WATCH
    with pytest.raises(UrlIngestError):
        validate_source_url(url)


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/playlist?list=PLx",
    f"https://www.youtube.com:443/watch?v={ID}",
    f"{WATCH}&v={ID}",
    f"https://www.youtube.com@other.test/watch?v={ID}",
])
def test_invalid_youtube_input_is_rejected(url):
    with pytest.raises(UrlIngestError):
        validate_source_url(url, allow_youtube=True)


def test_youtube_explicit_cloud_model_uses_youtube_default_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("ASR_BACKEND", "mlx")
    run = pipeline.create_run(
        tmp_path, WATCH, transcript_mode="auto", transcript_only=True, asr_model="paraformer-v2",
    )
    config = json.loads((run / "input.json").read_text())
    assert config["asr_backend"] == "paraformer" and config["asr_model"] == "paraformer-v2"


@pytest.mark.parametrize(
    "manual_usable, tag_suffix", [(True, "-orig"), (False, "-orig"), (False, "")],
)
def test_subtitle_only_uses_author_then_original_auto(
    tmp_path, monkeypatch, manual_usable, tag_suffix,
):
    monkeypatch.setenv("ASR_BACKEND", "paraformer")
    monkeypatch.setenv("ASR_MODEL", "paraformer-v2")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    calls = []
    track = "https://www.youtube.com/api/timedtext?"

    class FakeDownloader:
        def __init__(self, options):
            assert options["noplaylist"]
            assert options["ignore_no_formats_error"]
            assert "cookiefile" not in options and "cookiesfrombrowser" not in options

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def extract_info(self, url, *, download, ie_key):
            assert (url, download, ie_key) == (WATCH, False, "Youtube")
            return {
                "id": ID, "title": "测试视频", "duration": 10, "language": "es",
                "subtitles": {"es": [{"ext": "srt", "url": track + "lang=es"}]},
                "automatic_captions": {
                    f"en{tag_suffix}": [{"ext": "srt", "url": track + "kind=asr&lang=en"}],
                    f"es{tag_suffix}": [
                        {"ext": "srt", "url": track + "kind=asr&lang=en&tlang=es"},
                        {"ext": "srt", "url": track + "kind=asr&lang=es"},
                    ],
                },
            }

        def urlopen(self, url):
            calls.append(url)
            if not manual_usable and url == track + "lang=es":
                return BytesIO(b"")
            return BytesIO(b"1\n00:00:00,500 --> 00:00:02,000\nHola\n")

    def unselected(*args, **kwargs):
        pytest.fail("An unselected audio, ASR or report stage was called")

    monkeypatch.setattr(youtube_subtitles, "YoutubeDL", FakeDownloader)
    for name in ("download_youtube_audio", "transcribe_audio", "PiRunner", "render_report_image"):
        monkeypatch.setattr(pipeline, name, unselected)
    run = pipeline.create_run(tmp_path, WATCH, transcript_mode="auto", transcript_only=True)
    config = json.loads((run / "input.json").read_text())
    assert config["asr_model"] == QWEN_FILETRANS_MODEL and config["asr_language"] == "auto"
    status = pipeline.generate(run)
    assert status["state"] == "TRANSCRIPT_READY"
    assert status["transcript_source_status"]["subtitle_track"]["track_kind"] == (
        "author" if manual_usable else "automatic"
    )
    expected = [track + "lang=es"]
    if not manual_usable:
        expected.append(track + "kind=asr&lang=es")
    assert calls == expected
    assert "Hola" in (run / "transcript.md").read_text()


def test_no_usable_youtube_subtitle_uses_qwen_without_language_hints(tmp_path, monkeypatch):
    monkeypatch.setenv("ASR_BACKEND", "paraformer")
    monkeypatch.setenv("ASR_MODEL", "paraformer-v2")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    metadata = {"title": "Sample", "uploader": "Test", "attribution": "YouTube"}
    monkeypatch.setattr(pipeline, "fetch_youtube_subtitles", lambda _: {
        **metadata, "duration_ms": 1000, "sources": {"subtitle_track": {"status": "ABSENT"}},
    })

    def download(_source, run, _metadata):
        path = run / "source.m4a"
        path.touch()
        return SimpleNamespace(media_path=path, **metadata)

    def extract(_media, output):
        output.touch()
        return SimpleNamespace(path=output, probe=SimpleNamespace(duration_ms=1000))

    def recognize(_audio, **options):
        assert options["backend"] == "paraformer" and options["model"] == QWEN_FILETRANS_MODEL
        assert options["language"] == "auto"
        assert options["parameters"] == {"channel_id": [0], "keep_dialect": True}
        return AsrRun(
            engine="qwen-audio", backend="paraformer", provider="dashscope",
            model=QWEN_FILETRANS_MODEL, language="auto", elapsed_ms=1, raw_result={},
            segments=(AsrSegment(ordinal=1, start_ms=0, end_ms=900, text="Hola"),),
        )

    monkeypatch.setattr(pipeline, "download_youtube_audio", download)
    monkeypatch.setattr(pipeline, "extract_audio", extract)
    monkeypatch.setattr(pipeline, "transcribe_audio", recognize)
    run = pipeline.create_run(tmp_path, WATCH, transcript_mode="auto", transcript_only=True)
    status = pipeline.generate(run)
    assert status["state"] == "TRANSCRIPT_READY" and status["transcript_primary_source"] == "asr"
    assert "Hola" in (run / "transcript.md").read_text()
    assert not (run / "report.html").exists()
