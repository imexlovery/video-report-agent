"""Reuse completed local input artifacts across independent report runs."""

import json
import os
import shutil
from pathlib import Path

from pydantic import ValidationError

from .ingest import DownloadResult, UrlIngestError, _metadata
from .schemas import AsrSegment
from .transcript_foundation import FUSION_VERSION, NORMALIZER_VERSION


def previous_runs(run: Path, video_id: str):
    for candidate in sorted(run.parent.iterdir()):
        if candidate == run or not candidate.is_dir():
            continue
        try:
            metadata = json.loads((candidate / "input.json").read_text())
            if metadata.get("video_id") == video_id:
                yield candidate, metadata
        except (OSError, ValueError, AttributeError):
            continue


def same_asr_config(previous: dict, metadata: dict) -> bool:
    identity = (
        "asr_backend",
        "asr_provider",
        "asr_model",
        "asr_language",
        "asr_parameters",
        "asr_base_url",
    )
    return bool(previous.get("asr_backend")) and all(
        previous.get(key) == metadata.get(key) for key in identity
    )


def validated_asr(candidate: Path, metadata: dict):
    try:
        previous = json.loads((candidate / "input.json").read_text())
        payload = json.loads((candidate / "asr.json").read_text())
        segments = payload.get("segments")
        if (
            not same_asr_config(previous, metadata)
            or not isinstance(segments, list)
            or not segments
        ):
            return None
        parsed = [AsrSegment.model_validate(segment) for segment in segments]
        # ASR normalization drops invalid rows but retains their source ordinals.
        # Gaps are valid; duplicate or reversed ordinals are not.
        ordinals = [segment.ordinal for segment in parsed]
        if any(left >= right for left, right in zip(ordinals, ordinals[1:])):
            return None
        return candidate / "asr.json"
    except (OSError, ValueError, TypeError, AttributeError, ValidationError):
        return None


def reuse_asr(run: Path, metadata: dict):
    for candidate, _ in previous_runs(run, metadata["video_id"]):
        source = validated_asr(candidate, metadata)
        if source is not None:
            shutil.copy2(source, run / "asr.json")
            return candidate.name
    return None


def validated_transcript(candidate: Path, metadata: dict):
    """Return the reusable transcript files and units, or ``None``."""
    try:
        manifest = json.loads((candidate / "transcript-manifest.json").read_text())
        if (
            manifest.get("status") != "READY"
            or manifest.get("video_id") != metadata["video_id"]
            or manifest.get("transcript_mode") != metadata.get("transcript_mode", "asr-only")
            or manifest.get("normalizer_version") != NORMALIZER_VERSION
            or manifest.get("fusion_version") != FUSION_VERSION
        ):
            return None
        if validated_asr(candidate, metadata) is None:
            return None
        names = list(manifest["artifacts"].values()) + ["asr.json"]
        if any(Path(name).name != name or not (candidate / name).is_file() for name in names):
            return None
        units = [
            json.loads(line)
            for line in (candidate / "canonical-transcript.jsonl").read_text().splitlines()
        ]
        if not units or len(units) != manifest["canonical_unit_count"]:
            return None
        return names, units
    except (OSError, ValueError, KeyError, TypeError):
        return None


def reuse_download(source, run: Path, *, request_subtitles: bool, audio_only: bool = False):
    for candidate, metadata in previous_runs(run, source.video_id):
        # input metadata is written only after the downloader verifies its output.
        if not metadata.get("asr_model"):
            continue
        if request_subtitles and metadata.get("transcript_mode", "asr-only") != "fused":
            continue
        directory = candidate / "download"
        media = directory / "source.mp4"
        if audio_only:
            media = next(
                (path for path in sorted(directory.glob("source.*"))
                 if path.suffix in {
                     ".m4a", ".webm", ".opus", ".mp3", ".ogg", ".aac", ".flac", ".wav",
                 }
                 and path.is_file() and path.stat().st_size),
                media,
            )
        elif metadata.get("download_audio_only"):
            continue
        info = directory / "source.info.json"
        if not media.is_file() or not media.stat().st_size or not info.is_file():
            continue
        try:
            title, uploader, attribution = _metadata(info, source)
        except UrlIngestError:
            continue
        target = run / "download"
        target.mkdir(exist_ok=True)
        for path in directory.glob("source.*"):
            if path == media:
                os.link(path, target / path.name)
            elif path.is_file():
                shutil.copy2(path, target / path.name)
        return DownloadResult(
            source,
            target / media.name,
            target / info.name,
            title,
            uploader,
            attribution,
            (),
        ), candidate.name
    return None, None


def reuse_transcript(run: Path, metadata: dict):
    for candidate, previous in previous_runs(run, metadata["video_id"]):
        if not same_asr_config(previous, metadata):
            continue
        if metadata.get("transcript_mode") == "fused" and metadata.get("ocr_mode") != "off":
            if any(previous.get(k) != metadata.get(k) for k in ("ocr_backend", "ocr_model")):
                continue
        defaults = {"transcript_mode": "asr-only", "ocr_mode": "off", "ocr_roi": None}
        if previous.get("asr_model") != metadata["asr_model"] or any(
            previous.get(key, default) != metadata.get(key, default)
            for key, default in defaults.items()
        ):
            continue
        old_sub, new_sub = previous.get("subtitle_file"), metadata.get("subtitle_file")
        if bool(old_sub) != bool(new_sub):
            continue
        if old_sub:
            try:
                if (candidate / old_sub).read_bytes() != (run / new_sub).read_bytes():
                    continue
            except OSError:
                continue
        validated = validated_transcript(candidate, metadata)
        if validated is None:
            continue
        names, units = validated
        # Report mode does not affect source identity. Keep the source metadata and
        # description even when retention has already removed the original media.
        metadata.update({
            key: previous[key] for key in ("title", "uploader", "attribution") if key in previous
        })
        info = candidate / "download" / "source.info.json"
        if info.is_file():
            (run / "download").mkdir(exist_ok=True)
            shutil.copy2(info, run / "download" / "source.info.json")
        lines = [
            f"[{u['unit_id']} | {u['start_ms'] / 1000:.3f}–"
            f"{u['end_ms'] / 1000:.3f}s] {u['canonical_text']}"
            for u in units
        ]
        for name in names:
            shutil.copy2(candidate / name, run / name)
        (run / "transcript.md").write_text(
            "\n".join([f"# {metadata['title']}", metadata["attribution"], "", *lines])
        )
        return candidate.name
    return None
