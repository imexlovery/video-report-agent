"""Build a standalone Skill ZIP from the canonical Core assets."""

import argparse
import shutil
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Bundle version, for example 0.5.0")
    args = parser.parse_args()
    version = args.version.removeprefix("v")

    root = Path(__file__).resolve().parents[1]
    output = root / ".local" / "dist"
    output.mkdir(parents=True, exist_ok=True)
    name = f"video-report-skill-v{version}"
    docs = root / "docs" / "skill-bundle"

    with tempfile.TemporaryDirectory(dir=output) as staging:
        bundle = Path(staging) / name
        bundle.mkdir()
        shutil.copytree(
            root / "src/video_report_agent/skills/video-report", bundle / "video-report"
        )
        shutil.copytree(docs / "examples", bundle / "examples")
        readme = (docs / "README.md").read_text(encoding="utf-8")
        readme = readme.replace("# Video Report Skill", f"# Video Report Skill v{version}", 1)
        (bundle / "README.md").write_text(readme, encoding="utf-8")
        archive = shutil.make_archive(str(output / name), "zip", staging, name)

    print(archive)


if __name__ == "__main__":
    main()
