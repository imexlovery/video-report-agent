"""Real Docker/Pi-tool checks with synthetic inputs; no provider requests."""

import asyncio
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from video_report_agent import execution, pi, task_container
from video_report_agent.pi import PiError, PiRunner
from video_report_agent.task_cleaner import CLEANUP_GRACE_SECONDS

IMAGE = os.getenv("PI_TASK_TEST_IMAGE")
docker_test = pytest.mark.skipif(not IMAGE, reason="Set PI_TASK_TEST_IMAGE for real Docker checks")


@pytest.fixture(scope="module")
def cleaner():
    if not IMAGE:
        yield None
        return
    scope = "isolation-test-" + uuid.uuid4().hex
    name = scope + "-cleaner"
    command = [
        "docker", "run", "--detach", "--rm", "--init", "--name", name,
        "--read-only", "--network", "none", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--group-add", "0",
        "--memory", "256m", "--cpus", "0.5", "--pids-limit", "32",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=16m,mode=1777",
        "--label", f"video-report.cleaner={scope}",
        "--health-cmd", "python -m video_report_agent.task_cleaner --health",
        "--health-interval", "2s", "--health-timeout", "5s",
        "--mount", "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
        "--entrypoint", "python", IMAGE,
        "-m", "video_report_agent.task_cleaner", "--scope", scope,
    ]
    subprocess.run(command, check=True, capture_output=True)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                task_container.require_cleaner(scope)
                break
            except ValueError:
                assert time.monotonic() < deadline, subprocess.run(
                    ["docker", "logs", name], capture_output=True, text=True,
                ).stdout
                time.sleep(0.2)
        yield scope
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True)


