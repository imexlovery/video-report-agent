"""Anonymous-first Douyin single-video acquisition, without ASR or reports."""

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from types import MethodType
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
from yt_dlp import YoutubeDL
from yt_dlp.cookies import YoutubeDLCookieJar
from yt_dlp.utils import DownloadError, ExtractorError

from .ingest import DouyinSource, UrlIngestError
from .platform_media import (
    QuietLogger,
    finish_acquisition,
    metadata_for,
    platform_domain,
    read_platform_cookies,
    save_subtitles,
)

_HOSTS = {"www.douyin.com", "douyin.com", "v.douyin.com", "www.iesdouyin.com"}
_HOME = "https://www.douyin.com/"


def validate_douyin_url(value: str) -> DouyinSource:
    links = re.findall(r'https://[^\s<>"）)]+', value.strip())
    if len(links) != 1:
        raise UrlIngestError("URL_INVALID", "请输入抖音单视频链接或分享文字。")
    link = links[0]
    for _ in range(5):
        try:
            parsed = urlsplit(link)
            host = parsed.hostname
            valid = parsed.scheme == "https" and host in _HOSTS and parsed.netloc == host
        except ValueError:
            valid = False
        if not valid:
            raise UrlIngestError("URL_INVALID", "请输入抖音单视频的 HTTPS 链接。")
        match = re.fullmatch(r"/(?:share/)?video/([0-9]{10,25})/?", parsed.path)
        ids = parse_qs(parsed.query).get("modal_id", [])
        video_id = match.group(1) if match else (
            ids[0] if len(ids) == 1 and re.fullmatch(r"[0-9]{10,25}", ids[0]) else None
        )
        if video_id and host != "v.douyin.com":
            return DouyinSource(video_id, f"{_HOME}video/{video_id}")
        if host != "v.douyin.com" or not re.fullmatch(r"/[A-Za-z0-9_-]+/?", parsed.path):
            break
        try:
            response = httpx.get(link, follow_redirects=False, timeout=10)
        except httpx.HTTPError:
            raise UrlIngestError("URL_RESOLVE_FAILED", "抖音短链接解析失败。") from None
        if response.status_code not in {301, 302, 303, 307, 308}:
            break
        link = urljoin(link, response.headers.get("location", ""))
    raise UrlIngestError("URL_INVALID", "未找到抖音单视频，请使用完整视频链接。")


def read_douyin_cookies(browser=None, cookie_file=None):
    return read_platform_cookies("douyin.com", "DOUYIN_COOKIE", browser, cookie_file)


def _ensure_visitor(self):
    cookies = self._get_cookies(_HOME)
    if not any(name in cookies for name in ("UIFID", "UIFID_TEMP", "uifid")):
        response = self._request_webpage(_HOME, None)
        cookies = self._get_cookies(_HOME)
        if not any(name in cookies for name in ("UIFID", "UIFID_TEMP", "uifid")):
            identity = response.headers.get("x-tt-trace-host")
            if identity:
                self._set_cookie(".douyin.com", "UIFID", identity)
    if "ttwid" not in self._get_cookies(_HOME):
        self._request_webpage(
            _HOME + "ttwid/register", None,
            data=json.dumps({"aid": 6383, "service": "www.douyin.com"}).encode(),
            headers={"Content-Type": "application/json"},
        )
    if "passport_csrf_token" not in self._get_cookies(_HOME):
        self._set_cookie(".douyin.com", "passport_csrf_token", uuid.uuid4().hex)


