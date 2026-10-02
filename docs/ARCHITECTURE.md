# Core 当前架构

> 核对日期：2026-09-24；源码基线 `428f4fe`。运行参数以调用方环境和任务保存的输入为准。

## 模块与执行流程

`CLI / 调用方 → execution → 独立 Python Worker → pipeline → Pi RPC → HTML → Chromium PNG`

| 模块 | 当前职责 |
|---|---|
| [cli.py](../src/video_report_agent/cli.py)、[execution.py](../src/video_report_agent/execution.py) | CLI 参数、Worker 进程组、期限与取消监督 |
| [pipeline.py](../src/video_report_agent/pipeline.py) | 创建输入和状态，顺序组织下载、转写、生成、截图与错误落盘 |
| [ingest.py](../src/video_report_agent/ingest.py)、[audio.py](../src/video_report_agent/audio.py) | Bilibili 来源验证、所选分 P 元数据预查询、下载、16 kHz 单声道音频、时长与静音检测 |
| [asr.py](../src/video_report_agent/asr.py)、[paraformer.py](../src/video_report_agent/paraformer.py) | 本地/云端 ASR、云任务轮询、两段结果与时间戳合并 |
| [transcript.py](../src/video_report_agent/transcript.py)、[transcript_foundation.py](../src/video_report_agent/transcript_foundation.py) | 来源事件、规范单元、可选字幕/OCR 融合 |
| [pi.py](../src/video_report_agent/pi.py)、[产品 Skill](../src/video_report_agent/skills/video-report/SKILL.md) | 选择模式资产、Pi RPC 和完成判断；Skill 规定内容与表达 |
| [report_content.py](../src/video_report_agent/report_content.py)、[report_image.py](../src/video_report_agent/report_image.py) | 原始简介处理、HTML 后处理与长图导出 |
| [reuse.py](../src/video_report_agent/reuse.py)、[retention.py](../src/video_report_agent/retention.py) | 兼容输入复用与媒体清理 |
| [trace.py](../src/video_report_agent/trace.py)、[usage.py](../src/video_report_agent/usage.py) | 阶段事件、调用用量和费用估算缓存 |
| [inspect_report.py](../src/video_report_agent/inspect_report.py)、[evaluate_report.py](../src/video_report_agent/evaluate_report.py) | 程序检查与受控比较；不提供自动语义验收 |

生成先尝试完整转写复用，否则检查媒体与 ASR 复用；随后构建完整转写供 Agent 阅读。单任务主链路顺序执行，长音频 ASR 的两段并发不改变单 Agent 架构。没有向量检索或多 Agent 调度。

## 任务数据与接口

`create_run` 保存 `input.json`（来源、ASR 配置、报告模式、模型选择）与初始 `status.json`；`generate(run)` 执行管线，外层 execution 监督 Worker。

每个 `runs/<run-id>/` 保存媒体、`asr.json`、`source-text-events.jsonl`、`canonical-transcript.jsonl`、`transcript-manifest.json`、`transcript.md`、所选 Skill 资产、Pi 会话与最终报告。条件路径和失败任务不会拥有所有产物。 终态任务的原始媒体和转换音频默认按状态文件修改时间滚动保留七天（`MEDIA_KEEP_LAST=0`、`MEDIA_MAX_AGE_DAYS=7`），在现有清理时机删除过期媒体；运行中任务不清理。完整 ASR、整套转写及输入配置长期保留。

ASR 片段编号保留原始来源位置，过滤空白或无效片段后允许间断；复用检查要求编号严格递增，不要求从零连续编号。

规范单元具有 ID、时间范围与来源关系，`transcript.md` 为模型可读视图；报告的 `data-source-units` 用来定位来源，不能单凭绑定合法判断内容忠实。

`run.trace.jsonl` 记录下载、FFmpeg、ASR、转写、Agent、截图阶段；`pi.events.jsonl` 记录模型与工具事件。两层保留各自职责。

当调用方为单个任务提供 `model_recovery` 时，PiRunner 额外写入 `model-attempts.jsonl`：逐次追加模型请求的 provider/model、时间、结束原因、可用用量和供应商 request ID，不复制请求正文。可恢复的请求错误会在同一个 Pi 进程和 session 中通过 `set_model` 与新 prompt 续跑；这条路径用任务级 Pi 配置关闭 Pi Agent / Provider 自动重试，不修改共享设置。没有 recovery policy 的 CLI/Local 调用保持原行为。

Agent 日志默认保留每轮结束消息与 usage、工具参数/结果、生命周期和错误，丢弃流式碎片、重复结束快照与原始推理正文。`request_trace.ts` 使用 Pi 的 `before_provider_request` 与非空 `thinking_delta` / `text_delta` / `toolcall_delta` 记录请求起点、首个有效响应和单调时钟延迟；经 RPC notification 传回 Python，不记录请求凭证或完整 payload。该值包含客户端、网络与供应商等待，不代表供应商纯推理时间；未提供这些钩子的历史日志保持未知。

`PI_TRACE_FULL=1` 额外写入 `pi.raw.events.jsonl` 供诊断；终态任务的原始流在清理时按 `PI_TRACE_FULL_MAX_AGE_DAYS`（默认 7，0 禁用）过期。精简日志与历史 `pi.events.jsonl` 不自动删除，Pi 自身会话保留策略不变。

## 完成与检查

