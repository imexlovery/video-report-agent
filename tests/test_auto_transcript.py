import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from video_report_agent import pipeline
from video_report_agent.bilibili_subtitles import BilibiliSubtitles, SubtitleError
from video_report_agent.ingest import validate_bilibili_url
from video_report_agent.reuse import cache_key

URL = "https://www.bilibili.com/video/BV1qxn16yEvP/"
BODY = [{"from": 0, "to": 2, "content": "合成来源文字稿"}]


def snapshot(author="ABSENT", ai="READY"):
    result = {
        "video_id": "bilibili-BV1qxn16yEvP-p1",
        "bvid": "BV1qxn16yEvP",
        "cid": 12,
        "title": "合成视频",
        "uploader": "示例",
        "attribution": "Bilibili",
        "duration_ms": 3000,
        "sources": {},
    }
    for kind, status in (("subtitle_track", author), ("bilibili_ai", ai)):
        result["sources"][kind] = {"status": status, "count": int(status == "READY")}
        if status == "READY":
            result["sources"][kind]["body"] = copy.deepcopy(BODY)
    return result


def forbidden(*a, **kw):
    pytest.fail("A stopped or unselected stage was called")


@pytest.mark.parametrize(
    "author,ai,chosen",
    [
        ("READY", "SKIPPED", "subtitle_track"),
        ("ABSENT", "READY", "bilibili_ai"),
        ("FAILED", "READY", "bilibili_ai"),
    ],
)
def test_auto_captions_stop_without_asr_credentials_media_ocr_or_report(
    tmp_path,
    monkeypatch,
    author,
    ai,
    chosen,
):
    monkeypatch.setenv("ASR_BACKEND", "paraformer")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    def fetch(source, selected, *, first_available):
        assert selected == ["subtitle_track", "bilibili_ai"] and first_available
        return snapshot(author, ai)

    monkeypatch.setattr(pipeline, "fetch_subtitles", fetch)
    for name in ("download_bilibili_video", "extract_audio", "transcribe_audio", "PiRunner"):
        monkeypatch.setattr(pipeline, name, forbidden)
    runs = []
    for _ in range(2):
        run = pipeline.create_run(tmp_path, URL, transcript_mode="auto", transcript_only=True)
        runs.append(run)
        result = pipeline.generate(run)
        assert result["state"] == "TRANSCRIPT_READY"
        assert result["transcript_primary_source"] == chosen
        assert result["asr_performed"] is False
        manifest = json.loads((run / "transcript-manifest.json").read_text())
        assert manifest["transcript_mode"] == "auto"
        assert manifest["selected_sources"] == [chosen]
        assert manifest["source_status"]["subtitle_track"]["status"] == author
        assert not (run / "asr.json").exists()
        assert not (run / "download").exists()
    assert result["transcript_reused_from"] == runs[0].name


@pytest.mark.parametrize("source_error", [False, True, "invalid-nav"])
def test_auto_reaches_asr_only_after_captions_unavailable(tmp_path, monkeypatch, source_error):
    calls = []
    monkeypatch.setenv("ASR_BACKEND", "mlx")

    def fetch(*a, **kw):
        calls.append("subtitles")
        if source_error == "invalid-nav":

            def handler(request):
                if request.url.path.endswith("/nav"):
                    return httpx.Response(200, text="not json")
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "bvid": "BV1qxn16yEvP",
                            "aid": 1,
                            "title": "合成视频",
                            "pages": [{"cid": 12, "duration": 3}],
                        },
                    },
                )

            with httpx.Client(transport=httpx.MockTransport(handler)) as http:
                return BilibiliSubtitles(client=http, authenticate=False).fetch(a[0])
        if source_error:
            raise SubtitleError("SUBTITLE_FETCH_FAILED", "fixture")
        return snapshot("ABSENT", "FAILED")

    def download(source, run, **kwargs):
        calls.append("download")
        folder = run / "download"
        folder.mkdir()
        path = folder / "source.m4a"
        path.write_bytes(b"fixture")
        (folder / "source.info.json").write_text(
            json.dumps({"title": "合成视频", "uploader": "示例", "duration": 3})
        )
        return SimpleNamespace(
            media_path=path, title="合成视频", uploader="示例", attribution="Bilibili"
        )

    def audio(path, target):
        calls.append("audio")
        target.write_bytes(b"fixture")
        return SimpleNamespace(path=target, probe=SimpleNamespace(duration_ms=3000))

    def asr(*a, **kw):
        calls.append("asr")
        segments = [
            {"ordinal": 0, "start_ms": 0, "end_ms": 2000, "text": "合成语音文字稿", "words": []}
        ]
        return SimpleNamespace(
            segments=segments,
            to_dict=lambda: {
                "backend": "mlx",
                "segments": segments,
            },
        )

    monkeypatch.setattr(pipeline, "fetch_subtitles", fetch)
    monkeypatch.setattr(pipeline, "download_bilibili_video", download)
    monkeypatch.setattr(pipeline, "extract_audio", audio)
    monkeypatch.setattr(pipeline, "transcribe_audio", asr)
    monkeypatch.setattr(pipeline, "PiRunner", forbidden)
    run = pipeline.create_run(tmp_path, URL, transcript_mode="auto", transcript_only=True)
    result = pipeline.generate(run)
    assert result["state"] == "TRANSCRIPT_READY", result
    assert calls == ["subtitles", "download", "audio", "asr"]
    assert result["transcript_primary_source"] == "asr"
    assert result["asr_performed"] is True
    assert result["transcript_source_status"]["ocr"]["status"] == "OFF"