@pytest.fixture
def inputs(tmp_path, monkeypatch, cleaner):
    monkeypatch.setenv("PI_TASK_IMAGE", IMAGE or "missing-isolation-image")
    monkeypatch.setenv("PI_TASK_SCOPE", cleaner or "isolation-test-no-daemon")
    monkeypatch.delenv("PI_TASK_RUNS_VOLUME", raising=False)
    for key in ("ADMIN_TOKEN", "SMTP_PASSWORD", "GITHUB_CLIENT_SECRET", "DASHSCOPE_API_KEY"):
        monkeypatch.setenv(key, "FORBIDDEN_" + key)
    config = tmp_path / "shared-config"
    config.mkdir()
    models = {
        name: {
            "baseUrl": "https://example.invalid/v1",
            "api": "openai-completions",
            "models": [{"id": "fixture", "name": name, "input": ["text"], "reasoning": False}],
        }
        for name in ("primary", "alternate", "unrelated")
    }
    (config / "models.json").write_text(json.dumps({"providers": models}))
    (config / "auth.json").write_text(
        json.dumps({name: {"type": "api_key", "key": "FAKE_" + name} for name in models})
    )
    monkeypatch.setattr(pi, "PI_AGENT_DIR", config)
    run = tmp_path / "runs" / "current"
    run.mkdir(parents=True)
    (run / "transcript.md").write_text("[U001 | 0.000–1.000s] synthetic source")
    (run / "input.json").write_text(
        json.dumps(
            {
                "video_id": "fixture",
                "report_mode": "brief",
                "model_selection": {"api_key": "DO_NOT_STAGE"},
                "subtitle_file": "/private/file",
            }
        )
    )
    for relative in ("runs/neighbor/canary", "auth.sqlite3", "ledger.sqlite3", ".env"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("HOST_CANARY")
    return run, config


def prepare(run, config):
    stage = task_container.stage_inputs(run)
    snapshot = run / ".generation-config"
    task_container.snapshot_models(
        snapshot,
        config,
        [
            {"provider": "primary", "model": "fixture"},
            {"provider": "alternate", "model": "fixture"},
        ],
    )
    return stage, snapshot


def launch(stage, snapshot, command, *, seconds=45):
    return subprocess.Popen(
        task_container.launch_command(stage, snapshot, command, time.monotonic() + seconds),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def wait_started(stage, process):
    end = time.monotonic() + 30
    while not (stage / "started").exists():
        if process.poll() is not None or time.monotonic() > end:
            raise AssertionError(process.communicate(timeout=5)[1].decode())
        time.sleep(0.1)


def assert_removed(command):
    name = command[command.index("--name") + 1]
    assert subprocess.run(["docker", "inspect", name], capture_output=True).returncode
    assert subprocess.run(["docker", "network", "inspect", name], capture_output=True).returncode


@docker_test
def test_real_pi_tools_files_environment_and_inspection(inputs, tmp_path):
    run, config = inputs
    stage, snapshot = prepare(run, config)
    forbidden = [
        str(tmp_path / item)
        for item in (
            "runs/neighbor/canary",
            "auth.sqlite3",
            "ledger.sqlite3",
            ".env",
        )
    ] + ["/app/runs/neighbor/canary", "/app/config/pi/auth.json", "/var/run/docker.sock"]
    script = r"""
import { readFile, writeFile } from 'node:fs/promises';
const root = '/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/tools/';
const { createReadToolDefinition } = await import(root + 'read.js');
const { createWriteToolDefinition } = await import(root + 'write.js');
const { createEditToolDefinition } = await import(root + 'edit.js');
const { createBashToolDefinition } = await import(root + 'bash.js');
const read = createReadToolDefinition('/workspace');
const write = createWriteToolDefinition('/workspace');
const edit = createEditToolDefinition('/workspace');
const bash = createBashToolDefinition('/workspace');
const results = {};
const input = await read.execute('input', {path: 'transcript.md'});
results.input = input.content[0].text.includes('U001');
await write.execute('report', {path:'report.html',
  content:'<html><body data-source-units="U001">fixture</body></html>'});
await write.execute('asset', {path:'assets/probe.txt', content:'before'});
await edit.execute('edit', {path:'assets/probe.txt',
  edits:[{oldText:'before', newText:'after'}]});
results.edit = (await readFile('/workspace/assets/probe.txt','utf8')) === 'after';
results.denied = [];
for (const path of FORBIDDEN) {
  let readable = false, writable = false;
  try { await read.execute('denied-read', {path}); readable = true; } catch {}
  try { await write.execute('denied-write', {path,content:'CORRUPTED'}); writable = true; } catch {}
  results.denied.push({path,readable,writable});
}
const secretCommand = "python -c 'import os,json; from pathlib import Path; " +
  "keys=[\"ADMIN_TOKEN\",\"SMTP_PASSWORD\",\"GITHUB_CLIENT_SECRET\"," +
  "\"DASHSCOPE_API_KEY\",\"DOCKER_HOST\"]; " +
  "assert all(k not in os.environ for k in keys); " +
  "assert not Path(\"/var/run/docker.sock\").exists(); " +
  "assert os.getuid()!=0; " +
  "assert not os.access(\"/app/agent-core/src/video_report_agent/task_cleaner.py\",os.W_OK); " +
  "assert not Path(\"/tmp/task-cleaner-health.json\").exists(); " +
  "paths=Path(\"/pi-config\").glob(\"*.json\"); " +
  "assert all(\"FORBIDDEN_\" not in p.read_text() for p in paths); " +
  "auth=json.loads(Path(\"/tmp/pi-agent/auth.json\").read_text()); " +
  "assert set(auth)=={\"primary\",\"alternate\"}; " +
  "assert auth[\"primary\"][\"key\"]==\"FAKE_primary\"; " +
  "assert auth[\"alternate\"][\"key\"]==\"FAKE_alternate\"; " +
  "assert \"NoNewPrivs:\\t1\" in Path(\"/proc/self/status\").read_text(); print(\"PASS\")'";
const bashResult = await bash.execute('bash', {command:secretCommand});
results.bash = bashResult.content[0].text.includes('PASS');
const inspection = await bash.execute('inspect',
  {command:'$VIDEO_REPORT_PYTHON -m video_report_agent.inspect_report --label fixture'});
const inspectionText = inspection.content.map(part=>part.text||'').join('');
results.inspection = inspectionText.includes('"status": "checked"');
await writeFile('/workspace/probe-results.json', JSON.stringify(results));
"""
    (stage / "probe.mjs").write_text(
        script.replace("of FORBIDDEN)", "of " + json.dumps(forbidden) + ")")
    )
    process = launch(stage, snapshot, ["node", "/workspace/probe.mjs"], seconds=90)
    _, stderr = process.communicate(timeout=100)
    assert process.returncode == 0, stderr.decode()
    results = json.loads((stage / "probe-results.json").read_text())
    assert results["input"] and results["edit"] and results["bash"] and results["inspection"]
    assert all(not item["readable"] and not item["writable"] for item in results["denied"])
    for path in forbidden[:4]:
        assert Path(path).read_text() == "HOST_CANARY"
    assert "DO_NOT_STAGE" not in (stage / "input.json").read_text()
    assert not snapshot.exists()
    task_container.export_outputs(stage, run)
    assert (run / "report.html").is_file()
    assert (run / "inspection/fixture/top.png").is_file()
    assert_removed(process.args)


@docker_test
@pytest.mark.parametrize("outcome", ["normal", "failure", "timeout", "cancel", "worker_killed"])
def test_container_and_child_cleanup(inputs, outcome):
    run, config = inputs
    stage, snapshot = prepare(run, config)
    code = (
        "import subprocess,sys,time; from pathlib import Path; "
        "subprocess.Popen([sys.executable,'-c',"
        "\"import time; from pathlib import Path; time.sleep(8); "
        "Path('/workspace/survived').touch()\"]); "
        "Path('/workspace/started').touch(); "
    )
    code += (
        "sys.exit(0)"
        if outcome == "normal"
        else "sys.exit(7)"
        if outcome == "failure"
        else "time.sleep(60)"
    )
    command = task_container.launch_command(
        stage,
        snapshot,
        ["python", "-c", code],
        time.monotonic() + (6 if outcome == "timeout" else 45),
    )
    if outcome == "worker_killed":
        # An actual worker launches the independently supervised task, then receives SIGKILL.
        command[command.index("--parent") + 1] = "PARENT"
        worker = (
            "import os,subprocess,time,json; from pathlib import Path; "
            f"command={command!r}; command[command.index('--parent')+1]=str(os.getpid()); "
            "p=subprocess.Popen(command,start_new_session=True); "
            f"Path({str(run / 'supervisor')!r}).write_text(str(p.pid)); time.sleep(60)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", worker], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    else:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    if outcome in {"timeout", "cancel", "worker_killed"}:
        wait_started(stage, process)
    if outcome == "cancel":
        process.terminate()
    elif outcome == "worker_killed":
        os.kill(process.pid, signal.SIGKILL)
    _, stderr = process.communicate(timeout=80)
    if outcome == "normal":
        assert process.returncode == 0, stderr.decode()
    elif outcome in {"timeout", "failure"}:
        assert process.returncode != 0
    if outcome == "worker_killed":
        (run / "task-supervisor.pid").write_text((run / "supervisor").read_text())
        task_container.wait_for_cleanup(run)
    assert_removed(command)
    time.sleep(8)
    assert not (stage / "survived").exists()


@docker_test
def test_isolated_pi_rpc_and_start_failure_never_falls_back(inputs, monkeypatch):
    run, _ = inputs
    skill = run.parent / "fixture-skill"
    shutil.copytree(pi.SKILL, skill)
    (skill / "fixture.py").write_text(
        "import json,sys; from pathlib import Path\n"
        "json.loads(sys.stdin.readline())\n"
        "Path('report.html').write_text('<html><body>fixture</body></html>')\n"
        "print(json.dumps({'type':'message_end','message':{'role':'assistant','stopReason':'stop'}}))\n"
        "print(json.dumps({'type':'agent_settled'}),flush=True)\n"
        "sys.stdin.read()\n"
    )

    def fixture_launch(stage, config, command, deadline):
        return task_container.launch_command(
            stage,
            config,
            ["python", "/workspace/fixture.py"],
            deadline,
        )

    monkeypatch.setattr(pi, "launch_command", fixture_launch)
    runner = PiRunner(provider="primary", model="fixture", skill_dir=skill, timeout=30)
    assert asyncio.run(runner.run(run)) == run / "report.html"
    assert (run / ".generation/modes/brief.md").is_file()
    assert not (run / ".generation/modes/standard.md").exists()
    assert (run / ".generation/assets/brief-report-template.html").is_file()
    assert not (run / ".generation/assets/report-template.html").exists()
    assert json.loads((run / "invocation.json").read_text())["isolation"] == "docker"
    failed = run.parent / "failed"
    failed.mkdir()
    (failed / "transcript.md").write_text("fixture")
    monkeypatch.setenv("PI_TASK_IMAGE", "does-not-exist:task-isolation-fixture")
    with pytest.raises(PiError):
        asyncio.run(runner.run(failed))
    assert not (failed / "report.html").exists()
    assert not (failed / ".generation/report.html").exists()


def test_task_output_symlinks_are_not_followed(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    canary = tmp_path / "canary"
    canary.write_text("untouched")
    (stage / "report.html").symlink_to(canary)
    with pytest.raises(ValueError, match="Unsupported task output"):
        task_container.export_outputs(stage, run)
    assert canary.read_text() == "untouched"


@docker_test
def test_real_pi_rpc_loads_only_task_credentials(inputs):
    run, config = inputs
    stage, snapshot = prepare(run, config)
    process = launch(stage, snapshot, [
        "pi", "--mode", "rpc", "--provider", "primary", "--model", "fixture",
        "--offline", "--no-extensions", "--no-skills", "--no-context-files",
    ])
    try:
        process.stdin.write(b'{"id":"catalog","type":"get_available_models"}\n')
        process.stdin.flush()
        response = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            readable, _, _ = select.select([process.stdout], [], [], 0.1)
            if not readable:
                continue
            line = process.stdout.readline()
            assert line, process.stderr.read().decode()
            event = json.loads(line)
            if event.get("id") == "catalog":
                response = event
                break
        assert response and response["success"]
        assert {model["provider"] for model in response["data"]["models"]} == {
            "primary", "alternate",
        }
    finally:
        process.terminate()
        process.communicate(timeout=40)
    assert_removed(process.args)


@docker_test
@pytest.mark.parametrize("outcome", ["timeout", "cancel"])
def test_execution_waits_for_real_container_cleanup(inputs, monkeypatch, outcome):
    run, config = inputs
    (run / "status.json").write_text('{"state":"QUEUED"}')
    original = subprocess.Popen
    worker = f'''
import asyncio
from pathlib import Path
from video_report_agent import pi, task_container
run = Path({str(run)!r})
pi.PI_AGENT_DIR = Path({str(config)!r})
def launch(stage, config, command, deadline):
    code = "import time; from pathlib import Path; "
    code += "Path('/workspace/started').touch(); time.sleep(60)"
    cmd = task_container.launch_command(stage, config, ["python", "-c", code], deadline)
    (run / 'task-name.txt').write_text(cmd[cmd.index('--name')+1])
    return cmd
pi.launch_command = launch
asyncio.run(pi.PiRunner(provider='primary', model='fixture', timeout=30).run(run))
'''
    names = []

    def observe():
        end = time.monotonic() + 10
        while time.monotonic() < end and not (run / ".generation/started").exists():
            time.sleep(0.05)
        if (run / ".generation/started").exists():
            names.append((run / "task-name.txt").read_text())
            if outcome == "cancel":
                (run / "cancel.requested").touch()

    thread = threading.Thread(target=observe, daemon=True)
    thread.start()
    with monkeypatch.context() as patch:
        patch.setattr(execution.subprocess, "Popen",
                      lambda command, **kwargs: original([sys.executable, "-c", worker], **kwargs))
        status = execution.generate(run, timeout=6)
    thread.join(timeout=10)
    assert names, (run / "worker.log").read_text()
    assert status["state"] == ("CANCELLED" if outcome == "cancel" else "FAILED")
    if outcome == "timeout":
        assert status["error_category"] == "EXECUTION_TIMEOUT"
    assert not (run / "task-supervisor.pid").exists()
    assert_removed(["--name", names[0]])


def test_dead_supervisor_without_cleanup_confirmation_keeps_artifacts(tmp_path, monkeypatch):
    (tmp_path / 'task-supervisor.pid').write_text('12345')
    def dead(pid, sig):
        raise ProcessLookupError
    monkeypatch.setattr(task_container.os, 'kill', dead)
    with pytest.raises(RuntimeError, match='cleanup'):
        task_container.wait_for_cleanup(tmp_path)
    assert (tmp_path / 'task-supervisor.pid').exists()


def test_failed_supervisor_does_not_export_task_outputs(inputs, monkeypatch):
    run, _ = inputs
    exported = []
    class FailedProcess:
        pid = 12345
        returncode = 1
    async def start(*args, **kwargs):
        return FailedProcess()
    async def consume(*args, **kwargs):
        return None
    monkeypatch.setattr(pi.asyncio, 'create_subprocess_exec', start)
    monkeypatch.setattr(PiRunner, '_consume', consume)
    monkeypatch.setattr(pi, 'export_outputs', lambda *args: exported.append(args))
    monkeypatch.setattr(task_container, 'require_cleaner', lambda scope: None)
    with pytest.raises(PiError, match='exit cleanly'):
        asyncio.run(PiRunner(provider='primary', model='fixture').run(run))
    assert not exported


@pytest.mark.parametrize('confirmed', [True, False])
def test_pending_launch_requires_cleanup_confirmation(tmp_path, monkeypatch, confirmed):
    (tmp_path / 'task-supervisor.pid').write_text('pending')
    if confirmed:
        (tmp_path / 'task-cleanup.json').write_text('{"cleaned":true}')
        task_container.wait_for_cleanup(tmp_path)
        assert not (tmp_path / 'task-supervisor.pid').exists()
    else:
        ticks = iter([0, 0, 76])
        monkeypatch.setattr(task_container.time, 'monotonic', lambda: next(ticks))
        monkeypatch.setattr(task_container.time, 'sleep', lambda _: None)
        with pytest.raises(RuntimeError, match='cleanup'):
            task_container.wait_for_cleanup(tmp_path)
        assert (tmp_path / 'task-supervisor.pid').exists()


def test_task_output_fifo_is_rejected(tmp_path):
    stage = tmp_path / 'stage'
    stage.mkdir()
    run = tmp_path / 'run'
    run.mkdir()
    os.mkfifo(stage / 'report.html')
    with pytest.raises(ValueError, match='Unsupported task output'):
        task_container.export_outputs(stage, run)
    assert not (run / 'report.html').exists()


def test_create_timeout_cannot_confirm_cleanup(tmp_path, monkeypatch):
    config = tmp_path / 'config'
    config.mkdir()
    def docker(command, **kwargs):
        if command[1] == 'create':
            raise subprocess.TimeoutExpired(command, 30)
        if command[1] == 'rm':
            return subprocess.CompletedProcess(command, 1, stderr=b'No such container')
        return subprocess.CompletedProcess(command, 0, stderr=b'')
    monkeypatch.setattr(task_container.subprocess, 'run', docker)
    monkeypatch.setattr(task_container.signal, 'signal', lambda *args: None)
    assert task_container.supervise(
        os.getppid(), time.monotonic() + 60, 'fixture', config, ['docker', 'create'],
    ) == 1
    assert json.loads((tmp_path / 'task-cleanup.json').read_text()) == {'cleaned': False}


@docker_test
def test_cleaner_recovers_after_whole_controller_sigkill(cleaner):
    prefix = 'cleaner-controller-' + uuid.uuid4().hex[:12]
    volume = prefix + '-runs'
    controller = prefix + '-app'
    task = None

    def docker(*args):
        return subprocess.run(
            ['docker', *args], check=True, capture_output=True, text=True,
        ).stdout.strip()

    code = '''import subprocess,time
from pathlib import Path
from video_report_agent.task_container import launch_command
run=Path('/app/runs/probe'); run.mkdir()
stage=run/'.generation'; stage.mkdir()
config=run/'.generation-config'; config.mkdir()
command=launch_command(stage,config,['python','-c','import time; time.sleep(120)'],
                       time.monotonic()+8)
print(command[command.index('--name')+1],flush=True)
subprocess.Popen(command,start_new_session=True)
time.sleep(120)
'''
    try:
        docker('volume', 'create', volume)
        docker('run', '--rm', '--network', 'none', '--user', '0', '--entrypoint', 'chown',
               '--mount', f'type=volume,src={volume},dst=/app/runs', IMAGE,
               '10001:10001', '/app/runs')
        docker('run', '-d', '--init', '--name', controller, '--group-add', '0',
               '--mount', f'type=volume,src={volume},dst=/app/runs',
               '--mount', 'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
               '-e', f'PI_TASK_RUNS_VOLUME={volume}', '-e', 'PI_TASK_RUNS_ROOT=/app/runs',
               '-e', f'PI_TASK_IMAGE={IMAGE}', '-e', f'PI_TASK_SCOPE={cleaner}',
               '--entrypoint', 'python', IMAGE, '-c', code)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            lines = docker('logs', controller).splitlines()
            if lines:
                task = lines[0]
                result = subprocess.run(
                    ['docker', 'inspect', '--format', '{{.State.Running}}', task],
                    capture_output=True, text=True,
                )
                if result.stdout.strip() == 'true':
                    break
            time.sleep(0.1)
        else:
            pytest.fail('Task did not start: ' + docker('logs', controller))
        expiry = float(docker('inspect', '--format',
                              '{{index .Config.Labels "video-report.deadline"}}', task))
        docker('kill', '--signal', 'KILL', controller)
        while time.time() < expiry + CLEANUP_GRACE_SECONDS:
            container = subprocess.run(['docker', 'inspect', task], capture_output=True)
            network = subprocess.run(['docker', 'network', 'inspect', task], capture_output=True)
            if container.returncode and network.returncode:
                break
            time.sleep(0.2)
        else:
            pytest.fail('Task resources survived the cleanup grace period')
        print(json.dumps({'whole_controller_sigkill': 'recovered',
                          'cleanup_seconds_after_deadline': time.time() - expiry}))
    finally:
        for name in (task, controller):
            if name:
                subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
        if task:
            subprocess.run(['docker', 'network', 'rm', task], capture_output=True)
        subprocess.run(['docker', 'volume', 'rm', volume], capture_output=True)


@docker_test
def test_cleaner_handles_late_create_without_deleting_other_resources(cleaner, tmp_path):
    import http.client
    import socket
    import socketserver
    from http.server import BaseHTTPRequestHandler

    names = ['video-report-task-' + uuid.uuid4().hex for _ in range(4)]
    late, foreign, active, unrelated = names
    expiry = time.time() + 1
    sockpath = '/private/tmp/cleaner-' + uuid.uuid4().hex[:12] + '.sock'
    context = subprocess.run(
        ['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    daemon = os.getenv('DOCKER_HOST', context).removeprefix('unix://')
    assert daemon.startswith('/'), 'Synthetic delay proxy requires a local Unix Docker socket'
    created = threading.Event()
    responses = []

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def request(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            creating = self.path.split('?')[0].endswith('/containers/create')
            if creating:
                time.sleep(3)
            connection = http.client.HTTPConnection('localhost', timeout=10)
            connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.sock.connect(daemon)
            try:
                connection.request(self.command, self.path, body=body,
                                   headers=dict(self.headers))
                response = connection.getresponse()
                data = response.read()
                if creating:
                    responses.append(response.status)
                    created.set()
                self.send_response(response.status)
                for header in ('Api-Version', 'Docker-Experimental', 'Ostype'):
                    if response.getheader(header):
                        self.send_header(header, response.getheader(header))
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(data)
            except BrokenPipeError:
                pass  # The real Docker client has already timed out.
            finally:
                connection.close()

        do_GET = do_HEAD = do_POST = request

    proxy = socketserver.ThreadingUnixStreamServer(sockpath, Proxy)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()

    def command(name, scope, until, *, host=None):
        args = ['docker'] + (['--host', 'unix://' + host] if host else [])
        args += ['create', '--name', name, '--network', 'none']
        if scope:
            for label in ('video-report.task=1', f'video-report.scope={scope}',
                          f'video-report.task-id={name}', f'video-report.deadline={until}'):
                args += ['--label', label]
        return [*args, '--entrypoint', 'sleep', IMAGE, '120']

    try:
        for name, scope, until in ((foreign, cleaner + '-foreign', expiry),
                                   (active, cleaner, time.time() + 120),
                                   (unrelated, None, expiry)):
            subprocess.run(command(name, scope, until), check=True, capture_output=True)
        for name, scope, until in ((foreign, cleaner + '-foreign', expiry),
                                   (active, cleaner, time.time() + 120)):
            subprocess.run([
                'docker', 'network', 'create', '--label', 'video-report.task=1',
                '--label', f'video-report.scope={scope}',
                '--label', f'video-report.task-id={name}',
                '--label', f'video-report.deadline={until}', name,
            ], check=True, capture_output=True)
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess.run(command(late, cleaner, expiry, host=sockpath),
                           timeout=1, capture_output=True)
        assert not created.is_set()
        missing = subprocess.run(['docker', 'inspect', late], capture_output=True)
        assert missing.returncode and b'no such object' in missing.stderr.lower()
        subprocess.run(['docker', 'rm', '-f', late], check=True, capture_output=True)
        assert created.wait(15) and responses == [201]
        end = time.monotonic() + CLEANUP_GRACE_SECONDS
        while time.monotonic() < end:
            if subprocess.run(['docker', 'inspect', late], capture_output=True).returncode:
                break
            time.sleep(0.2)
        else:
            pytest.fail('Late Created container survived cleanup grace period')
        for name in (foreign, active, unrelated):
            assert subprocess.run(['docker', 'inspect', name], capture_output=True).returncode == 0
        for name in (foreign, active):
            assert subprocess.run(
                ['docker', 'network', 'inspect', name], capture_output=True,
            ).returncode == 0
        print(json.dumps({'create_timeout_then_late_creation': 'recovered',
                          'foreign_active_unlabelled': 'preserved'}))
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join()
        Path(sockpath).unlink(missing_ok=True)
        for name in names:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
        for name in (foreign, active):
            subprocess.run(['docker', 'network', 'rm', name], capture_output=True)
