# Core 当前架构

运行参数以调用方环境和任务保存的输入为准；本文描述当前实现，安装与配置入口见 [README](../README.md)。

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
| [report_diagram.ts](../src/video_report_agent/report_diagram.ts)、[diagram.py](../src/video_report_agent/diagram.py) | 主 Agent 按需委派独立绘图会话，程序将 SVG 嵌入指定位置 |
| [report_content.py](../src/video_report_agent/report_content.py)、[report_image.py](../src/video_report_agent/report_image.py) | 原始简介处理、HTML 后处理与长图导出 |
| [storage.py](../src/video_report_agent/storage.py) | SQLite 正文、执行身份、缓存认领与结构化事实；支持调用方活动事务 |
| [reuse.py](../src/video_report_agent/reuse.py)、[retention.py](../src/video_report_agent/retention.py) | 兼容输入复用与媒体清理 |
| [trace.py](../src/video_report_agent/trace.py)、[usage.py](../src/video_report_agent/usage.py) | 阶段事件、调用用量和费用估算缓存 |
| [inspect_report.py](../src/video_report_agent/inspect_report.py)、[evaluate_report.py](../src/video_report_agent/evaluate_report.py) | 程序检查与受控比较；不提供自动语义验收 |

自动路由先确定可用来源，再按来源尝试完整转写复用；确需 ASR 时才检查媒体与 ASR 复用。随后构建完整转写供 Agent 阅读。单任务主链路顺序执行，长音频 ASR 的两段并发不改变单 Agent 架构。没有向量检索或多 Agent 调度。

下载由 `ingest.py` 监督 `download_worker.py` 中的 yt-dlp，最多两次尝试、合计 120 秒运行预算，随后做有限进程清理。每 30 秒检查进度：最近 10 秒无进展，或未到 90% 且 ETA 不在 30 秒以内时，首轮中断并重新解析，优先选择平台返回的不同备用 URL；无备用则使用新解析的主 URL。第二轮不再增加重试。保持断点续传和外层 Worker 进程组，关闭 yt-dlp 内部额外重试，网络等待上限 10 秒。`download.log` 持续保留进度与切换原因，不打印签名媒体 URL。120 秒是体验预算，不是所有视频都能在此范围内完成的保证。

## 任务数据与接口

默认 Web/CLI 用 `auto` 依次尝试作者字幕轨、`ai-zh`、ASR，任一字幕正文可用即结束采集。Local 可主动使用自选来源。自选文字稿入口接收有序的 `transcript_sources`：ASR、非 AI 字幕轨、B站 `ai-zh`。第一路可用来源提供时间轴，其余已选来源参与局部校对；未选 ASR 时跳过媒体下载、音频提取与 ASR。字幕获取、来源状态、缓存及对照命令见 [可选文字稿来源](subtitle-sources.md)。`transcribe` 在 `TRANSCRIPT_READY` 结束，不启动后续 Agent/报告阶段；旧显式 ASR-only/fused 调用保持兼容。

`create_run` 登记数据库中的输入与初始状态，再导出任务材料；独立运行默认 `runs/state.sqlite3`，嵌入调用显式传 `database_path`。受监督 execution 原子认领 run，并把数据库路径与执行 ID 传给可信 Python Worker。调用方持有业务事务时，Core 方法使用同一个 `db`，不另开连接或提交；提交后才导出与派发。详见 [SQLite 存储](sqlite-storage.md)。

视频、任务、ASR/cloud task、来源事件、规范单元、转写正文、报告 HTML、缓存和结构化统计入库。`input.json`、`status.json`、转写和 HTML 是可重建执行导出；媒体、PNG、资源、Pi 会话/配置快照和原始诊断仍存文件。SQL 缓存区分下载、ASR 和完整转写，认领记录生产 run、执行 ID 与 deadline；READY 正文原子保存，活跃生产者不能被接管，退出后释放未完成占用，旧 ID 不得覆盖后续生产者。媒体清理沿用七天/数量策略，同步产物可用状态，不使 READY 转写或 HTML 失效。

