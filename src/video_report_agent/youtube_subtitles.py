"""Anonymous YouTube metadata and subtitle acquisition for transcript-only runs."""

import math
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.networking.exceptions import RequestError
from yt_dlp.utils import DownloadError

from .bilibili_subtitles import validate_cues
from .ingest import MAX_VIDEO_SECONDS, UrlIngestError, YouTubeSource
from .transcript_foundation import SubtitleParseError, parse_subtitle_file

MAX_SUBTITLE_BYTES = 8 * 1024 * 1024


def _automatic_tracks(info):
    language = (info.get("language") or "").lower()
    tracks = {
        key.removesuffix("-orig"): entries
        for key, entries in (info.get("automatic_captions") or {}).items()
        if key.endswith("-orig")
    }
    # Some non-translatable native tracks have only a bare language key.
    if not tracks:
        for key, entries in (info.get("automatic_captions") or {}).items():
            native = []
            for entry in entries:
                query = parse_qs(urlsplit(entry.get("url") or "").query, keep_blank_values=True)
                if (
                    query.get("kind") == ["asr"] and query.get("lang") == [key]
                    and "tlang" not in query
                ):
                    native.append(entry)
            if native:
                tracks[key] = native
    for candidate in (language, language.split("-")[0]):
        for key, entries in tracks.items():
            if key.lower() == candidate:
                return {key: entries}
    return tracks if len(tracks) == 1 else {}


def _read_track(ydl, entries, duration_ms, *, automatic):
    for extension in ("srt", "vtt"):
        for entry in entries:
            url = entry.get("url")
            if entry.get("ext") != extension or not url:
                continue
            try:
                parsed = urlsplit(url)
                if parsed.scheme != "https" or (automatic and "tlang" in parse_qs(
                    parsed.query, keep_blank_values=True,
                )):
                    continue
                with ydl.urlopen(url) as response:
                    data = response.read(MAX_SUBTITLE_BYTES + 1)
                if len(data) > MAX_SUBTITLE_BYTES:
                    continue
                with TemporaryDirectory() as directory:
                    path = Path(directory) / f"track.{extension}"
                    path.write_text(data.decode("utf-8-sig"), encoding="utf-8")
                    events = parse_subtitle_file(path)
                return validate_cues([
                    {"from": event.start_ms / 1000, "to": event.end_ms / 1000,
                     "content": event.text}
                    for event in events
                ], duration_ms)
            except (RequestError, OSError, ValueError, SubtitleParseError):
                continue
    return []


def fetch_youtube_subtitles(source: YouTubeSource, *, cookies=None) -> dict:
    options = {
        "quiet": True, "no_warnings": True, "noplaylist": True, "cachedir": False,
        "socket_timeout": 10, "extractor_retries": 0, "ignore_no_formats_error": True,
    }
    with YoutubeDL(options) as ydl:
        if cookies is not None:
            ydl.cookiejar = cookies
        try:
            info = ydl.extract_info(source.canonical_url, download=False, ie_key="Youtube")
        except DownloadError:
            raise UrlIngestError("YOUTUBE_UNAVAILABLE", "无法匿名读取这个 YouTube 视频。") from None
        if not info or info.get("id") != source.external_id or info.get("is_live"):
            raise UrlIngestError("URL_INVALID", "请选择可以访问的 YouTube 单视频。")
        duration = info.get("duration")
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            raise UrlIngestError("VIDEO_DURATION_INVALID", "无法确认视频时长，不能处理此视频。")
        if duration > MAX_VIDEO_SECONDS:
            raise UrlIngestError("VIDEO_TOO_LONG", "单个视频最长支持 5 小时。")
        title = info.get("title") or source.external_id
        uploader = info.get("uploader") or info.get("channel") or "作者未记录"
        duration_ms = round(duration * 1000)
        item = {"status": "ABSENT", "count": 0}
        manual = info.get("subtitles") or {}
        language = info.get("language")
        for kind, tracks in (("author", manual), ("automatic", _automatic_tracks(info))):
            for key in sorted(tracks, key=lambda tag: (tag != language, not tag.startswith("zh"))):
                body = _read_track(ydl, tracks[key], duration_ms, automatic=kind == "automatic")
                if body:
                    item = {"status": "READY", "count": len(body), "body": body,
                            "track_kind": kind, "language": key}
                    break
                item = {"status": "FAILED", "count": 0}
            if item["status"] == "READY":
                break
        return {
            "title": title, "uploader": uploader, "duration_ms": duration_ms,
            "attribution": f"YouTube；{uploader}；《{title}》；{source.canonical_url}",
            "sources": {"subtitle_track": item},
        }
