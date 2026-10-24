"""One sequential URL → ASR → Pi → report pipeline."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import traceback
import uuid
from pathlib import Path

from .asr import AsrError, transcribe_audio
from .audio import AudioExtractionError, extract_audio
from .bilibili_subtitles import SubtitleError, fetch_subtitles, validate_sources
from .failures import asr_failure
from .ingest import UrlIngestError, download_bilibili_video, validate_bilibili_url
from .media_config import resolve_media_config
from .paraformer import CloudAsrError
from .pi import PiError, PiRunner
from .report_image import render_report_image
from .retention import cleanup_media
from .reuse import (
    acquire_cache,
    cache_key,
    reuse_asr,
    reuse_download,
    reuse_transcript,
    valid_asr_payload,
    validated_transcript,
)
from .storage import CacheProductionFailed, Store
from .trace import RunTrace
from .transcript import build_transcript
from .transcript_foundation import parse_roi


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temporary.replace(path)


def create_run(
    root: Path,
    url: str,
    *,
    report_mode="standard",
    transcript_mode="asr-only",
    transcript_sources=None,
    transcript_only=False,
    ocr_mode="off",
    ocr_roi=None,
    subtitle_file=None,
    model_selection=None,
    model_recovery=None,
    asr_backend=None,
    asr_model=None,
    ocr_backend=None,
    database_path=None,
    db=None,
    export=True,
    subtitle_content=None,
    subtitle_name=None,
) -> Path:
    if report_mode not in ("standard", "brief"):
        raise ValueError("report_mode must be standard or brief")
    if transcript_mode not in {"auto", "asr-only", "fused"}:
        raise ValueError("transcript_mode must be auto, asr-only or fused")
    if ocr_mode == "on":
        ocr_mode = "roi"
    if ocr_mode not in {"off", "auto", "roi"}:
        raise ValueError("ocr_mode must be off, auto or roi")
    if transcript_mode == "fused" and ocr_mode == "roi":
        parse_roi(ocr_roi)
    if transcript_sources is not None:
        validate_sources(transcript_sources)
        if ocr_mode != "off":
            raise ValueError("自选文字稿来源暂不启用 OCR")
        if (
            subtitle_file or subtitle_content is not None
        ) and "subtitle_track" not in transcript_sources:
            raise ValueError("导入字幕时请选择字幕轨来源")
        transcript_mode = "selected"
    if transcript_mode == "auto" and (
        ocr_mode != "off" or subtitle_file or subtitle_content is not None
    ):
        raise ValueError("自动路由不接受 OCR 或导入字幕，请使用手动选源")
    media_config = (
        resolve_media_config(
            asr_backend,
            asr_model,
            ocr_backend,
            require_credentials=transcript_mode != "auto",
        )
        if transcript_sources is None or "asr" in transcript_sources
        else {}
    )
    source = validate_bilibili_url(url)
    run = root.resolve() / uuid.uuid4().hex
    metadata = {
        **({"model_selection": model_selection} if model_selection else {}),
        **({"model_recovery": model_recovery} if model_recovery else {}),
        **media_config,
        "url": source.canonical_url,
        "video_id": source.video_id,
        "report_mode": report_mode,
        "transcript_mode": transcript_mode,
        **({"transcript_sources": transcript_sources} if transcript_sources is not None else {}),
        **({"transcript_only": True} if transcript_only else {}),
        "ocr_mode": ocr_mode,
        "ocr_roi": ocr_roi,
        "subtitle_file": "subtitle" + Path(subtitle_file).suffix if subtitle_file else None,
    }
    status = {
        "run_id": run.name,
        "state": "QUEUED",
        "stage": "QUEUED",
        "started_at": time.time(),
        **(
            {"model_name": model_recovery.get("selected", {}).get("name")} if model_recovery else {}
        ),
    }
    store = Store(database_path or root / "state.sqlite3", initialize=db is None)
    content = Path(subtitle_file).read_text() if subtitle_file else subtitle_content
    if content is not None:
        metadata["subtitle_file"] = "subtitle" + Path(subtitle_file or subtitle_name).suffix
    with store.transaction(db) as connection:
        if db is None:
            store.mark_writes_open(db=connection)
        store.register(run, metadata, status, db=connection)
        if content is not None:
            store.document(run.name, metadata["subtitle_file"], content, db=connection)
    if export:
        if db is not None:
            raise ValueError("Export must happen after the caller transaction commits")
        store.export(run.name)
    return run


def generate(run: Path, *, database_path=None, execution_id=None) -> dict:
    store = Store(database_path or run.parent / "state.sqlite3", initialize=False)
    standalone = execution_id is None
    if standalone:
        execution_id = store.claim_run(run.name)
        store.supervise(run.name, execution_id)
    store.worker(run.name, execution_id, os.getpid())
    store.export(run.name, include_report=False)
    record = store.run(run.name)
    metadata = record["input"]
    status = record["status"]
    status.update(video_id=metadata["video_id"])

    def update(stage: str, **fields):
        if stage in {"RENDERED", "TRANSCRIPT_READY", "FAILED"}:
            fields["finished_at"] = time.time()
        status.update(state=stage, stage=stage, **fields)
        if stage not in {"RENDERED", "TRANSCRIPT_READY", "FAILED"}:
            store.stage(run.name, execution_id, status)
        write_json(run / "status.json", status)

    trace = RunTrace(run)
    try:
        documents = store.documents(run.name)
        automatic = metadata.get("transcript_mode") == "auto"
        selected = metadata.get("transcript_sources")
        needs_asr = selected is None or "asr" in selected
        asr_failed = False

        def continue_without_asr(exc):
            nonlocal needs_asr, asr_failed
            if (
                not selected
                or not metadata.get("duration_ms")
                or not (
                    metadata.get("subtitle_file")
                    or any(
                        source.get("status") == "READY"
                        for source in status.get("transcript_source_status", {}).values()
                    )
                )
            ):
                return False
            needs_asr, asr_failed = False, True
            status.setdefault("transcript_source_status", {})["asr"] = {
                "status": "FAILED",
                "count": 0,
                "error_code": type(exc).__name__,
            }
            return True

        source = validate_bilibili_url(metadata["url"])
        if automatic or (selected and (not needs_asr or any(s != "asr" for s in selected))):
            update("TRANSCRIBING")
            requested = (
                ["subtitle_track", "bilibili_ai"]
                if automatic
                else [
                    s
                    for s in selected
                    if s != "asr" and not (s == "subtitle_track" and metadata.get("subtitle_file"))
                ]
            )
            with trace.span("subtitles", input={"sources": requested}) as detail:
                try:
                    subtitle_snapshot = (
                        fetch_subtitles(source, requested, first_available=True)
                        if automatic
                        else fetch_subtitles(source, requested)
                    )
                except SubtitleError as exc:
                    if not needs_asr:
                        raise
                    subtitle_snapshot = {
                        "sources": {
                            s: {
                                "status": "LOGIN_REQUIRED"
                                if exc.category == "SUBTITLE_LOGIN_REQUIRED"
                                else "FAILED",
                                "count": 0,
                            }
                            for s in requested
                        }
                    }
                write_json(run / "bilibili-subtitles.json", subtitle_snapshot)
                store.document(run.name, "bilibili-subtitles.json", json.dumps(subtitle_snapshot))
                documents["bilibili-subtitles.json"] = json.dumps(subtitle_snapshot)
                metadata.update(
                    {
                        key: subtitle_snapshot[key]
                        for key in ("title", "uploader", "attribution", "duration_ms")
                        if key in subtitle_snapshot
                    }
                )
                status["transcript_source_status"] = {
                    key: {k: v for k, v in value.items() if k != "body"}
                    for key, value in subtitle_snapshot["sources"].items()
                }
                detail.update(output=status["transcript_source_status"])
        if automatic:
            chosen = next(
                (
                    kind
                    for kind in ("subtitle_track", "bilibili_ai")
                    if subtitle_snapshot["sources"].get(kind, {}).get("status") == "READY"
                ),
                "asr",
            )
            selected = [chosen]
            needs_asr = chosen == "asr"
            metadata["resolved_transcript_sources"] = selected
            if needs_asr:
                # Credentials are only required once the subtitle route really reaches ASR.
                resolve_media_config(
                    metadata["asr_backend"],
                    metadata["asr_model"],
                    metadata["ocr_backend"],
                )
        snapshot = store.cache("download", cache_key("download", metadata))
        if snapshot and snapshot["state"] == "READY":
            documents.update(json.loads(snapshot["payload_json"]).get("documents", {}))
        transcript_key = cache_key("transcript", metadata, documents)
        if metadata.get("transcript_mode") == "fused" and not (
            snapshot and snapshot["state"] == "READY"
        ):
            transcript_claim = "DEFER"
        else:
            try:
                transcript_claim, _ = acquire_cache(
                    store, "transcript", transcript_key, run, execution_id
                )
            except CacheProductionFailed as exc:
                # A waiter has its own captions and can finish without the producer's ASR.
                # Cancellation, deadlines and unrelated cache errors must still propagate.
                if str(exc) != "Selected ASR source failed" or not continue_without_asr(exc):
                    raise
                transcript_claim = "UNCLAIMED"
        transcript_source = (
            reuse_transcript(run, metadata, database_path=store.path, key=transcript_key)
            if transcript_claim == "READY"
            else None
        )
        status["download_reused_from"] = None
        try:
            if transcript_source is None and needs_asr:
                update("DOWNLOADING")
                with trace.span("download", input={"video_id": metadata["video_id"]}) as detail:
                    source = validate_bilibili_url(metadata["url"])
                    request_subtitles = metadata.get("transcript_mode") == "fused"
                    audio_only = (
                        bool(selected) or metadata.get("transcript_mode", "asr-only") == "asr-only"
                    )
                    download_key = cache_key("download", metadata)
                    download_claim, cache = acquire_cache(
                        store,
                        "download",
                        download_key,
                        run,
                        execution_id,
                    )
                    while True:
                        downloaded, download_source = reuse_download(
                            source,
                            run,
                            request_subtitles=request_subtitles,
                            audio_only=audio_only,
                            database_path=store.path,
                        )
                        if downloaded is not None:
                            break
                        if download_claim == "PRODUCE":
                            downloaded = download_bilibili_video(
                                source,
                                run,
                                request_subtitles=request_subtitles,
                                audio_only=audio_only,
                            )
                            break
                        store.invalidate_media(
                            "download", download_key, cache["producer_run"], cache["execution_id"]
                        )
                        download_claim, cache = acquire_cache(
                            store, "download", download_key, run, execution_id
                        )
                    store.artifact(
                        run.name,
                        "download/" + downloaded.media_path.name,
                        downloaded.media_path,
                        "media",
                    )
                    for path in (run / "download").glob("source.*"):
                        if path.suffix in {".json", ".srt", ".vtt", ".ass"}:
                            store.document(run.name, "download/" + path.name, path.read_text())
                    if download_claim == "PRODUCE":
                        store.ready_cache(
                            "download",
                            download_key,
                            run.name,
                            execution_id,
                            {
                                "media": str(downloaded.media_path),
                                "documents": {
                                    name: body
                                    for name, body in store.documents(run.name).items()
                                    if name.startswith("download/")
                                },
                            },
                        )
                    else:
                        store.use_cache(run.name, "download", download_key, download_source)
                    detail.update(
                        output={"media": downloaded.media_path.name},
                        reused_from=download_source,
                    )
                status["download_reused_from"] = download_source
                metadata.update(
                    download_audio_only=audio_only,
                    title=downloaded.title,
                    uploader=downloaded.uploader,
                    attribution=downloaded.attribution,
                )
                if metadata.get("transcript_mode") == "fused":
                    actual_key = cache_key("transcript", metadata, store.documents(run.name))
                    if transcript_claim == "DEFER" or actual_key != transcript_key:
                        if transcript_claim == "PRODUCE":
                            store.release_cache(
                                "transcript",
                                transcript_key,
                                run.name,
                                execution_id,
                                "Downloaded subtitle snapshot changed",
                            )
                        transcript_key = actual_key
                        transcript_claim, _ = acquire_cache(
                            store, "transcript", transcript_key, run, execution_id
                        )
                        if transcript_claim == "READY":
                            transcript_source = reuse_transcript(
                                run, metadata, database_path=store.path, key=transcript_key
                            )
        except (UrlIngestError, AudioExtractionError, AsrError, CloudAsrError) as exc:
            if not continue_without_asr(exc):
                raise
        write_json(run / "input.json", metadata)
        store.metadata(run.name, metadata, execution_id=execution_id)
        status["title"] = metadata.get("title")
        status["transcript_reused_from"] = transcript_source
        if transcript_source is None:
            update("TRANSCRIBING")
            try:
                if needs_asr:
                    with trace.span(
                        "ffmpeg", input={"media": downloaded.media_path.name}
                    ) as detail:
                        audio = extract_audio(downloaded.media_path, run / "audio.wav")
                        store.artifact(run.name, "audio.wav", run / "audio.wav", "media")
                        detail.update(
                            output={
                                "audio": audio.path.name,
                                "duration_ms": audio.probe.duration_ms,
                            }
                        )
                    with trace.span(
                        "asr",
                        backend=metadata["asr_backend"],
                        model=metadata["asr_model"],
                        input={"audio": audio.path.name},
                    ) as detail:
                        asr_key = cache_key("asr", metadata)
                        asr_claim, _ = acquire_cache(store, "asr", asr_key, run, execution_id)
                        asr_source = (
                            reuse_asr(run, metadata, database_path=store.path)
                            if asr_claim == "READY"
                            else None
                        )
                        status["asr_reused_from"] = asr_source
                        if asr_source is None:
                            status["asr_performed"] = True
                            asr = transcribe_audio(
                                audio.path,
                                model=metadata["asr_model"],
                                backend=metadata["asr_backend"],
                                base_url=metadata["asr_base_url"],
                                language=metadata["asr_language"],
                                parameters=metadata["asr_parameters"],
                                task_path=run / "asr-task.json",
                            )
                            write_json(run / "asr.json", asr.to_dict())
                            if not valid_asr_payload(asr.to_dict()):
                                raise AsrError("ASR produced no valid ordered segments")
                            store.capture(run, execution_id, names=("asr.json", "asr-task.json"))
                            store.ready_cache(
                                "asr", asr_key, run.name, execution_id, {"asr": asr.to_dict()}
                            )
                            segments = len(asr.segments)
                        else:
                            segments = len(json.loads((run / "asr.json").read_text())["segments"])
                        detail.update(
                            output={"artifact": "asr.json", "segments": segments},
                            reused_from=asr_source,
                        )
            except (AudioExtractionError, AsrError, CloudAsrError) as exc:
                if not continue_without_asr(exc):
                    raise
            with trace.span("transcript") as detail:
                options = argparse.Namespace(
                    transcript_mode=metadata.get("transcript_mode", "asr-only"),
                    transcript_sources=selected,
                    asr_failed=asr_failed,
                    ocr_mode=metadata.get("ocr_mode", "off"),
                    ocr_roi=metadata.get("ocr_roi"),
                    ocr_backend=metadata.get("ocr_backend", "rapidocr"),
                    subtitle_file=(
                        run / metadata["subtitle_file"] if metadata.get("subtitle_file") else None
                    ),
                )
                build, _, _ = build_transcript(
                    options,
                    source_path=downloaded.media_path if needs_asr else None,
                    artifact_root=run,
                    asr_path=run / "asr.json",
                    source_duration_ms=audio.probe.duration_ms
                    if needs_asr
                    else metadata["duration_ms"],
                    manifest=metadata,
                )
                transcript = [f"# {metadata['title']}", metadata["attribution"], ""]
                if selected:
                    status["transcript_primary_source"] = build.manifest.primary_source
                    status["transcript_source_status"] = build.manifest.source_status
                    status.setdefault(
                        "asr_performed", needs_asr and not bool(status.get("asr_reused_from"))
                    )
                    transcript += [f"文字稿主来源：{build.manifest.primary_source}", ""]
                transcript.extend(
                    f"[{u.unit_id} | {u.start_ms / 1000:.3f}–{u.end_ms / 1000:.3f}s] "
                    f"{u.canonical_text}"
                    for u in build.canonical_units
                )
                (run / "transcript.md").write_text("\n".join(transcript))
                if validated_transcript(run, metadata) is None:
                    raise ValueError("Transcript is incomplete or incompatible")
                documents = store.capture(run, execution_id)
                if asr_failed:
                    if transcript_claim == "PRODUCE":
                        store.release_cache(
                            "transcript",
                            transcript_key,
                            run.name,
                            execution_id,
                            "Selected ASR source failed",
                        )
                else:
                    store.ready_cache(
                        "transcript",
                        transcript_key,
                        run.name,
                        execution_id,
                        {"documents": documents, "metadata": metadata},
                    )
                detail.update(
                    output={
                        "artifact": "transcript.md",
                        "units": len(build.canonical_units),
                    }
                )
        else:
            with trace.span("transcript") as detail:
                detail.update(reused_from=transcript_source, output={"artifact": "transcript.md"})
            if selected:
                manifest = json.loads((run / "transcript-manifest.json").read_text())
                status["transcript_primary_source"] = manifest.get("primary_source")
                status["transcript_source_status"] = manifest["source_status"]
                status["asr_performed"] = False
        if metadata.get("transcript_only"):
            update("TRANSCRIPT_READY")
        else:
            update("GENERATING")
            runner_options = dict(metadata.get("model_selection", {}))
            if metadata.get("model_recovery"):
                runner_options["model_recovery"] = metadata["model_recovery"]
            runner = PiRunner(**runner_options)
            with trace.span(
                "agent",
                backend=runner.provider,
                model=runner.model,
                input={"artifact": "transcript.md"},
            ) as detail:
                asyncio.run(runner.run(run))
                detail.update(output={"artifact": "report.html"})
            store.save_report(run, execution_id, (run / "report.html").read_text())
            for asset in (run / "assets").rglob("*"):
                if asset.is_file() and not asset.is_symlink():
                    store.artifact(run.name, str(asset.relative_to(run)), asset, "resource")
            update("GENERATING_IMAGE")
            try:
                with trace.span("render", input={"artifact": "report.html"}) as detail:
                    render_report_image(run)
                    store.artifact(run.name, "report.png", run / "report.png", "image")
                    detail.update(output={"artifact": "report.png"})
            except Exception as exc:
                status["image_error"] = str(exc)
            update("RENDERED")
    except Exception as exc:
        if isinstance(exc, CloudAsrError):
            write_json(run / "asr-error.json", exc.to_dict())
            (run / "failure.log").write_text(str(exc))
        else:
            (run / "failure.log").write_text(traceback.format_exc())
        if isinstance(exc, CloudAsrError):
            category = "EXTERNAL_API_FAILURE"
        elif isinstance(exc, PiError):
            category = exc.category
        elif isinstance(exc, UrlIngestError):
            category = (
                "ENVIRONMENT_FAILURE" if "unavailable" in str(exc) else "EXTERNAL_API_FAILURE"
            )
        elif isinstance(exc, (AsrError, AudioExtractionError, OSError)):
            category = "ENVIRONMENT_FAILURE"
        else:
            category = "IMPLEMENTATION_FAILURE"
        code, message = category, str(exc)
        diagnostics = {}
        if isinstance(exc, CloudAsrError):
            code, message = asr_failure(exc)
            diagnostics = {
                "http_status": exc.http_status,
                "provider_code": exc.provider_code,
                "failed_stage": exc.stage,
            }
        elif isinstance(exc, UrlIngestError):
            code = exc.category
            if code in {"VIDEO_DURATION_INVALID", "URL_INVALID"}:
                category = "INPUT_REJECTED"
            elif code == "DOWNLOAD_ERROR" and str(exc) == "yt-dlp failed":
                message = "视频下载失败，可能是视频不可访问或下载服务异常，请稍后重试。"
        update("FAILED", error_category=category, error_code=code, error=message, **diagnostics)
    store.capture(run, execution_id)
    from .usage import execution_statistics

    store.statistics(run.name, execution_statistics(run, status))
    store.result(run.name, execution_id, status, exited=standalone)
    if standalone:
        store.release_caches(run.name, execution_id, status.get("error") or status["state"])
        store.finalize(run.name, execution_id, status)
    cleanup_media(run.parent, database_path=store.path)
    return status
