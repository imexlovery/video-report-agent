import json
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_report_agent import ingest, pipeline, source_access
from video_report_agent.ingest import UrlIngestError
from video_report_agent.storage import Store

URL = "https://www.youtube.com/watch?v=vid_0000001"


@pytest.mark.parametrize("subtitles", [True, False])
def test_youtube_parses_once_and_publishes_metadata_before_material(monkeypatch, subtitles):
    events = []

    class Downloader:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def extract_info(self, *a, **kw):
            events.append("extract")
            return {"id": "vid_0000001", "title": "Fixture", "uploader": "Creator",
                    "duration": 12, "language": "en", "subtitles": {"en": [{
                        "ext": "srt", "url": "https://captions.example/a.srt",
                    }]} if subtitles else {}, "formats": [{"url": "https://media.example/a.m4a",
                                                            "acodec": "aac", "vcodec": "none"}]}

        def urlopen(self, *a, **kw):
            events.append("subtitle")
            return BytesIO(b"1\n00:00:00,000 --> 00:00:01,000\nOriginal words\n")

    monkeypatch.setattr(source_access, "YoutubeDL", Downloader)
    monkeypatch.setattr("video_report_agent.youtube_subtitles.YoutubeDL", Downloader)
    monkeypatch.setattr(source_access, "_open_media", lambda *a: events.append("audio") or True)
    result = source_access.prepare_source_result(
        URL, on_metadata=lambda data: events.append("meta"),
    )
    assert events == ["extract", "meta", "subtitle" if subtitles else "audio"]
    assert result["public"]["available"]
    assert "body" not in json.dumps(result["public"])
    if subtitles:
        assert result["material"]["snapshot"]["sources"]["subtitle_track"]["body"]
    else:
        assert result["material"]["download_info"]["formats"]


def test_prepared_subtitles_enter_transcript_without_source_requests(tmp_path, monkeypatch):
    snapshot = {"title": "Fixture", "uploader": "Creator", "attribution": "YouTube",
                "duration_ms": 12000, "sources": {"subtitle_track": {
                    "status": "READY", "count": 1, "body": [
                        {"from": 0, "to": 1, "content": "Original words", "cue_index": 0},
                    ],
                }}}
    material = {"version": 1, "video_id": "youtube-vid_0000001", "url": URL,
                "prepared_at": time.time(), "snapshot": snapshot,
                "download_info": {"formats": [{"url": "https://media.example/?signed=private"}]}}
    for name in ("prepare_source", "fetch_youtube_subtitles", "transcribe_audio", "PiRunner"):
        monkeypatch.setattr(pipeline, name,
                            lambda *a, **kw: pytest.fail("must reuse prepared text"))
    run = pipeline.create_run(tmp_path, URL, transcript_mode="auto", transcript_only=True,
                              source_access=True, prepared_source=material)
    assert not (run / "source-preparation.json").exists()
    assert "private" not in (run / "input.json").read_text()
    result = pipeline.generate(run)
    assert result["state"] == "TRANSCRIPT_READY"
    assert "Original words" in (run / "transcript.md").read_text()
    saved = Store(tmp_path / "state.sqlite3").documents(run.name)["source-preparation.json"]
    assert "download_info" not in saved


def test_cached_format_is_loaded_without_extracting_source_url(tmp_path, monkeypatch):
    source = source_access.resolve_source(URL)
    info = {"id": "vid_0000001", "title": "Fixture", "duration": 12,
            "formats": [{"url": "https://media.example/a.m4a"}]}
    paths = []
    monkeypatch.setattr(ingest.shutil, "which", lambda _: "/test/yt-dlp")

    def runner(command, **kw):
        assert URL not in command
        path = command[command.index("--load-info-json") + 1]
        paths.append(path)
        assert json.loads(Path(path).read_text()) == info
        media = tmp_path / "download/source.m4a"
        media.write_bytes(b"audio")
        return SimpleNamespace(returncode=0, stdout=str(media), stderr="")

    ingest._download_video(
        source, tmp_path, runner=runner, audio_only=True, resolved_info=info,
        media_metadata={"duration_ms": 12000, "title": "Fixture", "uploader": "A"},
    )
    assert paths and all(not Path(path).exists() for path in paths)


def test_expired_media_url_refreshes_only_after_reuse_failure(tmp_path, monkeypatch):
    source = source_access.resolve_source(URL)
    attempts = []

    def download(*a, **kw):
        attempts.append(kw.get("resolved_info"))
        if kw.get("resolved_info"):
            raise UrlIngestError("DOWNLOAD_ERROR", "expired")
        return "downloaded"

    monkeypatch.setattr(source_access, "_download_video", download)
    from yt_dlp.cookies import YoutubeDLCookieJar

    result = source_access.download_source_audio(source, tmp_path, {}, YoutubeDLCookieJar(),
        prepared={"prepared_at": time.time(), "download_info": {"id": "existing"}})
    assert result == "downloaded" and attempts == [{"id": "existing"}, None]


def test_format_snapshot_removes_credentials():
    result = source_access._download_info({
        "id": "x", "http_headers": {"Cookie": "secret", "Referer": "https://source.test"},
        "formats": [{"url": "https://media.example", "cookies": "secret", "http_headers": {
            "Authorization": "secret", "User-Agent": "agent",
        }}],
    })
    assert "secret" not in json.dumps(result)
    assert result["http_headers"] == {"Referer": "https://source.test"}
