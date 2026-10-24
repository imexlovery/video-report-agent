"""Public core command-line entry points."""

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from .execution import generate
from .pi import PROJECT_ROOT
from .pipeline import create_run


def main():
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=Path, default=Path("runs"))
    parser.add_argument(
        "--database", type=Path, help="Shared embedding database; default runs/state.sqlite3"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("generate", "transcribe"):
        run = sub.add_parser(command)
        run.add_argument("url")
        run.add_argument("--report-mode", choices=["standard", "brief"], default="standard")
        run.add_argument("--transcript-mode", choices=["auto", "asr-only", "fused"], default="auto")
        run.add_argument("--ocr-mode", choices=["off", "auto", "roi", "on"], default="off")
        run.add_argument("--ocr-roi")
        run.add_argument("--subtitle-file", type=Path)
        run.add_argument("--asr-backend", choices=["mlx", "paraformer"])
        run.add_argument("--asr-model")
        run.add_argument("--ocr-backend", choices=["rapidocr"])
        run.add_argument(
            "--sources",
            nargs="+",
            choices=["asr", "subtitle_track", "bilibili_ai"],
            help="Selected sources in primary-source order; OCR is deferred",
        )
    compare = sub.add_parser("compare-asr")
    compare.add_argument("--manifest", type=Path, required=True)
    subtitles = sub.add_parser("audit-subtitles")
    subtitles.add_argument("--manifest", type=Path, required=True)
    subtitles.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "audit-subtitles":
        from .compare_subtitles import audit_subtitles

        print(json.dumps(audit_subtitles(args.manifest, args.output), ensure_ascii=False, indent=2))
        return 0
    if args.command == "compare-asr":
        from .compare_asr import compare_asr

        output = compare_asr(args.manifest, args.runs)
        print(output)
        return 0 if json.loads((output / "status.json").read_text())["state"] == "COMPLETE" else 1
    if args.command in {"generate", "transcribe"}:
        status = generate(
            create_run(
                args.runs,
                args.url,
                report_mode=args.report_mode,
                transcript_mode=args.transcript_mode,
                transcript_sources=args.sources,
                transcript_only=args.command == "transcribe",
                ocr_mode=args.ocr_mode,
                ocr_roi=args.ocr_roi,
                subtitle_file=args.subtitle_file,
                asr_backend=args.asr_backend,
                asr_model=args.asr_model,
                ocr_backend=args.ocr_backend,
                database_path=args.database,
            ),
            database_path=args.database,
        )
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if status["state"] in {"RENDERED", "TRANSCRIPT_READY"} else 1
