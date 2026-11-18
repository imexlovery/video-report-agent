# Video Report CLI

[简体中文](cli.md) | English · [Project README](../README.en.md)

Use a single-video URL or share text containing one link from Bilibili, YouTube, Douyin or Xiaohongshu. Export a transcript or continue to a Standard or Brief report:

```text
Video URL or share text
  → detect the platform and fetch usable subtitles, or extract and transcribe audio
  → canonical transcript (text, source IDs, times)
  → Pi RPC + video-report Skill → HTML
  → Chromium PNG export
```

Python handles deterministic processing and task supervision. Pi supplies model calls, file/shell tools, sessions and the Agent loop. The Skill defines content and visual presentation.

## Install

Use Python 3.12, uv, `ffmpeg` / `ffprobe` on PATH, and Node.js 22.19.0 or later with npm. Install from the source repository:

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

## Configure the model and ASR

Edit the root `.env` and supply report-model credentials. Automatic source selection falls back to DashScope Qwen Audio 3.1 Filetrans and reads `DASHSCOPE_API_KEY` when ASR is needed. The example `ASR_BACKEND` and `ASR_MODEL` configure manual source selection. The CLI also accepts explicit backend and model overrides:

```dotenv
PI_PROVIDER=deepseek
PI_MODEL=deepseek-flash
PI_API_KEY=your-report-model-key
PI_TASK_ISOLATION=local

ASR_BACKEND=paraformer
ASR_MODEL=paraformer-v2
DASHSCOPE_API_KEY=your-dashscope-api-key
```

For manual MLX transcription on Apple Silicon, replace the backend and model with:

```dotenv
ASR_BACKEND=mlx
ASR_MODEL=mlx-community/whisper-large-v3-turbo
```

The pipeline includes `mlx` and DashScope's `paraformer` backend. The DashScope backend supports Qwen Audio 3.1 Filetrans, Paraformer-v2, and the Fun-ASR file models listed in the code. Other ASR tools work with the standalone Skill. Cloud ASR uploads audio, and the report model receives the transcript content needed for generation.

## Generate and read

```sh
# Standard reading
uv run video-report generate 'https://www.bilibili.com/video/BV...'

# Concise overview
uv run video-report generate 'https://www.youtube.com/watch?v=VIDEO_ID' --report-mode brief

# Transcript only, without calling the report model
uv run video-report transcribe 'https://www.youtube.com/watch?v=VIDEO_ID'
```

Outputs live in `runs/<run-id>/`. `transcribe` writes `transcript.md`; `generate` continues to `report.html` and `report.png`. If image export fails, the HTML remains available and `status.json` records `image_error`.

## Choose subtitle and transcript sources

The CLI defaults to `auto`. Bilibili tries creator subtitles, then Bilibili AI subtitles (`ai-zh`). YouTube tries original-language creator subtitles, then original-language automatic captions. Douyin and Xiaohongshu prefer usable original-language platform subtitles. When no usable subtitles are available, the CLI extracts audio and defaults to DashScope Qwen Audio 3.1 Filetrans. Manual selection with `--sources` is available for Bilibili:

| Source | CLI arguments | Behavior |
|---|---|---|
| Creator subtitles | `--sources subtitle_track` | Use a non-AI subtitle track returned by Bilibili |
| Bilibili AI subtitles | `--sources bilibili_ai` | Fetch the platform's existing Chinese `ai-zh` subtitles |
| ASR | `--sources asr` | Download media and transcribe with the configured `mlx` or `paraformer` backend |
| Local subtitle file | `--sources subtitle_track --subtitle-file captions.srt` | Import SRT, VTT or ASS subtitles |
| Multiple sources | `--sources bilibili_ai asr` | Collect every selected source; the first usable source in argument order is primary, with the others used for local corrections |

```sh
# Use only Bilibili AI subtitles
uv run video-report generate 'https://www.bilibili.com/video/BV...' --sources bilibili_ai

# Export only the transcript for your own AI agent
uv run video-report transcribe 'https://www.bilibili.com/video/BV...' --sources bilibili_ai
```

Manual selection collects only the selected sources; the AI-only example fails if its subtitles are unavailable. Selecting both AI subtitles and ASR collects both. Subtitle-only runs require no media download or ASR credentials unless visual enhancement is enabled.

If manual Bilibili subtitle extraction requires authentication, set `BILIBILI_SUBTITLE_BROWSER=chrome` (or another browser supported by yt-dlp) in `.env` to use its existing Bilibili login, or supply `BILIBILI_SESSDATA` through the process environment. Browsers are not read by default. Keep credentials out of Git. See the [source selection guide](subtitle-sources.md).

