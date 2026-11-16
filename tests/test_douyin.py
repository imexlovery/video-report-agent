import hashlib
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError

from video_report_agent import cli, douyin
from video_report_agent.ingest import UrlIngestError

VIDEO_ID = "7696604074514402587"
URL = f"https://www.douyin.com/video/{VIDEO_ID}"


@pytest.mark.parametrize("value", [
    URL, URL + "?tracking=ignored", f"https://www.douyin.com/jingxuan?modal_id={VIDEO_ID}",
    f"分享一个视频 {URL} 复制后打开抖音", f"https://www.iesdouyin.com/share/video/{VIDEO_ID}/",
])
def test_single_video_links(value):
    assert douyin.validate_douyin_url(value).canonical_url == URL


@pytest.mark.parametrize("value", [
    "http://www.douyin.com/video/1234567890123456789", URL + "/extra",
    URL.replace("www.douyin.com", "www.douyin.com.evil.test"),
    URL.replace("www.douyin.com", "www.douyin.com@evil.test"),
    URL.replace("www.douyin.com", "www.douyin.com:443"),
    "https://www.douyin.com/user/someone", URL + " " + URL,
])
def test_invalid_sources(value):
    with pytest.raises(UrlIngestError):
        douyin.validate_douyin_url(value)


@pytest.mark.parametrize("location,valid", [
    (URL, True), (f"https://www.iesdouyin.com/share/video/{VIDEO_ID}/", True),
    ("https://127.0.0.1/private", False), ("https://evil.test/", False),
])
def test_short_link_validates_destination_before_request(monkeypatch, location, valid):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        assert kwargs == {"follow_redirects": False, "timeout": 10}
        return httpx.Response(302, headers={"location": location})

    monkeypatch.setattr(douyin.httpx, "get", get)
    if valid:
        assert douyin.validate_douyin_url("https://v.douyin.com/example/").canonical_url == URL
    else:
        with pytest.raises(UrlIngestError):
            douyin.validate_douyin_url("https://v.douyin.com/example/")
    assert calls == ["https://v.douyin.com/example/"]


def test_browser_only_keeps_unexpired_douyin_cookies(monkeypatch):
    monkeypatch.delenv("DOUYIN_COOKIE", raising=False)
    with YoutubeDL({"quiet": True}) as ydl:
        extractor = ydl.get_info_extractor("Douyin")
        extractor._set_cookie(".douyin.com", "UIFID", "identity")
        extractor._set_cookie("www.douyin.com", "sessionid", "session")
        extractor._set_cookie(".evil-douyin.com", "secret", "unrelated")
        extractor._set_cookie(".douyin.com", "expired", "old", expire_time=1)
        monkeypatch.setattr(douyin, "extract_cookies_from_browser", lambda *a, **kw: ydl.cookiejar)
        cookies = douyin.read_douyin_cookies("edge")
    assert {(c.name, c.value) for c in cookies} == {("UIFID", "identity"), ("sessionid", "session")}


def test_signed_request_and_cdn_cookie_scope(monkeypatch):
    monkeypatch.setenv("DOUYIN_COOKIE", "UIFID=test-identity; sid_tt=test-session")
    monkeypatch.setattr(douyin.time, "time", lambda: 1800000000)
    with YoutubeDL({"quiet": True}) as ydl:
        ydl.cookiejar = douyin.read_douyin_cookies()
        extractor = ydl.get_info_extractor("Douyin")

        def detail(endpoint, video_id, *, query, headers):
            assert endpoint == "https://www.douyin.com/aweme/v1/web/aweme/detail/"
            assert video_id == VIDEO_ID
            unsigned = {
                key: value for key, value in query.items() if key != "x-secsdk-web-signature"
            }
            expected = hashlib.md5(
                ("test-identity_1800000000_A96D855A08C0A9707F8BEF0D9A527E4E_"
                 + urlencode(unsigned)).encode()
            ).hexdigest()
            assert query["uifid"] == "test-identity"
            assert query["x-secsdk-web-signature"] == expected
            return {"aweme_detail": {"aweme_id": VIDEO_ID, "video": {"duration": 12000}}}

        def parse(_):
            extractor._set_cookie("media.example", "sid_tt", "test-session")
            return {"id": VIDEO_ID}

        monkeypatch.setattr(extractor, "_download_json", detail)
        monkeypatch.setattr(extractor, "_parse_aweme_video_app", parse)
        result = douyin.extract_douyin(extractor, URL)
        assert result["http_headers"] == {"Referer": "https://www.douyin.com/"}
        assert {cookie.domain for cookie in ydl.cookiejar} == {".douyin.com"}


