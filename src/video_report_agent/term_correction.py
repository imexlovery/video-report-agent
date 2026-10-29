"""Per-run term evidence and local patches; original transcripts stay immutable."""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

SOURCE_CONTEXT = "source-context.json"
TERM_REVIEW = "term-review.json"
CORRECTED_TRANSCRIPT = "transcript.corrected.md"
TERM_DOCUMENTS = (SOURCE_CONTEXT, TERM_REVIEW, CORRECTED_TRANSCRIPT)
MAX_LOOKUPS = 3
MAX_CONTEXT_CHARS = 6000
MAX_SOURCE_CONTENT_CHARS = 12000
TERM_KINDS = {"person", "organization", "product", "term"}
REVIEW_STATUSES = {"supported", "candidate", "unresolved"}
UNIT_LINE = re.compile(r"^(\[([^ |\]]+) \| [^\]]+\] )(.*)$")


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _units(workspace):
    lines = (workspace / "transcript.md").read_text(encoding="utf-8").splitlines(keepends=True)
    units = {}
    for index, line in enumerate(lines):
        match = UNIT_LINE.fullmatch(line.rstrip("\r\n"))
        if match:
            units[match[2]] = {"line": index, "prefix": match[1], "text": match[3]}
    return lines, units


def _latest_entries(entries):
    return {(e["unit_id"], e["start"], e["end"]): e for e in entries}.values()


def _render_corrected(workspace, entries):
    lines, units = _units(workspace)
    patches = {}
    for entry in _latest_entries(entries):
        if entry["status"] == "supported":
            patches.setdefault(entry["unit_id"], []).append(entry)
    for unit_id, group in patches.items():
        unit = units[unit_id]
        text = unit["text"]
        previous_end = -1
        for entry in sorted(group, key=lambda e: e["start"]):
            if entry["start"] < previous_end:
                raise ValueError(f"Overlapping term patches in {unit_id}")
            if text[entry["start"]:entry["end"]] != entry["before"]:
                raise ValueError(f"Original text mismatch in {unit_id}")
            previous_end = entry["end"]
        for entry in sorted(group, key=lambda e: e["start"], reverse=True):
            text = text[:entry["start"]] + entry["after"] + text[entry["end"]:]
        old_line = lines[unit["line"]]
        ending = "\n" if old_line.endswith("\n") else ""
        lines[unit["line"]] = unit["prefix"] + text + ending
    return "".join(lines)


def prepare_term_correction(workspace: Path, metadata: dict):
    info_path = workspace / "download" / "source.info.json"
    info = _read_json(info_path) if info_path.is_file() else {}
    context = {
        "title": metadata.get("title") or info.get("title") or "",
        "uploader": metadata.get("uploader") or info.get("uploader") or "",
        "description": info.get("description") or metadata.get("description") or "",
        "url": metadata.get("url") or info.get("webpage_url") or "",
    }
    _write_json(workspace / SOURCE_CONTEXT, context)
    review_path = workspace / TERM_REVIEW
    if not review_path.exists():
        _write_json(review_path, {"version": 1, "lookups": [], "entries": []})
    review = _read_json(review_path)
    (workspace / CORRECTED_TRANSCRIPT).write_text(
        _render_corrected(workspace, review["entries"]), encoding="utf-8"
    )


def start_lookup(workspace: Path, query: str, unit_ids: list[str]):
    review = _read_json(workspace / TERM_REVIEW)
    if len(review["lookups"]) >= MAX_LOOKUPS:
        raise ValueError("Term evidence budget exhausted (3 requests per run)")
    _, units = _units(workspace)
    if not query.strip() or not unit_ids or any(u not in units for u in unit_ids):
        raise ValueError("A nonempty query and valid original unit IDs are required")
    ids = list(units)
    nearby = set()
    for unit_id in unit_ids:
        index = ids.index(unit_id)
        nearby.update(ids[max(0, index - 2):index + 3])
    context = _read_json(workspace / SOURCE_CONTEXT)
    excerpts = "\n".join(
        units[u]["prefix"] + units[u]["text"] for u in ids if u in nearby
    )
    text = json.dumps(context, ensure_ascii=False) + "\n" + excerpts
    lookup = {
        "id": f"lookup-{len(review['lookups']) + 1:03d}",
        "operation": "search",
        "query": query,
        "unit_ids": unit_ids,
        "context": text[:MAX_CONTEXT_CHARS],
        "context_truncated": len(text) > MAX_CONTEXT_CHARS,
        "status": "pending",
        "started_at": time.time(),
        "sources": [],
    }
    review["lookups"].append(lookup)
    _write_json(workspace / TERM_REVIEW, review)
    return lookup


def start_fetch(workspace: Path, source_id: str):
    review = _read_json(workspace / TERM_REVIEW)
    for lookup in review["lookups"]:
        if lookup["status"] not in {"completed", "incomplete"}:
            continue
        for source in lookup["sources"]:
            if source["id"] == source_id:
                result = start_lookup(workspace, source["url"], lookup["unit_ids"])
                result.update(operation="fetch", url=source["url"], parent_source_id=source_id)
                review = _read_json(workspace / TERM_REVIEW)
                review["lookups"][-1] = result
                _write_json(workspace / TERM_REVIEW, review)
                return result
    raise ValueError("Fetch requires a source ID already returned in this run")


