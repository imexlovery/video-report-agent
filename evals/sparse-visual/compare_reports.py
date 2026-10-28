"""Generate a frozen-baseline report with an optional parallel visual text source."""

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from video_report_agent.visual import REPORT_PROMPT

VISUAL_PROMPT = "读取 visual-observations.jsonl；空文件表示无画面材料。\n" + REPORT_PROMPT.replace(
    "visual/observations.jsonl", "visual-observations.jsonl"
)


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


async def generate(args):
    # Set the isolated Pi configuration before importing the existing RPC consumer.
    os.environ["PI_CODING_AGENT_DIR"] = str(args.config.resolve())
    os.environ["PI_TRACE_FULL"] = "0"
    os.environ["VIDEO_REPORT_PYTHON"] = sys.executable
    from video_report_agent.pi import PiRunner
    from video_report_agent.report_content import fill_video_description
    from video_report_agent.report_image import render_report_image

    baseline = args.baseline.resolve()
    run = args.output.resolve()
    run.mkdir(parents=True, exist_ok=False)
    for name in ("transcript.md", "input.json", "SKILL.md", "request_trace.ts"):
        shutil.copyfile(baseline / name, run / name)
    for name in ("assets", "modes", "references", "download"):
        if (baseline / name).is_dir():
            shutil.copytree(baseline / name, run / name)
    if args.observations:
        shutil.copyfile(args.observations, run / "visual-observations.jsonl")
    else:
        (run / "visual-observations.jsonl").write_text("")
    frozen = json.loads((baseline / "invocation.json").read_text())
    command = [part.replace(frozen["cwd"], str(run)) for part in frozen["command"]]
    command[0] = shutil.which("pi") or command[0]
    prompt = frozen["prompt"] + "\n" + VISUAL_PROMPT
    metadata = json.loads((run / "input.json").read_text())
    selection = metadata["model_selection"]
    save(
        run / "invocation.json",
        {
            "baseline": str(baseline),
            "cwd": str(run),
            "command": command,
            "prompt": prompt,
            "model_selection": selection,
            "observations": bool(args.observations),
            "configuration_change": "same additional source-reading instructions in both arms",
        },
    )
    state = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat()}
    save(run / "run-status.json", state)
    print(json.dumps({"run": str(run), **state}), flush=True)
    started = time.monotonic()
    runner = PiRunner(**selection, review=False, isolation="local")
    with (run / "pi.stderr.log").open("wb") as stderr:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=run,
            env=os.environ.copy(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
            limit=32 * 1024 * 1024,
        )
        try:
            async with asyncio.timeout(1800):
                await runner._consume(process, run, prompt)
            state["generation_seconds"] = round(time.monotonic() - started, 3)
        except Exception as exc:
            state.update(status="failed", error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            state["elapsed_seconds"] = round(time.monotonic() - started, 3)
            save(run / "run-status.json", state)
    report = run / "report.html"
    fill_video_description(report, run)
    render_started = time.monotonic()
    await asyncio.to_thread(render_report_image, run)
    events = [json.loads(line) for line in (run / "pi.events.jsonl").read_text().splitlines()]
    messages = [
        event["message"]
        for event in events
        if event.get("type") == "message_end"
        and event.get("message", {}).get("role") == "assistant"
    ]
    usage = {
        key: sum(message.get("usage", {}).get(key, 0) for message in messages)
        for key in ("input", "output", "cacheRead", "cacheWrite", "totalTokens")
    }
    state.update(
        status="completed",
        finished_at=datetime.now(timezone.utc).isoformat(),
        render_seconds=round(time.monotonic() - render_started, 3),
        total_seconds=round(time.monotonic() - started, 3),
        usage=usage,
        request_count=sum(event.get("type") == "request_start" for event in events),
        tool_calls=sum(event.get("type") == "tool_execution_start" for event in events),
        agent_settled=any(event.get("type") == "agent_settled" for event in events),
        final_stop_reason=messages[-1].get("stopReason"),
    )
    save(run / "run-status.json", state)
    print(json.dumps({"run": str(run), **state}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--observations", type=Path)
    asyncio.run(generate(parser.parse_args()))


if __name__ == "__main__":
    main()