def extract_douyin(self, url):
    """Adapt yt-dlp's extractor to the current signed detail endpoint.

    Protocol reference: https://github.com/yt-dlp/yt-dlp/pull/17826
    Keep upstream parsing/downloading; initialize a fresh visitor without a browser.
    """
    video_id = self._match_id(url)
    _ensure_visitor(self)
    cookies = self._get_cookies(_HOME)
    identity = next((cookies[name].value for name in ("UIFID", "UIFID_TEMP", "uifid")
                     if name in cookies and cookies[name].value), None)
    if not identity:
        raise ExtractorError("DOUYIN_IDENTITY_MISSING", expected=True)
    timestamp = str(int(time.time()))
    query = {"aweme_id": video_id, "aid": "6383", "uifid": identity, "timestamp": timestamp}
    salt = "A96D855A08C0A9707F8BEF0D9A527E4E"
    signature_input = f"{identity}_{timestamp}_{salt}_{urlencode(query)}"
    signature = hashlib.md5(signature_input.encode()).hexdigest()
    query["x-secsdk-web-signature"] = signature
    try:
        payload = self._download_json(
            _HOME + "aweme/v1/web/aweme/detail/", video_id, query=query,
            headers={"Referer": _HOME},
        )
    except ExtractorError as exc:
        if getattr(exc.cause, "status", None) == 401:
            raise ExtractorError("PLATFORM_LOGIN_REQUIRED", expected=True) from None
        raise
    message = str(payload.get("status_msg") or "").lower()
    if "登录" in message or "login" in message or "log in" in message:
        raise ExtractorError("PLATFORM_LOGIN_REQUIRED", expected=True)
    detail = payload.get("aweme_detail") or {}
    if str(detail.get("aweme_id")) != video_id or not detail.get("video"):
        raise ExtractorError("DOUYIN_VIDEO_UNAVAILABLE", expected=True)
    result = self._parse_aweme_video_app(detail)
    # The shared TikTok parser copies sid_tt to CDN hosts; Douyin media needs no login cookie.
    for cookie in list(self.cookiejar):
        if not platform_domain(cookie.domain, "douyin.com"):
            self.cookiejar.clear(cookie.domain, cookie.path, cookie.name)
    callback = self.get_param("video_report_metadata_callback")
    if callback:
        callback(result)
    # Limit subtitle discovery to the returned Douyin data, not the TikTok webpage fallback.
    subtitles = self._get_subtitles(detail, video_id, None)
    original = (detail["video"].get("cla_info") or {}).get("original_language_info") or {}
    language = original.get("lang") or original.get("language_code")
    return {**result, "language": language, "subtitles": subtitles,
            "http_headers": {"Referer": _HOME}}


def fetch_douyin_media(
    value: str, output: Path, *, browser: str | None = None, cookie_file: Path | None = None,
) -> dict:
    source = validate_douyin_url(value)
    options = {
        "quiet": True, "no_warnings": True, "logger": QuietLogger(),
        "noplaylist": True, "cachedir": False, "socket_timeout": 10, "extractor_retries": 0,
    }
    cookies = YoutubeDLCookieJar()
    auth_mode = "anonymous"
    for attempt in range(2):
        with YoutubeDL(options) as ydl:
            ydl.cookiejar = cookies
            extractor = ydl.get_info_extractor("Douyin")
            extractor._real_extract = MethodType(extract_douyin, extractor)
            try:
                info = ydl.extract_info(source.canonical_url, download=False, ie_key="Douyin")
            except DownloadError as exc:
                if "PLATFORM_LOGIN_REQUIRED" in str(exc):
                    if attempt == 0:
                        cookies = read_douyin_cookies(browser, cookie_file)
                        auth_mode = "cookie"
                        continue
                    raise UrlIngestError("LOGIN_REQUIRED", "抖音未接受当前登录态。") from None
                raise UrlIngestError(
                    "DOUYIN_UNAVAILABLE", "抖音未返回可用视频，请检查视频可访问性及平台请求校验。",
                ) from None
            metadata = metadata_for(source, info)
            subtitles = save_subtitles(
                ydl, info.get("subtitles") or {}, output.resolve() / source.video_id,
                metadata["duration_ms"], original_language=info.get("language"),
            )
        return finish_acquisition(source, metadata, subtitles, cookies, output, auth_mode)
