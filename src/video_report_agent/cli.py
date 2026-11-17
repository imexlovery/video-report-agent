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
        login = run.add_mutually_exclusive_group()
        login.add_argument("--browser", help="仅需要登录时读取本机所选浏览器")
        login.add_argument("--cookies", type=Path, help="仅需要登录时读取本机 Cookie 文件")
        run.add_argument("--report-mode", choices=["standard", "brief"], default="standard")
        run.add_argument("--report-inspection", action="store_true", help="初稿后运行报告检查")
        run.add_argument("--style-edit", action=argparse.BooleanOptionalAction, default=True,
                         help="独立文风校订（默认开启；--no-style-edit 跳过）")
        run.add_argument("--term-correction", action="store_true", help="核对并修正名称与术语")
        run.add_argument(
            "--visual-enhancement", action="store_true", help="抽帧生成画面描述辅助理解"
        )
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
    for name in ("fetch-douyin", "fetch-xiaohongshu"):
        fetch = sub.add_parser(name, help="匿名优先取字幕，否则取音频；不运行 ASR 或报告")
        fetch.add_argument("url")
        login = fetch.add_mutually_exclusive_group()
        login.add_argument("--browser", help="仅平台明确要求登录时读取本机浏览器 Cookie，如 edge")
        login.add_argument("--cookies", type=Path, help="仅登录受限时读取本机导出的 Cookie 文件")
        fetch.add_argument("--output", type=Path, required=True)
    preflight = sub.add_parser("preflight", help="匿名检查四个平台的字幕或音频是否可访问")
    preflight.add_argument("url")
    material = sub.add_parser("fetch-source", help="统一取材入口，支持用户电脑上的登录回退")
    material.add_argument("url")
    material.add_argument("--output", type=Path, required=True)
    login = material.add_mutually_exclusive_group()
    login.add_argument("--browser")
    login.add_argument("--cookies", type=Path)
    args = parser.parse_args()
    if args.command in {"preflight", "fetch-source"}:
        from .audio import extract_audio
        from .ingest import UrlIngestError
        from .source_access import acquire_source, preflight_source

        try:
            if args.command == "preflight":
                result = preflight_source(args.url)
            else:
                source, snapshot, downloaded = acquire_source(
                    args.url, args.output, browser=args.browser, cookie_file=args.cookies,
                )
                material_root = args.output.resolve() / source.video_id
                result = {"platform": source.platform, "title": snapshot["title"],
                          "material_path": str(material_root / "source-material.json")}
                if downloaded:
                    audio = extract_audio(downloaded.media_path, material_root / "audio.wav")
                    result["audio_path"] = str(audio.path.resolve())
        except UrlIngestError as exc:
            print(json.dumps({"error": exc.category, "message": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("available", True) else 1
    if args.command in {"fetch-douyin", "fetch-xiaohongshu"}:
        from .douyin import fetch_douyin_media
        from .ingest import UrlIngestError
        from .xiaohongshu import fetch_xiaohongshu_media

        try:
            acquire = (fetch_douyin_media if args.command == "fetch-douyin"
                       else fetch_xiaohongshu_media)
            result = acquire(args.url, args.output, browser=args.browser, cookie_file=args.cookies)
        except UrlIngestError as exc:
            print(json.dumps({"error": exc.category, "message": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
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
                term_correction=args.term_correction,
                report_inspection=args.report_inspection,
                style_edit=args.style_edit,
                visual_enhancement=args.visual_enhancement,
                transcript_mode=args.transcript_mode,
                transcript_sources=args.sources,
                transcript_only=args.command == "transcribe",
                ocr_mode=args.ocr_mode,
                ocr_roi=args.ocr_roi,
                subtitle_file=args.subtitle_file,
                source_access=args.transcript_mode == "auto",
                source_login=bool(args.browser or args.cookies),
                asr_backend=args.asr_backend,
                asr_model=args.asr_model,
                ocr_backend=args.ocr_backend,
                database_path=args.database,
            ),
            database_path=args.database,
            env={"VIDEO_SOURCE_BROWSER": args.browser or "",
                 "VIDEO_SOURCE_COOKIE_FILE": str(args.cookies.resolve()) if args.cookies else ""},
        )
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if status["state"] in {"RENDERED", "TRANSCRIPT_READY"} else 1
