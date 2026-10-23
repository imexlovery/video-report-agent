"""Reuse completed local input artifacts across independent report runs."""

import json
import os
import time
from pathlib import Path

from pydantic import ValidationError

from .ingest import DownloadResult, UrlIngestError, _metadata
from .schemas import AsrSegment
from .storage import CacheProductionFailed, Store, encode
from .transcript_foundation import FUSION_VERSION, NORMALIZER_VERSION


def previous_runs(run: Path, video_id: str, *, database_path=None):
    store = Store(database_path or run.parent / "state.sqlite3", initialize=False)
    with store.read() as db:
        rows = db.execute(
            "SELECT id,workspace,input_json FROM runs WHERE video_id=? "
            "AND id<>? ORDER BY created_at,id",
            (video_id, run.name),
        ).fetchall()
    for row in rows:
        yield Path(row["workspace"]), json.loads(row["input_json"])


def cache_key(kind, metadata, documents=None):
    identity = {"video_id": metadata["video_id"], "processing_version": 1}
    if kind == "download":
        identity.update(
            audio_only=bool(metadata.get("transcript_sources"))
            or metadata.get("transcript_mode", "asr-only") == "asr-only",
            request_subtitles=metadata.get("transcript_mode") == "fused",
        )
    else:
        identity.update(
            {
                key: metadata.get(key)
                for key in (
                    "asr_backend",
                    "asr_provider",
                    "asr_model",
                    "asr_language",
                    "asr_parameters",
                    "asr_base_url",
                )
            }
        )
    if kind == "transcript":
        if metadata.get("transcript_sources"):
            identity["selected_sources"] = metadata["transcript_sources"]
            identity["subtitle_snapshot"] = (documents or {}).get("bilibili-subtitles.json")
        identity.update(
            {
                key: metadata.get(key, default)
                for key, default in {
                    "transcript_mode": "asr-only",
                    "ocr_mode": "off",
                    "ocr_roi": None,
                    "ocr_backend": None,
                    "ocr_model": None,
                }.items()
            }
        )
        identity.update(normalizer_version=NORMALIZER_VERSION, fusion_version=FUSION_VERSION)
        subtitle = metadata.get("subtitle_file")
        identity["subtitle_provided"] = bool(subtitle)
        identity["subtitle_format"] = Path(subtitle).suffix if subtitle else None
        identity["subtitle_content"] = (documents or {}).get(subtitle) if subtitle else None
        identity["downloaded_subtitles"] = (
            {
                name: body
                for name, body in (documents or {}).items()
                if name.startswith("download/") and Path(name).suffix in {".srt", ".vtt", ".ass"}
            }
            if metadata.get("transcript_mode") == "fused"
            else {}
        )
    return encode(identity)


def acquire_cache(store, kind, key, run, execution_id):
    if (run / "cancel.requested").exists():
        raise CacheProductionFailed("Cancelled while waiting for input cache")
    deadline = float(os.getenv("VIDEO_REPORT_RUN_DEADLINE", "inf"))
    if time.monotonic() >= deadline:
        raise CacheProductionFailed("Execution deadline reached while waiting for input cache")
    disposition, row = store.claim_cache(kind, key, run.name, execution_id)
    if disposition != "WAIT":
        return disposition, row
    producer, execution = row["producer_run"], row["execution_id"]
    while True:
        if (run / "cancel.requested").exists():
            raise CacheProductionFailed("Cancelled while waiting for input cache")
        deadline = float(os.getenv("VIDEO_REPORT_RUN_DEADLINE", "inf"))
        if time.monotonic() >= deadline:
            raise CacheProductionFailed("Execution deadline reached while waiting for input cache")
        store.reconcile_producer(producer, execution)
        row = store.cache(kind, key)
        if row["execution_id"] != execution or row["state"] == "FAILED":
            raise CacheProductionFailed(row.get("error") or "Input cache producer failed")
        if row["state"] == "READY":
            return "READY", row
        time.sleep(0.1)


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


def valid_asr_payload(payload):
    try:
        parsed = [AsrSegment.model_validate(segment) for segment in payload["segments"]]
        return bool(parsed) and all(a.ordinal < b.ordinal for a, b in zip(parsed, parsed[1:]))
    except (ValueError, KeyError, TypeError, ValidationError):
        return False


