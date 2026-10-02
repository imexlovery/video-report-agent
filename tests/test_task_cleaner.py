"""Expiry and availability failures must fail closed without touching foreign resources."""

import json
import os
import subprocess
import time

import pytest

from video_report_agent import task_cleaner


def test_cleaner_requires_healthy_matching_deployment(monkeypatch):
    calls = []

    def docker(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout='')

    monkeypatch.setattr(task_cleaner, 'docker', docker)
    with pytest.raises(ValueError, match='healthy task cleaner'):
        task_cleaner.require_cleaner('this-deployment')
    assert 'label=video-report.cleaner=this-deployment' in calls[0]
    assert 'health=healthy' in calls[0]


def test_sweep_filters_scope_and_ignores_missing_invalid_or_future_deadlines(monkeypatch):
    calls = []

    def docker(*args):
        calls.append(args)
        output = ('dead 99 video-report-task-fixture\nfuture 101 video-report-task-fixture\n'
                  'invalid nan video-report-task-fixture\nmissing\n'
                  'unmarked 99 different-resource\n') if args[1] == 'ls' else ''
        return subprocess.CompletedProcess(args, 0, stdout=output)

    monkeypatch.setattr(task_cleaner, 'docker', docker)
    task_cleaner.sweep('this-deployment', now=100)
    for call in (calls[0], calls[2]):
        assert 'label=video-report.scope=this-deployment' in call
        assert 'label=video-report.task=1' in call
    assert calls[1] == ('container', 'rm', '--force', 'dead')
    assert calls[3] == ('network', 'rm', 'dead')


def test_daemon_failure_marks_unhealthy_and_next_scan_recovers(tmp_path, monkeypatch):
    health = tmp_path / 'health.json'
    monkeypatch.setattr(task_cleaner, 'HEALTH', health)
    seen = []

    def sweep(scope):
        seen.append(scope)
        if len(seen) == 1:
            raise subprocess.TimeoutExpired('docker', 3)

    def sleep(seconds):
        assert seconds == 2
        if len(seen) == 1:
            assert not task_cleaner.healthy()
        else:
            assert task_cleaner.healthy()
            raise KeyboardInterrupt

    monkeypatch.setattr(task_cleaner, 'sweep', sweep)
    monkeypatch.setattr(task_cleaner.time, 'sleep', sleep)
    with pytest.raises(KeyboardInterrupt):
        task_cleaner.serve('this-deployment')
    assert seen == ['this-deployment', 'this-deployment']
    health.write_text(json.dumps({'pid': os.getpid(), 'at': time.time() - 11}))
    assert not task_cleaner.healthy()
