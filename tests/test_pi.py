"""Protocol failure tests, not evidence of a real model run."""

import asyncio
import json
import time
from pathlib import Path

import pytest

from video_report_agent.pi import (
    PI_AGENT_DIR,
    PiError,
    PiRunner,
    _provider_failure,
)


class Input:
    def write(self, data):
        assert json.loads(data)["type"] == "prompt"

    async def drain(self):
        pass


class Process:
    def __init__(self, events):
        self.stdin = Input()
        self.stdout = self
        self.lines = []
        for event in events:
            self.lines.append((json.dumps(event) + "\n").encode())

    async def readline(self):
        return self.lines.pop(0) if self.lines else b""


def provider_attempt(provider, model, request_id, *, stop_reason, error=None, age=0, settled=True):
    requested_at = time.time() - age
    message = {
        "role": "assistant", "provider": provider, "model": model,
        "stopReason": stop_reason, "timestamp": int(requested_at * 1000),
        "responseId": f"provider-{request_id}",
        "usage": {"input": 10, "output": 5, "totalTokens": 15,
                  "cost": {"input": 0.001, "output": 0.002, "total": 0.003}},
    }
    if error:
        message["errorMessage"] = error
    events = [
        {"type": "request_start", "request_id": request_id, "timestamp": requested_at,
         "provider": provider, "model": model},
        {"type": "message_end", "message": message},
    ]
    if settled:
        events.append({"type": "agent_settled"})
    return events


class RecoveryInput:
    def __init__(self, process):
        self.process = process

    def write(self, data):
        command = json.loads(data)
        if command["type"] == "prompt":
            self.process.prompts.append(command["message"])
            self.process.prompt_thinking.append(self.process.thinking)
            index = len(self.process.prompts) - 1
            self.process.lines.extend(self.process.steps[index])
            return
        if command["type"] == "set_model":
            self.process.model_commands.append(command)
            self.process.current_model = (command["provider"], command["modelId"])
            self.process.lines.append({
                "type": "response", "id": command["id"], "command": "set_model",
                "success": True,
            })
            return
        if command["type"] == "set_thinking_level":
            self.process.thinking = command["level"]
            self.process.lines.append({
                "type": "response", "id": command["id"], "command": "set_thinking_level",
                "success": self.process.thinking_success,
            })
            return
        raise AssertionError(f"unexpected RPC command: {command['type']}")

    async def drain(self):
        pass


class RecoveryProcess:
    def __init__(self, steps, workspace=None):
        self.stdin = RecoveryInput(self)
        self.stdout = self
        self.lines = []
        self.steps = steps
        self.prompts = []
        self.model_commands = []
        self.current_model = None
        self.thinking = "low"
        self.thinking_success = True
        self.prompt_thinking = []
        self.workspace = workspace

    async def readline(self):
        return (json.dumps(self.lines.pop(0)) + "\n").encode() if self.lines else b""


def test_default_thinking_is_low():
    assert PiRunner().thinking == "low"
    assert PiRunner(thinking="high").thinking == "high"


@pytest.mark.parametrize("thinking_success", [True, False])
def test_recovery_applies_each_models_thinking_before_continuing(tmp_path, thinking_success):
    primary = {"provider": "primary", "model": "first", "thinking": "low"}
    alternate = {"provider": "backup", "model": "second", "thinking": "high"}
    final = {"provider": "fallback", "model": "third", "thinking": "medium"}
    steps = [
        provider_attempt(model["provider"], model["model"], index, stop_reason="error",
                         error="HTTP 400 data_inspection_failed: input data")
        for index, model in enumerate((primary, alternate), 1)
    ]
    steps.append(provider_attempt(final["provider"], final["model"], 3, stop_reason="stop"))
    process = RecoveryProcess(steps)
    process.thinking_success = thinking_success
    recovery = {"selected": primary, "alternate": alternate, "final_fallback": final}
    state = {"initial": primary, "current": primary.copy(), "alternate": alternate,
             "final_fallback": final, "recovery_scheduled": 0, "recovery_count": 0,
             "request_count": 0}
    consume = PiRunner(model_recovery=recovery)._consume(
        process, tmp_path, "generate report", model_recovery=recovery,
        model_state=state, deadline=time.monotonic() + 900,
    )
    if thinking_success:
        assert asyncio.run(consume) == final
        assert process.prompt_thinking == ["low", "high", "medium"]
    else:
        with pytest.raises(PiError) as error:
            asyncio.run(consume)
        assert error.value.failure_category == "model_configuration_failure"
        assert process.prompt_thinking == ["low"]


