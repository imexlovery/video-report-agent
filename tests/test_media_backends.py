import json
import wave

import httpx
import pytest

from video_report_agent.asr import AsrError, normalize_asr_segments
from video_report_agent.media_config import QWEN_FILETRANS_MODEL, resolve_media_config
from video_report_agent.paraformer import CloudAsrError, ParaformerBackend
from video_report_agent.redaction import redact


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "audio.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\x00\x00" * 16000)
    return path


@pytest.fixture
def env(monkeypatch):
    for key in ("ASR_MODEL", "ASR_BACKEND", "DASHSCOPE_BASE_URL", "OCR_BACKEND"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-secret")
    return monkeypatch


def test_backend_default_and_explicit_precedence(env):
    assert "whisper" in resolve_media_config()["asr_model"]
    env.setenv("ASR_BACKEND", "paraformer")
    assert resolve_media_config()["asr_model"] == ParaformerBackend.default_model
    assert resolve_media_config("mlx")["asr_backend"] == "mlx"
    env.setenv("ASR_MODEL", "mlx-community/whisper-large-v3-turbo")
    with pytest.raises(ValueError, match="paraformer-v2"):
        resolve_media_config()
    assert resolve_media_config(asr_model="paraformer-v2")["asr_model"] == "paraformer-v2"
    with pytest.raises(ValueError, match="Unsupported file ASR model"):
        resolve_media_config(asr_model="paraformer-v1")
    env.setenv("ASR_MODEL", "paraformer-v2")
    with pytest.raises(ValueError, match="Whisper"):
        resolve_media_config("mlx")


def test_missing_key_and_invalid_ocr_fail_before_run_creation(env, tmp_path):
    from video_report_agent.pipeline import create_run

    env.delenv("DASHSCOPE_API_KEY")
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        create_run(
            tmp_path, "https://www.bilibili.com/video/BV1cZ8x6sEhF/", asr_backend="paraformer"
        )
    assert not list(tmp_path.iterdir())
    with pytest.raises(ValueError, match="OCR_BACKEND"):
        resolve_media_config("mlx", ocr_backend="unknown")


def test_config_snapshot_not_changed_by_environment(env, tmp_path):
    from video_report_agent.pipeline import create_run

    run = create_run(tmp_path, "https://www.bilibili.com/video/BV1cZ8x6sEhF/")
    env.setenv("ASR_BACKEND", "paraformer")
    data = json.loads((run / "input.json").read_text())
    assert data["asr_backend"] == "mlx"
    assert "sk-test-secret" not in (run / "input.json").read_text()


def service(mode="ok", model="paraformer-v2", usage=None):
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/uploads"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "upload_dir": "private",
                        "oss_access_key_id": "credential",
                        "signature": "sig",
                        "policy": "policy",
                        "x_oss_object_acl": "private",
                        "x_oss_forbid_overwrite": "true",
                        "upload_host": "https://store.test/upload",
                    }
                },
            )
        if request.url.host == "store.test":
            assert "authorization" not in request.headers
            return httpx.Response(200)
        if request.url.path.endswith("/transcription"):
            payload = json.loads(request.content)
            assert payload["model"] == model
            if model == QWEN_FILETRANS_MODEL:
                assert payload["parameters"] == {"channel_id": [0], "keep_dialect": True}
            elif model.startswith("fun-asr"):
                assert payload["parameters"] == {"channel_id": [0], "language_hints": ["zh"]}
            else:
                assert payload["parameters"]["timestamp_alignment_enabled"] is True
                assert payload["parameters"]["language_hints"] == ["zh", "en"]
            assert request.headers["X-DashScope-OssResourceResolve"] == "enable"
            if mode == "submit_timeout":
                raise httpx.ReadTimeout("secret URL https://host/?signature=secret")
            if mode == "submit_500":
                return httpx.Response(500, json={"output": {"task_id": "task-1"}})
            return httpx.Response(200, json={"output": {"task_id": "task-1"}})
        if "/tasks/" in request.url.path:
            state = "FAILED" if mode == "failed" else "SUCCEEDED"
            return httpx.Response(
                200,
                json={
                    "usage": usage,
                    "output": {
                        "task_status": state,
                        "results": [
                            {
                                "subtask_status": "SUCCEEDED",
                                "transcription_url": "https://result.test/file?signature=private",
                            }
                        ],
                    }
                },
            )
        assert request.url.host == "result.test"
        assert "authorization" not in request.headers
        rows = (
            []
            if mode == "empty"
            else [{"begin_time": 100, "end_time": 900, "text": "测试", "words": []}]
        )
        return httpx.Response(
            200,
            json={
                "file_url": "oss://private/file",
                "transcripts": [
                    {
                        "channel_id": 0,
                        "content_duration_in_milliseconds": 800,
                        "sentences": rows,
                    }
                ],
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handle)), calls


