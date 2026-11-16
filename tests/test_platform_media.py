import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from yt_dlp import YoutubeDL
from yt_dlp.cookies import YoutubeDLCookieJar
from yt_dlp.utils import DownloadError, ExtractorError

from video_report_agent import douyin, platform_media, xiaohongshu
from video_report_agent.ingest import UrlIngestError

XHS_ID = "0123456789abcdef01234567"
XHS_URL = f"https://www.xiaohongshu.com/explore/{XHS_ID}?xsec_token=share-token"
DY_URL = "https://www.douyin.com/video/7696604074514402587"
SRT = "1\n00:00:00,000 --> 00:00:01,000\n原语言字幕\n"


@pytest.mark.parametrize("url", [XHS_URL, XHS_URL.replace("/explore/", "/discovery/item/")])
def test_xhs_preserves_access_parameter_and_discards_tracking(url):
    source = xiaohongshu.validate_xiaohongshu_url("分享 " + url + "&tracking=discard 打开小红书")
    assert source.canonical_url == XHS_URL
    assert source.public_url == XHS_URL


@pytest.mark.parametrize("destination,valid", [(XHS_URL, True), ("https://127.0.0.1/", False)])
def test_xhs_short_link_never_follows_unvalidated_hosts(monkeypatch, destination, valid):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        assert not kwargs["follow_redirects"]
        return httpx.Response(302, headers={"location": destination})

    monkeypatch.setattr(xiaohongshu.httpx, "get", get)
    if valid:
        source = xiaohongshu.validate_xiaohongshu_url("http://xhslink.com/a/Abc")
        assert source.canonical_url == XHS_URL
    else:
        with pytest.raises(UrlIngestError):
            xiaohongshu.validate_xiaohongshu_url("http://xhslink.com/a/Abc")
    assert calls == ["https://xhslink.com/a/Abc"]


@pytest.mark.parametrize("url", [
    XHS_URL.replace("www.xiaohongshu.com", "www.xiaohongshu.com.evil.test"),
    XHS_URL.replace("www.xiaohongshu.com", "user@www.xiaohongshu.com"),
    XHS_URL.replace("www.xiaohongshu.com", "www.xiaohongshu.com:443"),
    "https://www.xiaohongshu.com/user/profile/0123456789abcdef01234567",
])
def test_xhs_invalid_input(url):
    with pytest.raises(UrlIngestError):
        xiaohongshu.validate_xiaohongshu_url(url)


def test_douyin_initializes_visitor_without_login_cookie(monkeypatch):
    with YoutubeDL({"quiet": True}) as ydl:
        ie = ydl.get_info_extractor("Douyin")
        calls = []

        def request(url, *args, **kwargs):
            calls.append(url)
            if url.endswith("ttwid/register"):
                ie._set_cookie(".douyin.com", "ttwid", "visitor")
            return SimpleNamespace(headers={"x-tt-trace-host": "anonymous-identity"})

        monkeypatch.setattr(ie, "_request_webpage", request)
        douyin._ensure_visitor(ie)
        assert {c.name for c in ydl.cookiejar} == {"UIFID", "ttwid", "passport_csrf_token"}
        douyin._ensure_visitor(ie)
        assert len(calls) == 2


