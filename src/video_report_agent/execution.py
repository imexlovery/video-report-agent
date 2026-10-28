"""Enforce a wall-clock deadline for one pipeline and its local subprocesses."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .pipeline import write_json
from .retention import cleanup_cancelled_run
from .storage import Store
from .task_container import wait_for_cleanup
from .trace import RunTrace
from .usage import execution_statistics

RUN_TIMEOUT_SECONDS = 30 * 60


def generate(
    run: Path,
    *,
    timeout=RUN_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
    database_path=None,
    execution_id=None,
) -> dict:
    run = run.resolve()
    store = Store(database_path or run.parent / "state.sqlite3", initialize=False)
    if execution_id is None:
        execution_id = store.claim_run(run.name, deadline=time.time() + timeout)
    store.supervise(run.name, execution_id)
    store.export(run.name, include_report=False)
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; import sys; "
        "from video_report_agent.pipeline import generate; "
        "generate(Path(sys.argv[1]), database_path=Path(sys.argv[2]), execution_id=sys.argv[3])",
        str(run),
        str(store.path),
        execution_id,
    ]
    deadline = time.monotonic() + timeout
    worker_env = {**os.environ, **env} if env is not None else os.environ.copy()
    worker_env["VIDEO_REPORT_RUN_DEADLINE"] = str(deadline)
    with (run / "worker.log").open("ab") as log:
        process = subprocess.Popen(
            command,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env=worker_env,
        )
        timed_out = False
        cancelled = False
        worker_error = None
        try:
            store.worker(run.name, execution_id, process.pid)
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
        except Exception as exc:
            worker_error = str(exc)
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
    record = store.run(run.name)
    status = record["result"] or record["status"]
    if cancelled:
        status.update(state="CANCELLED", stage="CANCELLED", finished_at=time.time())
        RunTrace(run).cancelled()
        write_json(path, status)
    elif (
        worker_error
        or timed_out
        or process.returncode
        or status.get("state") not in {"RENDERED", "TRANSCRIPT_READY", "FAILED"}
    ):
        status.update(
            state="FAILED",
            stage="FAILED",
            finished_at=time.time(),
            error_category="EXECUTION_TIMEOUT" if timed_out else "EXECUTION_FAILURE",
            error=(
                "任务执行超过 30 分钟，已停止本地处理。"
                if timed_out
                else worker_error or "任务进程异常结束，请查看 worker.log。"
            ),
        )
        write_json(path, status)
    store.capture(run, execution_id)
    store.statistics(run.name, execution_statistics(run, status))
    store.result(run.name, execution_id, status, exited=True)
    store.release_caches(run.name, execution_id, status.get("error") or status["state"])
    if not record["managed"]:
        store.finalize(run.name, execution_id, status)
    if cancelled:
        cleanup_cancelled_run(run, database_path=store.path)
    write_json(path, status)
    return status
