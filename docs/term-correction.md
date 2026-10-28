# 按需术语修正

任务通过 `create_run(..., term_correction=True)` 或 CLI `--term-correction` 开启，Core 默认关闭。Local 与 Docker 使用同一实现，参数保存为任务输入，不改变报告模式、模型或原始转写缓存身份。调用方决定是否让用户选择开关。

## 判断与取证

同一 Pi Agent 读取完整转写与 `source-context.json` 中的标题、作者、简介和 URL；在阅读或写作遇到影响理解的名称、专业术语疑点时处理，不增加独立全文审查阶段。自身知识仅提出候选，先用元数据，证据不足才按需联网，不逐词搜索。

开启 correction 就同时提供 `record_term_review`、`lookup_term` 与 `fetch_term_source`，允许按需使用付费 Search/Fetch；关闭时不加载这组工具。没有独立的搜索许可开关，旧 `PI_TERM_EVIDENCE_ALLOW_PAID_SEARCH` 不再读取。开启不意味着每份报告必定搜索，已有元数据足够时可直接记录修正。

取证直接使用 DeepSeek `/v1/tools/search` 与 `/v1/tools/fetch` JSON API，不经过辅助模型。凭据优先 `DEEPSEEK_SEARCH_API_KEY`，否则 `DEEPSEEK_API_KEY`，不使用报告模型 Key、`PI_API_KEY` 或 `DASHSCOPE_API_KEY`，不回退百炼。缺少官方凭据时，取证工具记录 `missing_evidence_credentials`，不会声称已搜索成功。

- `lookup_term(query, unit_ids)` 只发送具体 query；返回五条结果并默认 `need_content=true`。元数据与附近原文只交给当前 Agent 核对，不向搜索发送整份转写。
- `fetch_term_source(source_id)` 仅在已有正文不足时读取本任务搜索已返回的来源，不接受任意 URL。
- Search 与 Fetch 每任务合计最多三次，开始请求即计数，错误也占次数；单次含读取响应最多 30 秒，不重试或换供应商。三个工具沿用 Pi 串行执行。
- 每个来源正文最多保留 12000 字符并标记截断；摘要只作线索。保存来源、请求状态、耗时和响应计费标记，不保存 Key 或请求头；响应计费标记不是账单对账，搜索费不并入主模型 `usage.json`。

## 局部补丁与产物

`record_term_review(entries)` 按 `unit_id/start/end/before/after` 定位原单元正文，偏移为 Python 字符、左闭右开；保存类别、`supported/candidate/unresolved`、语境理由和 `source_id/quote`。

supported 要求位置及原词准确，引文来自保存的元数据、来源标题或返回正文，且包含候选名称；拒绝重叠位置、无来源及明显的纯数字或否定词替换。完整的数字、单位、公式、作者观点与否定关系边界同时由 Skill 约束，机械检查不是完整语义证明。候选或未解决项保留原文，不为存疑概念补定义；同一位置的后续 unresolved 判断可撤回修正，历史记录保留。

| 文档 | 用途 |
|---|---|
| `source-context.json` | 视频元数据 |
| `term-review.json` | 检索、来源、用量与逐次判断 |
| `transcript.corrected.md` | 仅应用 supported 补丁的阅读视图 |

原始 ASR、canonical transcript 和 `transcript.md` 不变，ID 与时间不变。报告归纳读取修正版，逐字引语取原文或改为转述。文档由既有 `run_documents` 保存与导出，不进入 READY 转写缓存、不传播到其他任务。

## Docker 传递

staging 保存任务开关和辨名所需的标题、作者、简介；Pi 在隔离工作目录初始化修正记录。仅当任务开启时，可信控制端把独立搜索 Key 写入现有 `.generation-config/term-search-key`（0600），通过现有只读配置挂载供容器启动脚本读取；Key 不放在命令、任务输入或产物中。配置沿用任务结束后的清理。该凭据与模型凭据一样可被任务进程读取，不宣称拥有额外的秘密隔离。

三个修正文档列入既有输出白名单，成功退出后回传并保存；仍拒绝符号链接和非普通文件，不回传配置。

## 验证与限制

已有离线检查覆盖开关、来源和位置校验、工具协议、预算、存储，以及 Docker staging、凭据范围和产物导出。2026-10-28 接入验收未调用真实付费供应商；本机 Docker daemon 未运行，真实容器启动仍待目标环境复核。

历史真实接口、小样、冻结转写报告与按需对照保留在忽略的 `.local/evals/term-correction-deepseek-20261022/`、`.local/evals/term-correction-full-transcript-20261022/`、`.local/evals/term-trigger-strategy-20261023/` 和 `.local/evals/term-trigger-qwen-pair-20261023/`。单会话按需比两段流程耗时更低，但两个追加样本没有稳定正文质量赢家；首次搜索实际仍在正文写入前。不能用修正数量、渲染成功或模型信心代替准确率，也不能把历史模型与旧 Skill 的结果作为当前设置的质量证明。
