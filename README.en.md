# Video Report Agent · Public Core

[简体中文](README.md) | English

Turn video transcripts into HTML reports you can skim, study and save. Use the [VReport website](https://vreport.tri4t.xyz), or give this repository's Skill and templates to your own Coding Agent to generate reports locally.

**The central asset is the [`video-report` template Skill](src/video_report_agent/skills/video-report/)**. It combines editing guidance, reading modes, visual templates and source constraints to help an Agent organize a readable report from a complete transcript. The Python pipeline handles downloading, transcription, Agent execution and image export. A separate Web Service owns the website, accounts and credits.

| What you want to do | Start here |
|---|---|
| Generate a report online | [Open VReport](https://vreport.tri4t.xyz) |
| Give an existing transcript to your own Coding Agent | [Download Skill ZIP v0.5.0](https://github.com/imexlovery/video-report-agent/releases/download/skill-zip-v0.5.0/video-report-skill-v0.5.0.zip) · [Quick start and examples (Chinese)](docs/skill-bundle/README.md) |
| Run the complete workflow from a video URL | [CLI installation](#option-2-run-the-complete-cli-pipeline) |

## See the result

This preview shows the top of a sample report. Click it for the complete image.

[![Reading report preview](docs/images/report-preview.png)](docs/examples/report.png)

[Sample HTML](docs/examples/report.html) · [Full PNG](docs/examples/report.png) · [Use online](https://vreport.tri4t.xyz)

On GitHub, the HTML link opens the source file. Download it and open it in a browser. These examples ship with the repository and require no running service.

## How the template Skill works

Input can be a complete Markdown, TXT or SRT transcript, or another long text. Title, author, URL and timestamps are optional. The Agent reads the complete source, organizes sections around its information relationships, then writes, illustrates and revises the report using the selected HTML template.

Keep all four parts together:

| Asset | Purpose |
|---|---|
| [`SKILL.md`](src/video_report_agent/skills/video-report/SKILL.md) | Shared entrypoint: source fidelity, organization, promotion exclusion, language and delivery rules |
| [`modes/`](src/video_report_agent/skills/video-report/modes/) | Standard / Brief content selection, reading depth and component guidance |
| [`assets/`](src/video_report_agent/skills/video-report/assets/) | A separate HTML template for each mode, with inline styles and visual components |
| [`references/`](src/video_report_agent/skills/video-report/references/) | On-demand guidance for mechanisms, procedures, evidence, arguments and narratives |

The templates supply layout and expression tools; the source determines the sections. Mechanisms may need relationship diagrams, procedures may need steps and branches, and evidence may need data comparisons. The Agent chooses one primary editing direction and, when needed, one additional reference. It loads the selected mode, template and references as needed within a single Agent task.

| Reading mode | Use case | Presentation |
|---|---|---|
| `standard` · Standard reading (default) | Understand the full main thread with necessary explanations, evidence and examples | Length follows useful information; long reports may include navigation and reliable source times |
| `brief` · Concise overview | Grasp the core question, main judgments and decisive conditions | Separate visual template; body targets 1,600–2,200 non-whitespace characters, capped at 2,400; short sources can be shorter |

Both modes read the full source and preserve number definitions, conditions, attribution and uncertainty. The Skill instructs the Agent to identify and exclude commercial promotion while retaining useful tutorial and test content. Hidden `data-source-units` attributes link claims to source locations; those links aid review but do not prove correctness.

Delivery is a self-contained `report.html` with inline styles, ready to open in a browser. Quality depends on the transcript, model and actual checks. The Skill does not include an ASR engine or configure your model.

## Option 1: use your own Coding Agent

This is the most direct way to reuse the core asset. It needs no project Python backend, Pi or website.

### 1. Prepare a complete transcript

Use your own ASR tool or complete existing subtitles and save them as `transcript.md`, TXT or SRT. Preserve timestamps and speakers when possible. Without reliable times, the report omits the corresponding chapter times.

ASR converts speech into text; your Coding Agent's model reads it and generates the report. Choose these independently.

### 2. Get the complete Skill directory

Download and extract [Skill ZIP v0.5.0](https://github.com/imexlovery/video-report-agent/releases/download/skill-zip-v0.5.0/video-report-skill-v0.5.0.zip). It includes the complete Skill, a short Chinese guide, and a paired transcript and HTML example. Start with the bundled `README.md`.

To get the source code, clone the repository:

```sh
git clone https://github.com/imexlovery/video-report-agent.git
```

Use `video-report/` from the download, or copy the complete `src/video_report_agent/skills/video-report/` directory from the source repository, preserving relative paths:

```text
my-report/
├── transcript.md
├── video-report/
│   ├── SKILL.md
│   ├── modes/
│   ├── references/
│   └── assets/
│       ├── report-template.html
│       └── brief-report-template.html
└── output/
```

If your Agent supports Skills, install the directory using its conventions. Other Coding Agents with local file access can read the entrypoint explicitly. Standalone use requires neither `input.json` nor `source.info.json`. When no video description is supplied, the Skill removes the description placeholder.

### 3. Ask the Agent to generate

Open your Coding Agent in that folder and send:

```text
Read video-report/SKILL.md and follow it to generate a reading report.
Use standard mode; read only its selected mode guide and template.
Read transcript.md in full and write self-contained HTML to output/report.html.
Preserve important conditions, numbers, formulas, attribution and uncertainty.
Do not modify the original transcript. Show chapter times only when reliable.
If browser tools are available, inspect the layout; otherwise state that
only static checks were performed.
```

Replace `standard` with `brief` for an overview. Open `output/report.html` in a browser. PNG export depends on your Agent's tools.

## Option 2: run the complete CLI pipeline

Start with a Bilibili URL and automate downloading, transcription and generation:

```text
Bilibili URL
  → yt-dlp download → FFmpeg audio extraction
  → ASR → canonical transcript (text, source IDs, times)
  → Pi RPC + video-report Skill → HTML
  → Chromium PNG export
```

Python handles deterministic processing and task supervision. Pi supplies model calls, file/shell tools, sessions and the Agent loop. The Skill defines content and visual presentation.

### Install

Use Python 3.12, uv, `ffmpeg` / `ffprobe` on PATH, and Node.js 22 with npm. Install from the source repository:

```sh
git clone https://github.com/imexlovery/video-report-agent.git
cd video-report-agent
npm install -g @earendil-works/pi-coding-agent@0.85.0
pi --version
uv sync
uv run playwright install chromium
test -f .env || cp .env.example .env
```

These instructions use Pi 0.85.0, the project's adapter baseline. `uv sync` does not install Pi. For local MLX Whisper on Apple Silicon, replace `uv sync` with `uv sync --extra mlx`.

### Configure the model and ASR

Edit the root `.env`, supply report-model credentials and choose one ASR backend. This example uses cloud transcription:

```dotenv
PI_PROVIDER=deepseek
PI_MODEL=deepseek-flash
PI_API_KEY=your-report-model-key
PI_TASK_ISOLATION=local

ASR_BACKEND=paraformer
ASR_MODEL=paraformer-v2
DASHSCOPE_API_KEY=your-dashscope-api-key
```

For local Apple Silicon transcription, replace the ASR settings with:

```dotenv
ASR_BACKEND=mlx
ASR_MODEL=mlx-community/whisper-large-v3-turbo
```

The pipeline includes `mlx` and `paraformer` backends, including the Fun-ASR file-transcription models adapted in the code. Other ASR tools work with the standalone Skill; integrating them into this pipeline requires an adapter. Cloud ASR uploads audio, and the report model receives the transcript content needed for generation.

### Generate and read

```sh
# Standard reading
uv run video-report generate 'https://www.bilibili.com/video/BV...'

# Concise overview
uv run video-report generate 'https://www.bilibili.com/video/BV...' --report-mode brief
```

Outputs live in `runs/<run-id>/`. Read `report.html` or `report.png`; use `transcript.md` for source review. HTML can complete even if image export fails, so check `image_error` in `status.json` before assuming the PNG exists.

The CLI defaults to creator-provided subtitles → Bilibili ai-zh → ASR and stops collecting once usable subtitles are available. Choose `--sources asr` for ASR alone, or list multiple sources for manual comparison/fusion. Web source controls are available only in Local advanced settings; Web OCR is disabled. The Core CLI retains legacy `--transcript-mode fused` subtitle/OCR support with `uv sync --extra enhancement`; include `--extra mlx` for local MLX. See the [source selection guide](docs/subtitle-sources.md) for subtitle login and transcript-only commands, or run `uv run video-report generate --help` for all options.

## Runtime and data

- **Storage:** standalone CLI uses `runs/state.sqlite3` by default for tasks, transcripts, report HTML and caches. Task-directory JSON, transcripts and HTML are execution exports; media, PNG, Pi sessions and diagnostic logs remain files. See [SQLite storage](docs/sqlite-storage.md).
- **Configuration:** `.env` is read from the working directory; process environment variables take precedence. Pi configuration lives in `config/pi/` there. Missing model defaults are initialized; existing configuration is preserved. Set `PI_CODING_AGENT_DIR` before startup to choose another directory. Do not commit credentials.
- **Isolation:** the example uses `PI_TASK_ISOLATION=local`; Pi tools have your local user permissions. Docker mode requires an application image, Docker daemon and healthy task cleaner with the same scope. It does not fall back to local execution on failure. See [Core architecture](docs/ARCHITECTURE.md).
- **Traces:** `run.trace.jsonl` records pipeline stages; `pi.events.jsonl` records model and tool events. Completion requires `agent_settled`, final assistant `stopReason == "stop"`, and HTML delivery checks.
- **Limits:** built-in ingestion targets public Bilibili videos, without login or private-video access. Videos are limited to five hours, execution to 30 minutes. Submitted cloud ASR may continue charging after a local stop. Default understanding comes from the transcript, so visual-only information may be missing. Native Windows execution of the full pipeline is not presented as verified here.

## Development and further reading

```sh
uv run --extra enhancement pytest -q
uv run ruff check src tests
uv lock --check
```

- [Project documentation](docs/README.md): scope, architecture, plans and decisions.
- [Report Skill entrypoint](src/video_report_agent/skills/video-report/SKILL.md): actual generation rules and reading order.
- [Report evaluation](evals/report-review/README.md): public synthetic smoke inputs and optional live-model evaluation. HTML/source-link checks, content fidelity and visual quality need separate assessment.

Callers use the packaged Skill by default. Experiments can select a complete directory through `PiRunner(skill_dir=...)` or the Worker environment variable `VIDEO_REPORT_SKILL_DIR`. Accounts, credits, queues and website deployment belong to the separate Web Service; public Core maintains generation capabilities.
