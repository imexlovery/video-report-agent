# Video Report CLI 使用指南

简体中文 | [English](cli.en.md) · [项目首页](../README.md)

支持 B 站、YouTube、抖音和小红书的单视频链接或含单个链接的分享文字。可以只生成转写，也可以生成 Standard 或 Brief 报告：

```text
视频链接或分享文字
  → 自动识别平台，优先获取可用字幕；没有可用字幕时提取音频并转写
  → 规范转写（正文、来源 ID、时间）
  → Pi RPC + video-report Skill → HTML
  → Chromium 导出 PNG 长图
```

Python 负责确定性的处理步骤与任务监督；Pi 提供模型调用、文件/命令工具、会话与 Agent 循环；Skill 决定内容和视觉表达。

## 安装

需要 Python 3.12、uv、PATH 中的 `ffmpeg` / `ffprobe`，以及 Node.js 22.19.0 或更新版本及 npm。从源码仓库安装：

```sh
git clone https://github.com/imexlovery/video-report-agent.git
cd video-report-agent
npm install -g @earendil-works/pi-coding-agent@0.85.0
pi --version
uv sync
uv run playwright install chromium
test -f .env || cp .env.example .env
```

这里使用项目适配的 Pi 0.85.0。`uv sync` 不会安装 Pi。Apple Silicon 如使用本地 MLX Whisper，将 `uv sync` 换成 `uv sync --extra mlx`。

## 配置模型与转写

编辑仓库根目录的 `.env`，填写报告模型凭证。自动选源的默认回退使用百炼 Qwen Audio 3.1 Filetrans，进入 ASR 时读取 `DASHSCOPE_API_KEY`。以下示例的 `ASR_BACKEND` 和 `ASR_MODEL` 用于手动选源，也可通过 CLI 显式指定 ASR 后端和模型：

```dotenv
PI_PROVIDER=deepseek
PI_MODEL=deepseek-flash
PI_API_KEY=your-report-model-key
PI_TASK_ISOLATION=local

ASR_BACKEND=paraformer
ASR_MODEL=paraformer-v2
DASHSCOPE_API_KEY=your-dashscope-api-key
```

Apple Silicon 手动使用本地转写时，可将上述 ASR 后端和模型替换为：

```dotenv
ASR_BACKEND=mlx
ASR_MODEL=mlx-community/whisper-large-v3-turbo
```

流水线内置 `mlx` 与百炼 `paraformer` 后端。百炼后端支持 Qwen Audio 3.1 Filetrans、Paraformer-v2 和代码列出的 Fun-ASR 文件转写模型。其他 ASR 可用于独立 Skill 流程。云端 ASR 会上传音频，报告模型会接收生成所需的转写内容。

## 生成与查看

```sh
# 标准阅读
uv run video-report generate 'https://www.bilibili.com/video/BV...'

# 精简速览
uv run video-report generate 'https://www.youtube.com/watch?v=VIDEO_ID' --report-mode brief

# 只生成转写，不调用报告模型
uv run video-report transcribe 'https://www.youtube.com/watch?v=VIDEO_ID'
```

输出目录为 `runs/<run-id>/`。`transcribe` 交付 `transcript.md`；`generate` 继续生成 `report.html` 和 `report.png`。截图失败时 HTML 仍可完成，可查看 `status.json` 的 `image_error`。

## 选择字幕与转写来源

CLI 默认使用 `auto`。B 站依次尝试作者字幕、B 站 AI 自动字幕 `ai-zh`；YouTube 依次尝试原语言作者字幕、原语言自动字幕；抖音和小红书优先使用可用的原语言平台字幕。没有可用字幕时才提取音频，默认用百炼 Qwen Audio 3.1 Filetrans 转写。仅 B 站支持通过 `--sources` 手动选源：

| 来源 | CLI 参数 | 说明 |
|---|---|---|
| 作者字幕 | `--sources subtitle_track` | 使用 B站返回的非 AI 字幕轨 |
| B站 AI 自动字幕 | `--sources bilibili_ai` | 直接获取平台已有的中文 `ai-zh` 字幕 |
| ASR 语音识别 | `--sources asr` | 下载媒体并使用配置的 `mlx` 或 `paraformer` 后端 |
| 本地字幕文件 | `--sources subtitle_track --subtitle-file captions.srt` | 导入已有 SRT、VTT 或 ASS 字幕 |
| 多路对照 | `--sources bilibili_ai asr` | 采集所有选中来源，按参数顺序选择首个可用主来源，其余参与局部校对 |

