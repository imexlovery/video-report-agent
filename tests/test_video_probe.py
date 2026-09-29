import json
import subprocess
from types import SimpleNamespace

import pytest

from video_report_agent.ingest import UrlIngestError, probe_bilibili_video, validate_bilibili_url


@pytest.mark.parametrize("duration", [599, 600, 659, 1139, 1140, 10800, 10801, 18000])
def test_probe_selected_part_without_download(monkeypatch, duration):
    monkeypatch.setattr("video_report_agent.ingest.shutil.which", lambda _: "/bin/yt-dlp")

    def runner(command, **kwargs):
        assert command[-1] == "https://www.bilibili.com/video/BV1aTtb6uE7d/?p=2"
        assert "--no-playlist" in command and "--skip-download" in command
        assert "--dump-single-json" in command and "--write-info-json" not in command
        assert kwargs["timeout"] == 45
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"title": "第二部分", "duration": duration})
        )

    result = probe_bilibili_video(
        validate_bilibili_url("https://www.bilibili.com/video/BV1aTtb6uE7d/?p=2"), runner=runner
    )
    assert result["page_number"] == 2 and result["video_id"].endswith("-p2")
    assert result["duration"] == duration


@pytest.mark.parametrize("duration", [None, True, 0, -1, "600", float("nan"), 18000.1, 18001])
def test_probe_rejects_unknown_or_unsupported_duration(monkeypatch, duration):
    monkeypatch.setattr("video_report_agent.ingest.shutil.which", lambda _: "/bin/yt-dlp")
    with pytest.raises(UrlIngestError):
        probe_bilibili_video(
            validate_bilibili_url("BV1aTtb6uE7d"),
            runner=lambda *a, **k: SimpleNamespace(
                returncode=0, stdout=json.dumps({"duration": duration})
            ),
        )


def test_probe_rejects_playlist_and_timeout(monkeypatch):
    monkeypatch.setattr("video_report_agent.ingest.shutil.which", lambda _: "/bin/yt-dlp")
    source = validate_bilibili_url("BV1aTtb6uE7d")
    with pytest.raises(UrlIngestError):
        probe_bilibili_video(
            source,
            runner=lambda *a, **k: SimpleNamespace(
                returncode=0, stdout='{"_type":"playlist","duration":60}'
            ),
        )

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("yt-dlp", 45)

    with pytest.raises(UrlIngestError):
        probe_bilibili_video(source, runner=timeout)
