import json
from types import SimpleNamespace

import httpx
import pytest

from video_report_agent import pipeline
from video_report_agent.asr import AsrError
from video_report_agent.audio import AudioExtractionError
from video_report_agent.bilibili_subtitles import (
    BilibiliSubtitles,
    parse_web_tracks,
    validate_cues,
)
from video_report_agent.compare_subtitles import compare_windows
from video_report_agent.ingest import validate_bilibili_url
from video_report_agent.reuse import cache_key, validated_transcript
from video_report_agent.storage import Store
from video_report_agent.transcript import build_transcript
from video_report_agent.usage import call_costs

URL = "https://www.bilibili.com/video/BV1qxn16yEvP/"


def snapshot():
    return {
        "video_id": "bilibili-BV1qxn16yEvP-p1",
        "bvid": "BV1qxn16yEvP",
        "cid": 12,
        "title": "合成视频",
        "uploader": "示例",
        "attribution": "Bilibili",
        "duration_ms": 10000,
        "sources": {
            "bilibili_ai": {
                "status": "READY",
                "count": 2,
                "track_id": "123",
                "language": "ai-zh",
                "body": [
                    {"from": 0, "to": 4, "content": "今天我们讨论如何进行训练"},
                    {"from": 5, "to": 9, "content": "这里保存原始字幕时间戳"},
                ],
            }
        },
    }


def forbidden(*args, **kwargs):
    pytest.fail("Unselected media, ASR or report stage was called")


def test_ai_only_pipeline_and_reuse_need_no_asr_credentials_media_or_report(tmp_path, monkeypatch):
    monkeypatch.setenv("ASR_BACKEND", "paraformer")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(pipeline, "fetch_subtitles", lambda *_: snapshot())
    for name in (
        "download_bilibili_video",
        "extract_audio",
        "transcribe_audio",
        "PiRunner",
        "render_report_image",
    ):
        monkeypatch.setattr(pipeline, name, forbidden)
    runs = []
    for _ in range(2):
        run = pipeline.create_run(
            tmp_path, URL, transcript_sources=["bilibili_ai"], transcript_only=True
        )
        runs.append(run)
        status = pipeline.generate(run)
        assert status["state"] == "TRANSCRIPT_READY"
        assert status["transcript_primary_source"] == "bilibili_ai"
        assert status["asr_performed"] is False
        assert not (run / "asr.json").exists()
        assert not (run / "download").exists()
        assert not (run / "report.html").exists()
        assert call_costs(run, status, 0.0001)["asr_cny_estimate"] == 0
        units = [
            json.loads(line)
            for line in (run / "canonical-transcript.jsonl").read_text().splitlines()
        ]
        assert units[0]["asr_text"] is None
        assert units[1]["start_ms"] == 5000
        assert units[0]["provenance"]["anchor_source"] == "bilibili_ai"
        assert validated_transcript(run, json.loads((run / "input.json").read_text()))
    assert status["transcript_reused_from"] == runs[0].name
    store = Store(tmp_path / "state.sqlite3", initialize=False)
    assert "bilibili-subtitles.json" in store.documents(runs[0].name)


def test_no_caption_does_not_secretly_fallback_to_asr(tmp_path, monkeypatch):
    data = snapshot()
    data["sources"]["bilibili_ai"] = {"status": "LOGIN_REQUIRED", "count": 0}
    monkeypatch.setattr(pipeline, "fetch_subtitles", lambda *_: data)
    monkeypatch.setattr(pipeline, "download_bilibili_video", forbidden)
    monkeypatch.setattr(pipeline, "transcribe_audio", forbidden)
    run = pipeline.create_run(
        tmp_path, URL, transcript_sources=["bilibili_ai"], transcript_only=True
    )
    status = pipeline.generate(run)
    assert status["state"] == "FAILED"
    assert status["error_code"] == "TRANSCRIPT_SOURCE_UNAVAILABLE"
    assert "登录态" in status["error"]
    assert status["transcript_source_status"]["bilibili_ai"]["status"] == "LOGIN_REQUIRED"