def reuse_asr(run: Path, metadata: dict, *, database_path=None):
    store = Store(database_path or run.parent / "state.sqlite3", initialize=False)
    key = cache_key("asr", metadata)
    row = store.cache("asr", key)
    if not row or row["state"] != "READY":
        return None
    payload = json.loads(row["payload_json"])
    if not valid_asr_payload(payload["asr"]):
        return None
    (run / "asr.json").write_text(encode(payload["asr"]))
    store.document(run.name, "asr.json", encode(payload["asr"]))
    store.use_cache(run.name, "asr", key, row["producer_run"])
    return row["producer_run"]


def validated_transcript(candidate: Path, metadata: dict):
    """Return the reusable transcript files and units, or ``None``."""
    try:
        manifest = json.loads((candidate / "transcript-manifest.json").read_text())
        if (
            manifest.get("status")
            not in ({"READY", "DEGRADED"} if metadata.get("transcript_sources") else {"READY"})
            or manifest.get("video_id") != metadata["video_id"]
            or manifest.get("transcript_mode") != metadata.get("transcript_mode", "asr-only")
            or manifest.get("normalizer_version") != NORMALIZER_VERSION
            or manifest.get("fusion_version") != FUSION_VERSION
        ):
            return None
        selected = metadata.get("transcript_sources")
        if selected and manifest.get("selected_sources") != selected:
            return None
        needs_asr = selected is None or manifest["source_status"]["asr"]["status"] == "READY"
        if needs_asr and validated_asr(candidate, metadata) is None:
            return None
        names = list(manifest["artifacts"].values()) + (["asr.json"] if needs_asr else [])
        if selected and any(s != "asr" for s in selected):
            names.append("bilibili-subtitles.json")
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


def reuse_download(
    source, run: Path, *, request_subtitles: bool, audio_only: bool = False, database_path=None
):
    store = Store(database_path or run.parent / "state.sqlite3", initialize=False)
    for candidate, metadata in previous_runs(run, source.video_id, database_path=store.path):
        if not metadata.get("asr_model"):
            continue
        if request_subtitles and metadata.get("transcript_mode", "asr-only") != "fused":
            continue
        if not audio_only and metadata.get("download_audio_only"):
            continue
        with store.read() as db:
            media_rows = db.execute(
                "SELECT * FROM artifacts WHERE run_id=? AND kind='media' "
                "AND name LIKE 'download/source.%' "
                "AND available=1 ORDER BY name",
                (candidate.name,),
            ).fetchall()
            documents = store.documents(candidate.name, db=db)
        info_body = documents.get("download/source.info.json")
        if not info_body:
            continue
        for row in media_rows:
            media = Path(row["path"])
            if not media.is_file() or not media.stat().st_size:
                store.artifact(candidate.name, row["name"], media, "media")
                continue
            if not audio_only and media.suffix not in {".mp4", ".mkv", ".webm"}:
                continue
            target = run / "download"
            target.mkdir(exist_ok=True)
            info = target / "source.info.json"
            info.write_text(info_body)
            try:
                title, uploader, attribution = _metadata(info, source)
            except UrlIngestError:
                continue
            os.link(media, target / media.name)
            for name, body in documents.items():
                if name.startswith("download/"):
                    (run / name).write_text(body)
                    store.document(run.name, name, body)
            store.artifact(run.name, "download/" + media.name, target / media.name, "media")
            return DownloadResult(
                source, target / media.name, info, title, uploader, attribution, ()
            ), candidate.name
    return None, None


def reuse_transcript(run: Path, metadata: dict, *, database_path=None, key=None):
    store = Store(database_path or run.parent / "state.sqlite3", initialize=False)
    key = key or cache_key("transcript", metadata, store.documents(run.name))
    row = store.cache("transcript", key)
    if not row or row["state"] != "READY":
        return None
    payload = json.loads(row["payload_json"])
    for name, body in payload["documents"].items():
        store.document(run.name, name, body)
        target = run / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    metadata.update(
        {
            key: payload["metadata"][key]
            for key in ("title", "uploader", "attribution", "download_audio_only")
            if key in payload["metadata"]
        }
    )
    store.use_cache(run.name, "transcript", key, row["producer_run"])
    return row["producer_run"]