Pi consumer 等待 `agent_settled`，并要求最终 assistant 的 `stopReason == "stop"`；`agent_end` 可能出现在重试或压缩之前。受控续跑只处理调用方明确配置且可识别的模型请求错误，沿用当前 Pi session 和任务工作区；报告仍须通过完整 HTML 检查才返回成功。整个 Worker 仍受调用方传入的总 deadline 限制。截图失败记录 `image_error`，HTML 仍可进入 `RENDERED`。

`REPORT_REVIEW` 默认关闭。开启后向同一 Agent 提供检查工具；工具返回 `semantic_review: not_performed`、`visual_quality: not_scored`，不保证模型调用、修复或质量提升。

## Pi 任务容器（2026-10-02 本地实现，未部署）

`PiRunner` 默认使用 Docker；仅显式 `PI_TASK_ISOLATION=local` 或 `isolation="local"` 才在本机直接运行 Pi。隔离启动失败使任务失败，不回退。下载、ASR 和交付后的简介填充/PNG 仍在可信 Worker 中运行。

每个任务仅挂载 `.generation/` 到 `/workspace`，材料为完整转写（含来源 ID）、来源/模式元数据、简介及选定 Skill/template。当前与备用模型由可信侧使用已安装 Pi 的离线目录解析；API-key 配置快照单独只读挂载，并复制到容器 tmpfs 中供 Pi 使用。不提供共享认证目录、OAuth、其他任务或调用方环境；不支持隔离模式中的 OAuth 模型。只有报告、assets、sessions、inspection 回传原 run，回传拒绝符号链接及非普通文件。

容器复用调用方配置的 `PI_TASK_IMAGE`（默认 `video-report-agent:linux-amd64`），以控制端的非 root UID/GID 运行；只读根文件系统、无 capabilities、no-new-privileges、受限 tmpfs，默认 2 GiB 内存、2 CPU、256 PID。每个任务创建独立 bridge，无端口发布、host PID/IPC/network 或 Docker socket。任务 bridge 与服务及其他任务分离；这没有实现所有宿主机/局域网私网出站封锁。模型凭证可被任务 bash 读取，是当前保留风险。

`task_container.py` 的可信监督进程位于 Worker 进程组之外，Pi 完成、失败、超时、取消或 Worker 被 SIGKILL 后执行 Docker force-remove 并删除任务网络。execution 必须收到原子写入的清理完成记录后才清除取消产物；启动前先写 pending 标记，未发布 PID、清理失败或结果未知时保留现场。监督进程非零退出不回传不可信输出。Docker create 客户端超时不等于 daemon 请求取消，这种结果不标记清理成功。

独立 `task_cleaner.py` 只做定时扫描，不调度任务。它复用应用镜像，只有此可信清理器和调用方控制服务持有 Docker socket，Pi 完全不持有。容器与网络在创建时写入 `video-report.scope`、`video-report.task-id` 和绝对 `video-report.deadline`；Pi 无权修改这些 daemon 标签、清理器代码或配置。清理器按部署 scope 每 2 秒重新发现过期资源，先删容器再删网络，包括尚未启动的 Created 容器。因此控制服务整体被强杀或 create 超时后晚到创建仍有独立回收责任方。正常可用的 daemon 下以截止后 30 秒为验收宽限；Docker API 不可用时不可能即时物理删除，清理器恢复连接后从标签补扫。主服务启动任务前必须发现匹配 scope 的健康清理器，否则失败关闭。

清理器使用非 root、只读根文件系统、无网络、无任务/凭证卷、cap-drop/no-new-privileges、受限 tmpfs 及 CPU/内存/PID 限制。其 socket 仍是高权限控制接口，标签筛选是程序行为限制而非 daemon API 权限降级。此权限例外已单独批准。若清理器本身也被移除，必须先恢复它；不声称所有控制进程与 daemon 同时永久不可用时仍能删除资源。

Docker 调用方使用 `PI_TASK_RUNS_VOLUME` 和 `PI_TASK_RUNS_ROOT` 选择已有命名卷的当前任务子目录（需要支持 volume-subpath 的 Engine）；本机调用默认 bind 当前生成子目录。Pi RPC、同 session 有限续跑及完成契约保持原有逻辑。隔离 fixture 通过不证明真实供应商生成质量。

## 配置与费用

CLI 从当前运行目录加载 `.env`，进程环境优先；调用方可传模型选择，或通过 `PiRunner(skill_dir=...)` / `VIDEO_REPORT_SKILL_DIR` 指定完整 Skill。Pi 状态存于运行目录的 `config/pi/`（可由环境指定），初始化默认模型配置不覆盖已有设置。安装与依赖细节沿用 [README](../README.md)。

`usage.json` 缓存 LLM 聚合，按日志文件元信息及语义版本失效。ASR backend/时长元数据有进程内有界缓存，费用每次按当前传入费率计算。DeepSeek V4.1 Flash 的 QwenAI 北京价格估算按北京时间 08:00–22:00 忙时、其余时间闲时计算；当前规则依照供应商价目表，不加入日历假日判断。其他可用 Pi 美元估算单列；均为估算，不是实时查询或供应商账单。缺失价格保持未知。

`ingest.probe_bilibili_video(source)` 通过现有 yt-dlp 只查询指定分 P 的标题与时长，不下载媒体、不创建任务文件；拒绝未知、非正时长和超过 5 小时的视频。返回规范 URL、BVID、分 P、video_id、title、duration。查询失败抛出 `UrlIngestError`，整体查询上限 45 秒。
