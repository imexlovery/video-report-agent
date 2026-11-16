"""Downloadable user-side script. Requires an installed Video Report Core and FFmpeg."""


def main():
    import argparse
    import json
    from pathlib import Path

    try:
        from video_report_agent.audio import extract_audio
        from video_report_agent.ingest import UrlIngestError
        from video_report_agent.source_access import acquire_source
    except ImportError:
        print("请先在这台电脑安装 Video Report Core 和 FFmpeg，再运行本脚本。")
        return 1
    parser = argparse.ArgumentParser(description="在当前电脑取材，Cookie 不上传到报告网站")
    parser.add_argument("url", nargs="?")
    parser.add_argument("--browser")
    parser.add_argument("--output", type=Path, default=Path("video-materials"))
    args = parser.parse_args()
    url = args.url or input("粘贴完整视频链接或分享文字：").strip()
    browser = args.browser or input(
        "需要登录时使用哪个浏览器？[edge/chrome/firefox，默认 edge]：",
    ).strip() or "edge"
    try:
        source, snapshot, downloaded = acquire_source(url, args.output, browser=browser)
        material_root = args.output.resolve() / source.video_id
        result = {"platform": source.platform, "title": snapshot["title"],
                  "material_path": str(material_root / "source-material.json")}
        if downloaded:
            audio = extract_audio(downloaded.media_path, material_root / "audio.wav")
            result["audio_path"] = str(audio.path.resolve())
    except UrlIngestError as exc:
        print(str(exc))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("素材已保存到当前电脑。Cookie 未上传到报告网站，也未写入素材文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
