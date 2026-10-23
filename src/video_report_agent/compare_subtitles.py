"""Audit subtitle availability and compare against existing ASR, without new ASR or reports."""

import argparse
import json
import time
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from .bilibili_subtitles import BilibiliSubtitles, SubtitleError
from .ingest import validate_bilibili_url
from .transcript import build_transcript
from .transcript_foundation import normalize_text


def compare_windows(cues, asr, *, seconds=60):
    """Time-aligned reading samples; similarity is disagreement, not an accuracy score."""
    windows = {}
    for kind, rows in (("ai", cues), ("asr", asr["segments"])):
        for row in rows:
            start = row["from"] * 1000 if kind == "ai" else row["start_ms"]
            end = row["to"] * 1000 if kind == "ai" else row["end_ms"]
            bucket = int((start + end) / 2 // (seconds * 1000))
            window = windows.setdefault(bucket, {"ai": [], "asr": []})
            window[kind].append(row["content"] if kind == "ai" else row["text"])
    result = []
    for bucket, texts in sorted(windows.items()):
        ai, asr_text = "".join(texts["ai"]), "".join(texts["asr"])
        left = "".join(c for c in normalize_text(ai).lower() if c.isalnum())
        right = "".join(c for c in normalize_text(asr_text).lower() if c.isalnum())
        result.append(
            {
                "start_ms": bucket * seconds * 1000,
                "end_ms": (bucket + 1) * seconds * 1000,
                "ai": ai,
                "asr": asr_text,
                "text_similarity": round(
                    SequenceMatcher(None, left, right, autojunk=False).ratio(), 4
                ),
            }
        )
    return result


def audit_subtitles(manifest_path, output):
    manifest_path, output = Path(manifest_path).resolve(), Path(output).resolve()
    entries = json.loads(manifest_path.read_text())["videos"]
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    with BilibiliSubtitles() as client:
        for entry in entries:
            source = validate_bilibili_url(entry["url"])
            folder = output / source.video_id
            folder.mkdir(exist_ok=True)
            started = time.monotonic()
            try:
                snapshot = client.fetch(source)
                (folder / "bilibili-subtitles.json").write_text(
                    json.dumps(snapshot, ensure_ascii=False, indent=2)
                )
                row = {
                    "video_id": source.video_id,
                    "title": snapshot["title"],
                    "duration_ms": snapshot["duration_ms"],
                    "authenticated": snapshot["authenticated"],
                    "sources": {
                        source: {k: v for k, v in data.items() if k != "body"}
                        for source, data in snapshot["sources"].items()
                    },
                    "fetch_seconds": round(time.monotonic() - started, 3),
                    "warnings": snapshot["warnings"],
                }
                ai = snapshot["sources"]["bilibili_ai"]
                if ai["status"] == "READY":
                    build_transcript(
                        argparse.Namespace(transcript_sources=["bilibili_ai"], subtitle_file=None),
                        source_path=None,
                        artifact_root=folder,
                        asr_path=folder / "unused.json",
                        source_duration_ms=snapshot["duration_ms"],
                        manifest=snapshot,
                    )
                    if entry.get("asr_path"):
                        asr_path = (manifest_path.parent / entry["asr_path"]).resolve()
                        asr = json.loads(asr_path.read_text())
                        windows = compare_windows(ai["body"], asr)
                        comparison = {
                            "asr_backend": asr.get("backend"),
                            "asr_model": asr.get("model"),
                            "windows": windows,
                        }
                        (folder / "comparison.json").write_text(
                            json.dumps(comparison, ensure_ascii=False, indent=2)
                        )
                        row["comparison_windows"] = len(windows)
                        row["asr_model"] = asr.get("model")
            except (SubtitleError, OSError, ValueError) as exc:
                row = {
                    "video_id": source.video_id,
                    "error": str(exc),
                    "fetch_seconds": round(time.monotonic() - started, 3),
                }
            rows.append(row)
            (output / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
            print(
                f"{len(rows)}/{len(entries)} {source.video_id} "
                f"{ {k: v['status'] for k, v in row.get('sources', {}).items()} }",
                flush=True,
            )
            # Keep this diagnostic batch sequential and gentle on the platform.
            time.sleep(0.3)
    summary = {
        "sample_count": len(rows),
        "request_failures": sum("error" in row for row in rows),
        "sources": {
            source: dict(
                Counter(
                    row.get("sources", {}).get(source, {}).get("status", "FAILED") for row in rows
                )
            )
            for source in ("subtitle_track", "bilibili_ai")
        },
        "compared_videos": sum(bool(row.get("comparison_windows")) for row in rows),
        "note": "现有视频样本的可获取率，不代表全站；文字差异和时间覆盖不是识别准确率。",
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary
