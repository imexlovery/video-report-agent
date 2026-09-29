import json

import pytest

from video_report_agent.ingest import UrlIngestError, validate_bilibili_url
from video_report_agent.pipeline import create_run

URL = "https://www.bilibili.com/video/BV1bFbK6BEut"


@pytest.mark.parametrize("value", [
    f"【百万人看过的官网动画 Skill 上手实测】{URL}?vd_source=tracking",
    f"  【百万人看过的官网动画 Skill 上手实测】\n{URL}?vd_source=tracking  ",
    f"【百万人看过的官网动画 Skill 上手实测】[{URL}]({URL}?vd_source=tracking)",
    f"【标题中有 BV1234567890】{URL}",
])
def test_share_text_creates_canonical_run(tmp_path, value):
    run = create_run(tmp_path, value)
    metadata = json.loads((run / "input.json").read_text())
    assert metadata["url"] == URL + "/"
    assert metadata["video_id"] == "bilibili-BV1bFbK6BEut-p1"


def test_share_preserves_page():
    source = validate_bilibili_url(f"【标题】{URL}?vd_source=tracking&p=2")
    assert source.page_number == 2
    assert source.canonical_url == URL + "/?p=2"


@pytest.mark.parametrize("link", [
    "https://example.com/video/BV1bFbK6BEut",
    "https://www.bilibili.com.evil.example/video/BV1bFbK6BEut",
    "https://www.bilibili.com@evil.example/video/BV1bFbK6BEut",
    "https://127.0.0.1/video/BV1bFbK6BEut",
    "http://www.bilibili.com/video/BV1bFbK6BEut",
    "https://www.bilibili.com:443/video/BV1bFbK6BEut",
    "https://b23.tv/example/extra",
    URL + "/extra",
    URL + "#fragment",
    URL + "?p=0",
    URL + "?p=1&p=2",
    f"[{URL}](https://evil.example/video/BV1bFbK6BEut)",
])
@pytest.mark.parametrize("prefix", ["", "【视频 BV1bFbK6BEut】"])
def test_invalid_link_rejected_before_run_creation(tmp_path, prefix, link):
    with pytest.raises(UrlIngestError):
        create_run(tmp_path, prefix + link)
    assert not list(tmp_path.iterdir())


AGENT_URL = "https://www.bilibili.com/video/BV1uhY46kExs"
AGENT_TITLE = "【Agent planning 核心就是把复杂长任务拆成可执行的子任务，在执行中根据反馈动态调整，并通过协调与权限控制可靠地完成最终目标！】"


@pytest.mark.parametrize("value", [
    AGENT_TITLE + AGENT_URL + "?vd_source=4a10906fc2b3dfee7559f7a5cfbb49d2",
    AGENT_TITLE + f"[{AGENT_URL}?vd\\_source=4a10906fc2b3dfee7559f7a5cfbb49d2]({AGENT_URL}?vd_source=4a10906fc2b3dfee7559f7a5cfbb49d2)",
    AGENT_URL.removeprefix("https://"),
    AGENT_TITLE + AGENT_URL.removeprefix("https://"),
    AGENT_TITLE + f"[视频]({AGENT_URL.removeprefix('https://')})",
])
def test_agent_share_and_schemeless_url(tmp_path, value):
    run = create_run(tmp_path, value)
    assert json.loads((run / "input.json").read_text())["url"] == AGENT_URL + "/"


def test_schemeless_url_preserves_page():
    source = validate_bilibili_url(AGENT_URL.removeprefix("https://") + "?p=2&vd_source=tracking")
    assert source.canonical_url == AGENT_URL + "/?p=2"


@pytest.mark.parametrize("value", [
    "www.bilibili.com.evil.example/video/BV1uhY46kExs",
    "www.bilibili.com@evil.example/video/BV1uhY46kExs",
])
def test_schemeless_url_still_validates_host(value):
    with pytest.raises(UrlIngestError):
        validate_bilibili_url(value)


