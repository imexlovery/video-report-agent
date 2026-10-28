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


def test_description_sends_images_and_preserves_provider_usage(tmp_path, monkeypatch):
    prepared(tmp_path)
    root = tmp_path / "visual"
    root.mkdir()
    (root / "frame.jpg").write_bytes(b"fixture-image")
    visual.write_json(
        root / "manifest.json",
        {
            "frames": [
                {
                    "frame_id": "frame-000001",
                    "timestamp_ms": 400,
                    "path": "visual/frame.jpg",
                    "nearby_subtitles": [{"unit_id": "u1", "canonical_text": "原始字幕"}],
                }
            ]
        },
    )
    response = {
        "id": "response-1",
        "model": "deepseek-flash",
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {
                            "frames": [
                                {
                                    "frame_id": "frame-000001",
                                    "description": "红色图形",
                                    "visible_text": [],
                                    "candidate_additions": ["红色"],
                                    "uncertainties": [],
                                }
                            ]
                        }
                    )
                },
            }
        ],
    }
    requests = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps(response).encode()

    def send(request, **options):
        requests.append(json.loads(request.data))
        return Reply()

    monkeypatch.setattr(visual, "urlopen", send)
    result = visual.describe_frames(tmp_path, api_key="SECRET-FIXTURE")
    assert result["status"] == "completed"
    assert len(requests) == 1
    assert requests[0]["messages"][1]["content"][1]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )
    assert visual.read_json(root / "usage.json")["total_tokens"] == 120
    row = json.loads((root / "observations.jsonl").read_text())
    assert (row["frame_id"], row["timestamp_ms"]) == ("frame-000001", 400)
    assert row["source_kind"] == "model_derived_visual_observation"
    assert "SECRET-FIXTURE" not in (root / "request.json").read_text()
    with pytest.raises(FileExistsError):
        visual.describe_frames(tmp_path, api_key="SECRET-FIXTURE")
    assert len(requests) == 1
    incomplete = tmp_path / "incomplete"
    (incomplete / "visual").mkdir(parents=True)
    shutil.copyfile(root / "manifest.json", incomplete / "visual/manifest.json")
    (incomplete / "visual/frame.jpg").write_bytes(b"fixture-image")
    response["choices"][0]["finish_reason"] = "length"
    with pytest.raises(visual.VisualError):
        visual.describe_frames(incomplete, api_key="SECRET-FIXTURE")
    assert visual.read_json(incomplete / "visual/invocation.json")["status"] == "failed"
    assert not (incomplete / "visual/observations.jsonl").exists()
    assert len(requests) == 2


def test_visual_credentials_use_existing_deepseek_auth(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DeepSeek API Key"):
        visual.resolve_api_key(tmp_path)
    visual.write_json(tmp_path / "auth.json", {"deepseek": {"key": "stored-key"}})
    assert visual.resolve_api_key(tmp_path) == "stored-key"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-key")
    assert visual.resolve_api_key(tmp_path) == "environment-key"


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
