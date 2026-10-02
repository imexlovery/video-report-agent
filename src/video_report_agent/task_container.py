"""Trusted Docker task launcher and parent-death cleanup (never runs in Pi)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .task_cleaner import require_cleaner

TASK_PATH = Path("/workspace")
IMAGE_PYTHON = "/app/web-service/.venv/bin/python"
IMAGE_PATH = "/app/web-service/.venv/bin:/usr/local/bin:/usr/bin:/bin"
OUTPUTS = ("report.html", "assets", "sessions", "inspection")


def stage_inputs(run: Path) -> Path:
    stage = run / ".generation"
    stage.mkdir(mode=0o700)
    shutil.copy2(run / "transcript.md", stage / "transcript.md")
    metadata = json.loads((run / "input.json").read_text()) if (run / "input.json").exists() else {}
    (stage / "input.json").write_text(
        json.dumps(
            {key: metadata[key] for key in ("url", "video_id", "report_mode") if key in metadata},
            ensure_ascii=False,
        )
    )
    info = run / "download" / "source.info.json"
    if info.is_file():
        source = json.loads(info.read_text())
        (stage / "download").mkdir()
        (stage / "download" / "source.info.json").write_text(
            json.dumps(
                {
                    "description": source.get("description"),
                },
                ensure_ascii=False,
            )
        )
    return stage


def export_outputs(stage: Path, run: Path) -> None:
    # Pi can create arbitrary symlinks/FIFOs. Never follow them on the trusted side.
    def copy(source: Path, target: Path):
        mode = source.lstat().st_mode
        if stat.S_ISDIR(mode):
            target.mkdir(exist_ok=True)
            for child in source.iterdir():
                copy(child, target / child.name)
        elif stat.S_ISREG(mode):
            shutil.copyfile(source, target)
        else:
            raise ValueError(f"Unsupported task output: {source.relative_to(stage)}")

    for name in OUTPUTS:
        source = stage / name
        if source.exists() or source.is_symlink():
            copy(source, run / name)


def snapshot_models(directory: Path, agent_dir: Path, selections: list[dict]) -> None:
    executable = shutil.which("pi")
    if not executable:
        raise ValueError("Pi is required to resolve task model configuration")
    package = Path(executable).resolve().parent
    while not (package / "package.json").is_file() and package != package.parent:
        package = package.parent
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("task_models.mjs")), str(package), str(agent_dir)],
        input=json.dumps(selections),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        # Provider configuration and credentials must not escape into error messages/logs.
        raise ValueError("Cannot resolve API-key configuration for task models")
    config = json.loads(result.stdout)
    directory.mkdir(mode=0o700)
    for name in ("models", "auth"):
        path = directory / f"{name}.json"
        path.write_text(json.dumps(config[name]))
        path.chmod(0o600)


def mount(path: Path, destination: str) -> str:
    volume = os.getenv("PI_TASK_RUNS_VOLUME")
    if volume:
        root = Path(os.environ["PI_TASK_RUNS_ROOT"]).resolve()
        subpath = path.resolve().relative_to(root)
        return f"type=volume,src={volume},dst={destination},volume-subpath={subpath},volume-nocopy"
    return f"type=bind,src={path.resolve()},dst={destination}"


def launch_command(stage: Path, config: Path, pi_command: list[str], deadline: float) -> list[str]:
    if os.getuid() == 0:
        raise ValueError("Task controller must run as a non-root user")
    scope = os.getenv("PI_TASK_SCOPE", "video-report")
    require_cleaner(scope)
    name = "video-report-task-" + uuid.uuid4().hex
    expires = time.time() + max(0, deadline - time.monotonic())
    image = os.getenv("PI_TASK_IMAGE", "video-report-agent:linux-amd64")
    task_env = [
        "HOME=/tmp/home",
        f"PATH={IMAGE_PATH}",
        "PYTHONUNBUFFERED=1",
        "PLAYWRIGHT_BROWSERS_PATH=/opt/playwright",
        f"VIDEO_REPORT_PYTHON={IMAGE_PYTHON}",
        "PI_CODING_AGENT_DIR=/tmp/pi-agent",
    ]
    create = [
        "docker",
        "create",
        "--name",
        name,
        "--label",
        "video-report.task=1",
        "--label",
        f"video-report.scope={scope}",
        "--label",
        f"video-report.task-id={name}",
        "--label",
        f"video-report.deadline={expires}",
        "--interactive",
        "--init",
        "--pull=never",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--memory",
        os.getenv("PI_TASK_MEMORY", "2g"),
        "--memory-swap",
        os.getenv("PI_TASK_MEMORY", "2g"),
        "--cpus",
        os.getenv("PI_TASK_CPUS", "2"),
        "--pids-limit",
        "256",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
        "--shm-size",
        "256m",
        "--network",
        name,
        "--workdir",
        str(TASK_PATH),
        "--mount",
        mount(stage, str(TASK_PATH)),
        "--mount",
        mount(config, "/pi-config") + ",readonly",
        "--entrypoint",
        "/usr/bin/env",
        image,
        "-i",
        *task_env,
        "sh",
        "-c",
        'mkdir -p "$HOME" /tmp/pi-agent; cp /pi-config/* /tmp/pi-agent/; exec "$@"',
        "task-pi",
        *pi_command,
    ]
    return [
        sys.executable,
        "-m",
        "video_report_agent.task_container",
        "--parent",
        str(os.getpid()),
        "--deadline",
        str(deadline),
        "--name",
        name,
        "--config",
        str(config),
        "--",
        *create,
    ]


def supervise(parent: int, deadline: float, name: str, config: Path, create: list[str]) -> int:
    """Run outside the Worker's process group so SIGKILL cannot skip Docker cleanup."""
    stopping = False

    def stop(_signal, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def live():
        return not stopping and os.getppid() == parent and time.monotonic() < deadline

    attached = None
    result = 1
    cleaned = False
    creation_started = False
    creation_completed = False
    labels = [create[i + 1] for i, argument in enumerate(create) if argument == "--label"]
    try:
        # A separate bridge prevents joining the service or another task's network.
        subprocess.run(
            [
                "docker",
                "network",
                "create",
                "--driver",
                "bridge",
                "--label",
                "video-report.task=1",
                *[argument for label in labels if label != "video-report.task=1"
                  for argument in ("--label", label)],
                "--opt",
                "com.docker.network.bridge.enable_icc=false",
                name,
            ],
            stdout=subprocess.DEVNULL,
            check=True,
            timeout=30,
        )
        if not live():
            return 1
        creation_started = True
        subprocess.run(create, stdout=subprocess.DEVNULL, check=True, timeout=30)
        creation_completed = True
        if not live():
            return 1
        attached = subprocess.Popen(["docker", "start", "--attach", "--interactive", name])
        while attached.poll() is None and live():
            time.sleep(0.1)
        result = 0 if stopping else attached.returncode if attached.returncode is not None else 1
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Task container failed: {type(exc).__name__}", file=sys.stderr)
    finally:
        # Killing an attached Docker client alone does not stop its container.
        try:
            removed = subprocess.run(
                ["docker", "rm", "--force", name], capture_output=True, timeout=30
            )
            if removed.returncode and b"No such container" not in removed.stderr:
                print("Task container cleanup failed", file=sys.stderr)
                result = 1
            network = subprocess.run(
                ["docker", "network", "rm", name], capture_output=True, timeout=30
            )
            if network.returncode and b"not found" not in network.stderr:
                print("Task network cleanup failed", file=sys.stderr)
                result = 1
            cleaned = (
                (removed.returncode == 0 or b"No such container" in removed.stderr)
                and (network.returncode == 0 or b"not found" in network.stderr)
                # A timed-out Docker client does not cancel the daemon request.
                # "Not found" now cannot certify no late creation will occur.
                and (not creation_started or creation_completed)
            )
        except (OSError, subprocess.SubprocessError):
            print("Docker unavailable during task cleanup", file=sys.stderr)
            result = 1
        finally:
            if attached is not None:
                try:
                    attached.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    attached.kill()
                    attached.wait()
            shutil.rmtree(config, ignore_errors=True)
            confirmation = config.parent / "task-cleanup.json"
            temporary = confirmation.with_suffix(".tmp")
            temporary.write_text(json.dumps({"cleaned": cleaned}))
            temporary.replace(confirmation)
    return result


def wait_for_cleanup(run: Path) -> None:
    marker = run / "task-supervisor.pid"
    if not marker.is_file():
        return
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        value = marker.read_text()
        if value != "pending":
            try:
                os.kill(int(value), 0)
            except ProcessLookupError:
                confirmation = run / "task-cleanup.json"
                if (not confirmation.is_file()
                        or not json.loads(confirmation.read_text())["cleaned"]):
                    raise RuntimeError("Task cleanup was not confirmed; keep run artifacts")
                marker.unlink(missing_ok=True)
                return
        else:
            # No PID was published before worker death; only the supervisor's
            # explicit completion can authorize deletion of the mounted files.
            confirmation = run / "task-cleanup.json"
            if confirmation.is_file() and json.loads(confirmation.read_text())["cleaned"]:
                marker.unlink(missing_ok=True)
                return
        time.sleep(0.1)
    raise RuntimeError("Task supervisor has not finished container cleanup; keep run artifacts")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    sys.exit(supervise(args.parent, args.deadline, args.name, args.config, args.command[1:]))