@pytest.mark.parametrize("value", [AGENT_TITLE + AGENT_URL, AGENT_URL.removeprefix("https://")])
def test_bilibili_412_explains_download_failure(tmp_path, monkeypatch, value):
    from types import SimpleNamespace

    from video_report_agent.ingest import download_bilibili_video

    monkeypatch.setattr("video_report_agent.ingest.shutil.which", lambda _: "/test/yt-dlp")
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=1, stderr="HTTP Error 412: Precondition Failed", stdout="")

    source = validate_bilibili_url(value)
    with pytest.raises(UrlIngestError, match="视频链接已识别.*HTTP 412"):
        download_bilibili_video(source, tmp_path, runner=runner)
    assert commands[0][-1] == AGENT_URL + "/"


@pytest.mark.parametrize("audio_only", [True, False])
def test_download_selects_audio_only_when_requested(tmp_path, monkeypatch, audio_only):
    from types import SimpleNamespace

    from video_report_agent.ingest import download_bilibili_video

    monkeypatch.setattr("video_report_agent.ingest.shutil.which", lambda _: "/test/yt-dlp")

    def runner(command, **kwargs):
        if audio_only:
            assert command[command.index("-f") + 1] == "bestaudio"
            assert "--merge-output-format" not in command
        else:
            assert "--merge-output-format" in command
        media = tmp_path / "download" / ("source.m4a" if audio_only else "source.mp4")
        media.write_bytes(b"media")
        (media.parent / "source.info.json").write_text(
            json.dumps({"title": "Title", "uploader": "UP", "duration": 60})
        )
        return SimpleNamespace(returncode=0, stderr="", stdout=str(media))

    result = download_bilibili_video(
        validate_bilibili_url(URL), tmp_path, runner=runner, audio_only=audio_only
    )
    assert result.media_path.suffix == (".m4a" if audio_only else ".mp4")


@pytest.mark.parametrize("duration,allowed", [(10800, True), (10801, True), (18000, True), (18000.1, False), (None, False)])
def test_duration_limit(tmp_path, duration, allowed):
    from video_report_agent.ingest import _metadata
    from yt_dlp.utils import match_filter_func

    info = {"title": "Test", "uploader": "UP", "duration": duration}
    assert (match_filter_func("duration <= 18000")(info) is None) == allowed
    path = tmp_path / "info.json"
    path.write_text(json.dumps(info))
    if allowed:
        assert _metadata(path, validate_bilibili_url(URL))[0] == "Test"
    else:
        with pytest.raises(UrlIngestError):
            _metadata(path, validate_bilibili_url(URL))


@pytest.mark.parametrize("title", [
    "【一旦有钱立刻升级这7样东西！——Dan Martell【中英字幕】】",
    "【电诈，进化到这种程度了？【大国之治·反诈系统】-哔哩哔哩】",
])
def test_nested_share_title(title):
    url = "https://www.bilibili.com/video/BV1AzYs6bEeX/"
    source = validate_bilibili_url(title + f" [{url}]({url}?share_source=copy_web)")
    assert source.canonical_url == url


@pytest.mark.parametrize("destination", [
    URL + "?p=2", "https://127.0.0.1/", "https://evil.example/", "https://b23.tv/other",
])
def test_short_share_validates_redirect(tmp_path, monkeypatch, destination):
    import httpx

    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(302, headers={"location": destination})

    monkeypatch.setattr("video_report_agent.ingest.httpx.get", get)
    value = "【电诈，进化到这种程度了？【大国之治·反诈系统】-哔哩哔哩】 [https://b23.tv/Rfx6XG4](https://b23.tv/Rfx6XG4)"
    if destination.startswith(URL):
        run = create_run(tmp_path, value)
        assert json.loads((run / "input.json").read_text())["url"] == URL + "/?p=2"
    else:
        with pytest.raises(UrlIngestError):
            create_run(tmp_path, value)
        assert not list(tmp_path.iterdir())
    assert calls == [("https://b23.tv/Rfx6XG4", {"follow_redirects": False, "timeout": 10})]