@pytest.mark.parametrize("isolated", [False, True])
def test_auto_subtitles_preserve_description_for_term_evidence(tmp_path, monkeypatch, isolated):
    from video_report_agent.task_container import stage_inputs
    from video_report_agent.term_correction import SOURCE_CONTEXT, prepare_term_correction

    data = snapshot()
    data["description"] = "简介中的训练术语与来源"
    monkeypatch.setattr(pipeline, "fetch_subtitles", lambda *_, **kwargs: data)
    monkeypatch.setattr(pipeline, "download_bilibili_video", forbidden)
    monkeypatch.setattr(pipeline, "transcribe_audio", forbidden)
    runs = []
    for _ in range(2):
        run = pipeline.create_run(
            tmp_path, URL, transcript_mode="auto", term_correction=True, transcript_only=True
        )
        runs.append(run)
        status = pipeline.generate(run)
        assert status["state"] == "TRANSCRIPT_READY"
        assert not (run / "download").exists()
        metadata = Store(tmp_path / "state.sqlite3", initialize=False).run(run.name)["input"]
        assert metadata["description"] == data["description"]
        workspace = stage_inputs(run) if isolated else run
        # Pi initializes both local and isolated context with the saved host metadata.
        prepare_term_correction(workspace, metadata)
        context = json.loads((workspace / SOURCE_CONTEXT).read_text())
        assert context["description"] == data["description"]
    assert status["transcript_reused_from"] == runs[0].name


@pytest.mark.parametrize("sources", [[], ["ocr"], ["bilibili_ai"] * 2, ["bad"], "asr", [{}]])
def test_selection_rejected_before_run_created(tmp_path, sources):
    with pytest.raises(ValueError):
        pipeline.create_run(tmp_path, URL, transcript_sources=sources)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("order", [["bilibili_ai", "asr"], ["asr", "bilibili_ai"]])
def test_primary_source_controls_timeline_and_preserves_both_sources(tmp_path, order):
    (tmp_path / "bilibili-subtitles.json").write_text(json.dumps(snapshot()))
    (tmp_path / "asr.json").write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "ordinal": 0,
                        "start_ms": 100,
                        "end_ms": 9500,
                        "text": "今天我们讨论如何进行训练",
                    },
                ]
            }
        )
    )
    options = SimpleNamespace(transcript_sources=order, subtitle_file=None)
    build, _, _ = build_transcript(
        options,
        artifact_root=tmp_path,
        source_path=None,
        asr_path=tmp_path / "asr.json",
        source_duration_ms=10000,
        manifest={"video_id": snapshot()["video_id"]},
    )
    assert build.manifest.primary_source == order[0]
    assert len(build.canonical_units) == (2 if order[0] == "bilibili_ai" else 1)
    assert {event.source for event in build.source_events} == {"asr", "bilibili_ai"}
    assert build.canonical_units[0].provenance["anchor_source"] == order[0]


def test_only_explicitly_selected_sources_can_be_primary(tmp_path):
    data = snapshot()
    data["sources"]["subtitle_track"] = {"status": "ABSENT", "count": 0}
    (tmp_path / "bilibili-subtitles.json").write_text(json.dumps(data))
    build, _, _ = build_transcript(
        SimpleNamespace(transcript_sources=["subtitle_track", "bilibili_ai"], subtitle_file=None),
        artifact_root=tmp_path,
        source_path=None,
        asr_path=tmp_path / "absent.json",
        source_duration_ms=10000,
        manifest={"video_id": data["video_id"]},
    )
    assert build.manifest.primary_source == "bilibili_ai"
    assert build.manifest.status == "DEGRADED"
    assert build.manifest.source_status["asr"]["status"] == "OFF"


def test_source_order_and_caption_content_isolate_cache():
    metadata = {
        "video_id": snapshot()["video_id"],
        "transcript_mode": "selected",
        "transcript_sources": ["bilibili_ai", "asr"],
    }
    first = cache_key("transcript", metadata, {"bilibili-subtitles.json": "first"})
    assert first != cache_key("transcript", metadata, {"bilibili-subtitles.json": "second"})
    metadata["transcript_sources"].reverse()
    assert first != cache_key("transcript", metadata, {"bilibili-subtitles.json": "first"})


def _bytes(number, value):
    # All synthetic messages are shorter than 128 bytes.
    return bytes([number * 8 + 2, len(value)]) + value


