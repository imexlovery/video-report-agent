# Video Report Agent · 公开核心

简体中文 | [English](README.en.md)

把视频转写整理成可以扫读、精读和保存的 HTML 阅读报告。可以直接在 [VReport 网站](https://vreport.tri4t.xyz) 使用，也可以把本仓库的 Skill 和模板交给自己的 Coding Agent，在本地生成报告。

**本仓库的核心资产是 [`video-report` 模板 Skill](src/video_report_agent/skills/video-report/)**：它把内容编辑规则、阅读模式、视觉模板和来源约束放在一起，指导 Agent 从完整转写中组织一份可读的报告。Python 流水线负责下载、转写、运行 Agent 和导出长图；网站、账户和额度由独立的 Web Service 维护。

| 你想做什么 | 从这里开始 |
|---|---|
| 在线生成报告 | [打开 VReport](https://vreport.tri4t.xyz) |
| 把已有转写交给自己的 Coding Agent | [下载 Skill ZIP v0.5.1](https://github.com/imexlovery/video-report-agent/releases/download/skill-zip-v0.5.1/video-report-skill-v0.5.1.zip) · [简短使用说明与示例](docs/skill-bundle/README.md) |
| 从视频链接运行完整流程 | [完整 CLI 安装说明](#方式二运行完整-cli-流水线) |

## 先看成品

下面是示例报告的顶部，点击图片查看完整长图。

[![阅读报告预览](docs/images/report-preview.png)](docs/examples/report.png)

[示例 HTML](docs/examples/report.html) · [完整 PNG](docs/examples/report.png) · [在线使用](https://vreport.tri4t.xyz)

GitHub 上的 HTML 链接会打开源码页，下载后用浏览器打开即可阅读。示例随仓库提供，无需启动服务。

## 模板 Skill 如何工作

输入可以是 Markdown、TXT、SRT 等完整转写，也可以是其他长篇文本。标题、作者、链接和时间戳属于可选信息。Agent 先理解完整来源，再按内容关系组织章节，使用选定模式的 HTML 模板完成写作、图示和修订。

Skill 包含四部分，使用时需要保留整个目录：

| 资产 | 作用 |
|---|---|
| [`SKILL.md`](src/video_report_agent/skills/video-report/SKILL.md) | 共用入口：来源忠实、内容组织、推广排除、语言与交付规则 |
| [`modes/`](src/video_report_agent/skills/video-report/modes/) | Standard / Brief 的内容取舍、阅读深度和组件约定 |
| [`assets/`](src/video_report_agent/skills/video-report/assets/) | 两种模式各自的 HTML 模板，提供内联样式与视觉组件 |
| [`references/`](src/video_report_agent/skills/video-report/references/) | 机制、步骤、实证、论证、叙事五类编辑参考，按需读取 |

模板提供排版与表达工具，章节按来源内容组织。机制适合关系图，步骤适合流程与条件分支，实证适合数据和证据对照；Agent 选择一个主要编辑方向，必要时补充一个参考。模式、模板和参考按需加载，在同一个 Agent 任务中完成理解、写作与修订。

| 阅读模式 | 适合什么用途 | 表达方式 |
|---|---|---|
| `standard` · 标准阅读（默认） | 理解完整主线，保留必要解释、依据和例子 | 按有效信息量调整篇幅；长文可带章节导航和可靠来源时间 |
| `brief` · 精简速览 | 先抓核心问题、主要判断与决定性条件 | 独立速览模板，视觉表达优先；正文目标 1600–2200 字、上限 2400 字，短来源可更短 |

两种模式都先读完整来源，保留关键数字口径、条件、归属与不确定性。Skill 要求识别并排除商业推广，正常教程和实测中的有效内容仍需保留。报告使用隐藏的 `data-source-units` 绑定来源位置；这方便核对，但不能单凭绑定证明内容正确。

最终交付是内联样式的自包含 `report.html`，可直接用浏览器打开。质量取决于转写、模型与实际检查；Skill 不包含 ASR 引擎，也不替用户配置模型。

## 方式一：交给自己的 Coding Agent

这是复用核心资产最直接的方式，无需安装本项目的 Python 后端、Pi 或网站。

### 1. 准备完整转写

可以使用作者字幕、B站 AI 自动字幕、自己的 ASR 转写或其他已有完整文本，保存为 `transcript.md`、TXT 或 SRT。尽量保留时间戳和说话人；缺少可靠时间时，报告会省略相应章节时间。

ASR 负责把语音变成文字，Coding Agent 的模型负责阅读和生成报告，两者可以独立选择。

### 2. 获取完整 Skill 目录

下载 [Skill ZIP v0.5.1](https://github.com/imexlovery/video-report-agent/releases/download/skill-zip-v0.5.1/video-report-skill-v0.5.1.zip) 并解压。包内有完整 Skill、简短说明，以及配套的转写和 HTML 示例；可以先按包内 `README.md` 试用。

需要源码时，也可以克隆仓库：

```sh
git clone https://github.com/imexlovery/video-report-agent.git
```

使用下载包中的 `video-report/`，或从源码复制完整的 `src/video_report_agent/skills/video-report/`，保持相对路径：

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

支持 Skills 的 Agent 可以按自身约定安装这个目录；其他能读写本地文件的 Coding Agent 可以直接读取入口。独立使用不要求 `input.json` 或 `source.info.json`。未提供视频简介时，Skill 会移除模板里的简介占位符。

### 3. 发送生成指令

在上述目录打开 Coding Agent，发送：

```text
读取 video-report/SKILL.md，按其要求生成阅读报告。
使用 standard 模式，只读取该模式指定的模式文件和模板。
完整阅读 transcript.md，将自包含 HTML 写入 output/report.html。
保留重要条件、数字、公式、人物归属与不确定性，不修改原始转写。
只有来源时间可靠时才显示章节时间。
如有浏览器工具，检查页面布局；否则说明仅完成静态检查。
```

需要速览时，把 `standard` 改成 `brief`。完成后用浏览器打开 `output/report.html`；PNG 导出取决于你的 Agent 是否具备相应工具。

## 方式二：运行完整 CLI 流水线

适合从 Bilibili 视频链接开始，自动获取文字稿并生成报告：

```text
Bilibili URL
  → 获取文字稿：作者字幕 → B站 AI 自动字幕 → ASR（默认依次尝试）
      仅需 ASR 时：yt-dlp 下载 → FFmpeg 提取音频 → 语音识别
  → 规范转写（正文、来源 ID、时间）
  → Pi RPC + video-report Skill → HTML
  → Chromium 导出 PNG 长图
```

Python 负责确定性的处理步骤与任务监督；Pi 提供模型调用、文件/命令工具、会话与 Agent 循环；Skill 决定内容和视觉表达。

### 安装

需要 Python 3.12、uv、PATH 中的 `ffmpeg` / `ffprobe`，以及 Node.js 22 和 npm。从源码仓库安装：

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

### 配置模型与转写

编辑仓库根目录的 `.env`，填写报告模型凭证。使用 ASR 时再配置相应后端；以下示例配置云端 ASR，供自动路由回退或手动选用：

```dotenv
PI_PROVIDER=deepseek
PI_MODEL=deepseek-flash
PI_API_KEY=your-report-model-key
PI_TASK_ISOLATION=local

ASR_BACKEND=paraformer
ASR_MODEL=paraformer-v2
DASHSCOPE_API_KEY=your-dashscope-api-key
```

Apple Silicon 本地转写替换为：

```dotenv
ASR_BACKEND=mlx
ASR_MODEL=mlx-community/whisper-large-v3-turbo
```

流水线内置 `mlx` 与 `paraformer` 后端，云端还支持代码已适配的 Fun-ASR 文件转写模型。其他 ASR 可用于独立 Skill 流程；接入本流水线需要对应适配器。云端 ASR 会上传音频，报告模型会接收生成所需的转写内容。

### 生成与查看

```sh
# 标准阅读
uv run video-report generate 'https://www.bilibili.com/video/BV...'

# 精简速览
uv run video-report generate 'https://www.bilibili.com/video/BV...' --report-mode brief
```

输出目录为 `runs/<run-id>/`，主要阅读产物是 `report.html` 和 `report.png`；`transcript.md` 可用于来源核对。截图失败时 HTML 仍可完成，查看 `status.json` 的 `image_error`，确认 PNG 是否生成。

### 选择字幕与转写来源

CLI 默认使用 `auto`，按“作者字幕轨 → B站 AI 自动字幕 `ai-zh` → ASR”依次尝试。取得可用字幕正文后就进入报告生成；只有前两路不可用时才运行 ASR。也可以通过 `--sources` 主动选择：

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

字幕需要登录态时，在 `.env` 中设置 `BILIBILI_SUBTITLE_BROWSER=chrome`（或 yt-dlp 支持的其他浏览器），读取该浏览器已有的 B站登录态；也可通过进程环境提供 `BILIBILI_SESSDATA`。默认不读取浏览器，凭据不要提交到 Git。导入字幕需要 `uv sync --extra enhancement`。详细规则见[选源说明](docs/subtitle-sources.md)。

B站 AI 自动字幕的新接口 Protobuf 字段解析参考了 [ccBilly-aipm/bilibili-ai-subtitle](https://github.com/ccBilly-aipm/bilibili-ai-subtitle)（MIT）。感谢作者公开字幕提取流程与协议解析实现，为本项目的 AI 字幕支持提供了参考。

### 报告流程中可选的工具与能力

以下是完整 Core 流水线的开关。独立 Skill ZIP 使用你自己的 Agent 工具与配置。

| 工具／能力 | 默认状态 | 如何开启或关闭 |
|---|---|---|
| 术语纠错 `term_correction` | 关闭 | CLI 添加 `--term-correction` 开启，省略即关闭；Python 用 `create_run(..., term_correction=True/False)` |
| 画面辅助理解 | 关闭 | CLI 添加 `--visual-enhancement` 开启，省略即关闭；Python 用 `visual_enhancement=True/False` |
| 报告检查 `inspect_report` | 专用工具关闭 | `.env` 设置 `REPORT_REVIEW=1` 开启，`0` 关闭；Python 可用 `PiRunner(review=True/False)` |
| 独立图示 `generate_diagram` | 开启，Agent 按需调用 | Python 用 `create_run(..., diagrams=False)` 关闭，`True` 开启；当前没有 CLI 开关 |
| OCR 画面文字识别（旧融合流程） | 关闭 | CLI 用 `--transcript-mode fused --ocr-mode auto` 开启，`--ocr-mode off` 关闭；需安装 `enhancement` 扩展 |

画面辅助会额外取得视频轨、抽取 9 帧，使用本次任务选定的主报告模型生成画面描述，再交给报告 Agent。它沿用主模型的 provider、model、思考档位与 Pi 认证（API Key 或 OAuth），无需单独的视觉模型或 Key。主模型须实际支持图片，并在 `config/pi/models.json` 对应模型中声明 `"input": ["text", "image"]`；未声明图片输入时会明确报错。开启后会增加一次图文模型请求的耗时与用量。

独立图示最多调用一次，可通过下方的独立绘图模型配置指定模型；开启时也可由 Agent 判断无需绘图。

报告检查工具向同一 Agent 返回程序检查结果与可用截图，最多检查两次、集中修订一次。`REPORT_REVIEW=0` 关闭专用工具；Skill 仍要求集中自检，也可以通过 Bash 调用检查器。基础读写／命令工具、HTML 交付检查和最终 PNG 导出属于常规流程，当前没有对应 CLI 关闭参数。完整参数见 `uv run video-report generate --help`。

### 术语纠错的搜索服务配置

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

保存配置后，为生成命令添加 `--term-correction` 即可启用；仅填写搜索配置不会自动开启纠错。配置保存到新任务，运行中的服务修改 `.env` 后需重启。详细证据规则与 Docker 配置见[术语修正说明](docs/term-correction.md)。

## 运行与数据约定

- **存储**：独立 CLI 默认使用 `runs/state.sqlite3`，保存任务、转写、报告 HTML 与缓存等结构化数据。任务目录里的 JSON、转写和 HTML 是执行导出；媒体、PNG、Pi 会话和诊断日志仍存文件。详见 [SQLite 存储](docs/sqlite-storage.md)。
- **配置**：从工作目录读取 `.env`，进程环境变量优先。Pi 配置保存在工作目录的 `config/pi/`，缺失的模型默认配置会初始化，已有配置保留。其他目录通过启动前的 `PI_CODING_AGENT_DIR` 指定。不要提交凭证。
- **独立绘图模型**：可同时设置 `DIAGRAM_PROVIDER`、`DIAGRAM_MODEL`，可选 `DIAGRAM_THINKING`；在 Pi 中配置对应 provider 的 API Key 或 OAuth。留空时沿用主报告模型和认证，新配置只影响新任务。OAuth 需要无隔离 Local；Docker 绘图使用 API Key。见 [Core 架构](docs/ARCHITECTURE.md#完成与检查)。
- **隔离**：示例使用 `PI_TASK_ISOLATION=local`，Pi 工具拥有本机用户权限。Docker 模式需要应用镜像、Docker daemon 与同一 scope 的健康 task cleaner；选用后失败不会退回本地执行。详见 [Core 架构](docs/ARCHITECTURE.md)。
- **追踪**：`run.trace.jsonl` 记录流水线阶段，`pi.events.jsonl` 记录 Agent 模型与工具事件。完成需等待 `agent_settled`、最终 assistant `stopReason == "stop"`，并通过 HTML 交付检查。
- **边界**：内置视频入口面向公开 Bilibili 视频；字幕提取可使用已有登录态，私有视频不在支持范围。视频上限 5 小时，执行期限 30 分钟；本地停止后已提交的云 ASR 仍可能继续计费。默认只理解转写，画面中的非语音信息可能缺失。完整流水线的原生 Windows 运行未在此作为已验证方案。

## 开发与进一步阅读

```sh
uv run --extra enhancement pytest -q
uv run ruff check src tests
uv lock --check
```

- [项目文档](docs/README.md)：目标、架构、计划与重要取舍。
- [报告 Skill 入口](src/video_report_agent/skills/video-report/SKILL.md)：实际生成规则与阅读顺序。
- [报告评测](evals/report-review/README.md)：公开合成 smoke 与可选真实模型评测。HTML/来源绑定检查和内容、视觉质量需分别判断。

调用方默认使用包内 Skill；实验时可通过 `PiRunner(skill_dir=...)` 或 Worker 环境变量 `VIDEO_REPORT_SKILL_DIR` 选择完整 Skill 目录。账户、额度、队列和网站部署属于独立 Web Service，公开 Core 只维护生成能力。
