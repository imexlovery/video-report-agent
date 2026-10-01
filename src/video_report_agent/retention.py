"""Prune terminal runs' media while retaining reports and source text."""

import json
import logging
import os
import shutil
import time
from pathlib import Path

from .ingest import UrlIngestError, _metadata, validate_bilibili_url
from .reuse import validated_asr, validated_transcript

_MEDIA_SUFFIXES = {
    ".mp4", ".m4a", ".webm", ".opus", ".mp3",
    ".ogg", ".aac", ".flac", ".wav", ".mkv",
}


def cleanup_media(root: Path) -> None:
    """Keep terminal runs' media for seven days by default; zero disables a limit."""
    try:
        keep = int(os.getenv("MEDIA_KEEP_LAST", "0"))
        days = int(os.getenv("MEDIA_MAX_AGE_DAYS", "7"))
        trace_days = int(os.getenv("PI_TRACE_FULL_MAX_AGE_DAYS", "7"))
        if keep < 0 or days < 0 or trace_days < 0:
            raise ValueError("media retention limits must be non-negative")
        terminal = []
        for run in root.iterdir():
            if not run.is_dir() or run.is_symlink():
                continue
            status = run / "status.json"
            try:
                if json.loads(status.read_text()).get("state") in {
                    "RENDERED", "FAILED", "CANCELLED",
                }:
                    terminal.append((status.stat().st_mtime, run))
            except (OSError, ValueError, AttributeError):
                continue
        cutoff = time.time() - days * 86400
        for index, (finished, run) in enumerate(sorted(terminal, reverse=True)):
            # Only the opt-in raw stream expires; compact traces remain usable.
            raw = run / "pi.raw.events.jsonl"
            if trace_days and finished < time.time() - trace_days * 86400:
                raw.unlink(missing_ok=True)
            if not ((keep and index >= keep) or (days and finished < cutoff)):
                continue
            # Keep subtitles, metadata, report assets and all diagnostic text.
            paths = [run / "audio.wav"]
            download = run / "download"
            if not download.is_symlink():
                paths.extend(
                    path for path in download.glob("source*")
                    if path.suffix in _MEDIA_SUFFIXES
                )
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logging.exception("Could not remove expired media: %s", path)
    except (OSError, ValueError):
        # A housekeeping failure must not change a report's successful status.
        logging.exception("Media cleanup failed")


def cleanup_cancelled_run(run: Path) -> None:
    """Keep only complete reusable inputs and small run diagnostics after cancellation."""
    try:
        metadata = json.loads((run / "input.json").read_text())
    except (OSError, ValueError, AttributeError):
        metadata = {}

    keep = {
        "input.json", "status.json", "queue.json", "run.trace.jsonl",
        "worker.log", "download.log", "cancel.requested",
    }

    download = run / "download"
    try:
        source = validate_bilibili_url(metadata["url"])
        info = download / "source.info.json"
        audio_only = metadata.get("transcript_mode", "asr-only") == "asr-only"
        media = next(
            (
                path for path in sorted(download.glob("source.*"))
                if path.is_file() and path.stat().st_size
                and path.suffix in _MEDIA_SUFFIXES
                and (audio_only or path.suffix in {".mp4", ".mkv", ".webm"})
            ),
            None,
        )
        if media is None or not info.is_file():
            raise UrlIngestError("DOWNLOAD_ERROR", "incomplete download")
        _metadata(info, source)
        reusable_download = {media.name, info.name}
        reusable_download.update(
            path.name for path in download.glob("source.*")
            if path.suffix in {".srt", ".vtt", ".ass"} and path.is_file()
        )
        for path in download.iterdir():
            if path.name not in reusable_download:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
        keep.add("download")
    except (OSError, KeyError, UrlIngestError):
        shutil.rmtree(download, ignore_errors=True)

    validated = validated_transcript(run, metadata) if metadata else None
    if validated is not None:
        names, _ = validated
        keep.update(names)
        keep.add("transcript.md")
    elif metadata and validated_asr(run, metadata) is not None:
        keep.add("asr.json")

    if "download" in keep:
        keep.add("audio.wav")

    for path in run.iterdir():
        if path.name in keep:
            continue
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            logging.exception("Could not remove cancelled-run artifact: %s", path)
    cleanup_media(run.parent)
