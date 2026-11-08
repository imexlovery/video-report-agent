import json
import subprocess
import sys
import time

from video_report_agent import execution
from video_report_agent.pipeline import create_run
from video_report_agent.storage import Store


def test_timeout_stops_worker_and_child(tmp_path, monkeypatch):
    run = create_run(tmp_path, "BV1aTtb6uE7d")
    original = subprocess.Popen
    child_code = "import time; from pathlib import Path; time.sleep(2); Path('survived').touch()"
    worker = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(30)"
    )

    def launch(command, **kwargs):
        return original([sys.executable, "-c", worker], cwd=run, **kwargs)

    monkeypatch.setattr(execution.subprocess, "Popen", launch)
    status = execution.generate(run, timeout=0.5)
    assert status["state"] == "FAILED"
    assert status["error_category"] == "EXECUTION_TIMEOUT"
    time.sleep(2)
    assert not (run / "survived").exists()
    assert json.loads((run / "status.json").read_text())["state"] == "FAILED"


def test_worker_completes_without_provider_call(tmp_path):
    run = create_run(tmp_path, "BV1aTtb6uE7d")
    # Invalid input fails inside the actual worker before network/provider work.
    path = run / "input.json"
    metadata = json.loads(path.read_text())
    metadata["url"] = "https://example.com/invalid"
    Store(tmp_path / "state.sqlite3", initialize=False).metadata(run.name, metadata)
    status = execution.generate(run, timeout=10)
    assert status["state"] == "FAILED"
    assert status["error_category"] != "EXECUTION_TIMEOUT"
    assert "Bilibili" in status["error"]


def test_timeout_restores_interrupted_style_before_capturing_documents(tmp_path, monkeypatch):
    run = create_run(tmp_path, "BV1aTtb6uE7d")
    original = subprocess.Popen
    code = (
        "import json,time; from pathlib import Path; "
        "Path('style').mkdir(); "
        "Path('style/before.html').write_text('<html>before</html>'); "
        "Path('style/result.json').write_text(json.dumps("
        "{'status':'fallback','reason':'interrupted'})); "
        "Path('report.html').write_text('<html>candidate</html>'); "
        "time.sleep(30)"
    )

    def launch(_command, **kwargs):
        return original([sys.executable, "-c", code], cwd=run, **kwargs)

    monkeypatch.setattr(execution.subprocess, "Popen", launch)
    status = execution.generate(run, timeout=0.8)
    assert status["state"] == "FAILED"
    assert (run / "report.html").read_text() == '<html>before</html>'
    documents = Store(tmp_path / "state.sqlite3", initialize=False).documents(run.name)
    assert documents["style/before.html"] == '<html>before</html>'
    assert json.loads(documents["style/result.json"])["reason"] == "interrupted"
    assert "candidate" in documents["style/candidate.html"]
    assert documents["style/delivered.html"] == '<html>before</html>'


def test_cancel_stops_worker_and_child(tmp_path, monkeypatch):
    import threading

    run = create_run(tmp_path, "BV1aTtb6uE7d")
    original = subprocess.Popen
    child_code = "import time; from pathlib import Path; time.sleep(1); Path('survived').touch()"
    worker = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "Path('started').touch(); time.sleep(30)"
    )

    def launch(command, **kwargs):
        return original([sys.executable, "-c", worker], cwd=run, **kwargs)

    def cancel():
        while not (run / "started").exists():
            time.sleep(0.01)
        (run / "cancel.requested").touch()

    monkeypatch.setattr(execution.subprocess, "Popen", launch)
    thread = threading.Thread(target=cancel, daemon=True)
    thread.start()
    from video_report_agent.trace import RunTrace

    RunTrace(run).emit("stage_start", "test-agent", "agent")
    status = execution.generate(run, timeout=5)
    thread.join(timeout=1)
    assert status["state"] == "CANCELLED"
    events = [json.loads(line) for line in (run / "run.trace.jsonl").read_text().splitlines()]
    assert events[-1]["type"] == "run_cancelled"
    assert events[-2]["type"] == "stage_end"
    assert events[-2]["status"] == "cancelled"
    time.sleep(1.1)
    assert not (run / "survived").exists()
    assert json.loads((run / "status.json").read_text())["state"] == "CANCELLED"


def test_worker_registration_failure_still_terminates_launched_process(tmp_path, monkeypatch):
    run = create_run(tmp_path, "BV1aTtb6uE7d")
    original = subprocess.Popen
    processes = []
    code = (
        "import time; from pathlib import Path; time.sleep(1); "
        "Path('survived').touch(); time.sleep(30)"
    )

    def launch(command, **options):
        process = original([sys.executable, "-c", code], cwd=run, **options)
        processes.append(process)
        return process

    monkeypatch.setattr(execution.subprocess, "Popen", launch)
    monkeypatch.setattr(
        Store,
        "worker",
        lambda *args, **options: (_ for _ in ()).throw(RuntimeError("worker registration fault")),
    )
    status = execution.generate(run, timeout=5)
    assert status["state"] == "FAILED" and status["error"] == "worker registration fault"
    assert processes[0].poll() is not None
    time.sleep(1.1)
    assert not (run / "survived").exists()