ASR 片段编号保留原始来源位置，过滤空白或无效片段后允许间断；复用检查要求编号严格递增，不要求从零连续编号。

规范单元具有 ID、时间范围与来源关系，`transcript.md` 为模型可读视图；报告的 `data-source-units` 用来定位来源，不能单凭绑定合法判断内容忠实。

`run.trace.jsonl` 记录下载、FFmpeg、ASR、转写、Agent、截图阶段；`pi.events.jsonl` 记录模型与工具事件。两层保留各自职责。

当调用方为单个任务提供 `model_recovery` 时，PiRunner 额外写入 `model-attempts.jsonl`：逐次追加模型请求的 provider/model、时间、结束原因、可用用量和供应商 request ID，不复制请求正文。新请求另保存发送 payload 的 `input_bytes`，结束时保存已接收文本、推理和工具参数的 `output_bytes`，供调用方独立粗估缺失用量的成本；这些字节数不是供应商 Token 用量，进程被终止而没有结束事件时输出大小仍未知。可恢复的请求错误会在同一个 Pi 进程和 session 中通过 `set_model` 与新 prompt 续跑；保留有效历史和已写文件，重新发起模型请求，不从失败输出的 token 位置接续；这条路径用任务级 Pi 配置关闭 Pi Agent / Provider 自动重试，不修改共享设置。没有 recovery policy 的 CLI/Local 调用保持原行为。

连接错误 `Connection error.` 属可恢复的瞬态故障。初始模型的慢速瞬态故障仍直接切备用；备用模型首次发生瞬态故障（包括慢失败）或首次失败为输出检查拒绝时，可在该模型续试一次。所有续试共用调用方的恢复次数和剩余时间预算，切换后不返回初始模型；输入检查拒绝、未知错误等未覆盖路径不会因还有预算就自动重试。失败次数按模型累计，不因中间成功的工具调用轮次清零。

调用方可另提供 `model_recovery.final_fallback`。它不改变初始/备用顺序；在明确的输入拒绝、输出拒绝或瞬态错误下，原策略无可用动作或已用完为原两模型预留的恢复机会时，切到最终模型。总预算最后一次恢复留给它（无显式次数配置时三模型默认 3、两模型默认 2）。最终模型可正常进行多轮工具调用，但再失败不重试、不回切；剩余总时间不足 120 秒时仍不会接管。认证/额度、非法请求、未知错误不因此自动恢复。结构化 `content_filter` 归为输出检查拒绝，明确的供应商 `internal_error` 归为瞬态故障，单独 HTTP 426 不作此推断。`task_result.recovery_stop_reason` 区分策略、次数、剩余时间和最终模型失败；取消及外层异常仍看 `failure_category`。

Agent 日志默认保留每轮结束消息与 usage、工具参数/结果、生命周期和错误，丢弃流式碎片、重复结束快照与原始推理正文。`request_trace.ts` 使用 Pi 的 `before_provider_request` 与非空 `thinking_delta` / `text_delta` / `toolcall_delta` 记录请求起点、首个有效响应和单调时钟延迟；经 RPC notification 传回 Python，不记录请求凭证或完整 payload。该值包含客户端、网络与供应商等待，不代表供应商纯推理时间；未提供这些钩子的历史日志保持未知。

`PI_TRACE_FULL=1` 额外写入 `pi.raw.events.jsonl` 供诊断；终态任务的原始流在清理时按 `PI_TRACE_FULL_MAX_AGE_DAYS`（默认 7，0 禁用）过期。精简日志与历史 `pi.events.jsonl` 不自动删除，Pi 自身会话保留策略不变。

## 完成与检查

