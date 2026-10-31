import json
import shutil
import subprocess

import pytest

from video_report_agent import visual


def prepared(tmp_path):
    (tmp_path / "transcript.md").write_text("[u1 | 0–2s] 原始字幕")
    units = tmp_path / "canonical-transcript.jsonl"
    units.write_text(
        json.dumps({"unit_id": "u1", "start_ms": 0, "end_ms": 2000, "canonical_text": "原始字幕"})
        + "\n"
    )
    return units


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is required")
def test_sampling_uses_decoded_timestamps_and_keeps_original_text(tmp_path):
    units = prepared(tmp_path)
    video = tmp_path / "video.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=80x60:r=5:d=2",
            "-c:v",
            "libx264",
            str(video),
        ],
        check=True,
    )
    result = visual.sample_frames(video, tmp_path, units, 3)
    assert [f["timestamp_ms"] for f in result["frames"]] == [400, 1000, 1800]
    assert all((tmp_path / f["path"]).is_file() for f in result["frames"])
    assert result["frames"][0]["nearby_subtitles"][0]["unit_id"] == "u1"
    assert (tmp_path / "transcript.md").read_text() == "[u1 | 0–2s] 原始字幕"


@pytest.fixture
def vision_model(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    if not shutil.which("pi") or not shutil.which("node"):
        pytest.skip("Pi and Node are required for the local transport test")
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy",
                "https_proxy", "all_proxy", "no_proxy", "NODE_USE_ENV_PROXY"):
        monkeypatch.delenv(key, raising=False)
    prepared(tmp_path)
    root = tmp_path / "visual"
    root.mkdir()
    (root / "frame.jpg").write_bytes(b"fixture-image")
    visual.write_json(root / "manifest.json", {"frames": [{
        "frame_id": "frame-000001", "timestamp_ms": 400, "path": "visual/frame.jpg",
        "nearby_subtitles": [{"unit_id": "u1", "canonical_text": "原始字幕"}],
    }]})
    response = {"id": "response-1", "model": "report-model", "choices": [{"index": 0,
        "finish_reason": "stop", "delta": {"role": "assistant", "content": json.dumps({
            "frames": [{"frame_id": "frame-000001", "description": "红色图形",
                        "visible_text": [], "candidate_additions": ["红色"],
                        "uncertainties": []}]})}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.headers.get("Authorization"), json.loads(
                self.rfile.read(int(self.headers["Content-Length"])))))
            if "error" in response:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(("data: " + json.dumps(response) + "\n\ndata: [DONE]\n\n").encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    agent_dir = tmp_path / "config"
    agent_dir.mkdir()
    visual.write_json(agent_dir / "auth.json", {
        "fixture": {"type": "api_key", "key": "stored-key"},
    })
    visual.write_json(agent_dir / "models.json", {"providers": {"fixture": {
        "api": "openai-completions", "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
        "models": [{"id": "report-model", "input": ["text", "image"], "reasoning": True}],
    }}})
    try:
        yield agent_dir, requests, response
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("key", [None, "runtime-key"])
def test_description_uses_report_model_images_auth_and_usage(tmp_path, vision_model, key):
    config, requests, response = vision_model
    options = {"provider": "fixture", "model": "report-model", "thinking": "low",
               "agent_dir": config, "api_key": key}
    result = visual.describe_frames(tmp_path, **options)
    assert result["status"] == "completed"
    assert (result["provider"], result["model"], result["thinking"]) == (
        "fixture", "report-model", "low")
    assert len(requests) == 1
    auth, payload = requests[0]
    assert auth == "Bearer " + (key or "stored-key")
    assert payload["model"] == "report-model"
    assert payload["reasoning_effort"] == "low"
    assert payload["messages"][-1]["content"][1]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,")
    root = tmp_path / "visual"
    assert visual.read_json(root / "usage.json")["totalTokens"] == 120
    row = json.loads((root / "observations.jsonl").read_text())
    assert (row["frame_id"], row["timestamp_ms"]) == ("frame-000001", 400)
    assert row["source_kind"] == "model_derived_visual_observation"
    assert (row["provider"], row["model"]) == ("fixture", "report-model")
    assert (tmp_path / "transcript.md").read_text() == "[u1 | 0–2s] 原始字幕"
    assert "runtime-key" not in (root / "request.json").read_text()
    assert "stored-key" not in (root / "request.json").read_text()
    with pytest.raises(FileExistsError):
        visual.describe_frames(tmp_path, **options)
    assert len(requests) == 1
    incomplete = tmp_path / "incomplete"
    (incomplete / "visual").mkdir(parents=True)
    shutil.copyfile(root / "manifest.json", incomplete / "visual/manifest.json")
    (incomplete / "visual/frame.jpg").write_bytes(b"fixture-image")
    response["choices"][0]["finish_reason"] = "length"
    with pytest.raises(visual.VisualError):
        visual.describe_frames(incomplete, **options)
    assert visual.read_json(incomplete / "visual/invocation.json")["status"] == "failed"
    assert not (incomplete / "visual/observations.jsonl").exists()
    assert len(requests) == 2


def test_text_only_report_model_fails_without_sending_request(tmp_path, vision_model):
    config, requests, _ = vision_model
    models = visual.read_json(config / "models.json")
    models["providers"]["fixture"]["models"][0]["input"] = ["text"]
    visual.write_json(config / "models.json", models)
    with pytest.raises(visual.VisualError, match=r'input: \["text", "image"\]'):
        visual.describe_frames(tmp_path, provider="fixture", model="report-model", agent_dir=config)
    assert requests == []
    invocation = visual.read_json(tmp_path / "visual/invocation.json")
    assert invocation["status"] == "failed" and invocation["request_count"] == 0


def test_provider_auth_error_does_not_persist_echoed_key_or_retry(tmp_path, vision_model):
    config, requests, response = vision_model
    response.clear()
    response["error"] = {"message": "Rejected Bearer echoed-fixture-key",
                         "type": "authentication_error", "code": "invalid_api_key"}
    with pytest.raises(visual.VisualError):
        visual.describe_frames(tmp_path, provider="fixture", model="report-model",
                               agent_dir=config, api_key="echoed-fixture-key")
    assert len(requests) == 1
    saved = visual.read_json(tmp_path / "visual/response.json")
    assert saved["stopReason"] == "error" and saved["content"] == []
    assert "errorMessage" not in saved
    for artifact in (tmp_path / "visual").glob("*.json"):
        assert "echoed-fixture-key" not in artifact.read_text()
    assert not (tmp_path / "visual/observations.jsonl").exists()


def test_video_track_fetch_uses_part_metadata_and_keeps_existing_description(tmp_path, monkeypatch):
    from video_report_agent.ingest import validate_bilibili_url

    (tmp_path / "download").mkdir()
    (tmp_path / "download/source.info.json").write_text('{"description":"original"}')

    class Client:
        client = None

        def __init__(self, **options):
            self.client = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def video_info(self, source):
            assert source.page_number == 2
            return {"cid": 22, "title": "P2", "uploader": "UP", "duration_ms": 2000}

        def _json(self, path, *, params):
            assert params["cid"] == 22
            return {
                "dash": {
                    "video": [
                        {"height": 480, "codecs": "avc1", "baseUrl": "low"},
                        {"height": 1080, "codecs": "avc1", "baseUrl": "high"},
                    ]
                }
            }

        def stream(self, method, url, **options):
            assert url == "high"
            return self

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            return iter([b"video-", b"track"])

    monkeypatch.setattr(visual, "BilibiliSubtitles", Client)
    source = validate_bilibili_url("https://www.bilibili.com/video/BV1cZ8x6sEhF/?p=2")
    result = visual.fetch_video(source, tmp_path)
    assert result.name == "visual.mp4"
    assert result.read_bytes() == b"video-track"
    assert visual.read_json(tmp_path / "download/source.info.json")["description"] == "original"