@pytest.mark.parametrize("module,url", [(douyin, DY_URL), (xiaohongshu, XHS_URL)])
def test_anonymous_subtitles_skip_browser_and_media(module, url, tmp_path, monkeypatch):
    monkeypatch.setenv("DOUYIN_COOKIE", "sessionid=do-not-read")
    monkeypatch.setenv("XIAOHONGSHU_COOKIE", "web_session=do-not-read")
    monkeypatch.setattr(platform_media, "extract_cookies_from_browser",
                        lambda *a, **kw: pytest.fail("must not read browser"))
    monkeypatch.setattr(platform_media, "_download_video",
                        lambda *a, **kw: pytest.fail("subtitles need no media"))

    def extract(self, *a, **kw):
        assert not list(self.cookiejar)
        return {"title": "Title", "duration": 12, "uploader": "Author", "subtitles": {
            "en": [{"ext": "srt", "data": SRT.replace("原语言字幕", "translation")}],
            "source": [{"ext": "srt", "data": SRT, "language": "zh-CN"}],
        }}

    monkeypatch.setattr(YoutubeDL, "extract_info", extract)
    acquire = module.fetch_douyin_media if module is douyin else module.fetch_xiaohongshu_media
    result = acquire(url, tmp_path, browser="edge")
    assert result["source"] == "subtitles" and result["auth_mode"] == "anonymous"
    assert "audio_path" not in result
    body = json.loads(Path(result["subtitle_path"]).read_text())
    assert body["body"][0]["content"] == "原语言字幕"


@pytest.mark.parametrize("module,url", [(douyin, DY_URL), (xiaohongshu, XHS_URL)])
@pytest.mark.parametrize("message,expected_reads", [
    ("PLATFORM_LOGIN_REQUIRED", 1), ("HTTP Error 403: signature rejected", 0),
    ("XHS_LINK_UNAVAILABLE", 0),
])
def test_login_fallback_only_on_explicit_login(module, url, message, expected_reads,
                                             monkeypatch, tmp_path):
    attempts, reads = [], []

    def extract(*a, **kw):
        attempts.append(1)
        if len(attempts) == 1:
            raise DownloadError(message)
        return {"title": "Title", "duration": 12, "subtitles": {
            "source": [{"ext": "srt", "data": SRT}],
        }}

    def read(*a, **kw):
        reads.append(1)
        return YoutubeDLCookieJar()

    monkeypatch.setattr(YoutubeDL, "extract_info", extract)
    monkeypatch.setattr(module, "read_douyin_cookies" if module is douyin else
                        "read_platform_cookies", read)
    acquire = module.fetch_douyin_media if module is douyin else module.fetch_xiaohongshu_media
    if expected_reads:
        assert acquire(url, tmp_path, browser="edge")["auth_mode"] == "cookie"
    else:
        with pytest.raises(UrlIngestError):
            acquire(url, tmp_path, browser="edge")
    assert len(reads) == expected_reads


def test_bad_subtitle_body_does_not_count_as_ready(tmp_path):
    with YoutubeDL({"quiet": True}) as ydl:
        assert platform_media.save_subtitles(ydl, {"source": [{
            "ext": "srt", "data": "1\n00:00:00,000 --> 01:00:00,000\nwrong timing\n",
        }]}, tmp_path, 12000) == {"subtitle_status": "FAILED"}
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("language,expected", [("en", "en"), (None, None)])
def test_multiple_languages_do_not_prefer_chinese_translation(tmp_path, language, expected):
    tracks = {
        "en": [{"ext": "srt", "data": SRT.replace("原语言字幕", "original English")}],
        "zh-CN": [{"ext": "srt", "data": SRT.replace("原语言字幕", "中文译文")}],
    }
    with YoutubeDL({"quiet": True}) as ydl:
        result = platform_media.save_subtitles(ydl, tracks, tmp_path, 12000, language)
    if expected:
        assert result["subtitle_language"] == expected
        assert "original English" in Path(result["subtitle_path"]).read_text()
    else:
        assert result == {"subtitle_status": "LANGUAGE_UNRESOLVED"}


def test_failed_original_track_does_not_switch_to_translation(tmp_path):
    with YoutubeDL({"quiet": True}) as ydl:
        result = platform_media.save_subtitles(ydl, {
            "source": [{"ext": "srt", "data": "invalid subtitle"}],
            "zh-CN": [{"ext": "srt", "data": SRT}],
        }, tmp_path, 12000)
    assert result == {"subtitle_status": "FAILED"}