def test_missing_identity_is_not_reported_as_expired_login(monkeypatch):
    monkeypatch.setenv("DOUYIN_COOKIE", "sessionid=valid-session")
    with YoutubeDL({"quiet": True}) as ydl:
        ydl.cookiejar = douyin.read_douyin_cookies()
        with pytest.raises(ExtractorError, match="DOUYIN_IDENTITY_MISSING"):
            douyin.extract_douyin(ydl.get_info_extractor("Douyin"), URL)


@pytest.mark.parametrize("download_fails", [False, True])
def test_acquisition_cleans_credentials_and_saves_only_public_metadata(
    tmp_path, monkeypatch, download_fails,
):
    monkeypatch.setenv("DOUYIN_COOKIE", "UIFID=private-identity; sessionid=private-session")
    monkeypatch.setattr(YoutubeDL, "extract_info", lambda *a, **kw: {
        "id": VIDEO_ID, "title": "Title", "channel": "Author", "duration": 12,
        "url": "https://media.example/?private-token=value", "http_headers": {"Cookie": "secret"},
    })
    files = []

    def download(source, run, metadata, cookie_file):
        files.append(cookie_file)
        assert cookie_file.stat().st_mode & 0o777 == 0o600
        assert "private-identity" in cookie_file.read_text()
        assert "private-token" not in str(metadata)
        if download_fails:
            raise UrlIngestError("DOWNLOAD_ERROR", "download failed")
        run.mkdir(parents=True)
        media = run / "source.mp4"
        media.write_bytes(b"video")
        return SimpleNamespace(media_path=media, attribution="抖音；Author")

    monkeypatch.setattr(douyin, "download_douyin_video", download)
    monkeypatch.setattr(douyin, "extract_audio", lambda media, path: SimpleNamespace(
        path=path, probe=SimpleNamespace(duration_ms=12000),
    ))
    if download_fails:
        with pytest.raises(UrlIngestError):
            douyin.fetch_douyin_media(URL, tmp_path)
    else:
        result = douyin.fetch_douyin_media(URL, tmp_path)
        assert result["platform"] == "抖音"
        assert result["uploader"] == "Author"
        saved = (Path(result["video_path"]).parent / "media.json").read_text()
        assert all(secret not in saved for secret in (
            "private-identity", "private-session", "private-token", "Cookie",
        ))
    assert files and all(not path.exists() for path in files)


def test_raw_extractor_errors_are_not_exposed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DOUYIN_COOKIE", "UIFID=private-identity")

    def fail(*a, **kw):
        raise DownloadError("https://example.test/?uifid=private-identity")

    monkeypatch.setattr(YoutubeDL, "extract_info", fail)
    with pytest.raises(UrlIngestError) as error:
        douyin.fetch_douyin_media(URL, tmp_path)
    assert "private-identity" not in str(error.value)
    assert "private-identity" not in str(capsys.readouterr())


def test_fetch_cli_does_not_create_report_run(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", [
        "video-report", "fetch-douyin", URL, "--browser", "edge", "--output", str(tmp_path),
    ])
    monkeypatch.setattr(cli, "create_run", lambda *a, **kw: pytest.fail("must not create a run"))
    monkeypatch.setattr(douyin, "fetch_douyin_media", lambda *a, **kw: {"platform": "抖音"})
    assert cli.main() == 0
