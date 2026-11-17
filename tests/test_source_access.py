import json
from types import SimpleNamespace

import pytest
from yt_dlp.cookies import YoutubeDLCookieJar

from video_report_agent import pipeline, source_access
from video_report_agent.ingest import UrlIngestError


@pytest.mark.parametrize("value,platform", [
    ("BV1aTtb6uE7d", "bilibili"),
    ("分享 https://m.bilibili.com/video/BV1aTtb6uE7d?p=2 复制打开", "bilibili"),
    ("标题 [https://youtu.be/vid_0000001](https://youtu.be/vid_0000001?si=share)", "youtube"),
    ("分享 https://www.youtube.com/shorts/vid_0000001。", "youtube"),
    ("www.youtube.com/watch?v=vid_0000001", "youtube"),
    ("https://www.youtube.com/embed/vid_0000001", "youtube"),
    ("www.douyin.com/video/7696604074514402587", "douyin"),
    ("打开抖音 https://www.douyin.com/jingxuan?modal_id=7696604074514402587", "douyin"),
    ("笔记 https://www.xiaohongshu.com/explore/0123456789abcdef01234567?xsec_token=keep",
     "xiaohongshu"),
])
def test_shared_link_recognition(value, platform):
    assert source_access.platform_key(source_access.resolve_source(value)) == platform


@pytest.mark.parametrize("value", [
    "https://www.douyin.com.evil.test/video/7696604074514402587",
    "https://127.0.0.1/private", "https://www.youtube.com@evil.test/watch?v=vid_0000001",
    "https://youtu.be/vid_0000001 https://youtu.be/vid_0000002",
])
def test_unknown_or_ambiguous_links_are_rejected(value):
    with pytest.raises(UrlIngestError):
        source_access.resolve_source(value)


def test_preflight_never_uses_configured_browser_or_cookie(monkeypatch):
    monkeypatch.setenv("BILIBILI_COOKIE", "secret")
    monkeypatch.setenv("BILIBILI_SUBTITLE_BROWSER", "edge")
    monkeypatch.setattr(source_access, "read_platform_cookies",
                        lambda *a, **kw: pytest.fail("preflight must be anonymous"))
    monkeypatch.setattr(source_access, "inspect_source", lambda *a, **kw: (
        {"title": "Test", "duration_ms": 1000, "sources": {
            "subtitle_track": {"status": "READY", "count": 1, "body": [{"content": "private"}]},
        }}, YoutubeDLCookieJar(), "READY", "SKIPPED",
    ))
    result = source_access.preflight_source("BV1aTtb6uE7d")
    assert result["available"] and "private" not in json.dumps(result)


def test_preview_does_not_count_as_available_audio(monkeypatch):
    monkeypatch.setattr(source_access, "_subtitle_snapshot", lambda *a: None)
    monkeypatch.setattr(source_access, "_open_media",
                        lambda *a: pytest.fail("preview is not full media"))

    class Downloader:
        def __init__(self, options):
            self.logger = options["logger"]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def extract_info(self, *a, **kw):
            self.logger.warning("supporter-only video, only the preview will be extracted")
            return {"duration": 30, "formats": [{"url": "https://media.test/preview.mp4"}]}

    monkeypatch.setattr(source_access, "YoutubeDL", Downloader)
    result = source_access.preflight_source("BV1aTtb6uE7d")
    assert not result["available"]
    assert result["access"] == "LOGIN_REQUIRED" and result["audio"] == "PREVIEW_ONLY"


def test_server_pipeline_cannot_fall_back_to_account_cookie(monkeypatch):
    monkeypatch.setattr(source_access, "inspect_source", lambda *a, **kw: (
        {"sources": {}}, YoutubeDLCookieJar(), "LOGIN_REQUIRED", "FAILED",
    ))
    monkeypatch.setattr(source_access, "read_platform_cookies",
                        lambda *a, **kw: pytest.fail("server must not load credentials"))
    with pytest.raises(UrlIngestError) as exc:
        source_access.prepare_source("BV1aTtb6uE7d", allow_login=False)
    assert exc.value.category == "LOGIN_REQUIRED"


def test_youtube_login_retry_passes_cookie_to_subtitle_reader(monkeypatch):
    jar = YoutubeDLCookieJar()
    captured = []

    def captions(source, *, cookies, info, on_metadata):
        captured.append(cookies)
        return {"sources": {"subtitle_track": {"status": "READY", "count": 1}}}

    monkeypatch.setattr(source_access, "fetch_youtube_subtitles", captions)
    monkeypatch.setattr(source_access.YoutubeDL, "extract_info", lambda *a, **kw: {
        "id": "vid_0000001", "title": "Fixture", "duration": 10,
    })
    source = source_access.resolve_source("https://youtu.be/vid_0000001")
    snapshot, result_jar, access, audio = source_access.inspect_source(source, cookies=jar)
    assert captured == [jar] and result_jar is jar
    assert access == "READY" and audio == "SKIPPED"


def test_login_option_is_not_silently_ignored_for_manual_sources(tmp_path):
    with pytest.raises(ValueError, match="仅支持自动选源"):
        pipeline.create_run(tmp_path, "BV1aTtb6uE7d", transcript_mode="auto",
                            transcript_sources=["bilibili_ai"], source_login=True)


def test_local_acquisition_isolates_each_video(tmp_path, monkeypatch):
    def prepare(url, **kw):
        source = source_access.resolve_source(url)
        return source, {"title": source.video_id, "sources": {}}, YoutubeDLCookieJar()

    def download(source, output, *args):
        path = output / "source.mp4"
        if not path.exists():
            path.write_text(source.video_id)
        return SimpleNamespace(media_path=path)

    monkeypatch.setattr(source_access, "prepare_source", prepare)
    monkeypatch.setattr(source_access, "download_source_audio", download)
    paths = []
    for video in ("7696604074514402587", "7681133341692677414"):
        source, snapshot, result = source_access.acquire_source(
            "https://www.douyin.com/video/" + video, tmp_path,
        )
        assert result.media_path.read_text() == source.video_id
        saved = json.loads((result.media_path.parent / "source-material.json").read_text())
        assert saved["video_id"] == source.video_id
        paths.append(result.media_path)
    assert paths[0].parent != paths[1].parent


@pytest.mark.parametrize("url", [
    "https://www.douyin.com/video/7696604074514402587",
    "https://www.xiaohongshu.com/explore/0123456789abcdef01234567?xsec_token=share",
])
def test_platform_pipeline_stops_at_transcript_without_report(tmp_path, monkeypatch, url):
    source = source_access.resolve_source(url)
    snapshot = {"title": "Fixture", "uploader": "Author", "attribution": source.platform,
                "duration_ms": 2000, "sources": {"subtitle_track": {
                    "status": "READY", "count": 1,
                    "body": [{"from": 0, "to": 1, "content": "原语言材料", "cue_index": 0}],
                }}}
    monkeypatch.setattr(pipeline, "prepare_source", lambda *a, **kw: (
        source, snapshot, YoutubeDLCookieJar(),
    ))
    for name in ("PiRunner", "transcribe_audio", "download_source_audio"):
        monkeypatch.setattr(pipeline, name, lambda *a, **kw: pytest.fail("must only use subtitles"))
    run = pipeline.create_run(tmp_path, url, transcript_mode="auto", transcript_only=True)
    result = pipeline.generate(run)
    assert result["state"] == "TRANSCRIPT_READY"
    assert "原语言材料" in (run / "transcript.md").read_text()