def test_public_resume_keeps_one_pi_session_and_prior_work(tmp_path):
    primary = ("qwenai", "deepseek-v4.1-flash")
    alternate = ("heyroute ", "grok-4.7")
    first = []
    for index in range(20):
        first.extend(provider_attempt(*primary, index + 1, stop_reason="toolUse", settled=False))
    first.extend(provider_attempt(
        *primary, 21, stop_reason="error", error="HTTP 503 connection terminated", age=150,
    ))
    first[-3]["input_bytes"] = 4000
    first[-2]["message"]["usage"] = {"input": 0, "output": 0, "totalTokens": 0}
    first[-2]["message"]["content"] = [
        {"type": "thinking", "thinking": "分析"}, {"type": "text", "text": "partial"},
        {"type": "toolCall", "arguments": {"path": "report.html"}},
    ]
    second = provider_attempt(*alternate, 22, stop_reason="stop")
    process = RecoveryProcess([first, second], tmp_path)
    (tmp_path / "prior-work.txt").write_text("saved work")
    (tmp_path / "transcript.md").write_text("source transcript")
    (tmp_path / "download-count.txt").write_text("1")
    recovery = {
        "selected": {"provider": primary[0], "model": primary[1], "thinking": "low",
                     "name": "DeepSeek V4.1 Flash"},
        "alternate": {"provider": alternate[0], "model": alternate[1], "thinking": "low",
                       "name": "Grok 4.7"},
        "max_additional_attempts": 2,
    }
    state = {
        "initial": recovery["selected"], "current": recovery["selected"].copy(),
        "alternate": recovery["alternate"], "recovery_scheduled": 0,
        "recovery_count": 0, "request_count": 0, "started_at": time.time(),
    }

    final = asyncio.run(PiRunner(model_recovery=recovery)._consume(
        process, tmp_path, "generate report", model_recovery=recovery,
        model_state=state, deadline=time.monotonic() + 1800,
    ))

    assert final["provider"] == alternate[0] and final["model"] == alternate[1]
    assert len(process.prompts) == 2
    assert "不要重跑下载、ASR" in process.prompts[1]
    assert len(process.model_commands) == 1
    assert process.model_commands[0]["provider"] == alternate[0]
    assert (tmp_path / "prior-work.txt").read_text() == "saved work"
    assert (tmp_path / "download-count.txt").read_text() == "1"
    saved = [json.loads(line) for line in (tmp_path / "pi.events.jsonl").read_text().splitlines()]
    assert sum(event.get("type") == "message_end" for event in saved) == 22
    attempts = [
        json.loads(line)
        for line in (tmp_path / "model-attempts.jsonl").read_text().splitlines()
    ]
    starts = [event for event in attempts if event["kind"] == "request_started"]
    assert len(starts) == 22
    assert starts[-1]["provider"] == alternate[0]
    assert starts[-1]["recovery_number"] == 1
    assert state["recovery_count"] == 1
    assert starts[-2]["input_bytes"] == 4000
    interrupted = next(event for event in attempts
                       if event["kind"] == "request_finished" and event["attempt"] == 21)
    assert interrupted["usage"]["totalTokens"] == 0
    assert interrupted["output_bytes"] == len('分析partial{"path": "report.html"}'.encode())


