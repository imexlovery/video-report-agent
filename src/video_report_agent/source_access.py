"""Platform recognition, anonymous preflight, and user-side material acquisition."""

import json
import re
import tempfile
from pathlib import Path
from types import MethodType
from urllib.parse import urlsplit

import httpx
from yt_dlp import YoutubeDL
from yt_dlp.cookies import YoutubeDLCookieJar
from yt_dlp.networking import Request
from yt_dlp.utils import DownloadError

from .bilibili_subtitles import BilibiliSubtitles, SubtitleError
from .douyin import extract_douyin, validate_douyin_url
from .ingest import (
    BilibiliSource,
    DouyinSource,
    UrlIngestError,
    XiaohongshuSource,
    YouTubeSource,
    _download_video,
    validate_bilibili_url,
    validate_source_url,
)
from .platform_media import QuietLogger, metadata_for, read_platform_cookies, save_subtitles
from .xiaohongshu import extract_xiaohongshu, validate_xiaohongshu_url
from .youtube_subtitles import fetch_youtube_subtitles


def resolve_source(value):
    if not isinstance(value, str) or len(value) > 8192:
        raise UrlIngestError("URL_INVALID", "请输入一个视频链接或分享文字。")
    text = value.strip()
    if re.match(r"^(?:(?:www|m)\.)?(?:bilibili\.com|youtube\.com|youtu\.be|"
                r"douyin\.com|xiaohongshu\.com|xhslink\.(?:com|cn))/", text):
        text = "https://" + text
    # Markdown link labels may repeat or escape the URL; only their targets are links.
    text = re.sub(r"\[[^\]\r\n]*\]\((https?://[^\s()]+)\)", r"\1", text)
    links = list(dict.fromkeys(
        link.rstrip("。），,；;！!") for link in re.findall(r"https?://[^\s<>\"）)]+", text)
    ))
    if not links:
        return validate_bilibili_url(text)
    if len(links) != 1:
        raise UrlIngestError("URL_INVALID", "请一次提交一个视频链接。")
    link = links[0]
    try:
        host = urlsplit(link).hostname
    except ValueError:
        raise UrlIngestError("URL_INVALID", "视频链接格式无效。") from None
    if host in {"www.bilibili.com", "bilibili.com", "m.bilibili.com", "b23.tv"}:
        return validate_bilibili_url(link)
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        return validate_source_url(link, allow_youtube=True)
    if host in {"douyin.com", "www.douyin.com", "v.douyin.com", "www.iesdouyin.com"}:
        return validate_douyin_url(link)
    if host in {"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com", "www.xhslink.com",
                "xhslink.cn", "www.xhslink.cn"}:
        return validate_xiaohongshu_url(link)
    raise UrlIngestError("URL_INVALID", "支持 B站、YouTube、抖音和小红书的单视频链接。")


def platform_key(source):
    return source.video_id.split("-", 1)[0]


class AccessLogger(QuietLogger):
    preview = False

    def warning(self, message):
        if "only the preview" in message.lower() or ("仅" in message and "试看" in message):
            self.preview = True


def _subtitle_snapshot(source, cookies):
    if isinstance(source, BilibiliSource):
        with httpx.Client(timeout=15, follow_redirects=False, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/",
        }) as client:
            for cookie in cookies:
                if cookie.name == "SESSDATA":
                    client.cookies.set("SESSDATA", cookie.value, domain="api.bilibili.com")
            with BilibiliSubtitles(client=client, authenticate=False) as subtitles:
                return subtitles.fetch(source, first_available=True)
    if isinstance(source, YouTubeSource):
        return fetch_youtube_subtitles(source, cookies=cookies)
    return None


def _open_media(ydl, formats):
    candidates = [f for f in formats if f.get("url") and f.get("acodec") != "none"]
    candidates.sort(key=lambda f: f.get("vcodec") != "none")
    for item in candidates[:2]:
        if urlsplit(item["url"]).scheme not in {"http", "https"}:
            continue
        headers = {k: v for k, v in item.get("http_headers", {}).items()
                   if k.lower() not in {"cookie", "authorization"}}
        headers["Range"] = "bytes=0-1023"
        try:
            with ydl.urlopen(Request(item["url"], headers=headers)) as response:
                body = response.read(1024)
                mime = response.headers.get("Content-Type", "").lower()
            if body and "text/html" not in mime and "application/json" not in mime:
                return True
        except Exception:
            continue
    return False


