"""Bounded public Bilibili URL download and existing-ingest composition."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

import httpx

BILIBILI_HOSTS = frozenset({"bilibili.com", "www.bilibili.com"})
_BVID_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}")
_PAGE_PATTERN = re.compile(r"[0-9]+")
_SOURCE_NAME = "source"
MAX_VIDEO_SECONDS = 5 * 60 * 60

CommandRunner = Callable[..., Any]


@dataclass(frozen=True)
class BilibiliSource:
    """The validated public source URL and its stable BVID identity."""

    bvid: str
    page_number: int
    submitted_url: str
    canonical_url: str

    @property
    def video_id(self) -> str:
        return f"bilibili-{self.bvid}-p{self.page_number}"

    @property
    def cache_key(self) -> str:
        return f"{self.bvid}/P{self.page_number}"


@dataclass(frozen=True)
class DownloadResult:
    """Verified yt-dlp output and public metadata."""

    source: BilibiliSource
    media_path: Path
    info_path: Path
    title: str
    uploader: str
    attribution: str
    command: tuple[str, ...]


class UrlIngestError(RuntimeError):
    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(message)


def _parse_bilibili_url(value: object) -> tuple[str, str, int, str]:
    if not isinstance(value, str):
        raise UrlIngestError("URL_INVALID", "url must be a string")
    submitted_url = value.strip()
    # Bilibili's copied share text prefixes the link with a bracketed title.
    link = re.sub(r"^【.*】\s*", "", submitted_url, count=1, flags=re.DOTALL).strip()
    markdown_link = re.fullmatch(r"\[[^\]\r\n]*\]\(((?:https://|www\.)[^\s()]+)\)", link)
    if markdown_link:
        link = markdown_link.group(1)
    if link.startswith("www."):
        link = "https://" + link
    if link.startswith("https://b23.tv/"):
        short = urlsplit(link)
        if not re.fullmatch(r"/[A-Za-z0-9]+/?", short.path) or short.fragment:
            raise UrlIngestError("URL_INVALID", "无效的 Bilibili 短链接")
        try:
            response = httpx.get(link, follow_redirects=False, timeout=10)
        except httpx.HTTPError as exc:
            raise UrlIngestError(
                "URL_RESOLVE_FAILED", "Bilibili 短链接解析失败，请稍后重试或粘贴完整视频链接"
            ) from exc
        if response.status_code not in {301, 302, 303, 307, 308}:
            raise UrlIngestError("URL_RESOLVE_FAILED", "Bilibili 短链接未返回视频地址")
        # Validate the redirect below before any request to its destination.
        link = response.headers.get("location", "")
    normalized_url = (
        f"https://www.bilibili.com/video/{link}/"
        if _BVID_PATTERN.fullmatch(link)
        else link
    )
    try:
        parsed = urlsplit(normalized_url)
        hostname = parsed.hostname.lower() if parsed.hostname else None
        port = parsed.port
    except ValueError as exc:
        raise UrlIngestError("URL_INVALID", "url is not a valid HTTPS Bilibili URL") from exc
    if parsed.scheme != "https" or hostname not in BILIBILI_HOSTS:
        raise UrlIngestError("URL_INVALID", "url must use HTTPS and a Bilibili host")
    if parsed.username or parsed.password or port is not None:
        raise UrlIngestError("URL_INVALID", "url must not include credentials or a port")
    if parsed.fragment:
        raise UrlIngestError("URL_INVALID", "url must not include a fragment")
    match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10})/?", parsed.path)
    if match is None or _BVID_PATTERN.fullmatch(match.group(1)) is None:
        raise UrlIngestError("URL_INVALID", "url must use /video/<BVID>")
    bvid = match.group(1)
    page_values = [
        query_value
        for key, query_value in parse_qsl(parsed.query, keep_blank_values=True)
        if key == "p"
    ]
    if len(page_values) > 1 or (page_values and _PAGE_PATTERN.fullmatch(page_values[0]) is None):
        raise UrlIngestError("URL_INVALID", "p must be one positive page number")
    page_number = int(page_values[0]) if page_values else 1
    if page_number < 1:
        raise UrlIngestError("URL_INVALID", "p must be one positive page number")
    canonical_url = f"https://www.bilibili.com/video/{bvid}/"
    if page_values:
        canonical_url += f"?p={page_number}"
    return submitted_url, bvid, page_number, canonical_url


def clean_bilibili_url(value: object) -> str:
    """Keep the BVID and an optional numeric page, dropping other URL data."""

    return _parse_bilibili_url(value)[3]


def validate_bilibili_url(value: object) -> BilibiliSource:
    """Validate a public BV URL, resolving b23.tv share links when necessary."""

    submitted_url, bvid, page_number, canonical_url = _parse_bilibili_url(value)
    return BilibiliSource(
        bvid=bvid,
        page_number=page_number,
        submitted_url=submitted_url,
        canonical_url=canonical_url,
    )


def probe_bilibili_video(
    source: BilibiliSource, *, runner: CommandRunner | None = None,
) -> dict:
    """Read the selected part's metadata without downloading media or writing files."""
    executable = shutil.which("yt-dlp")
    if executable is None:
        raise UrlIngestError("VIDEO_METADATA_UNAVAILABLE", "视频时长查询暂不可用。")
    command = [
        executable, "--ignore-config", "--no-playlist", "--skip-download",
        "--dump-single-json", "--socket-timeout", "15", "--retries", "0",
        source.canonical_url,
    ]
    try:
        result = (runner or subprocess.run)(
            command, capture_output=True, text=True, check=False, shell=False, timeout=45,
        )
        if result.returncode != 0:
            raise ValueError("metadata command failed")
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or payload.get("_type") in {"playlist", "multi_video"}:
            raise ValueError("expected a single video part")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise UrlIngestError(
            "VIDEO_METADATA_UNAVAILABLE", "无法查询视频时长，请稍后重试。",
        ) from exc
    duration = payload.get("duration")
    if (
        isinstance(duration, bool) or not isinstance(duration, (int, float))
        or not math.isfinite(duration) or duration <= 0
    ):
        raise UrlIngestError("VIDEO_DURATION_INVALID", "无法确认视频时长，不能处理此视频。")
    if duration > MAX_VIDEO_SECONDS:
        raise UrlIngestError("VIDEO_TOO_LONG", "单个视频最长支持 5 小时。")
    return {
        "bvid": source.bvid, "page_number": source.page_number,
        "video_id": source.video_id, "url": source.canonical_url,
        "title": str(payload.get("title") or source.bvid), "duration": duration,
    }


