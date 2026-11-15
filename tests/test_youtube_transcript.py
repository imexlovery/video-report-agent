from io import BytesIO

import pytest

from video_report_agent import pipeline, youtube_subtitles
from video_report_agent.ingest import UrlIngestError, validate_source_url

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


@pytest.mark.parametrize(
    "manual_usable, tag_suffix", [(True, "-orig"), (False, "-orig"), (False, "")],
)
def test_subtitle_only_uses_author_then_original_auto(
    tmp_path, monkeypatch, manual_usable, tag_suffix,
):
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