def test_xhs_decodes_platform_subtitles_and_author(monkeypatch):
    video = {"media": {"video": {"duration": 12345}}, "mediaV2": json.dumps({
        "video": {"subtitles": {"source": [{
            "url": "https://sns-subtitle.xhscdn.com/a.srt?sign=temporary",
            "language": "zh-CN", "format": 0,
        }]}},
    })}
    state = {"note": {"noteDetailMap": {XHS_ID: {"note": {
        "type": "video", "video": video, "user": {"nickname": "Author"},
    }}}}}
    page = "window.__INITIAL_STATE__=" + json.dumps(state)
    monkeypatch.setattr(xiaohongshu, "_ORIGINAL_EXTRACT", lambda self, url: {"formats": []})
    with YoutubeDL({"quiet": True}) as ydl:
        ie = ydl.get_info_extractor("XiaoHongShu")
        monkeypatch.setattr(ie, "_download_webpage_handle", lambda *a: (
            page, SimpleNamespace(url=XHS_URL),
        ))
        result = xiaohongshu.extract_xiaohongshu(ie, XHS_URL)
    assert result["duration"] == 12.345
    assert result["uploader"] == "Author"
    assert result["subtitles"]["source"][0]["ext"] == "srt"


def test_xhs_expired_share_link_is_not_reported_as_login(monkeypatch):
    with YoutubeDL({"quiet": True}) as ydl:
        ie = ydl.get_info_extractor("XiaoHongShu")
        monkeypatch.setattr(ie, "_download_webpage_handle", lambda *a: (
            'window.__INITIAL_STATE__={"note":{"noteDetailMap":{}}}',
            SimpleNamespace(url="https://www.xiaohongshu.com/404?error_code=300031"),
        ))
        with pytest.raises(ExtractorError, match="XHS_LINK_UNAVAILABLE"):
            xiaohongshu.extract_xiaohongshu(ie, XHS_URL)


def test_local_cookie_export_is_scoped_private_and_never_overwrites(monkeypatch, tmp_path):
    monkeypatch.setenv("XIAOHONGSHU_COOKIE", "web_session=wrong-environment")
    script = Path(__file__).resolve().parents[1] / "scripts/export_platform_cookies.py"
    export = runpy.run_path(str(script))["export"]
    with YoutubeDL({"quiet": True}) as ydl:
        ie = ydl.get_info_extractor("XiaoHongShu")
        ie._set_cookie(".xiaohongshu.com", "web_session", "local-session")
        ie._set_cookie(".douyin.com", "sessionid", "other-platform")
        monkeypatch.setattr(platform_media, "extract_cookies_from_browser",
                            lambda *a, **kw: ydl.cookiejar)
        output = export("xiaohongshu", "edge", tmp_path / "login.cookies.txt")
        assert output.stat().st_mode & 0o777 == 0o600
        assert "local-session" in output.read_text()
        assert "other-platform" not in output.read_text()
        assert "wrong-environment" not in output.read_text()
        loaded = platform_media.read_platform_cookies("xiaohongshu.com", None, cookie_file=output)
        assert [(c.name, c.value) for c in loaded] == [("web_session", "local-session")]
        with pytest.raises(FileExistsError):
            export("xiaohongshu", "edge", output)
        assert "local-session" in output.read_text()


def test_explicit_local_cookie_file_is_not_read_for_public_content(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_media, "_download_video",
                        lambda *a, **kw: pytest.fail("must not download media"))
    monkeypatch.setattr(YoutubeDL, "extract_info", lambda *a, **kw: {
        "title": "Title", "duration": 12, "subtitles": {"source": [{"ext": "srt", "data": SRT}]},
    })
    result = xiaohongshu.fetch_xiaohongshu_media(
        XHS_URL, tmp_path, cookie_file=tmp_path / "nonexistent.cookies.txt",
    )
    assert result["auth_mode"] == "anonymous"
