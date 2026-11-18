# Video Report Agent · Public Core

[简体中文](README.md) | English

Turn video transcripts into HTML reports you can skim, read in depth, and save.

- **Supported platforms** Single-video links from Bilibili, YouTube, Douyin, and Xiaohongshu. You can also give the Skill a complete transcript, subtitles, or other long-form text.
- **How it works** Core fetches usable subtitles first and extracts and transcribes audio when needed. Pi runs the AI agent that reads the full transcript and writes a self-contained HTML report. Core then attempts to export a full-length PNG.
- **Core asset** The [`video-report` Skill](src/video_report_agent/skills/video-report/SKILL.md) contains editing and source-checking rules, HTML templates for Standard reading and Brief overviews, and references for organizing different kinds of material.
- **Models** The CLI defaults to `deepseek-flash` for report writing, and you can select another model in Pi. Automatic source selection falls back to Alibaba Cloud Qwen Audio 3.1 Filetrans for speech recognition. On Apple Silicon, you can explicitly select local MLX Whisper.

## See the result

GPT-6.2 Sol generated this Standard report about Walter Pitts and the early history of neural networks. It explains AND, OR, and inhibition through a bubble tea example, then uses a generated diagram to show how signals feed back through a neural loop.

<a href="docs/examples/report.png"><img src="docs/images/report-preview.png" width="460" alt="Light report preview beginning with the title and introduction. Click for the full image."></a>

[Full HTML report (Chinese)](docs/examples/report.html) · [Light PNG](docs/examples/report.png) · [Dark PNG](docs/examples/report-dark.png)

Download the HTML from GitHub and open it in a browser. The report follows your system theme by default. Use the button in the upper-right corner to switch themes.

## Choose how to use it

| What you want to do | Start here |
|---|---|
| Generate a report online | [Open VReport](https://vreport.tri4t.xyz) |
| Turn an existing transcript, subtitles, or long text into a report | [Download Skill ZIP v0.7.0](https://github.com/imexlovery/video-report-agent/releases/download/skill-zip-v0.7.0/video-report-skill-v0.7.0.zip) for your own AI agent |
| Transcribe or generate a report locally from a video link | [Use the full CLI](#option-2-run-the-complete-cli-pipeline) |

<a name="option-1-use-your-own-coding-agent"></a>

## Option 1: use your own AI agent

You’ll need an AI agent with a configured model and access to read and write local files.

1. <a name="2-get-the-complete-skill-directory"></a>Download and unzip the Skill ZIP above. Keep the entire `video-report/` directory together.
2. <a name="1-prepare-a-complete-transcript"></a><a name="3-ask-the-agent-to-generate"></a>Open your AI agent in the extracted directory and try the bundled transcript with this prompt:

   ```text
   Read video-report/SKILL.md and use standard mode.
   Read all of examples/transcript.md and treat it as read-only input.
   Preserve important conditions, numbers, and attributions.
   Write a self-contained HTML report to output/report.html.
   ```

3. Open `output/report.html` in your browser.

<a name="how-the-template-skill-works"></a>The [report Skill](src/video_report_agent/skills/video-report/SKILL.md) provides separate reading rules and templates for Standard and Brief. Replace the example path with your own transcript to run it on new material. Use `brief` in place of `standard` for a shorter read focused on the main findings and evidence. See the [Skill ZIP guide (Chinese)](docs/skill-bundle/README.md).

## Option 2: run the complete CLI pipeline

<a name="install"></a>You'll need Python 3.12, uv, FFmpeg, and Node.js 22.19.0 or later. Follow the guide to [install the CLI and its dependencies](docs/cli.en.md#install), then <a name="configure-the-model-and-asr"></a>[configure the report model and transcription](docs/cli.en.md#configure-the-model-and-asr).

<a name="generate-and-read"></a>From the repository directory, run this command with your video link:

```sh
uv run video-report generate 'https://www.bilibili.com/video/BV...'
```

Open `runs/<run-id>/report.html` when the run finishes. If you only need a transcript, use [`transcribe`](docs/cli.en.md#generate-and-read).

<a name="optional-tools-and-capabilities"></a>

### Report generation tools

| Tool and flag | What it does |
|---|---|
| **Diagram generation** | In Standard, the AI agent can request an SVG to explain a mechanism, sequence, or relationship. Core embeds it in the report. |
| **Term verification** `--term-correction` | Checks names and technical terms against video metadata first, then looks up web sources when needed. The original transcript is preserved. |
| **Visual context** `--visual-enhancement` | Samples nine frames from a Bilibili video and uses an image-capable model to describe details beyond the transcript. |
| **Report inspection** `--report-inspection` | Returns structural checks, source references, and available screenshots from the draft to the same AI agent for revision. |
| **Style editing** | Runs a separate prose edit for smoother sentences and transitions. On by default. Use `--no-style-edit` to skip it. |

To prepare source material, use `preflight` to check a link or `fetch-source` to collect subtitles when available or extract audio. See the [CLI guide](docs/cli.en.md#optional-tools-and-capabilities) for defaults, limits, and Python options.

## Development and further reading

- [CLI guide](docs/cli.en.md) · <a name="choose-subtitle-and-transcript-sources"></a>[Subtitle and transcript sources](docs/cli.en.md#choose-subtitle-and-transcript-sources) · <a name="unified-source-access"></a>[Link checks and local acquisition](docs/cli.en.md#unified-source-access) · <a name="douyin-and-xiaohongshu-acquisition"></a>[Douyin and Xiaohongshu acquisition](docs/cli.en.md#douyin-and-xiaohongshu-acquisition)
- <a name="configure-search-for-term-correction"></a>[Search for term correction](docs/cli.en.md#configure-search-for-term-correction) · <a name="runtime-and-data"></a>[Runtime and data](docs/cli.en.md#runtime-and-data)
- [Core documentation (Chinese)](docs/README.md) · [Report evaluation](evals/report-review/README.md)

Public Core maintains the generation pipeline and report Skill. A separate web service runs the website and manages accounts and credits.
