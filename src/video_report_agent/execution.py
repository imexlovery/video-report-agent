"""Enforce a wall-clock deadline for one pipeline and its local subprocesses."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .pipeline import write_json
from .retention import cleanup_cancelled_run
from .task_container import wait_for_cleanup
from .trace import RunTrace

RUN_TIMEOUT_SECONDS = 30 * 60


def generate(run: Path, *, timeout=RUN_TIMEOUT_SECONDS, env: dict[str, str] | None = None) -> dict:
    run = run.resolve()
    command = [
        sys.executable, "-c",
        "from pathlib import Path; import sys; "
        "from video_report_agent.pipeline import generate; generate(Path(sys.argv[1]))",
        str(run),
    ]
    deadline = time.monotonic() + timeout
    worker_env = {**os.environ, **env} if env is not None else os.environ.copy()
    worker_env["VIDEO_REPORT_RUN_DEADLINE"] = str(deadline)
    with (run / "worker.log").open("ab") as log:
        process = subprocess.Popen(
            command, stdout=log, stderr=log, start_new_session=True,
            env=worker_env,
        )
        timed_out = False
        cancelled = False
        try:
            while process.poll() is None:
                if (run / "cancel.requested").exists():
                    cancelled = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    process.wait(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    pass
        finally:
            # The worker and yt-dlp/FFmpeg/Pi/browser children share this process group.
            # Kill before writing FAILED so a surviving worker cannot overwrite it.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            # Docker tasks are supervised outside this process group. Wait for
            # parent-death cleanup before cancellation deletes generation files.
            wait_for_cleanup(run)
    path = run / "status.json"
    status = json.loads(path.read_text())
    if cancelled:
        status.update(state="CANCELLED", stage="CANCELLED", finished_at=time.time())
        RunTrace(run).cancelled()
        write_json(path, status)
        cleanup_cancelled_run(run)
    elif timed_out or process.returncode or status.get("state") not in {"RENDERED", "FAILED"}:
        status.update(
            state="FAILED", stage="FAILED", finished_at=time.time(),
            error_category="EXECUTION_TIMEOUT" if timed_out else "EXECUTION_FAILURE",
            error=("任务执行超过 30 分钟，已停止本地处理。" if timed_out
                   else "任务进程异常结束，请查看 worker.log。"),
        )
        write_json(path, status)
    return status