def test_public_recovery_allows_two_extra_attempts_and_does_not_return_to_primary(tmp_path):
    primary = ("qwenai", "deepseek-v4.1-flash")
    alternate = ("heyroute ", "grok-4.7")
    steps = [
        provider_attempt(*primary, 1, stop_reason="error", error="HTTP 503 unavailable", age=2),
        provider_attempt(*primary, 2, stop_reason="error", error="HTTP 503 unavailable", age=2),
        provider_attempt(*alternate, 3, stop_reason="error", error="HTTP 503 unavailable", age=2),
    ]
    process = RecoveryProcess(steps)
    recovery = {
        "selected": {"provider": primary[0], "model": primary[1], "thinking": "low",
                     "name": "DeepSeek V4.1 Flash"},
        "alternate": {"provider": alternate[0], "model": alternate[1], "thinking": "low",
                       "name": "Grok 4.7"},
        "max_additional_attempts": 2,
    }
    state = {
        "initial": recovery["selected"], "current": recovery["selected"].copy(),
        "alternate": recovery["alternate"], "recovery_scheduled": 0,
        "recovery_count": 0, "request_count": 0, "started_at": time.time(),
    }

    with pytest.raises(PiError) as error:
        asyncio.run(PiRunner(model_recovery=recovery)._consume(
            process, tmp_path, "generate report", model_recovery=recovery,
            model_state=state, deadline=time.monotonic() + 1800,
        ))

    assert error.value.failure_category == "transient_provider_failure"
    assert len(process.prompts) == 3
    assert len(process.model_commands) == 1
    assert process.current_model == alternate
    assert state["recovery_scheduled"] == 2
    assert state["recovery_count"] == 2
    attempts = [
        json.loads(line)
        for line in (tmp_path / "model-attempts.jsonl").read_text().splitlines()
    ]
    assert len([event for event in attempts if event["kind"] == "request_started"]) == 3
    assert len([event for event in attempts if event["kind"] == "recovery_scheduled"]) == 2


@pytest.mark.parametrize("age,retry_count", [(2, 1), (164.93, 0)])
@pytest.mark.parametrize("provider_error", [
    "OpenAI Responses stream ended before a terminal response event", "Connection error.",
])
def test_grok_responses_stream_failure_recovers_to_deepseek(
    tmp_path, age, retry_count, provider_error,
):
    primary = ("heyroute ", "grok-4.7")
    alternate = ("qwenai", "deepseek-v4.1-flash")
    steps = []
    for index in range(retry_count + 1):
        events = provider_attempt(
            *primary, index + 1, stop_reason="error", age=age,
            error=provider_error,
        )
        events[1]["message"].update(
            api="openai-responses",
            content=[{"type": "text", "text": "Transcript read; preparing the report."}],
        )
        steps.append(events)
    steps.append(provider_attempt(*alternate, retry_count + 2, stop_reason="stop"))
    process = RecoveryProcess(steps)
    recovery = {
        "selected": {"provider": primary[0], "model": primary[1], "thinking": "low"},
        "alternate": {"provider": alternate[0], "model": alternate[1], "thinking": "low"},
        "max_additional_attempts": 2,
    }
    state = {
        "initial": recovery["selected"], "current": recovery["selected"].copy(),
        "alternate": recovery["alternate"], "recovery_scheduled": 0,
        "recovery_count": 0, "request_count": 0, "started_at": time.time(),
    }
    final = asyncio.run(PiRunner(model_recovery=recovery)._consume(
        process, tmp_path, "generate report", model_recovery=recovery,
        model_state=state, deadline=time.monotonic() + 1800,
    ))
    assert (final["provider"], final["model"]) == alternate
    assert len(process.prompts) == retry_count + 2
    assert len(process.model_commands) == 1
    assert process.current_model == alternate
    attempts = [json.loads(line) for line in
                (tmp_path / "model-attempts.jsonl").read_text().splitlines()]
    actions = [event["action"] for event in attempts if event["kind"] == "recovery_scheduled"]
    assert actions == ["retry_model"] * retry_count + ["switch_model"]
    failures = [event for event in attempts if event["kind"] == "request_finished"
                and event["stop_reason"] == "error"]
    assert all(event["failure_category"] == "transient_provider_failure" for event in failures)
    starts = [event for event in attempts if event["kind"] == "request_started"]
    assert starts[-1]["model"] == alternate[1]
    assert state["recovery_count"] == retry_count + 1


