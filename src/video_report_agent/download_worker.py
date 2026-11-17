"""Run yt-dlp with Bilibili's own backup URLs available on one retry."""

from __future__ import annotations

import copy
import signal
import sys
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.extractor.bilibili import BilibiliBaseIE
from yt_dlp.utils import ExtractorError, Popen


def select_backup_urls(value: object, *, backup: bool) -> None:
    """Change only returned media URLs, preserving format and request headers."""
    if isinstance(value, list):
        for item in value:
            select_backup_urls(item, backup=backup)
    elif isinstance(value, dict):
        key = next((key for key in ("baseUrl", "base_url", "url") if value.get(key)), None)
        alternatives = value.get("backupUrl") or value.get("backup_url") or []
        if key and isinstance(alternatives, list):
            primary = value[key]
            candidates = [url for url in alternatives if isinstance(url, str) and url != primary]
            if backup and candidates:
                value[key] = candidates[0]
            # Only hostnames are diagnostic data; signed media URLs stay in yt-dlp metadata.
            print(
                f"[node] mode={'backup' if backup and candidates else 'primary'} "
                f"host={urlsplit(str(value[key])).hostname} alternatives={len(candidates)}",
                file=sys.stderr, flush=True,
            )
        for item in value.values():
            if isinstance(item, (dict, list)):
                select_backup_urls(item, backup=backup)


def main() -> None:
    backup = sys.argv[1] == "1"
    original = BilibiliBaseIE.extract_formats

    def extract_formats(self, play_info):
        if any("试看" in str(label) for label in play_info.get("accept_description", [])):
            raise ExtractorError("仅取得试看内容，请使用有该视频访问权限的账号。", expected=True)
        selected = copy.deepcopy(play_info)
        select_backup_urls(selected, backup=backup)
        return original(self, selected)

    BilibiliBaseIE.extract_formats = extract_formats

    if any(arg.startswith("https://www.douyin.com/video/") for arg in sys.argv[2:]):
        from yt_dlp.extractor.tiktok import DouyinIE

        from .douyin import extract_douyin

        DouyinIE._real_extract = extract_douyin

    if any(arg.startswith("https://www.xiaohongshu.com/explore/") for arg in sys.argv[2:]):
        from yt_dlp.extractor.xiaohongshu import XiaoHongShuIE

        from .xiaohongshu import extract_xiaohongshu

        XiaoHongShuIE._real_extract = extract_xiaohongshu

    # Keep the outer pipeline process group. On our own deadline, also stop any
    # FFmpeg process created by yt-dlp before exiting this worker.
    children = []
    original_init = Popen.__init__

    def child_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        children.append(self)

    Popen.__init__ = child_init

    def stop(signum, frame):
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    yt_dlp.main(sys.argv[2:])


if __name__ == "__main__":
    main()