def test_protobuf_tracks_retain_language_and_reject_truncation():
    track = (
        _bytes(3, b"ai-zh") + _bytes(4, b"Chinese") + _bytes(5, b"https://aisubtitle.hdslb.com/a")
    )
    payload = _bytes(1, _bytes(3, track))
    assert parse_web_tracks(payload)[0]["lan"] == "ai-zh"
    assert parse_web_tracks(b"\x0a\x00") == []
    with pytest.raises(ValueError):
        parse_web_tracks(payload[:-1])


def test_signed_metadata_part_binding_and_credential_scope(monkeypatch):
    monkeypatch.setenv("BILIBILI_SESSDATA", "test-secret")
    monkeypatch.setattr("video_report_agent.bilibili_subtitles.time.time", lambda: 1700000000)
    requests = []

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/view") and "web-interface" in path:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "bvid": "BV1qxn16yEvP",
                        "aid": 1,
                        "title": "示例",
                        "desc": "示例简介原文",
                        "owner": {"name": "示例"},
                        "pages": [{"cid": 11, "duration": 10}, {"cid": 12, "duration": 10}],
                    },
                },
            )
        if path.endswith("/nav"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "isLogin": True,
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/7cd084941338484aae1ad9425b84077c.png",
                            "sub_url": "https://i0.hdslb.com/4932caff0ff746eab6f01bf08b70ac45.png",
                        },
                    }
                },
            )
        if path.endswith("/wbi/v2"):
            # Fixed vector: the mixed key is ea1db124af3c7062474693fa704f4ff8.
            # Keep the expected digest literal, independent of the production signer.
            assert dict(request.url.params) == {
                "aid": "1", "bvid": "BV1qxn16yEvP", "cid": "12", "wts": "1700000000",
                "w_rid": "e7f982ede807146fe64157a42704f644",
            }
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "aid": 1,
                        "cid": 12,
                        "subtitle": {
                            "subtitles": [
                                {
                                    "id": 1,
                                    "lan": "ai-zh",
                                    "subtitle_url": "https://aisubtitle.hdslb.com/a?auth_key=test",
                                }
                            ]
                        },
                    },
                },
            )
        if "subtitle/web/view" in path:
            return httpx.Response(200, content=b"\x0a\x00")
        assert request.url.host == "aisubtitle.hdslb.com"
        assert "cookie" not in request.headers
        return httpx.Response(200, json={"body": snapshot()["sources"]["bilibili_ai"]["body"]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = BilibiliSubtitles(client=http).fetch(validate_bilibili_url(URL + "?p=2"))
    assert result["cid"] == 12
    assert result["description"] == "示例简介原文"
    assert result["sources"]["bilibili_ai"]["status"] == "READY"
    assert "auth_key" not in json.dumps(result)
    assert "test-secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "body",
    [
        [],
        [{"from": 1, "to": 0, "content": "字幕"}],
        [{"from": 0, "to": 100, "content": "字幕"}],
        [{"from": 0, "to": 1, "content": ""}],
    ],
)
def test_malformed_captions_are_not_complete_transcripts(body):
    with pytest.raises(ValueError):
        validate_cues(body, 10000)


def test_comparison_keeps_text_and_time_without_claiming_accuracy():
    result = compare_windows(
        [{"from": 1, "to": 2, "content": "机器学习"}],
        {"segments": [{"start_ms": 1000, "end_ms": 2000, "text": "机器学期"}]},
    )
    assert result[0]["start_ms"] == 0
    assert result[0]["ai"] == "机器学习"
    assert result[0]["asr"] == "机器学期"
    assert "accuracy" not in result[0]


def test_empty_cue_is_skipped_but_original_source_ordinal_is_kept():
    body = validate_cues(
        [
            {"from": 0, "to": 1, "content": ""},
            {"from": 1, "to": 2, "content": "原始第二条字幕"},
        ],
        10000,
    )
    assert len(body) == 1
    assert body[0]["cue_index"] == 1


@pytest.mark.parametrize("failed_stage", ["download", "audio", "asr"])
@pytest.mark.parametrize("order", [["bilibili_ai", "asr"], ["asr", "bilibili_ai"]])
def test_failed_selected_asr_does_not_discard_available_captions(
    tmp_path,
    monkeypatch,
    failed_stage,
    order,
):
    from video_report_agent.ingest import UrlIngestError

    monkeypatch.setattr(pipeline, "fetch_subtitles", lambda *_: snapshot())

    def download(source, run, **kwargs):
        if failed_stage == "download":
            raise UrlIngestError("DOWNLOAD_ERROR", "fixture download unavailable")
        folder = run / "download"
        folder.mkdir()
        media = folder / "source.m4a"
        media.write_bytes(b"fixture")
        (folder / "source.info.json").write_text(
            json.dumps(
                {
                    "title": "合成视频",
                    "uploader": "示例",
                    "duration": 10,
                }
            )
        )
        return SimpleNamespace(
            media_path=media, title="合成视频", uploader="示例", attribution="Bilibili"
        )

    def extract(media, output):
        if failed_stage == "audio":
            raise AudioExtractionError("fixture audio unavailable")
        output.write_bytes(b"fixture")
        return SimpleNamespace(path=output, probe=SimpleNamespace(duration_ms=10000))

    def transcribe(*a, **kw):
        raise AsrError("fixture ASR unavailable")

    monkeypatch.setattr(pipeline, "download_bilibili_video", download)
    monkeypatch.setattr(pipeline, "extract_audio", extract)
    monkeypatch.setattr(pipeline, "transcribe_audio", transcribe)
    monkeypatch.setattr(pipeline, "PiRunner", forbidden)
    run = pipeline.create_run(tmp_path, URL, transcript_sources=order, transcript_only=True)
    status = pipeline.generate(run)
    assert status["state"] == "TRANSCRIPT_READY"
    assert status["transcript_primary_source"] == "bilibili_ai"
    assert status["transcript_source_status"]["asr"]["status"] == "FAILED"
    assert status["asr_performed"] is (failed_stage == "asr")
    store = Store(tmp_path / "state.sqlite3", initialize=False)
    with store.read() as db:
        assert (
            db.execute("SELECT state FROM cache_entries WHERE kind='transcript'").fetchone()[0]
            == "FAILED"
        )


def test_metadata_for_import_does_not_read_cookie_or_query_tracks(monkeypatch):
    monkeypatch.setattr("video_report_agent.bilibili_subtitles.read_sessdata", forbidden)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "bvid": "BV1qxn16yEvP",
                        "aid": 1,
                        "title": "示例",
                        "pages": [{"cid": 12, "duration": 10}],
                    },
                },
            )
        )
    ) as http:
        client = BilibiliSubtitles(client=http, authenticate=False)
        monkeypatch.setattr(client, "tracks", forbidden)
        result = client.fetch(validate_bilibili_url(URL), selected=[])
    assert result["duration_ms"] == 10000
    assert result["sources"] == {}