```sh
# 只使用 B站 AI 自动字幕
uv run video-report generate 'https://www.bilibili.com/video/BV...' --sources bilibili_ai

# 只生成文字稿，后续交给自己的 Coding Agent
uv run video-report transcribe 'https://www.bilibili.com/video/BV...' --sources bilibili_ai
```

手动选源只采集选中的来源；上例的 AI 字幕不可用时会报错。多路对照会同时采集 AI 字幕和 ASR。仅选字幕且未开启画面辅助时，不下载媒体，也不要求 ASR 凭证。

手动提取 B 站字幕需要登录态时，在 `.env` 中设置 `BILIBILI_SUBTITLE_BROWSER=chrome`（或 yt-dlp 支持的其他浏览器），读取该浏览器已有的 B 站登录态；也可通过进程环境提供 `BILIBILI_SESSDATA`。默认不读取浏览器，凭据不要提交到 Git。详细规则见[选源说明](subtitle-sources.md)。

B站 AI 自动字幕的新接口 Protobuf 字段解析参考了 [ccBilly-aipm/bilibili-ai-subtitle](https://github.com/ccBilly-aipm/bilibili-ai-subtitle)（MIT）。感谢作者公开字幕提取流程与协议解析实现，为本项目的 AI 字幕支持提供了参考。

## 统一链接检查与用户电脑取材

```sh
uv run video-report preflight '完整视频链接或分享文字'
uv run video-report fetch-source '完整视频链接或分享文字' --output .local/materials
```

支持 B 站 BV／分 P／短链／移动链接、YouTube watch／Shorts／youtu.be、抖音分享链接和小红书完整分享链接／短链。自动识别平台，只接受单视频；小红书保留访问所需分享参数。`preflight` 始终匿名，验证字幕正文或读取媒体地址的少量字节，返回 `READY`、`LOGIN_REQUIRED` 或 `UNAVAILABLE`；不会执行 ASR、报告或整段下载。

`fetch-source` 在每个视频子目录保存 `source-material.json`，无字幕时下载媒体并提取音频，止于取材。`transcribe`／`generate` 的自动选源使用同一入口。平台明确要求登录时，可在本机为这三个命令加 `--browser edge` 或 `--cookies 本机文件`。手动选源和画面增强仅支持 B 站。嵌入调用可用 `create_run(..., transcript_mode="auto", source_access=True)`；账号登录须另显式启用 `source_login=True`，并通过执行环境传入 `VIDEO_SOURCE_BROWSER` 或 `VIDEO_SOURCE_COOKIE_FILE`，不把凭据值写入任务。

本机取材需要安装 Core 和 FFmpeg。取得的素材保存在用户电脑。

B站充电内容需要账号具有对应视频权限。检测到试看时停止，不把片段当成完整视频；该路径尚无真实付费样本验证，不承诺所有充电视频都可获取。

## 抖音与小红书取材

```sh
uv run video-report fetch-douyin 'https://www.douyin.com/video/VIDEO_ID' --output .local/media
uv run video-report fetch-xiaohongshu '完整小红书分享链接' --output .local/media
```

两者默认匿名访问，优先取得平台返回的原语言字幕；多轨但无法确认原语言时改取音频，不默认选择中文译文。可用字幕保存为带时间戳的 `subtitles.json`，不再下载媒体。没有可用字幕时优先下载音轨；平台只提供音视频合流时下载合流，再提取 16 kHz 单声道 WAV。`media.json` 保存作者、实际平台、来源与匿名／登录模式。入口不运行 ASR、报告或网站任务。

抖音自动建立访客会话，补齐 UIFID 与 `x-secsdk-web-signature`，继续复用 yt-dlp 下载；协议参考 [上游修复讨论](https://github.com/yt-dlp/yt-dlp/pull/17826)，无需 F2。小红书接受分享文字与 `xhslink.com` 短链，保留必要的 `xsec_token`／`xsec_source` 分享参数，从页面 `mediaV2` 读取字幕。链接失效、签名拒绝或字幕不存在不会自动触发账号登录。

只有平台明确返回登录要求时，才使用显式提供的 `--browser edge`、`--cookies 本机文件` 或对应平台环境变量 `DOUYIN_COOKIE`／`XIAOHONGSHU_COOKIE`。即使配置了它们，也先匿名尝试。浏览器与文件在运行命令的用户电脑读取，Cookie 不上传服务端、不写入取材产物或日志。建议需要登录的取材在用户本机完成，只交付字幕／音频。

需要单独准备本机登录态时，可运行备用脚本；不要提前为公开内容导出 Cookie：

```sh
uv run python scripts/export_platform_cookies.py xiaohongshu --browser edge
# 仅登录受限时读取；仍先匿名尝试
uv run video-report fetch-xiaohongshu '完整分享链接' --output .local/media \
  --cookies .local/platform-cookies/xiaohongshu.cookies.txt
```

脚本也支持 `douyin`，仅保留该平台未过期 Cookie，默认保存到已忽略的 `.local/platform-cookies/`，文件权限为 0600，不覆盖已有文件、不输出 Cookie 内容。文件应留在用户本机，用完后删除；本轮没有服务端 Cookie 上传或集中存储功能。平台接口变化可能需要更新适配。

## 报告流程中可选的工具与能力

以下是完整 Core 流水线的开关。独立 Skill ZIP 使用你自己的 Agent 工具与配置。

| 工具／能力 | 默认状态 | 如何开启或关闭 |
|---|---|---|
| 术语纠错 `term_correction` | 关闭 | CLI 添加 `--term-correction` 开启，省略即关闭；Python 用 `create_run(..., term_correction=True/False)` |
| 画面辅助理解 | 关闭 | CLI 添加 `--visual-enhancement` 开启，省略即关闭；Python 用 `visual_enhancement=True/False` |
| 报告检查 `report_inspection` | 关闭 | CLI `--report-inspection` 开启；Python `create_run(..., report_inspection=True/False)` |
| 独立文风校订 `style_edit` | 开启 | CLI `--no-style-edit` 关闭、`--style-edit` 开启；Python `create_run(..., style_edit=True/False)` |
| 独立图示 `generate_diagram` | Standard 开启，Agent 按需调用；Brief 关闭 | Standard 可用 Python `create_run(..., diagrams=False)` 关闭；Brief 暂不提供此功能；当前没有 CLI 开关 |
| OCR 画面文字识别（旧融合流程） | 关闭 | CLI 用 `--transcript-mode fused --ocr-mode auto` 开启，`--ocr-mode off` 关闭；需安装 `enhancement` 扩展 |

画面辅助会额外取得视频轨、抽取 9 帧，使用本次任务选定的主报告模型生成画面描述，再交给报告 Agent。它沿用主模型的 provider、model、思考档位与 Pi 认证（API Key 或 OAuth），无需单独的视觉模型或 Key。主模型须实际支持图片，并在 `config/pi/models.json` 对应模型中声明 `"input": ["text", "image"]`；未声明图片输入时会明确报错。开启后会增加一次图文模型请求的耗时与用量。

Standard 的独立图示工具最多调用一次，可通过下方的独立绘图模型配置指定模型。Agent 按需发起绘图，图示在后台生成，正文可以继续撰写。绘图遇到可恢复的请求错误时最多重试一次，共用 180 秒时限；仍无法嵌入时清除占位符，交付已正常完成的正文。

报告检查工具向同一 Agent 返回程序检查结果与可用截图，最多检查两次、集中修订一次。任务关闭报告检查时，写完初稿直接结束，不通过 Bash 运行检查器。文风校订独立可选，校订后不再运行检查器。`REPORT_REVIEW` 仅作为独立构造 `PiRunner` 时的默认值，完整任务以任务参数为准。基础读写／命令工具、HTML 交付检查和最终 PNG 导出属于常规流程，当前没有对应 CLI 关闭参数。完整参数见 `uv run video-report generate --help`。

## 术语纠错的搜索服务配置

术语纠错在阅读或写作遇到重要名称疑点时按需核对，保留原始转写。开启 `--term-correction` 后，Agent 可使用 `record_term_review`、`lookup_term`、`fetch_term_source`；搜索与网页抓取每任务合计最多 3 次，按需产生费用。搜索服务独立于报告模型，通过 `.env` 二选一配置：

```dotenv
# 方案一：DeepSeek Search/Fetch（默认供应商）
TERM_SEARCH_PROVIDER=deepseek
DEEPSEEK_SEARCH_API_KEY=your-official-deepseek-key
```

```dotenv
# 方案二：OpenAI Responses Web Search
TERM_SEARCH_PROVIDER=openai
OPENAI_SEARCH_API_KEY=your-openai-api-key
OPENAI_SEARCH_MODEL=gpt-6-luna
```

DeepSeek 优先读取 `DEEPSEEK_SEARCH_API_KEY`，留空时使用 `DEEPSEEK_API_KEY`；OpenAI 优先读取 `OPENAI_SEARCH_API_KEY`，留空时使用 `OPENAI_API_KEY`。两者均不读取 `PI_API_KEY` 或 `DASHSCOPE_API_KEY`，需提供对应官方服务凭据。OpenAI 的搜索模型可单独选择；示例显式使用 `gpt-6-luna`，未配置 `OPENAI_SEARCH_MODEL` 时代码默认 `gpt-6.1-sol`，模型和搜索分别计费。

保存配置后，为生成命令添加 `--term-correction` 即可启用；仅填写搜索配置不会自动开启纠错。配置保存到新任务，运行中的服务修改 `.env` 后需重启。详细证据规则与 Docker 配置见[术语修正说明](term-correction.md)。

## 运行与数据约定

- **存储**：独立 CLI 默认使用 `runs/state.sqlite3`，保存任务、转写、报告 HTML 与缓存等结构化数据。任务目录里的 JSON、转写和 HTML 是执行导出；媒体、PNG、Pi 会话和诊断日志仍存文件。详见 [SQLite 存储](sqlite-storage.md)。
- **配置**：从工作目录读取 `.env`，进程环境变量优先。Pi 配置保存在工作目录的 `config/pi/`，缺失的模型默认配置会初始化，已有配置保留。其他目录通过启动前的 `PI_CODING_AGENT_DIR` 指定。不要提交凭证。
- **独立绘图模型**：可同时设置 `DIAGRAM_PROVIDER`、`DIAGRAM_MODEL`，可选 `DIAGRAM_THINKING`；在 Pi 中配置对应 provider 的 API Key 或 OAuth。留空时沿用主报告模型和认证，新配置只影响新任务。OAuth 需要无隔离 Local；Docker 绘图使用 API Key。见 [Core 架构](ARCHITECTURE.md#完成与检查)。
- **隔离**：示例使用 `PI_TASK_ISOLATION=local`，Pi 工具拥有本机用户权限。Docker 模式需要应用镜像、Docker daemon 与同一 scope 的健康 task cleaner；选用后失败不会退回本地执行。详见 [Core 架构](ARCHITECTURE.md)。
- **追踪**：`run.trace.jsonl` 记录流水线阶段，`pi.events.jsonl` 记录 Agent 模型与工具事件。完成需等待 `agent_settled`、最终 assistant `stopReason == "stop"`，并通过 HTML 交付检查。
- **边界**：内置入口只接受上述四个平台的单视频链接，优先匿名取材，能否访问受平台、链接和账号权限影响。视频上限 5 小时，执行期限 30 分钟；本地停止后已提交的云 ASR 仍可能继续计费。默认只理解转写，画面中的非语音信息可能缺失。完整流水线的原生 Windows 运行未在此作为已验证方案。

## 开发与集成

```sh
uv run --extra enhancement pytest -q
uv run ruff check src tests
uv lock --check
```

调用方默认使用包内 Skill；实验时可通过 `PiRunner(skill_dir=...)` 或 Worker 环境变量 `VIDEO_REPORT_SKILL_DIR` 选择完整 Skill 目录。账户、额度、队列和网站部署属于独立 Web Service，公开 Core 只维护生成能力。
