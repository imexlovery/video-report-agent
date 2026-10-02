"""Trusted, deployment-scoped expiry sweeper. Never runs inside a Pi task."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import time
from pathlib import Path

SCAN_SECONDS = 2
API_TIMEOUT = 3
CLEANUP_GRACE_SECONDS = 30
HEALTH = Path("/tmp/task-cleaner-health.json")
LABEL_FORMAT = (
    '{{.ID}} {{.Label "video-report.deadline"}} {{.Label "video-report.task-id"}}'
)


def docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=API_TIMEOUT, check=True,
    )


def require_cleaner(scope: str) -> None:
    try:
        result = docker(
            "ps", "--filter", f"label=video-report.cleaner={scope}",
            "--filter", "health=healthy", "--format", "{{.ID}}",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Cannot verify task cleaner availability") from exc
    if not result.stdout.strip():
        raise ValueError("A healthy task cleaner is required for this deployment scope")


def sweep(scope: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    # Remove containers before their networks; Created containers are included.
    for kind in ("container", "network"):
        args = [kind, "ls"]
        if kind == "container":
            args.append("--all")
        result = docker(
            *args, "--filter", "label=video-report.task=1",
            "--filter", f"label=video-report.scope={scope}", "--format", LABEL_FORMAT,
        )
        expired = []
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 3:
                continue
            identifier, deadline, task_id = fields
            try:
                expires = float(deadline)
            except ValueError:
                continue
            if (task_id.startswith("video-report-task-")
                    and math.isfinite(expires) and expires <= now):
                expired.append(identifier)
        if expired:
            # A concurrent normal cleanup may remove an ID first. A failed call
            # is retried by the next scan, which discovers the remaining IDs.
            docker(kind, "rm", *(["--force"] if kind == "container" else []), *expired)


def healthy() -> bool:
    try:
        status = json.loads(HEALTH.read_text())
        os.kill(status["pid"], 0)
        return 0 <= time.time() - status["at"] < 10
    except (OSError, ValueError, KeyError):
        return False


def serve(scope: str) -> None:
    while True:
        try:
            sweep(scope)
            temporary = HEALTH.with_suffix(".tmp")
            temporary.write_text(json.dumps({"pid": os.getpid(), "at": time.time()}))
            temporary.replace(HEALTH)
        except (OSError, subprocess.SubprocessError):
            HEALTH.unlink(missing_ok=True)
            print("Task cleanup scan failed; retrying next scan", flush=True)
        time.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", default=os.getenv("PI_TASK_SCOPE", "video-report"))
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()
    if args.health:
        raise SystemExit(0 if healthy() else 1)
    serve(args.scope)