def finish_lookup(workspace: Path, lookup_id: str, result: dict):
    review = _read_json(workspace / TERM_REVIEW)
    lookup = next(item for item in review["lookups"] if item["id"] == lookup_id)
    # Only response data is persisted; credentials and request headers never enter this file.
    for key in (
        "status", "analysis", "usage", "request_id", "elapsed_ms", "error_code",
        "provider", "model",
        "response_finish_reason", "analysis_truncated",
    ):
        if key in result:
            lookup[key] = result[key]
    lookup["sources"] = [
        {"id": f"{lookup_id}:source-{index}",
         **{key: source.get(key) for key in ("index", "title", "url", "site_name",
                                             "snippet", "summary", "date", "authority")},
         "content": (source.get("content") or "")[:MAX_SOURCE_CONTENT_CHARS],
         "content_truncated": len(source.get("content") or "") > MAX_SOURCE_CONTENT_CHARS}
        for index, source in enumerate(result.get("sources", []), 1)
        if (source.get("title") or source.get("content")) and source.get("url")
    ]
    _write_json(workspace / TERM_REVIEW, review)
    return lookup


def record_term_review(workspace: Path, entries: list[dict]):
    review = _read_json(workspace / TERM_REVIEW)
    _, units = _units(workspace)
    context = _read_json(workspace / SOURCE_CONTEXT)
    sources = {f"metadata:{key}": [value] for key, value in context.items() if key != "url"}
    sources.update({
        source["id"]: [source.get("title") or "", source.get("content") or ""]
        # Summaries and the legacy auxiliary model's explanation are not source text.
        for lookup in review["lookups"] if lookup["status"] in {"completed", "incomplete"}
        for source in lookup["sources"]
    })
    checked = []
    for entry in entries:
        unit_id, start, end = entry["unit_id"], entry["start"], entry["end"]
        before, after = entry["before"], entry.get("after")
        if unit_id not in units or type(start) is not int or type(end) is not int:
            raise ValueError("Invalid unit ID or character offsets")
        if not (0 <= start < end <= len(units[unit_id]["text"])):
            raise ValueError(f"Invalid character range in {unit_id}")
        if not before or units[unit_id]["text"][start:end] != before:
            raise ValueError(f"Original text mismatch in {unit_id}")
        if entry["kind"] not in TERM_KINDS or entry["status"] not in REVIEW_STATUSES:
            raise ValueError("Only names and terms with a known review status are supported")
        if not entry["reason"].strip():
            raise ValueError("A context-based reason is required")
        if entry["status"] == "supported":
            if not after or not after.strip() or after == before or "\n" in after or "\r" in after:
                raise ValueError("A local replacement term is required")
            if re.fullmatch(r"[\d\s.,%+−-]+", before) or before in {
                "不", "未", "无", "没有", "不是",
            }:
                raise ValueError("Numbers and negations are outside term correction scope")
            evidence = entry.get("evidence", [])
            if not evidence:
                raise ValueError("Supported corrections require source evidence")
            for item in evidence:
                quote = item["quote"]
                source_texts = sources.get(item["source_id"], [])
                if not quote.strip() or not any(quote in text for text in source_texts):
                    raise ValueError(
                        "Evidence must quote saved metadata, a source title "
                        "or returned page content"
                    )
            if not any(after.casefold() in e["quote"].casefold() for e in evidence):
                raise ValueError("The replacement term must appear in the supporting evidence")
        checked.append({**entry, "reviewed_at": time.time()})
    combined = review["entries"] + checked
    corrected = _render_corrected(workspace, combined)
    review["entries"] = combined
    _write_json(workspace / TERM_REVIEW, review)
    (workspace / CORRECTED_TRANSCRIPT).write_text(corrected, encoding="utf-8")
    latest = list(_latest_entries(combined))
    return {
        "recorded": len(checked),
        "supported": sum(e["status"] == "supported" for e in latest),
        "unresolved": sum(e["status"] == "unresolved" for e in latest),
        "corrected_transcript": CORRECTED_TRANSCRIPT,
        "semantic_correctness": "agent_judgment_not_independently_verified",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["start-lookup", "start-fetch", "finish-lookup", "record"],
    )
    payload_input = parser.add_mutually_exclusive_group(required=True)
    payload_input.add_argument("payload", nargs="?", help="JSON supplied by the Pi term tools")
    payload_input.add_argument("--payload-file", type=Path, help="Task-local JSON payload file")
    args = parser.parse_args()
    payload = json.loads(
        args.payload_file.read_text(encoding="utf-8") if args.payload_file else args.payload
    )
    workspace = Path.cwd()
    try:
        if args.action == "start-lookup":
            result = start_lookup(workspace, payload["query"], payload["unit_ids"])
        elif args.action == "start-fetch":
            result = start_fetch(workspace, payload["source_id"])
        elif args.action == "finish-lookup":
            result = finish_lookup(workspace, payload["id"], payload["result"])
        else:
            result = record_term_review(workspace, payload["entries"])
    except (ValueError, KeyError, StopIteration) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