@pytest.mark.parametrize("primary,alternate,first_error,first_age,second_error,second_age", [
    (("qwenai", "deepseek-v4.1-flash"), ("heyroute ", "grok-4.7"),
     "HTTP 400 data_inspection_failed: input data", 1,
     "OpenAI Responses stream ended before a terminal response event", 169),
    (("heyroute ", "grok-4.7"), ("qwenai", "deepseek-v4.1-flash"),
     "OpenAI Responses stream ended before a terminal response event", 172,
     "Output data may contain inappropriate content.", 42),
])
@pytest.mark.parametrize("final_stop", ["stop", "error"])
def test_fallback_failure_gets_one_bounded_retry(
    tmp_path, primary, alternate, first_error, first_age, second_error, second_age, final_stop,
):
    process = RecoveryProcess([
        provider_attempt(*primary, 1, stop_reason="error", error=first_error, age=first_age),
        provider_attempt(*alternate, 2, stop_reason="error", error=second_error, age=second_age),
        provider_attempt(*alternate, 3, stop_reason=final_stop,
                         error=second_error if final_stop == "error" else None),
    ])
    recovery = {
        "selected": {"provider": primary[0], "model": primary[1], "thinking": "low"},
        "alternate": {"provider": alternate[0], "model": alternate[1], "thinking": "low"},
        "max_additional_attempts": 2,
    }
    state = {
        "initial": recovery["selected"], "current": recovery["selected"].copy(),
        "alternate": recovery["alternate"], "recovery_scheduled": 0,
        "recovery_count": 0, "request_count": 0, "started_at": time.time(),
    }
    consume = PiRunner(model_recovery=recovery)._consume(
        process, tmp_path, "generate report", model_recovery=recovery,
        model_state=state, deadline=time.monotonic() + 600,
    )
    if final_stop == "stop":
        assert asyncio.run(consume) == recovery["alternate"]
    else:
        with pytest.raises(PiError):
            asyncio.run(consume)
    assert len(process.prompts) == 3
    assert len(process.model_commands) == 1
    assert state["recovery_count"] == 2
    events = [json.loads(line) for line in
              (tmp_path / "model-attempts.jsonl").read_text().splitlines()]
    assert [e["action"] for e in events if e["kind"] == "recovery_scheduled"] == [
        "switch_model", "retry_model",
    ]
    assert [(e["provider"], e["model"]) for e in events if e["kind"] == "request_started"] == [
        primary, alternate, alternate,
    ]


@pytest.mark.parametrize("message,expected", [
    ({"provider": "qwenai", "stopReason": "error",
      "errorMessage": "HTTP 400 data_inspection_failed: input data"},
     ("input_content_rejected", "data_inspection_failed", 400)),
    ({"provider": "qwenai", "stopReason": "error",
      "errorMessage": "data_inspection_failed: Output data may contain inappropriate content."},
     ("output_content_rejected", "data_inspection_failed", None)),
    ({"provider": "heyroute ", "stopReason": "error",
      "errorMessage": "Stream ended without finish_reason: terminated"},
     ("transient_provider_failure", None, None)),
    ({"provider": "heyroute ", "stopReason": "error", "errorMessage": "Connection error."},
     ("transient_provider_failure", None, None)),
    ({"provider": "deepseek", "stopReason": "error", "rawStopReason": "content_filter"},
     ("output_content_rejected", None, None)),
    ({"provider": "deepseek", "stopReason": "error",
      "errorMessage": "Output data may contain inappropriate content."},
     ("output_content_rejected", None, None)),
    ({"provider": "heyroute ", "stopReason": "error",
      "errorMessage": '426 {"error":{"code":"internal_error"}}'},
     ("transient_provider_failure", "internal_error", 426)),
    ({"provider": "heyroute ", "stopReason": "error", "errorMessage": "HTTP 426"},
     ("unknown_provider_failure", None, 426)),
    ({"provider": "qwenai", "stopReason": "error",
      "errorMessage": "HTTP 401 invalid_api_key"},
     ("auth_or_quota_failure", "invalid_api_key", 401)),
    ({"provider": "qwenai", "stopReason": "error",
      "errorMessage": "unexpected response"},
     ("unknown_provider_failure", None, None)),
])
def test_public_provider_failure_classification(message, expected):
    assert _provider_failure(message) == expected


