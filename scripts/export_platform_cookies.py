"""Run on the user's computer only when a platform explicitly requires login."""

import argparse
import os
from pathlib import Path

from video_report_agent.ingest import UrlIngestError
from video_report_agent.platform_media import read_platform_cookies


def export(platform, browser, output):
    host = {"douyin": "douyin.com", "xiaohongshu": "xiaohongshu.com"}[platform]
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        # An explicit browser is the only source; never export server environment credentials.
        jar = read_platform_cookies(host, None, browser)
        jar.save(str(output), ignore_discard=True, ignore_expires=True)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return output


def main():
    parser = argparse.ArgumentParser(
        description="在用户本机导出指定平台 Cookie；不上传、不显示内容",
    )
    parser.add_argument("platform", choices=["douyin", "xiaohongshu"])
    parser.add_argument("--browser", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path(".local/platform-cookies") / f"{args.platform}.cookies.txt"
    try:
        export(args.platform, args.browser, output)
    except (OSError, UrlIngestError) as exc:
        message = (str(exc) if isinstance(exc, UrlIngestError)
                   else "无法创建文件；请使用新的输出路径。")
        print(message)
        return 1
    print(f"已保存到本机：{output}（仅当前用户可读写）。不要上传此文件；用完后删除。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
