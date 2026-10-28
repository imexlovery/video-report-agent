# Video Report Core

## 目标与范围

将公开 Bilibili 视频转成可独立阅读、回查来源的 HTML 报告。Core 维护下载、ASR、规范转写、Pi 集成、唯一产品 Skill、渲染、CLI 与评测工具。账户、网站路由、额度、调度与运营部署由调用方负责。

## 当前能力

- 两条使用路径：独立复制 Skill 与模板，用自己的 Coding Agent 阅读完整转写；或通过 `video-report generate` 执行下载到报告的完整流水线。
- 流水线支持公开 Bilibili 视频及受限短链解析，最长 5 小时；默认 `asr-only`、OCR 关闭。可选 `fused` 支持字幕/OCR 文字融合。
- 内置 ASR 为 Apple Silicon MLX Whisper 与百炼文件转写适配（支持代码列出的 Paraformer/Fun-ASR 模型）。满足时长与静音条件的云端长音频分两段并行识别，恢复原时间轴。
- Standard 用于理解与查阅；Brief 用于快速掌握结论、依据和限制。两个模式读完整转写，各有规则与模板，可以复用同一来源转写。
- Pi 负责单 Agent 循环；Python 负责确定性前后处理、进程生命周期与产物校验。支持 Local 宿主执行和显式 Docker 任务隔离；调用方可配置同会话有限模型恢复及最终模型。HTML 交付后尝试输出 PNG 长图。
- 本地可显式开启名称与术语修正：默认由同一 Agent 结合元数据记录局部补丁，原转写保留。可选 DeepSeek Search/Fetch 已完成离线检查、局部样例、完整转写术语阶段及冻结修正结果到报告的对照；确认名称未必在成文中保留，同会话完整流程的净增耗时及更多语义样例仍待验证。付费请求需另外允许，当前默认关闭，见 [术语修正](term-correction.md)。
- SQLite 保存来源正文、执行、报告与缓存事实，文件保留执行导出、大产物和原始日志；按参数、版本与字幕内容兼容条件复用下载、ASR 或完整转写。提供可选 `inspect_report` 和合成 smoke/A/B 工具。
- 用量工具聚合完成的 assistant 消息，分开返回美元与部分模型的人民币估算；ASR 按配置费率估算。

## 重要限制

- 下载器不支持任意平台；独立 Skill 可接受其他来源的文本。字幕/OCR 融合不是全面视频视觉理解。
- 执行期限默认 30 分钟，不含调用方排队；下载有独立的 120 秒执行预算。停止本地进程不保证取消已提交的云端 ASR。
- `RENDERED` 表示 HTML 交付，PNG 可能失败；Pi 正常结束、来源 ID 合法或页面可渲染均不证明语义忠实和阅读质量。
- Local 工作目录约束不是操作系统沙箱；Docker 的挂载、凭证与网络边界见 [架构](ARCHITECTURE.md)。同会话恢复保留有效上下文和文件，重新发起模型请求；不支持进程重启后自动续跑。
- 费用是代码费率与日志计算的估算，不是账单；独立 ASR 复用的估算缺口见 [PLAN](PLAN.md)。

## 文档入口

[使用与安装](../README.zh-CN.md) · [架构](ARCHITECTURE.md) · [候选方向与待办](PLAN.md) · [设计决策](DECISIONS.md) · [领域词汇](../CONTEXT.md) · [设计偏好](report-design-preferences.md) · [全部文档](README.md)