@pytest.mark.parametrize("path", [
    "retry-primary", "retry-alternate", "input-rejected", "qwen-primary",
])
@pytest.mark.parametrize("final_stop", ["stop", "error"])
def test_final_fallback_has_reserved_attempt_and_never_routes_back(tmp_path, path, final_stop):
    primary = ("heyroute ", "grok-4.7")
    alternate = ("qwenai", "deepseek-v4.1-flash")
    final = ("deepseek", "deepseek-flash")
    if path == "qwen-primary":
        primary, alternate = alternate, primary
    scenarios = {
        "retry-primary": [
            (primary, "Connection error.", 2), (primary, "Connection error.", 2),
            (alternate, "Output data may contain inappropriate content.", 42),
        ],
        "retry-alternate": [
            (primary, "HTTP 503 terminated", 172),
            (alternate, "Output data may contain inappropriate content.", 42),
            (alternate, "Output data may contain inappropriate content.", 42),
        ],
        "input-rejected": [
            (primary, "HTTP 503 terminated", 172),
            (alternate, "HTTP 400 data_inspection_failed: input data", 2),
        ],
        "qwen-primary": [
            (primary, "HTTP 400 data_inspection_failed: input data", 2),
            (alternate, "HTTP 503 terminated", 169),
            (alternate, "HTTP 503 terminated", 169),
        ],
    }
    sequence = scenarios[path]
    steps = [provider_attempt(*pair, i + 1, stop_reason="error", error=error, age=age)
             for i, (pair, error, age) in enumerate(sequence)]
    steps.append(provider_attempt(*final, len(steps) + 1, stop_reason=final_stop,
                                  error="HTTP 503 unavailable" if final_stop == "error" else None))
    process = RecoveryProcess(steps)
    def selection(pair):
        return {"provider": pair[0], "model": pair[1], "thinking": "low"}

    recovery = {"selected": selection(primary), "alternate": selection(alternate),
                "final_fallback": selection(final), "max_additional_attempts": 3}
    state = {"initial": recovery["selected"], "current": recovery["selected"].copy(),
             "alternate": recovery["alternate"], "final_fallback": recovery["final_fallback"],
             "recovery_scheduled": 0, "recovery_count": 0, "request_count": 0}
    consume = PiRunner(model_recovery=recovery)._consume(
        process, tmp_path, "generate report", model_recovery=recovery,
        model_state=state, deadline=time.monotonic() + 900,
    )
    if final_stop == "stop":
        assert asyncio.run(consume) == selection(final)
    else:
        with pytest.raises(PiError):
            asyncio.run(consume)
        assert state["recovery_stop_reason"] == "final_fallback_failed"
    assert len(process.prompts) == len(sequence) + 1
    assert [(c["provider"], c["modelId"]) for c in process.model_commands] == [alternate, final]
    assert state["recovery_count"] == len(sequence) <= 3


@pytest.mark.parametrize("drawing,enabled", [
    (None, True), ("drawing", True), ("drawing", False), ("primary", True),
])
def test_docker_recovery_snapshots_final_provider(tmp_path, monkeypatch, drawing, enabled):
    from video_report_agent import pi

    (tmp_path / "transcript.md").write_text("synthetic transcript")
    (tmp_path / "input.json").write_text(json.dumps({
        "diagrams": enabled,
        **({"diagram_model_selection": {"provider": drawing, "model": "fixture"}}
           if drawing else {}),
    }))
    monkeypatch.setattr(pi, "PI_AGENT_DIR", tmp_path / "shared-pi")
    selections = []

    def snapshot(directory, agent_dir, models):
        selections.extend(models)
        directory.mkdir()

    async def no_provider_call(self, workspace, **kwargs):
        return workspace / "report.html"

    monkeypatch.setattr(pi, "snapshot_models", snapshot)
    monkeypatch.setattr(PiRunner, "_run", no_provider_call)
    recovery = {"alternate": {"provider": "alternate", "model": "fixture"},
                "final_fallback": {"provider": "final", "model": "fixture"}}
    asyncio.run(PiRunner(provider="primary", model="fixture", api_key="FAKE",
                        model_recovery=recovery, isolation="docker").run(tmp_path))
    assert [item["provider"] for item in selections] == (
        ["primary"] + ([drawing] if drawing and enabled else []) + ["alternate", "final"]
    )
    if drawing and enabled:
        assert selections[1]["api_key"] == ("FAKE" if drawing == "primary" else None)


