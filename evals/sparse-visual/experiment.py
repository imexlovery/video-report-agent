"""Standalone visual experiment using the production visual module."""

import argparse
import json
import os
import shutil
from pathlib import Path

from video_report_agent.visual import describe_frames, sample_frames


def prepare(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(args.transcript, output / "transcript.md")
    shutil.copyfile(args.units, output / "canonical-transcript.jsonl")
    result = sample_frames(args.video, output, output / "canonical-transcript.jsonl", args.frames)
    print(json.dumps({"output": str(output), "frames": result["actual_frames"]}))


def describe(args):
    result = describe_frames(
        args.output,
        api_key=os.environ["DEEPSEEK_API_KEY"],
        model=args.model,
        max_tokens=args.max_tokens,
    )
    print(json.dumps(result, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="Local extraction only; no model request")
    for name in ("video", "transcript", "units", "output"):
        prep.add_argument(f"--{name}", type=Path, required=True)
    prep.add_argument("--frames", type=int, default=9)
    desc = commands.add_parser("describe", help="One paid DeepSeek vision request; no retries")
    desc.add_argument("--output", type=Path, required=True)
    desc.add_argument("--model", default="deepseek-flash")
    desc.add_argument("--max-tokens", type=int, default=6144)
    args = parser.parse_args()
    (prepare if args.command == "prepare" else describe)(args)


if __name__ == "__main__":
    main()