任务输入 `diagrams` 默认 `true`，`false` 时不提供绘图工具；没有前端选择控件。主 Agent 决定图示必要性、位置、解释目标、局部上下文与来源 ID，至多调用一次 `generate_diagram`。扩展从完整转写提取指定原文，启动全新 Pi RPC 进程／会话，只加载独立绘图指引，不继承主会话历史、不提供任何工具。子会话沿用调用时的模型和思考档位，关闭自动重试，180 秒截止，仅一次输出 SVG。产物与会话保存在 `assets/diagram/`，主上下文只收到插入标记。Python 在检查预览和交付时内联嵌入 SVG、图注及来源绑定；已委派却缺图或缺插入位置时交付失败，不把完整绘图过程交还主 Agent。

子会话的请求与完成用量通过 RPC notification 写入同一 `pi.events.jsonl`，标记 `_agent_role: diagram`，参与现有费用汇总；这些事件不参与主会话的完成或模型恢复判断。针对实测出现的主 Agent 回读，扩展拦截常规 Read/Bash 对图示材料和检查 HTML 的直接访问，检查器返回 `diagram_count` 与 PNG 供确认；该路径约束不等于文件系统沙箱。取消和执行时限仍由原 Worker／任务容器负责，工具的取消信号同时停止绘图进程。独立会话仅隔离上下文，其输出的语义和布局仍需核对。

Pi consumer 等待 `agent_settled`，并要求最终 assistant 的 `stopReason == "stop"`；`agent_end` 可能出现在重试或压缩之前。受控续跑只处理调用方明确配置且可识别的模型请求错误，沿用当前 Pi session 和任务工作区；报告仍须通过完整 HTML 检查才返回成功。整个 Worker 仍受调用方传入的总 deadline 限制。截图失败记录 `image_error`，HTML 仍可进入 `RENDERED`。

`REPORT_REVIEW` 默认关闭。开启后向同一 Agent 提供检查工具；工具返回 `semantic_review: not_performed`、`visual_quality: not_scored`，不保证模型调用、修复或质量提升。

## Pi 任务容器

任务 `term_correction` 默认关闭，CLI 可用 `--term-correction` 开启。启用时在同一 Pi 阅读或写作遇到重要疑点后按需核对，不增加全文校订阶段。`record_term_review` 校验来源与原文位置，修正版保留单元 ID 和时间；元数据、修正记录和修正版进入 `run_documents`，不进入共享转写缓存。仅 `PI_TERM_EVIDENCE_ALLOW_PAID_SEARCH=1` 时提供 DeepSeek Search/Fetch，独立凭据和请求预算见[术语修正](term-correction.md)。

`PiRunner` 默认直接使用宿主机的 Pi；只有显式设置 `PI_TASK_ISOLATION=docker` 或 `isolation="docker"` 才为每个任务启动 Docker 容器。选择隔离模式后若启动失败，任务直接失败，不回退到宿主机。下载、ASR 和交付后的简介填充/PNG 仍在可信 Worker 中运行。

公开仓库的 `.env.example` 为本地 CLI 入门显式设置 `PI_TASK_ISOLATION=local`，复制后无需 Docker 或清理器；这与代码默认值一致，也不改变托管服务 Public 模式强制使用 Docker 的策略。公开 Core 不包含托管服务的 Dockerfile 或 Compose 部署配置。

每个任务仅挂载 `.generation/` 到 `/workspace`，材料为完整转写（含来源 ID）、来源/模式元数据、简介及选定 Skill/template。当前、备用及可选最终模型由可信侧使用已安装 Pi 的离线目录解析；API-key 配置快照单独只读挂载，并复制到容器 tmpfs 中供 Pi 使用。不提供共享认证目录、OAuth、其他任务或调用方环境；不支持隔离模式中的 OAuth 模型。只有报告、assets、sessions、inspection 及术语修正的三个文档回传原 run，回传拒绝符号链接及非普通文件。