@pytest.mark.parametrize("category,count,seconds,expected_action", [
    ("input_content_rejected", 1, 2, "switch_model"),
    ("output_content_rejected", 1, 2, "retry_model"),
    ("output_content_rejected", 2, 2, "switch_model"),
    ("transient_provider_failure", 1, 2, "retry_model"),
    ("transient_provider_failure", 1, 150, "switch_model"),
    ("unknown_provider_failure", 1, 2, None),
])
def test_public_recovery_action_matrix(category, count, seconds, expected_action):
    initial = {"provider": "qwenai", "model": "deepseek-v4.1-flash"}
    alternate = {"provider": "heyroute ", "model": "grok-4.7"}
    selected = PiRunner._select_recovery(category, initial, initial, alternate, count, seconds)
    assert (selected[0] if selected else None) == expected_action


def test_explicit_input_rejection_can_switch_non_qwen_initial_model():
    initial = {"provider": "heyroute ", "model": "grok-4.7"}
    alternate = {"provider": "qwenai", "model": "deepseek-v4.1-flash"}
    assert PiRunner._select_recovery(
        "input_content_rejected", initial, initial, alternate, 1, 2,
    ) == ("switch_model", alternate)


@pytest.mark.parametrize("cancelled,remaining,expected", [
    (False, 60, "recovery_time_budget_exhausted"),
    (True, 900, "cancelled"),
])
def test_public_recovery_stops_before_request_when_time_or_cancel_blocks(
    tmp_path, cancelled, remaining, expected,
):
    primary = ("qwenai", "deepseek-v4.1-flash")
    alternate = ("heyroute ", "grok-4.7")
    process = RecoveryProcess([provider_attempt(
        *primary, 1, stop_reason="error", error="HTTP 503 unavailable", age=2,
    )])
    if cancelled:
        (tmp_path / "cancel.requested").touch()
    recovery = {
        "selected": {"provider": primary[0], "model": primary[1], "thinking": "low"},
        "alternate": {"provider": alternate[0], "model": alternate[1], "thinking": "low"},
        "max_additional_attempts": 2, "min_remaining_seconds": 120,
    }
    state = {
        "initial": recovery["selected"], "current": recovery["selected"].copy(),
        "alternate": recovery["alternate"], "recovery_scheduled": 0,
        "recovery_count": 0, "request_count": 0, "started_at": time.time(),
    }

    with pytest.raises(PiError) as error:
        asyncio.run(PiRunner(model_recovery=recovery)._consume(
            process, tmp_path, "generate report", model_recovery=recovery,
            model_state=state, deadline=time.monotonic() + remaining,
        ))

    assert error.value.failure_category == expected
    assert len(process.prompts) == 1
    assert state["recovery_scheduled"] == 0


