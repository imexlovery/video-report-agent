# Video Report Core

> 2026-09-24 核对本地源码与 Git，基线 `428f4fe`。本文记录实现状态，不代表本次已跑真实模型、重新验证报告质量或完成部署。

## 目标与范围

将公开 Bilibili 视频转成可独立阅读、回查来源的 HTML 报告。Core 维护下载、ASR、规范转写、Pi 集成、唯一产品 Skill、渲染、CLI 与评测工具。账户、网站路由、额度、调度与运营部署不属于本包。

## 当前能力

- 两条使用路径：独立复制 Skill 与模板，用自己的 Coding Agent 阅读完整转写；或通过 `video-report generate` 执行下载到报告的完整流水线。
- 流水线支持公开 Bilibili 视频及受限短链解析，最长 5 小时；默认 `asr-only`、OCR 关闭。可选 `fused` 支持字幕/OCR 文字融合，不是全面视频视觉理解。
- 内置 ASR 为 Apple Silicon MLX Whisper 与百炼 `paraformer` 适配（含代码支持的 Fun-ASR 模型）。云端长音频在满足时长与静音条件时分两段并行识别，恢复原时间轴。
- 默认 Standard 用于理解与查阅；Brief 用于快速掌握结论、依据和限制。两个模式读完整转写，各有规则与模板，可以复用同一来源转写。
- Pi 负责单 Agent 循环；Python 负责确定性前后处理、进程生命周期与产物校验。生成 HTML 后尝试输出 PNG 长图。
- 保存来源单元、流水线和 Agent 日志；按兼容条件复用下载、ASR 或完整转写。提供可选 `inspect_report` 和合成 smoke/A/B 工具。
- 用量工具聚合完成的 assistant 消息，分开返回美元与部分模型的人民币估算；ASR 按当前配置费率估算。

## 已知限制

- 流水线不支持任意平台下载；独立 Skill 可接受其他来源的文本，不等于下载器已经适配。
- 执行期限默认 30 分钟，不含调用方排队。停止本地进程不保证取消已提交的云端 ASR。
- `RENDERED` 表示 HTML 交付，PNG 可能失败；Pi 正常结束、来源 ID 合法或页面可渲染均不证明语义忠实和阅读质量。
- 每任务独立目录不是操作系统沙箱；保存 Pi 会话、云任务 ID 和输入复用不等于中途自动续跑。
- 费用是代码费率与日志计算的估算，不是账单；独立 ASR 复用路径尚不能笼统视为已去重计费，见 [PLAN](PLAN.md)。

## 文档入口

[使用与安装](../README.zh-CN.md) · [架构](ARCHITECTURE.md) · [当前计划与问题](PLAN.md) · [设计决策](DECISIONS.md) · [领域词汇](../CONTEXT.md) · [设计偏好](report-design-preferences.md) · [全部文档](README.md)
