"""Anonymous-first Xiaohongshu share-page subtitles and media."""

import json
import re
from pathlib import Path
from types import MethodType
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit

import httpx
from yt_dlp import YoutubeDL
from yt_dlp.cookies import YoutubeDLCookieJar
from yt_dlp.extractor.xiaohongshu import XiaoHongShuIE
from yt_dlp.utils import DownloadError, ExtractorError, js_to_json

from .ingest import UrlIngestError, XiaohongshuSource
from .platform_media import (
    QuietLogger,
    finish_acquisition,
    metadata_for,
    read_platform_cookies,
    save_subtitles,
)

_ORIGINAL_EXTRACT = XiaoHongShuIE._real_extract
_HOSTS = {"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com", "www.xhslink.com"}


def validate_xiaohongshu_url(value):
    links = re.findall(r'https?://[^\s<>"）)]+', value.strip())
    if len(links) != 1:
        raise UrlIngestError("URL_INVALID", "请粘贴小红书完整分享链接或分享文字。")
    link = links[0]
    for _ in range(5):
        try:
            parsed = urlsplit(link)
            host = parsed.hostname
            valid = parsed.scheme in {"http", "https"} and host in _HOSTS and parsed.netloc == host
        except ValueError:
            valid = False
        if not valid:
            raise UrlIngestError("URL_INVALID", "请输入小红书视频笔记分享链接。")
        link = parsed._replace(scheme="https").geturl()
        if host in {"xiaohongshu.com", "www.xiaohongshu.com"}:
            match = re.fullmatch(r"/(?:explore|discovery/item)/([a-f0-9]{24})/?", parsed.path)
            if not match:
                break
            query = [(k, v) for k, v in parse_qsl(parsed.query)
                     if k in {"xsec_token", "xsec_source"}]
            canonical = "https://www.xiaohongshu.com/explore/" + match.group(1)
            if query:
                canonical += "?" + urlencode(query)
            return XiaohongshuSource(match.group(1), canonical)
        if not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+/?", parsed.path):
            break
        try:
            response = httpx.get(link, follow_redirects=False, timeout=10)
        except httpx.HTTPError:
            raise UrlIngestError("URL_RESOLVE_FAILED", "小红书短链接解析失败。") from None
        if response.status_code not in {301, 302, 303, 307, 308}:
            break
        link = urljoin(link, response.headers.get("location", ""))
    raise UrlIngestError("URL_INVALID", "未找到视频笔记，请重新复制完整分享链接。")


def extract_xiaohongshu(self, url):
    note_id = self._match_id(url)
    try:
        page, response = self._download_webpage_handle(url, note_id)
    except ExtractorError as exc:
        if getattr(exc.cause, "status", None) == 401:
            raise ExtractorError("PLATFORM_LOGIN_REQUIRED", expected=True) from None
        raise
    final = urlsplit(response.url)
    message = " ".join(parse_qs(final.query).get("error_msg", []))
    if final.path.startswith("/login") or "登录" in message:
        raise ExtractorError("PLATFORM_LOGIN_REQUIRED", expected=True)
    state = self._search_json(
        r"window\.__INITIAL_STATE__\s*=", page, "initial state", note_id,
        transform_source=js_to_json,
    )
    note = state.get("note", {}).get("noteDetailMap", {}).get(note_id, {}).get("note") or {}
    if not note:
        raise ExtractorError("XHS_LINK_UNAVAILABLE", expected=True)
    if note.get("type") != "video" or not note.get("video"):
        raise ExtractorError("XHS_NOT_VIDEO", expected=True)
    # Reuse upstream format parsing without a second share-page request.
    original_page = self._download_webpage
    self._download_webpage = lambda *args, **kwargs: page
    try:
        info = _ORIGINAL_EXTRACT(self, url)
    finally:
        self._download_webpage = original_page
    video = note["video"]
    subtitles = {}
    try:
        v2 = json.loads(video.get("mediaV2") or "{}")
        for language, tracks in (v2.get("video", {}).get("subtitles") or {}).items():
            subtitles[language] = [
                {"url": track["url"], "language": track.get("language", language), "ext": "srt"}
                for track in tracks if isinstance(track, dict) and track.get("url")
                and urlsplit(track["url"]).path.endswith(".srt")
            ]
    except (ValueError, TypeError, AttributeError):
        pass
    duration_ms = video.get("media", {}).get("video", {}).get("duration")
    duration = duration_ms / 1000 if isinstance(duration_ms, (int, float)) else (
        video.get("capa", {}).get("duration")
    )
    return {**info, "duration": duration, "subtitles": subtitles,
            "uploader": note.get("user", {}).get("nickname") or info.get("uploader_id")}


def fetch_xiaohongshu_media(
    value: str, output: Path, *, browser: str | None = None, cookie_file: Path | None = None,
) -> dict:
    source = validate_xiaohongshu_url(value)
    cookies = YoutubeDLCookieJar()
    auth_mode = "anonymous"
    options = {
        "quiet": True, "no_warnings": True, "logger": QuietLogger(),
        "noplaylist": True, "cachedir": False, "socket_timeout": 10, "extractor_retries": 0,
    }
    for attempt in range(2):
        with YoutubeDL(options) as ydl:
            ydl.cookiejar = cookies
            extractor = ydl.get_info_extractor("XiaoHongShu")
            extractor._real_extract = MethodType(extract_xiaohongshu, extractor)
            try:
                info = ydl.extract_info(source.canonical_url, download=False, ie_key="XiaoHongShu")
            except DownloadError as exc:
                error = str(exc)
                if "PLATFORM_LOGIN_REQUIRED" in error:
                    if attempt == 0:
                        cookies = read_platform_cookies(
                            "xiaohongshu.com", "XIAOHONGSHU_COOKIE", browser, cookie_file,
                        )
                        auth_mode = "cookie"
                        continue
                    raise UrlIngestError("LOGIN_REQUIRED", "小红书未接受当前登录态。") from None
                if "XHS_NOT_VIDEO" in error:
                    raise UrlIngestError(
                        "URL_INVALID", "请选择视频笔记，暂不处理图文笔记。",
                    ) from None
                raise UrlIngestError(
                    "XHS_UNAVAILABLE",
                    "无法读取小红书视频，请重新复制完整分享链接并保留 xsec_token；"
                    "也可能是内容不可访问。",
                ) from None
            metadata = metadata_for(source, info)
            subtitles = save_subtitles(
                ydl, info.get("subtitles") or {}, output.resolve() / source.video_id,
                metadata["duration_ms"], original_language=info.get("language"),
            )
        return finish_acquisition(source, metadata, subtitles, cookies, output, auth_mode)