def test_public_recovery_settings_are_run_scoped(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    (config / "auth.json").write_text('{"qwenai":{"type":"api_key","key":"fixture"}}')
    (config / "models.json").write_text('{"providers":{}}')
    monkeypatch.setattr("video_report_agent.pi.PI_AGENT_DIR", config)

    run_config = PiRunner._prepare_recovery_config(tmp_path / "run")

    settings = json.loads((run_config / "settings.json").read_text())
    assert settings["retry"]["enabled"] is False
    assert settings["retry"]["maxRetries"] == 0
    assert settings["retry"]["provider"] == {"maxRetries": 0, "timeoutMs": 600000}
    assert (run_config / "auth.json").resolve() == (config / "auth.json").resolve()
    assert (run_config / "models.json").resolve() == (config / "models.json").resolve()
    assert json.loads((config / "auth.json").read_text())["qwenai"]["type"] == "api_key"


def test_review_is_opt_in_and_evaluation_can_override(monkeypatch):
    monkeypatch.delenv("REPORT_REVIEW", raising=False)
    assert PiRunner().review is False
    monkeypatch.setenv("REPORT_REVIEW", "1")
    assert PiRunner().review is True
    assert PiRunner(review=False).review is False


def test_agent_end_is_not_success(tmp_path):
    with pytest.raises(PiError, match="before agent_settled"):
        asyncio.run(PiRunner()._consume(Process([{"type": "agent_end"}]), tmp_path, "task"))


def test_failed_final_message_is_not_success(tmp_path):
    events = [
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "quota exceeded",
            },
        },
        {"type": "agent_settled"},
    ]
    with pytest.raises(PiError, match="quota exceeded") as error:
        asyncio.run(PiRunner()._consume(Process(events), tmp_path, "task"))
    assert error.value.category == "EXTERNAL_MODEL_FAILURE"


def test_settled_after_retry_uses_final_message(tmp_path):
    events = [
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "error"}},
        {"type": "agent_end", "willRetry": True},
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}},
        {"type": "agent_settled"},
    ]
    asyncio.run(PiRunner()._consume(Process(events), tmp_path, "task"))
    recorded = [
        json.loads(line) for line in (tmp_path / "pi.events.jsonl").read_text().splitlines()
    ]
    assert len(recorded) == 4
    assert all(isinstance(event.pop("_trace_received_at"), float) for event in recorded)
    assert recorded == events


def test_rejected_prompt(tmp_path):
    with pytest.raises(PiError, match="unknown model"):
        asyncio.run(
            PiRunner()._consume(
                Process([{"type": "response", "success": False, "error": "unknown model"}]),
                tmp_path,
                "task",
            )
        )


def test_missing_output_and_run_cwd(tmp_path, monkeypatch):
    executable = tmp_path / "fake-pi"
    executable.write_text("""#!/usr/bin/env python3
import json,sys,os
json.loads(sys.stdin.readline())
assert os.path.isfile("transcript.md")
assert os.path.isfile("SKILL.md")
assert os.path.isfile("assets/report-template.html")
print(json.dumps({"type":"message_end","message":{"role":"assistant","stopReason":"stop"}}))
print(json.dumps({"type":"agent_settled"}),flush=True)
""")
    executable.chmod(0o755)
    monkeypatch.setattr("video_report_agent.pi.shutil.which", lambda _: str(executable))
    workspace = tmp_path / "run"
    workspace.mkdir()
    (workspace / "transcript.md").write_text("sample")
    with pytest.raises(PiError, match="without report.html"):
        asyncio.run(PiRunner(review=True, isolation="local").run(workspace))
    invocation = json.loads((workspace / "invocation.json").read_text())
    assert Path(invocation["cwd"]) == workspace
    assert "read,write,edit,bash,inspect_report,generate_diagram" in invocation["command"]
    assert "--extension" in invocation["command"]
    assert "{{VIDEO_DESCRIPTION}}" in invocation["prompt"]
    assert "不要读取、改写或自行生成视频简介" in invocation["prompt"]
    assert "蓝色或黄色背景" not in invocation["prompt"]
    assert "布局前读取 modes/standard.md 的选型表" in invocation["prompt"]