def test_cloud_normalizes_and_redacts_without_offset(audio, env):
    client, calls = service()
    result = ParaformerBackend(client=client, poll_seconds=0).transcribe(audio)
    assert (result.segments[0].start_ms, result.segments[0].end_ms) == (100, 900)
    assert result.provider == "dashscope"
    assert result.to_dict()["raw_segment_count"] == 1
    assert result.usage["content_duration_ms"] == 800
    stored = json.dumps(result.to_dict())
    assert "signature" not in stored and "oss://" not in stored and "sk-test" not in stored
    assert len([r for r in calls if r.url.path.endswith("/transcription")]) == 1


@pytest.mark.parametrize("output_tokens,expected", [(4, 0.0000276), ("sk-fake", None)])
def test_filetrans_task_response_retains_safe_billing_counters(
    audio, env, output_tokens, expected,
):
    from video_report_agent.usage import cached_call_costs

    client, _ = service(model=QWEN_FILETRANS_MODEL, usage={
        "duration": 1, "input_tokens": 21, "output_tokens": output_tokens, "total_tokens": 25,
        "access_token": "secret",
    })
    with client:
        result = ParaformerBackend(model=QWEN_FILETRANS_MODEL, client=client).transcribe(
            audio, language="auto",
        )
    stored = json.dumps(result.to_dict())
    assert "secret" not in stored and "sk-fake" not in stored
    assert result.usage["service_usage"]["total_tokens"] == 25
    (audio.parent / "asr.json").write_text(stored)
    costs = cached_call_costs(audio.parent, {}, 0.00008)
    assert costs["asr_cny_estimate"] == pytest.approx(expected)


@pytest.mark.parametrize(
    "mode,stage",
    [
        ("submit_timeout", "submit_unknown"),
        ("submit_500", "submit_unknown"),
        ("failed", "poll"),
        ("empty", "result"),
    ],
)
def test_cloud_failures_never_resubmit(audio, env, mode, stage):
    client, calls = service(mode)
    with pytest.raises(CloudAsrError) as error:
        ParaformerBackend(client=client, poll_seconds=0).transcribe(audio)
    assert error.value.stage == stage
    if mode != "submit_timeout":
        assert error.value.task_id == "task-1"
    assert "signature" not in str(error.value)
    assert len([r for r in calls if r.url.path.endswith("/transcription")]) == 1


def test_wait_timeout_keeps_task_id(audio, env):
    client, _ = service()
    with pytest.raises(CloudAsrError) as error:
        ParaformerBackend(client=client, wait_seconds=0).transcribe(audio)
    assert error.value.stage == "poll"
    assert error.value.task_id == "task-1"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_invalid_timestamps(value):
    with pytest.raises(AsrError):
        normalize_asr_segments([{"start": value, "end": 1, "text": "test"}])


def test_diagnostic_redaction():
    data = {
        "nested": [
            {
                "api_key": "secret",
                "policy": "private",
                "text": "保留正文",
                "transcription_url": "https://host/?token=secret",
            }
        ],
        "message": "Bearer secret https://host/?sig=x",
        "task_id": "task-1",
    }
    clean = redact(data)
    assert clean["nested"] == [{"text": "保留正文"}]
    assert "secret" not in json.dumps(clean)
    assert clean["task_id"] == "task-1"


@pytest.mark.parametrize(
    "model",
    [
        "paraformer-v2",
        "fun-asr",
        "fun-asr-2025-08-25",
        "fun-asr-2025-11-07",
        "fun-asr-mtl",
        "fun-asr-mtl-2025-08-25",
        QWEN_FILETRANS_MODEL,
    ],
)
def test_environment_model_switch_preserves_transcript_contract(audio, env, model):
    env.setenv("ASR_BACKEND", "paraformer")
    env.setenv("ASR_MODEL", model)
    config = resolve_media_config()
    client, calls = service(model=model)
    backend = ParaformerBackend(model=config["asr_model"], client=client, poll_seconds=0)
    assert backend.parameters == config["asr_parameters"]
    result = backend.transcribe(audio, language=config["asr_language"])
    assert result.model == model
    if model == QWEN_FILETRANS_MODEL:
        assert result.language == "auto" and result.engine == "qwen-audio"
    assert (result.segments[0].start_ms, result.segments[0].end_ms) == (100, 900)
    upload = next(r for r in calls if r.url.path.endswith("/uploads"))
    assert upload.url.params["model"] == model


@pytest.mark.parametrize("model", ["gummy-realtime-v1", "qwen3-asr-flash", "unknown"])
def test_incompatible_models_fail_before_upload(env, model):
    env.setenv("ASR_BACKEND", "paraformer")
    env.setenv("ASR_MODEL", model)
    with pytest.raises(ValueError, match="Unsupported file ASR model"):
        resolve_media_config()
    with pytest.raises(ValueError, match="Unsupported file ASR model"):
        ParaformerBackend(model=model)
