# Video Report Agent

Hosted website 👉 [vreport.tri4t.xyz](https://vreport.tri4t.xyz)

[简体中文](README.zh-CN.md) | English

Turn videos into self-contained HTML reading reports. **[Try the website](https://vreport.tri4t.xyz)** to generate and read reports online.

This repository provides the **video-report Skill** for use in your own Coding Agent, and a **pipeline** that automates video downloading, transcription, and report generation.

## Quick start: use your own Coding Agent

> Video / audio → your choice of ASR → transcript → Coding Agent + video-report Skill → report.html

### 1. Prepare a transcript

Transcribe the video or audio with your own ASR tool or API and save the result as `transcript.md`, TXT, SRT, or another format your Agent can read. Reuse complete subtitles or an existing transcript when available.

- Apple Silicon: the developer uses MLX Whisper.
- Windows and other platforms: use a local ASR compatible with your system and hardware, or a cloud speech-to-text API.
- Preserve timestamps, speaker information, and the full text where available. A transcript without reliable timestamps still works; the report should omit unsupported chapter times.

ASR converts speech to text. Your Coding Agent's model reads that text and creates the HTML report. You can choose these services independently.

### 2. Get the Skill and template

Download this repository using **Code → Download ZIP** and extract it, or clone it with `git clone https://github.com/imexlovery/video-report-agent.git`. Copy the complete [`src/video_report_agent/skills/video-report/`](src/video_report_agent/skills/video-report/) directory. Keep `SKILL.md`, `modes/`, `assets/`, and `references/` together with their relative paths intact.

Arrange your working folder as follows:

```text
my-report/
├── transcript.md
├── video-report/
│   ├── SKILL.md
│   ├── modes/                # Read only standard.md or brief.md
│   ├── references/           # Editing guidance selected by the Skill
│   └── assets/
│       ├── report-template.html       # Standard only
│       └── brief-report-template.html # Brief only
└── output/
```

You do not need this project's Python backend, Pi RPC, or Web UI. Use a Coding Agent that can read local files and write HTML, with its own model configured. If it supports Skills, install the directory using its conventions; otherwise, explicitly ask it to read the instructions from the file.

A transcript is enough: `input.json` and `source.info.json` are not required. A supplied video description is optional; without one, the Skill omits that section. Standalone output is complete HTML with no pending description placeholder or Python postprocessing step. Browser inspection and PNG export are optional and depend on your Agent's available tools.

### 3. Ask your Agent to generate the report

Open your Coding Agent in that working folder and send:

```text
Read video-report/SKILL.md and follow its instructions.
Read only the template selected by the reading mode guide.
Read transcript.md in full and produce a self-contained HTML reading report.

Use output/ as this task's output directory and write output/report.html.
Preserve important conditions, numbers, formulas, attribution, and uncertainty.
Include chapter times only when reliable source timestamps are available.
Do not modify the original transcript. If browser tools are available, inspect
the page layout; otherwise, state that only static checks were performed.
```

To request a concise overview, add `Use report_mode=brief (精简速览).` to your prompt.
The default is `standard` (标准阅读). Each mode has its own template and components;
only the selected mode guide and template are read. Both modes
read the full transcript and preserve decisive conditions, attribution and number definitions.
The CLI accepts `video-report generate <url> --report-mode brief`. Each task saves its mode;
changing modes creates a new report and can reuse compatible source transcripts.

Open `output/report.html` in a browser. The Skill guides content organization, source fidelity, and layout. Results depend on transcript quality, the Agent's model, and available tools. The Skill does not include an ASR engine or configure your transcription service.

## Preview

**Website preview**

![Web homepage in its default state](docs/images/web-home.png)

**Sample report · China's fiscal rebalancing (in Chinese)**

The image below shows the top of the report. Follow the links for the complete report without expanding the entire long image in this README.

[![HTML reading report preview](docs/images/report-preview.png)](docs/examples/report.png)

↗ 🔗 [HTML report](docs/examples/report.html) · [Full PNG](docs/examples/report.png)

On GitHub, the HTML link opens the file page; download it and open it in a browser to read. The example files are included in the repository and do not require a running local service.

## Optional: run the pipeline

The local CLI quickstart below runs Pi directly on your machine, without Docker
or a task cleaner. The supplied `.env.example` explicitly sets
`PI_TASK_ISOLATION=local`; Pi's file and shell tools use your local user permissions.
Standalone Skill use above is unchanged.

For per-task Docker isolation, set `PI_TASK_ISOLATION=docker` and supply a compatible
built application image via `PI_TASK_IMAGE`, a running Docker daemon, and an
independently running healthy task cleaner with the same `PI_TASK_SCOPE`
(default `video-report`). The runtime defaults to Docker when the isolation setting
is omitted, and isolation failures do not fall back to local execution. Hosted
Public mode always requires Docker. See [architecture](docs/ARCHITECTURE.md) for
mounts, API-key requirements and limitations; the public Core does not include
the hosted service's Dockerfile or Compose deployment.

Use the CLI to automate downloading, transcription, and report generation without a website server.

> Bilibili URL → yt-dlp → FFmpeg → ASR → Canonical Transcript → Pi RPC + Skill → report.html

Clone the repository and enter its directory (skip cloning if already downloaded):

```sh
git clone https://github.com/imexlovery/video-report-agent.git
cd video-report-agent
```

### Requirements and installation

Install Python 3.12, uv, `ffmpeg` / `ffprobe` on `PATH`, and Pi 0.85.0. Configure credentials for your report model. `uv sync` does not install Pi. Run these commands from the repository root.

Install Node.js 22 (including npm), then install the pinned Pi version and verify it is on `PATH`:

```sh
npm install -g @earendil-works/pi-coding-agent@0.85.0
pi --version
```

For local MLX on Apple Silicon:

```sh
uv sync --extra mlx
uv run playwright install chromium
```

For the built-in cloud ASR path, MLX is unnecessary:

```sh
uv sync
uv run playwright install chromium
```

For a new setup, copy `.env.example` to `.env`; preserve an existing `.env`.
Set the report model and explicitly select local execution:

```dotenv
PI_PROVIDER=deepseek
PI_MODEL=deepseek-flash
PI_API_KEY=your-api-key
PI_TASK_ISOLATION=local
```

### Choose ASR

The developer's local Apple Silicon configuration:

```dotenv
ASR_BACKEND=mlx
ASR_MODEL=mlx-community/whisper-large-v3-turbo
```

For the built-in Bailian file-transcription API:

```dotenv
ASR_BACKEND=paraformer
ASR_MODEL=paraformer-v2
DASHSCOPE_API_KEY=your-dashscope-api-key
```

The `paraformer` backend supports `paraformer-v2` and the Fun-ASR file-transcription models adapted in the code. Changes to `.env` take effect on the next CLI invocation. Cloud ASR uploads audio; the report model receives transcript content needed for generation.

For audio at least 60 minutes long, this backend looks for a quiet pause of at least 250 ms within 30 seconds of the midpoint and transcribes two non-overlapping parts concurrently. Shorter audio, or audio without a suitable pause, stays in one request. Sentence and word timestamps are shifted back to the original timeline, and usage is summed across both parts. Each report still occupies one task slot, but can use two concurrent cloud ASR requests.

**Standalone Skill use lets you choose any external ASR workflow. The project pipeline currently includes only the `mlx` and `paraformer` backends.** Other local models or APIs require an adapter, not just a model-name change. Windows users can start with the standalone Skill workflow above; native Windows execution of the complete project is not presented here as verified.

### Start and generate

With the `.env` settings above, this command runs without Docker:

```sh
uv run video-report generate 'https://www.bilibili.com/video/BV...' --transcript-mode asr-only
```

If you are keeping an existing `.env`, you can select local execution for one run
with `PI_TASK_ISOLATION=local uv run video-report generate '<url>'`.

Outputs are saved in `runs/<run-id>/`, including `transcript.md`, provenance records, and `report.html`. The pipeline also attempts to export `report.png` using Chromium. If export fails, the HTML can still be marked `RENDERED`; inspect `image_error` in `status.json` before assuming the PNG exists.

## Project behavior and boundaries

- ASR-only with OCR off is the default. Optional `fused` mode supports subtitle import, OCR, and fusion. Install its dependencies with `uv sync --extra enhancement`; include `--extra mlx` as well when using MLX.
- Every generation uses its own `runs/<run-id>/` working directory. Pi owns the Agent loop, tools, and context management; the Skill defines report-editing requirements.
- Videos are limited to 5 hours. Tasks have a 30-minute execution deadline, excluding queue time. Already-submitted cloud ASR may continue and incur charges after a local task stops.
- Ingestion supports public Bilibili videos, without login or private-video access. Check generated content against the original source as needed.
- In local mode, Pi's file and shell tools use your local user permissions; a dedicated working directory is not an operating-system sandbox. Docker mode isolates the Pi generation process as described in the architecture document.

## Development checks

```sh
uv run --extra enhancement pytest -q
uv run ruff check src tests
uv lock --check
```

See [`video-report/SKILL.md`](src/video_report_agent/skills/video-report/SKILL.md) for report-writing instructions and [`report-template.html`](src/video_report_agent/skills/video-report/assets/report-template.html) for Standard styles; [Brief](src/video_report_agent/skills/video-report/assets/brief-report-template.html) uses its own template.

## Core package configuration

The CLI reads `.env` from the working directory; existing process environment variables take precedence. Pi state lives in `config/pi/` there, never in the installed package. The first generation initializes `models.json` from packaged defaults without replacing existing settings. Set `PI_CODING_AGENT_DIR` in the process environment to choose another directory.

The packaged Skill is the default. Callers can select a complete Skill directory with `PiRunner(skill_dir=...)` or the worker environment variable `VIDEO_REPORT_SKILL_DIR`.

For public synthetic smoke inputs and optional live evaluation, see [evals/report-review](evals/report-review/README.md).

[Project documentation](docs/README.md)