def test_project_provider_model_and_api_key_are_passed_but_redacted(tmp_path, monkeypatch):
    executable = tmp_path / "fake-pi"
    executable.write_text(
        """#!/usr/bin/env python3
import json,sys,os
open("received-argv.json", "w").write(json.dumps(sys.argv[1:]))
open("received-agent-dir.txt", "w").write(os.environ["PI_CODING_AGENT_DIR"])
json.loads(sys.stdin.readline())
open("report.html", "w").write("<html><body>ok</body></html>")
print(json.dumps({"type":"message_end","message":{"role":"assistant","stopReason":"stop"}}))
print(json.dumps({"type":"agent_settled"}),flush=True)
"""
    )
    executable.chmod(0o755)
    monkeypatch.setattr("video_report_agent.pi.shutil.which", lambda _: str(executable))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "machine-pi"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PI_PROVIDER", "openai")
    monkeypatch.setenv("PI_MODEL", "gpt-test")
    monkeypatch.setenv("PI_API_KEY", "project-secret")
    workspace = tmp_path / "run"
    workspace.mkdir()
    (workspace / "transcript.md").write_text("sample")

    asyncio.run(PiRunner(thinking="high", isolation="local").run(workspace))

    assert Path((workspace / "received-agent-dir.txt").read_text()) == PI_AGENT_DIR
    assert PI_AGENT_DIR.is_absolute()
    received = json.loads((workspace / "received-argv.json").read_text())
    assert received[received.index("--thinking") + 1] == "high"
    assert received[received.index("--provider") + 1] == "openai"
    assert received[received.index("--model") + 1] == "gpt-test"
    assert received[received.index("--api-key") + 1] == "project-secret"
    invocation = json.loads((workspace / "invocation.json").read_text())
    assert "project-secret" not in json.dumps(invocation)
    assert invocation["command"][invocation["command"].index("--api-key") + 1] == "[redacted]"


def test_empty_project_api_key_allows_project_login(monkeypatch):
    monkeypatch.setenv("PI_PROVIDER", "deepseek")
    monkeypatch.setenv("PI_MODEL", "deepseek-chat")
    monkeypatch.setenv("PI_API_KEY", "   ")

    runner = PiRunner()

    assert runner.provider == "deepseek"
    assert runner.model == "deepseek-chat"
    assert runner.api_key is None


def test_env_key_is_not_sent_to_another_provider(monkeypatch):
    monkeypatch.setenv("PI_PROVIDER", "deepseek")
    monkeypatch.setenv("PI_API_KEY", "deepseek-secret")
    assert PiRunner(provider="custom-other", model="test").api_key is None


def test_selected_provider_saved_key_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setattr("video_report_agent.pi.PI_AGENT_DIR", tmp_path)
    (tmp_path / "auth.json").write_text(json.dumps({
        "deepseek": {"type": "api_key", "key": "new-key"},
    }))
    monkeypatch.setenv("PI_PROVIDER", "deepseek")
    monkeypatch.setenv("PI_API_KEY", "old-key")
    assert PiRunner(provider="deepseek", model="test").api_key is None


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("report_mode", ["standard", "brief"])
def test_custom_skill_directory(tmp_path, monkeypatch, explicit, report_mode):
    custom = tmp_path / "custom"
    (custom / "assets").mkdir(parents=True)
    (custom / "SKILL.md").write_text("custom skill marker")
    (custom / "assets/report-template.html").write_text("custom template marker")
    executable = tmp_path / "fake-pi"
    executable.write_text("""#!/usr/bin/env python3
import json, sys
assert open("SKILL.md").read() == "custom skill marker"
assert open("assets/report-template.html").read() == "custom template marker"
json.loads(sys.stdin.readline())
open("report.html", "w").write("<html><body>fixture</body></html>")
print(json.dumps({"type":"message_end","message":{"role":"assistant","stopReason":"stop"}}))
print(json.dumps({"type":"agent_settled"}), flush=True)
""")
    executable.chmod(0o755)
    monkeypatch.setattr("video_report_agent.pi.shutil.which", lambda _: str(executable))
    monkeypatch.setattr("video_report_agent.pi.PI_AGENT_DIR", tmp_path / "config")
    monkeypatch.setenv("VIDEO_REPORT_SKILL_DIR", str(tmp_path / "wrong" if explicit else custom))
    workspace = tmp_path / "run"
    workspace.mkdir()
    (workspace / "transcript.md").write_text("fixture")
    (workspace / "input.json").write_text(json.dumps({"report_mode": report_mode}))
    runner = PiRunner(skill_dir=custom if explicit else None, isolation="local")
    assert asyncio.run(runner.run(workspace)) == workspace / "report.html"
    assert (tmp_path / "config/models.json").is_file()