容器复用调用方配置的 `PI_TASK_IMAGE`（默认 `video-report-agent:linux-amd64`），以控制端的非 root UID/GID 运行；只读根文件系统、无 capabilities、no-new-privileges、受限 tmpfs，默认 2 GiB 内存、2 CPU、256 PID。每个任务创建独立 bridge，无端口发布、host PID/IPC/network 或 Docker socket。任务 bridge 与服务及其他任务分离；这没有实现所有宿主机/局域网私网出站封锁。模型凭证可被任务 bash 读取，是当前保留风险。

`task_container.py` 的可信监督进程位于 Worker 进程组之外，Pi 完成、失败、超时、取消或 Worker 被 SIGKILL 后执行 Docker force-remove 并删除任务网络。execution 必须收到原子写入的清理完成记录后才清除取消产物；启动前先写 pending 标记，未发布 PID、清理失败或结果未知时保留现场。监督进程非零退出不回传不可信输出。Docker create 客户端超时不等于 daemon 请求取消，这种结果不标记清理成功。

独立 `task_cleaner.py` 只做定时扫描，不调度任务。它复用应用镜像，只有此可信清理器和调用方控制服务持有 Docker socket，Pi 完全不持有。容器与网络在创建时写入 `video-report.scope`、`video-report.task-id` 和绝对 `video-report.deadline`；Pi 无权修改这些 daemon 标签、清理器代码或配置。清理器按部署 scope 每 2 秒重新发现过期资源，先删容器再删网络，包括尚未启动的 Created 容器。因此控制服务整体被强杀或 create 超时后晚到创建仍有独立回收责任方。正常可用的 daemon 下以截止后 30 秒为验收宽限；Docker API 不可用时不可能即时物理删除，清理器恢复连接后从标签补扫。主服务启动任务前必须发现匹配 scope 的健康清理器，否则失败关闭。

清理器使用非 root、只读根文件系统、无网络、无任务/凭证卷、cap-drop/no-new-privileges、受限 tmpfs 及 CPU/内存/PID 限制。其 socket 仍是高权限控制接口，标签筛选是程序行为限制而非 daemon API 权限降级。此权限例外已单独批准。若清理器本身也被移除，必须先恢复它；不声称所有控制进程与 daemon 同时永久不可用时仍能删除资源。

Docker 调用方使用 `PI_TASK_RUNS_VOLUME` 和 `PI_TASK_RUNS_ROOT` 选择已有命名卷的当前任务子目录（需要支持 volume-subpath 的 Engine）；本机调用默认 bind 当前生成子目录。Pi RPC、同 session 有限续跑及完成契约保持原有逻辑。隔离 fixture 通过不证明真实供应商生成质量。

## 配置与费用

CLI 从当前运行目录加载 `.env`，进程环境优先；调用方可传模型选择，或通过 `PiRunner(skill_dir=...)` / `VIDEO_REPORT_SKILL_DIR` 指定完整 Skill。Pi 状态存于运行目录的 `config/pi/`（可由环境指定），初始化默认模型配置不覆盖已有设置。安装与依赖细节沿用 [README](../README.md)。

`usage.json` 缓存 LLM 聚合，按日志文件元信息及语义版本失效。ASR backend/时长元数据有进程内有界缓存，费用每次按当前传入费率计算。DeepSeek V4.1 Flash 的 QwenAI 北京价格估算按北京时间 08:00–22:00 忙时、其余时间闲时计算；当前规则依照供应商价目表，不加入日历假日判断。其他可用 Pi 美元估算单列；均为估算，不是实时查询或供应商账单。缺失价格保持未知。

`ingest.probe_bilibili_video(source)` 通过现有 yt-dlp 只查询指定分 P 的标题与时长，不下载媒体、不创建任务文件；拒绝未知、非正时长和超过 5 小时的视频。返回规范 URL、BVID、分 P、video_id、title、duration。查询失败抛出 `UrlIngestError`，整体查询上限 45 秒。
