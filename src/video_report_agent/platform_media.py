"""Shared local acquisition helpers for subtitle-first platform inputs."""

import json
import math
import os
from http.cookiejar import Cookie, LoadError, MozillaCookieJar
from http.cookies import SimpleCookie
from pathlib import Path
from tempfile import TemporaryDirectory

from yt_dlp import YoutubeDL
from yt_dlp.cookies import YDLLogger, YoutubeDLCookieJar, extract_cookies_from_browser
from yt_dlp.networking.exceptions import RequestError

from .audio import extract_audio
from .bilibili_subtitles import validate_cues
from .ingest import MAX_VIDEO_SECONDS, UrlIngestError, _download_video
from .transcript_foundation import SubtitleParseError, parse_subtitle_file


class QuietLogger:
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = debug


def platform_domain(domain, host):
    return domain.lstrip(".") == host or domain.endswith("." + host)


def read_platform_cookies(host, env, browser=None, cookie_file=None):
    """Called only after an explicit login-required response, on the local machine."""
    jar = YoutubeDLCookieJar()
    value = os.getenv(env, "").strip() if env and not browser and not cookie_file else ""
    if cookie_file:
        try:
            stored = MozillaCookieJar(str(cookie_file))
            stored.load(ignore_discard=True, ignore_expires=True)
            for cookie in stored:
                if cookie.expires == 0:
                    cookie.expires, cookie.discard = None, True
                if platform_domain(cookie.domain, host) and not cookie.is_expired():
                    jar.set_cookie(cookie)
        except (OSError, LoadError):
            raise UrlIngestError("COOKIE_UNAVAILABLE", "无法读取本机 Cookie 文件。") from None
    elif value:
        parsed = SimpleCookie()
        parsed.load(value)
        for name, item in parsed.items():
            jar.set_cookie(Cookie(
                version=0, name=name, value=item.value, port=None, port_specified=False,
                domain="." + host, domain_specified=True, domain_initial_dot=True,
                path="/", path_specified=True, secure=True, expires=None, discard=True,
                comment=None, comment_url=None, rest={},
            ))
    elif browser:
        try:
            with YoutubeDL({"quiet": True, "logger": QuietLogger()}) as ydl:
                cookies = extract_cookies_from_browser(browser, logger=YDLLogger(ydl))
            for cookie in cookies:
                if platform_domain(cookie.domain, host) and not cookie.is_expired():
                    jar.set_cookie(cookie)
        except Exception:
            raise UrlIngestError(
                "COOKIE_UNAVAILABLE", "无法读取所选浏览器的平台 Cookie。",
            ) from None
    if not jar:
        raise UrlIngestError(
            "LOGIN_REQUIRED", "该内容要求登录，请在本机登录后用 --browser 指定浏览器。",
        )
    return jar


def metadata_for(source, info):
    duration = info.get("duration")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or duration <= 0):
        raise UrlIngestError("VIDEO_DURATION_INVALID", "无法确认视频时长。")
    if duration > MAX_VIDEO_SECONDS:
        raise UrlIngestError("VIDEO_TOO_LONG", "单个视频最长支持 5 小时。")
    return {
        "platform": source.platform, "video_id": source.video_id, "url": source.public_url,
        "title": info.get("title") or source.external_id,
        "uploader": info.get("channel") or info.get("uploader") or "作者未记录",
        "duration_ms": round(duration * 1000),
    }


def save_subtitles(ydl, tracks, run, duration_ms, original_language=None):
    """Prefer original-language tracks; persist text and timing, never signed URLs."""
    if "source" in tracks:
        languages = ["source"]
    elif original_language:
        languages = [lang for lang in tracks if lang.lower() == original_language.lower()]
    elif len(tracks) <= 1:
        languages = list(tracks)
    else:
        return {"subtitle_status": "LANGUAGE_UNRESOLVED"}
    status = "ABSENT"
    for language in languages:
        for entry in sorted(tracks[language], key=lambda item: item.get("ext") != "srt"):
            extension = entry.get("ext")
            if extension not in {"srt", "vtt", "ass"}:
                continue
            status = "FAILED"
            try:
                data = entry.get("data")
                if data is None:
                    url = entry.get("url", "")
                    if not url.startswith("https://"):
                        continue
                    with ydl.urlopen(url) as response:
                        raw = response.read(8 * 1024 * 1024 + 1)
                    if len(raw) > 8 * 1024 * 1024:
                        continue
                    data = raw.decode("utf-8-sig")
                with TemporaryDirectory() as directory:
                    path = Path(directory) / f"subtitles.{extension}"
                    path.write_text(data, encoding="utf-8")
                    events = parse_subtitle_file(path)
                cues = validate_cues([
                    {"from": event.start_ms / 1000, "to": event.end_ms / 1000,
                     "content": event.text} for event in events
                ], duration_ms)
                run.mkdir(parents=True, exist_ok=True)
                target = run / "subtitles.json"
                target.write_text(json.dumps({
                    "language": entry.get("language") or language,
                    "source": "platform", "body": cues,
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                return {"subtitle_status": "READY", "subtitle_path": str(target),
                        "subtitle_language": entry.get("language") or language,
                        "subtitle_count": len(cues)}
            except (RequestError, OSError, ValueError, SubtitleParseError):
                continue
    return {"subtitle_status": status}


def finish_acquisition(source, metadata, subtitles, cookies, output, auth_mode):
    run = output.resolve() / source.video_id
    result = {**metadata, **subtitles, "auth_mode": auth_mode}
    result["attribution"] = (
        f"{source.platform}；{metadata['uploader']}；《{metadata['title']}》；{source.public_url}"
    )
    if subtitles["subtitle_status"] == "READY":
        result["source"] = "subtitles"
    else:
        with TemporaryDirectory(prefix="video-report-platform-") as temporary:
            cookie_file = Path(temporary) / "cookies.txt"
            cookie_file.touch(mode=0o600)
            cookies.save(str(cookie_file), ignore_discard=True, ignore_expires=True)
            download = _download_video(
                source, run, media_metadata=metadata, cookie_file=cookie_file,
                audio_only=True, allow_muxed_audio=True,
            )
        audio = extract_audio(download.media_path, run / "audio.wav")
        result.update(source="audio", media_path=str(download.media_path),
                      audio_path=str(audio.path), audio_duration_ms=audio.probe.duration_ms)
    (run / "media.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return result