The Protobuf field parsing for Bilibili's newer AI subtitle endpoint references [ccBilly-aipm/bilibili-ai-subtitle](https://github.com/ccBilly-aipm/bilibili-ai-subtitle) (MIT). Thank you to its author for publishing the extraction workflow and protocol implementation that informed this support.

## Unified source access

```sh
uv run video-report preflight 'VIDEO_URL_OR_SHARE_TEXT'
uv run video-report fetch-source 'VIDEO_URL_OR_SHARE_TEXT' --output .local/materials
```

`preflight` anonymously checks subtitle text or reads a small media range and returns `READY`, `LOGIN_REQUIRED` or `UNAVAILABLE`. `fetch-source` saves `source-material.json` in a per-video directory and extracts audio if no subtitles are available. It stops before ASR or report generation.

`transcribe` and `generate` use the same acquisition path in automatic mode. If the platform explicitly requires login, run `fetch-source`, `transcribe` or `generate` on your computer with `--browser edge` or `--cookies LOCAL_FILE`. Manual source selection and visual enhancement are available only for Bilibili. Embedded callers enable `source_access=True`; login additionally requires `source_login=True` and execution-only `VIDEO_SOURCE_BROWSER` / `VIDEO_SOURCE_COOKIE_FILE` settings. Credentials are not persisted in run metadata.

Local acquisition requires Core and FFmpeg. The acquired files stay on your computer. Bilibili supporter-only videos require the account's actual entitlement. Core rejects preview-only media. This path has yet to be tested with a real paid-video sample.

## Douyin and Xiaohongshu acquisition

```sh
uv run video-report fetch-douyin 'https://www.douyin.com/video/VIDEO_ID' --output .local/media
uv run video-report fetch-xiaohongshu 'FULL_SHARE_URL' --output .local/media
```

Both commands try anonymous access first. Multiple subtitle languages without a known original language fall back to audio; translated Chinese is not preferred by default. Available native subtitles are saved to timestamped `subtitles.json` without downloading media. Otherwise, audio is downloaded (or muxed media when no audio-only format exists) and converted to mono 16 kHz WAV. `media.json` records platform, author, source, and authentication mode. ASR, report generation, and web integration are not invoked.