@pytest.mark.parametrize("author_valid", [True, False])
def test_auto_provider_downloads_in_order_and_stops_at_first_usable_track(
    monkeypatch, author_valid
):
    with httpx.Client() as http:
        client = BilibiliSubtitles(client=http, authenticate=False)
        monkeypatch.setattr(
            client,
            "video_info",
            lambda _: {
                "duration_ms": 3000,
                "bvid": "BV1qxn16yEvP",
                "cid": 12,
            },
        )
        monkeypatch.setattr(
            client,
            "tracks",
            lambda _: (
                [
                    {"id": 1, "lan": "zh", "subtitle_url": "https://aisubtitle.hdslb.com/author"},
                    {"id": 2, "lan": "ai-zh", "subtitle_url": "https://aisubtitle.hdslb.com/ai"},
                ],
                True,
                [],
            ),
        )
        fetched = []

        def get(url):
            fetched.append(url.rsplit("/", 1)[-1])
            return httpx.Response(
                200,
                json={
                    "body": BODY if author_valid or url.endswith("/ai") else [],
                },
            )

        monkeypatch.setattr(client, "_get", get)
        result = client.fetch(validate_bilibili_url(URL), first_available=True)
    assert fetched == (["author"] if author_valid else ["author", "ai"])
    assert result["sources"]["bilibili_ai"]["status"] == ("SKIPPED" if author_valid else "READY")


def test_auto_and_manual_transcripts_have_separate_cache_identity():
    metadata = {
        "video_id": "example",
        "transcript_mode": "auto",
        "resolved_transcript_sources": ["bilibili_ai"],
    }
    key = cache_key("transcript", metadata, {"bilibili-subtitles.json": "captions"})
    metadata["transcript_mode"] = "selected"
    assert key != cache_key("transcript", metadata, {"bilibili-subtitles.json": "captions"})


def test_second_usable_author_track_wins_before_ai(monkeypatch):
    with httpx.Client() as http:
        client = BilibiliSubtitles(client=http, authenticate=False)
        monkeypatch.setattr(client, "video_info", lambda _: {"duration_ms": 3000})
        monkeypatch.setattr(
            client,
            "tracks",
            lambda _: (
                [
                    {"id": 1, "lan": "zh", "subtitle_url": "https://aisubtitle.hdslb.com/bad"},
                    {
                        "id": 2,
                        "lan": "zh-Hans",
                        "subtitle_url": "https://aisubtitle.hdslb.com/good",
                    },
                    {"id": 3, "lan": "ai-zh", "subtitle_url": "https://aisubtitle.hdslb.com/ai"},
                ],
                True,
                [],
            ),
        )
        fetched = []

        def get(url):
            fetched.append(url.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"body": BODY if url.endswith("/good") else []})

        monkeypatch.setattr(client, "_get", get)
        result = client.fetch(validate_bilibili_url(URL), first_available=True)
    assert fetched == ["bad", "good"]
    assert result["sources"]["subtitle_track"]["status"] == "READY"
    assert result["sources"]["subtitle_track"]["track_id"] == "2"
    assert "error_code" not in result["sources"]["subtitle_track"]
    assert result["sources"]["bilibili_ai"]["status"] == "SKIPPED"
