"""Cookie-authenticated Douyin single-video acquisition, without ASR or reports."""

import hashlib
import json
import math
import os
import re
import time
from http.cookiejar import Cookie
from http.cookies import SimpleCookie
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MethodType
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
from yt_dlp import YoutubeDL
from yt_dlp.cookies import YDLLogger, YoutubeDLCookieJar, extract_cookies_from_browser
from yt_dlp.utils import DownloadError, ExtractorError

from .audio import extract_audio
from .ingest import MAX_VIDEO_SECONDS, DouyinSource, UrlIngestError, download_douyin_video

_HOSTS = {"www.douyin.com", "douyin.com", "v.douyin.com", "www.iesdouyin.com"}
_HOME = "https://www.douyin.com/"


class _QuietLogger:
    # Upstream errors may include signed URLs. Expose only our classified errors.
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = debug


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


def _douyin_domain(domain: str) -> bool:
    return domain.lstrip(".") == "douyin.com" or domain.endswith(".douyin.com")


def read_douyin_cookies(browser: str | None = None) -> YoutubeDLCookieJar:
    jar = YoutubeDLCookieJar()
    value = os.getenv("DOUYIN_COOKIE", "").strip()
    if value:
        parsed = SimpleCookie()
        parsed.load(value)
        for name, item in parsed.items():
            jar.set_cookie(Cookie(
                version=0, name=name, value=item.value, port=None, port_specified=False,
                domain=".douyin.com", domain_specified=True, domain_initial_dot=True,
                path="/", path_specified=True, secure=True, expires=None, discard=True,
                comment=None, comment_url=None, rest={},
            ))
    elif browser:
        try:
            with YoutubeDL({"quiet": True, "logger": _QuietLogger()}) as ydl:
                cookies = extract_cookies_from_browser(browser, logger=YDLLogger(ydl))
            for cookie in cookies:
                if _douyin_domain(cookie.domain) and not cookie.is_expired():
                    jar.set_cookie(cookie)
        except Exception:
            raise UrlIngestError(
                "DOUYIN_COOKIE_UNAVAILABLE", "无法读取所选浏览器的抖音 Cookie。",
            ) from None
    if not jar:
        raise UrlIngestError(
            "DOUYIN_COOKIE_UNAVAILABLE", "请使用 --browser 指定浏览器，或设置 DOUYIN_COOKIE。",
        )
    return jar


def extract_douyin(self, url):
    """Adapt yt-dlp's extractor to the current signed detail endpoint.

    Protocol reference: https://github.com/yt-dlp/yt-dlp/pull/17826
    Keep upstream format parsing/downloading; use only supplied browser identity.
    """
    video_id = self._match_id(url)
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
    payload = self._download_json(
        _HOME + "aweme/v1/web/aweme/detail/", video_id, query=query,
        headers={"Referer": _HOME},
    )
    detail = payload.get("aweme_detail") or {}
    if str(detail.get("aweme_id")) != video_id or not detail.get("video"):
        raise ExtractorError("DOUYIN_VIDEO_UNAVAILABLE", expected=True)
    result = self._parse_aweme_video_app(detail)
    # The shared TikTok parser copies sid_tt to CDN hosts; Douyin media needs no login cookie.
    for cookie in list(self.cookiejar):
        if not _douyin_domain(cookie.domain):
            self.cookiejar.clear(cookie.domain, cookie.path, cookie.name)
    return {**result, "http_headers": {"Referer": _HOME}}


def fetch_douyin_media(value: str, output: Path, *, browser: str | None = None) -> dict:
    source = validate_douyin_url(value)
    cookies = read_douyin_cookies(browser)
    options = {
        "quiet": True, "no_warnings": True, "logger": _QuietLogger(),
        "noplaylist": True, "cachedir": False, "socket_timeout": 10, "extractor_retries": 0,
    }
    with YoutubeDL(options) as ydl:
        ydl.cookiejar = cookies
        extractor = ydl.get_info_extractor("Douyin")
        extractor._real_extract = MethodType(extract_douyin, extractor)
        try:
            info = ydl.extract_info(source.canonical_url, download=False, ie_key="Douyin")
        except DownloadError as exc:
            if "DOUYIN_IDENTITY_MISSING" in str(exc):
                raise UrlIngestError(
                    "DOUYIN_IDENTITY_MISSING",
                    "Cookie 中缺少 UIFID，请先在所选浏览器打开抖音视频。",
                ) from None
            raise UrlIngestError(
                "DOUYIN_UNAVAILABLE", "抖音未返回可用视频，请检查视频可访问性及平台请求校验。",
            ) from None
    duration = info.get("duration")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or duration <= 0):
        raise UrlIngestError("VIDEO_DURATION_INVALID", "无法确认视频时长。")
    if duration > MAX_VIDEO_SECONDS:
        raise UrlIngestError("VIDEO_TOO_LONG", "单个视频最长支持 5 小时。")
    metadata = {
        "platform": "抖音", "video_id": source.video_id, "url": source.canonical_url,
        "title": info.get("title") or source.external_id,
        "uploader": info.get("channel") or info.get("uploader") or "作者未记录",
        "duration_ms": round(duration * 1000),
    }
    run = output.resolve() / source.video_id
    with TemporaryDirectory(prefix="video-report-douyin-") as temporary:
        cookie_file = Path(temporary) / "cookies.txt"
        cookie_file.touch(mode=0o600)
        # yt-dlp serializes session cookies as expiry 0; they must survive this save.
        cookies.save(str(cookie_file), ignore_discard=True, ignore_expires=True)
        download = download_douyin_video(source, run, metadata, cookie_file)
    audio = extract_audio(download.media_path, run / "audio.wav")
    result = {
        **metadata, "attribution": download.attribution,
        "video_path": str(download.media_path), "audio_path": str(audio.path),
        "audio_duration_ms": audio.probe.duration_ms,
    }
    (run / "media.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return result