def inspect_source(source, *, cookies=None, check_audio=True):
    """Return a public snapshot and ephemeral cookies; never load a browser implicitly."""
    jar = cookies if cookies is not None else YoutubeDLCookieJar()
    snapshot = {"sources": {"subtitle_track": {"status": "ABSENT", "count": 0}}}
    subtitle_login = False
    try:
        snapshot = _subtitle_snapshot(source, jar) or snapshot
        subtitle_login = any(x.get("status") == "LOGIN_REQUIRED"
                             for x in snapshot["sources"].values())
    except SubtitleError as exc:
        subtitle_login = exc.category == "SUBTITLE_LOGIN_REQUIRED"
    except UrlIngestError:
        pass
    if any(x.get("status") == "READY" for x in snapshot["sources"].values()):
        return snapshot, jar, "READY", "SKIPPED"
    logger = AccessLogger()
    options = {"quiet": True, "no_warnings": True, "logger": logger, "cachedir": False,
               "noplaylist": True, "socket_timeout": 10, "extractor_retries": 0}
    with YoutubeDL(options) as ydl:
        ydl.cookiejar = jar
        key = None
        if isinstance(source, DouyinSource):
            key, extract = "Douyin", extract_douyin
        elif isinstance(source, XiaohongshuSource):
            key, extract = "XiaoHongShu", extract_xiaohongshu
        if key:
            extractor = ydl.get_info_extractor(key)
            extractor._real_extract = MethodType(extract, extractor)
        try:
            info = ydl.extract_info(source.canonical_url, download=False, ie_key=key)
        except DownloadError as exc:
            error = str(exc).lower()
            login = any(term in error for term in (
                "platform_login_required", "supporter-only", "premium members only", "sign in",
                "login required", "log in", "登录", "充电专属",
            ))
            return snapshot, jar, "LOGIN_REQUIRED" if login else "UNAVAILABLE", "FAILED"
        if logger.preview:
            return snapshot, jar, "LOGIN_REQUIRED", "PREVIEW_ONLY"
        if info.get("is_live") or info.get("live_status") == "is_live":
            return snapshot, jar, "UNAVAILABLE", "LIVE_UNSUPPORTED"
        if isinstance(source, (BilibiliSource, YouTubeSource)):
            duration = info.get("duration")
            if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                    or not 0 < duration <= 18000):
                raise UrlIngestError("VIDEO_DURATION_INVALID", "视频时长无效或超过 5 小时。")
            title = info.get("title") or source.video_id
            uploader = info.get("uploader") or info.get("channel") or "作者未记录"
            snapshot.update(title=title, uploader=uploader, duration_ms=round(duration * 1000),
                            attribution=f"{source.platform}；{uploader}；《{title}》；{source.canonical_url}")
        else:
            snapshot.update(metadata_for(source, info))
            snapshot["attribution"] = (
                f"{source.platform}；{snapshot['uploader']}；《{snapshot['title']}》；{source.canonical_url}"
            )
            with tempfile.TemporaryDirectory() as directory:
                selected = save_subtitles(ydl, info.get("subtitles") or {}, Path(directory),
                                          snapshot["duration_ms"], info.get("language"))
                track = {"status": selected["subtitle_status"], "count": 0}
                if track["status"] == "READY":
                    saved = json.loads(Path(selected["subtitle_path"]).read_text())
                    track.update(body=saved["body"], count=len(saved["body"]),
                                 language=saved["language"])
                snapshot["sources"] = {"subtitle_track": track}
            if track["status"] == "READY":
                return snapshot, jar, "READY", "SKIPPED"
        available = (_open_media(ydl, info.get("formats") or []) if check_audio
                     else bool(info.get("formats")))
        return snapshot, jar, ("READY" if available else
                               "LOGIN_REQUIRED" if subtitle_login else "UNAVAILABLE"), (
                                   "READY" if available else "FAILED")


def preflight_source(value):
    source = resolve_source(value)
    snapshot, _, access, audio = inspect_source(source)
    return {
        "platform": source.platform, "platform_key": platform_key(source),
        "url": source.canonical_url, "video_id": source.video_id,
        "page_number": getattr(source, "page_number", 1),
        "title": snapshot.get("title", "视频取材检查"),
        "duration": snapshot.get("duration_ms", 0) / 1000,
        "access": access, "audio": audio,
        "subtitles": {name: {k: v for k, v in item.items() if k != "body"}
                      for name, item in snapshot["sources"].items()},
        "available": access == "READY",
    }


def prepare_source(value, *, browser=None, cookie_file=None, allow_login=True):
    source = resolve_source(value)
    snapshot, cookies, access, _ = inspect_source(source)
    if access == "LOGIN_REQUIRED":
        if not allow_login:
            raise UrlIngestError("LOGIN_REQUIRED", "该视频需要在有访问权限的账号所在电脑取材。")
        key = platform_key(source)
        host = {"bilibili": "bilibili.com", "youtube": "youtube.com",
                "douyin": "douyin.com", "xiaohongshu": "xiaohongshu.com"}[key]
        cookies = read_platform_cookies(host, key.upper() + "_COOKIE", browser, cookie_file)
        snapshot, cookies, access, _ = inspect_source(source, cookies=cookies)
    if access != "READY":
        raise UrlIngestError(access, "未取得完整字幕或音频；请检查链接及当前账号的视频访问权限。")
    return source, snapshot, cookies


def download_source_audio(source, output, snapshot, cookies):
    with tempfile.TemporaryDirectory(prefix="video-report-source-") as directory:
        path = Path(directory) / "cookies.txt"
        path.touch(mode=0o600)
        cookies.save(str(path), ignore_discard=True, ignore_expires=True)
        return _download_video(source, output, media_metadata=snapshot,
                               cookie_file=path, audio_only=True, allow_muxed_audio=True)


def acquire_source(value, output, *, browser=None, cookie_file=None):
    """User-side and pipeline entry: anonymous first; login only on an access requirement."""
    source, snapshot, cookies = prepare_source(value, browser=browser, cookie_file=cookie_file)
    output = output.resolve() / source.video_id
    output.mkdir(parents=True, exist_ok=True)
    (output / "source-material.json").write_text(json.dumps({
        "url": source.canonical_url, "video_id": source.video_id, **snapshot,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    if any(x.get("status") == "READY" for x in snapshot["sources"].values()):
        return source, snapshot, None
    downloaded = download_source_audio(source, output, snapshot, cookies)
    return source, snapshot, downloaded
