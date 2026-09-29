import json
from types import SimpleNamespace

import httpx
import pytest

from video_report_agent.failures import asr_failure
from video_report_agent.ingest import UrlIngestError, download_bilibili_video, validate_bilibili_url
from video_report_agent.paraformer import CloudAsrError, ParaformerBackend


def test_silent_duration_filter_exit(tmp_path, monkeypatch):
    monkeypatch.setattr("video_report_agent.ingest.shutil.which", lambda _: "/bin/yt-dlp")
    with pytest.raises(UrlIngestError) as error:
        download_bilibili_video(
            validate_bilibili_url("BV1aTtb6uE7d"),
            tmp_path,
            runner=lambda *a, **kw: SimpleNamespace(returncode=101, stdout="", stderr=""),
        )
    assert error.value.category == "VIDEO_DURATION_INVALID"
    assert "5 小时" in str(error.value)


@pytest.mark.parametrize(
    "http,code,expected",
    [
        (400, "Arrearage", "ASR_BILLING_UNAVAILABLE"),
        (429, "Throttling", "ASR_RATE_LIMITED"),
        (403, "AccessDenied", "ASR_ACCESS_DENIED"),
        (400, "InvalidParameter", "ASR_SERVICE_ERROR"),
    ],
)
def test_asr_http_diagnostics_are_safe(tmp_path, monkeypatch, http, code, expected):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-secret")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")

    def handle(request):
        return httpx.Response(
            http, json={"code": code, "message": "sk-secret https://signed.test/?secret=value"}
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(CloudAsrError) as error:
            ParaformerBackend(client=client)._run(client, audio, "zh")
    assert error.value.http_status == http
    assert error.value.provider_code == code
    assert asr_failure(error.value)[0] == expected
    assert "secret" not in json.dumps(error.value.to_dict())






def test_pipeline_persists_public_asr_error(tmp_path, monkeypatch):
    from video_report_agent.pipeline import create_run, generate
    run = create_run(tmp_path, 'BV1aTtb6uE7d')
    def fail(*args, **kwargs):
        raise CloudAsrError('submit', 'HTTP request rejected', http_status=400,
                            provider_code='Arrearage')
    monkeypatch.setattr('video_report_agent.pipeline.reuse_download', fail)
    status = generate(run)
    assert status['error_code'] == 'ASR_BILLING_UNAVAILABLE'
    assert status['http_status'] == 400
    assert status['provider_code'] == 'Arrearage'
    assert '欠费' in status['error']
    assert json.loads((run / 'asr-error.json').read_text())['provider_code'] == 'Arrearage'