Douyin initializes a visitor session and signs its detail request, following the [upstream fix discussion](https://github.com/yt-dlp/yt-dlp/pull/17826); no F2 dependency is needed. Xiaohongshu preserves share access parameters and reads subtitles from `mediaV2`. Expired links, signature failures, and absent subtitles do not trigger account login.

Only an explicit login-required response enables `--browser edge`, `--cookies LOCAL_FILE`, or the platform-specific `DOUYIN_COOKIE` / `XIAOHONGSHU_COOKIE` environment fallback. Run authenticated acquisition on the user's own computer and transfer only subtitle/audio artifacts. No service-side credential upload or account pool is provided.

When login is required, `uv run python scripts/export_platform_cookies.py xiaohongshu --browser edge` exports only that platform's cookies to `.local/platform-cookies/xiaohongshu.cookies.txt`. The script also supports `douyin`. Files use mode 0600, existing files are not overwritten, and cookie values are never printed. Keep these files local and delete them after use.

## Optional tools and capabilities

These switches apply to the complete Core pipeline. The standalone Skill ZIP uses your own Agent's tools and configuration.

| Tool / capability | Default | Enable or disable |
|---|---|---|
| Term correction | Off | Add `--term-correction` to enable; omit to disable. Python: `create_run(..., term_correction=True/False)` |
| Visual enhancement | Off | Add `--visual-enhancement` to enable; omit to disable. Python: `visual_enhancement=True/False` |
| Report inspection (`report_inspection`) | Off | Add `--report-inspection` to enable. Python: `create_run(..., report_inspection=True/False)` |
| Independent style editing (`style_edit`) | On | Use `--no-style-edit` to disable, `--style-edit` to enable. Python: `create_run(..., style_edit=True/False)` |
| Independent diagrams (`generate_diagram`) | On as needed for Standard; off for Brief | Python: `create_run(..., diagrams=False)` disables it for Standard. Brief does not currently offer it; no CLI switch |
| OCR (legacy fusion workflow) | Off | Use `--transcript-mode fused --ocr-mode auto` to enable, `--ocr-mode off` to disable. Requires the `enhancement` extra |

Visual enhancement obtains a video track, samples nine frames and uses the task's selected report model to describe them. It shares the report model's provider, model, thinking level and Pi authentication (API Key or OAuth), then passes the descriptions to the report Agent. No separate vision model or key is needed. The model must support images and declare `"input": ["text", "image"]` in its `config/pi/models.json` entry; missing image input produces an explicit error. Enabling this adds one multimodal request's latency and usage.

Standard allows at most one independent drawing tool call. The Agent requests a diagram when needed and can keep writing while it renders in the background. The drawing model can be configured separately as described below. Transient request failures receive at most one retry within the 180-second limit. If the diagram still cannot be embedded, its placeholder is removed and the otherwise complete report is delivered.

Report inspection gives the same Agent programmatic findings and available screenshots, allowing at most two inspections and one consolidated revision. When task inspection is disabled, the Agent finishes after the complete draft without running checks through Bash. Independent style editing runs before rendering and adds model usage; it does not invoke the inspector afterward. `REPORT_REVIEW` only supplies the default for a standalone `PiRunner`; complete tasks use their task parameters. Basic file/shell tools, HTML delivery checks and final PNG export are part of the regular workflow, with no corresponding CLI disable switches. Run `uv run video-report generate --help` for all options.

## Configure search for term correction

Term correction checks important names as questions arise during reading or writing and preserves the original transcript. `--term-correction` enables `record_term_review`, `lookup_term` and `fetch_term_source`. Search and page fetch share a limit of three requests per task and may incur charges. Choose one search provider in `.env`, independently of the report model:

```dotenv
# Option 1: DeepSeek Search/Fetch (default provider)
TERM_SEARCH_PROVIDER=deepseek
DEEPSEEK_SEARCH_API_KEY=your-official-deepseek-key
```

```dotenv
# Option 2: OpenAI Responses Web Search
TERM_SEARCH_PROVIDER=openai
OPENAI_SEARCH_API_KEY=your-openai-api-key
OPENAI_SEARCH_MODEL=gpt-6-luna
```

DeepSeek reads `DEEPSEEK_SEARCH_API_KEY`, falling back to `DEEPSEEK_API_KEY`. OpenAI reads `OPENAI_SEARCH_API_KEY`, falling back to `OPENAI_API_KEY`. Neither uses `PI_API_KEY` or `DASHSCOPE_API_KEY`; provide credentials for the corresponding official service. OpenAI's search model is independently configurable: the example explicitly selects `gpt-6-luna`; the code defaults to `gpt-6.1-sol` when `OPENAI_SEARCH_MODEL` is absent. Model tokens and search are billed separately.

After saving the configuration, add `--term-correction` to the generation command. Search configuration alone does not enable correction. Provider settings are saved on new tasks; restart a running service after changing `.env`. See [term correction](term-correction.md) for evidence rules and Docker configuration.

## Runtime and data

- **Storage:** standalone CLI uses `runs/state.sqlite3` by default for tasks, transcripts, report HTML and caches. Task-directory JSON, transcripts and HTML are execution exports; media, PNG, Pi sessions and diagnostic logs remain files. See [SQLite storage](sqlite-storage.md).
- **Configuration:** `.env` is read from the working directory; process environment variables take precedence. Pi configuration lives in `config/pi/` there. Missing model defaults are initialized; existing configuration is preserved. Set `PI_CODING_AGENT_DIR` before startup to choose another directory. Do not commit credentials.
- **Independent drawing model:** set both `DIAGRAM_PROVIDER` and `DIAGRAM_MODEL`, with optional `DIAGRAM_THINKING`, and configure that provider's API Key or OAuth in Pi. Leave them blank to inherit the report model and authentication. Changes apply to new tasks. OAuth requires Local execution; Docker drawing uses API Keys. See [Core architecture](ARCHITECTURE.md).
- **Isolation:** the example uses `PI_TASK_ISOLATION=local`; Pi tools have your local user permissions. Docker mode requires an application image, Docker daemon and healthy task cleaner with the same scope. It does not fall back to local execution on failure. See [Core architecture](ARCHITECTURE.md).
- **Traces:** `run.trace.jsonl` records pipeline stages; `pi.events.jsonl` records model and tool events. Completion requires `agent_settled`, final assistant `stopReason == "stop"`, and HTML delivery checks.
- **Limits:** built-in ingestion accepts single videos from the four supported platforms and tries anonymous access first. Availability depends on the platform, link and account permissions. Videos are limited to five hours, execution to 30 minutes. Submitted cloud ASR may continue charging after a local stop. Default understanding comes from the transcript, so visual-only information may be missing. Native Windows execution of the full pipeline is not presented as verified here.

## Development and integration

```sh
uv run --extra enhancement pytest -q
uv run ruff check src tests
uv lock --check
```

Callers use the packaged Skill by default. Experiments can select a complete directory through `PiRunner(skill_dir=...)` or the Worker environment variable `VIDEO_REPORT_SKILL_DIR`. Accounts, credits, queues and website deployment belong to the separate Web Service; public Core maintains generation capabilities.