def _run_command(command: list[str], runner: CommandRunner | None, *, category: str) -> Any:
    active_runner = runner or subprocess.run
    try:
        return active_runner(
            command,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UrlIngestError(category, "external command could not be started") from exc


def _inside(path: Path, root: Path, *, category: str, message: str) -> Path:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise UrlIngestError(category, message) from exc
    return resolved_path


def _printed_media_path(stdout: object, download_dir: Path) -> Path:
    if not isinstance(stdout, str):
        raise UrlIngestError("DOWNLOAD_ERROR", "yt-dlp did not report a media path")
    for line in reversed(stdout.splitlines()):
        candidate_text = line.strip()
        if not candidate_text:
            continue
        candidate = Path(candidate_text)
        if not candidate.is_absolute():
            candidate = download_dir / candidate
        if candidate.name.startswith(f"{_SOURCE_NAME}.") and candidate.name != "source.info.json":
            return candidate
    raise UrlIngestError("DOWNLOAD_ERROR", "yt-dlp did not report a media path")


def _metadata(info_path: Path, source: BilibiliSource) -> tuple[str, str, str]:
    try:
        payload = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UrlIngestError("DOWNLOAD_ERROR", "yt-dlp metadata is unavailable") from exc
    if not isinstance(payload, dict):
        raise UrlIngestError("DOWNLOAD_ERROR", "yt-dlp metadata is invalid")
    duration = payload.get("duration")
    if (
        isinstance(duration, bool) or not isinstance(duration, (int, float))
        or not math.isfinite(duration) or duration <= 0
    ):
        raise UrlIngestError("VIDEO_DURATION_INVALID", "无法确认视频时长，不能处理此视频。")
    if duration > MAX_VIDEO_SECONDS:
        raise UrlIngestError("VIDEO_TOO_LONG", "单个视频最长支持 5 小时。")
    title = payload.get("title")
    uploader = payload.get("uploader") or payload.get("channel")
    if not isinstance(title, str) or not title.strip():
        raise UrlIngestError("DOWNLOAD_ERROR", "yt-dlp metadata has no title")
    if not isinstance(uploader, str) or not uploader.strip():
        raise UrlIngestError("DOWNLOAD_ERROR", "yt-dlp metadata has no uploader")
    title = title.strip()
    uploader = uploader.strip()
    attribution = f"Bilibili；{uploader}；《{title}》；{source.canonical_url}"
    return title, uploader, attribution


def download_bilibili_video(
    source: BilibiliSource,
    run_dir: Path,
    *,
    runner: CommandRunner | None = None,
    request_subtitles: bool = False,
    audio_only: bool = False,
) -> DownloadResult:
    """Run the yt-dlp command and verify its run-local output."""

    download_dir = run_dir.resolve() / "download"
    try:
        download_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UrlIngestError("DOWNLOAD_ERROR", "download directory is unavailable") from exc
    executable = shutil.which("yt-dlp")
    if executable is None:
        raise UrlIngestError("DOWNLOAD_ERROR", "yt-dlp is unavailable")
    command = [
        executable,
        "--ignore-config",
        "--no-playlist",
        "--match-filter",
        f"duration <= {MAX_VIDEO_SECONDS}",
        "--break-on-reject",
        "-P",
        str(download_dir),
        "-o",
        "source.%(ext)s",
        "--write-info-json",
        "--print",
        "after_move:filepath",
    ]
    if audio_only:
        command.extend(["-f", "bestaudio"])
    else:
        command.extend(["-S", "res:1080,vcodec:h264,acodec:aac", "--merge-output-format", "mp4"])
    if request_subtitles:
        command.extend(["--write-subs", "--sub-langs", "all", "--sub-format", "srt/vtt/ass/best"])
    command.append(source.canonical_url)
    completed = _run_command(command, runner, category="DOWNLOAD_ERROR")
    (run_dir / "download.log").write_text(str(getattr(completed, "stderr", "")))
    if getattr(completed, "returncode", None) == 101 or (
        not str(getattr(completed, "stdout", "")).strip()
        and "does not pass filter" in str(getattr(completed, "stderr", ""))
    ):
        raise UrlIngestError(
            "VIDEO_DURATION_INVALID",
            "当前支持的视频最长为 5 小时，请选择不超过 5 小时的视频。",
        )
    if getattr(completed, "returncode", 1) != 0:
        if "HTTP Error 412" in str(getattr(completed, "stderr", "")):
            raise UrlIngestError(
                "DOWNLOAD_ERROR",
                "视频链接已识别，但 Bilibili 拒绝了下载请求（HTTP 412）。请稍后重试。",
            )
        raise UrlIngestError("DOWNLOAD_ERROR", "yt-dlp failed")
    media_path = _inside(
        _printed_media_path(getattr(completed, "stdout", None), download_dir),
        download_dir,
        category="DOWNLOAD_ERROR",
        message="downloaded media path is outside the run directory",
    )
    if not media_path.is_file() or media_path.stat().st_size == 0:
        raise UrlIngestError("DOWNLOAD_ERROR", "downloaded media is missing")
    info_path = download_dir / "source.info.json"
    if not info_path.is_file():
        raise UrlIngestError("DOWNLOAD_ERROR", "yt-dlp metadata file is missing")
    title, uploader, attribution = _metadata(info_path, source)
    result = DownloadResult(
        source=source,
        media_path=media_path,
        info_path=info_path.resolve(),
        title=title,
        uploader=uploader,
        attribution=attribution,
        command=tuple(command),
    )
    return result