def test_waiter_can_use_own_captions_when_shared_asr_fails(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    waiting = threading.Event()
    original_claim = Store.claim_cache

    def claim(self, kind, *args):
        result = original_claim(self, kind, *args)
        if kind == "transcript" and result[0] == "WAIT":
            waiting.set()
        return result

    monkeypatch.setattr(Store, "claim_cache", claim)
    monkeypatch.setattr(pipeline, "fetch_subtitles", lambda *_: snapshot())

    def download(source, run, **kwargs):
        folder = run / "download"
        folder.mkdir()
        media = folder / "source.m4a"
        media.write_bytes(b"fixture")
        return SimpleNamespace(
            media_path=media, title="合成视频", uploader="示例", attribution="Bilibili"
        )

    def extract(media, output):
        output.write_bytes(b"fixture")
        return SimpleNamespace(path=output, probe=SimpleNamespace(duration_ms=10000))

    def fail_asr(*args, **kwargs):
        assert waiting.wait(3), "second run never waited for the shared transcript"
        raise AsrError("fixture provider failure")

    monkeypatch.setattr(pipeline, "download_bilibili_video", download)
    monkeypatch.setattr(pipeline, "extract_audio", extract)
    monkeypatch.setattr(pipeline, "transcribe_audio", fail_asr)
    monkeypatch.setattr(pipeline, "PiRunner", forbidden)
    runs = [
        pipeline.create_run(
            tmp_path, URL, transcript_sources=["bilibili_ai", "asr"], transcript_only=True
        )
        for _ in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(pipeline.generate, runs))
    assert [result["state"] for result in results] == ["TRANSCRIPT_READY"] * 2
    assert all(result["transcript_primary_source"] == "bilibili_ai" for result in results)
