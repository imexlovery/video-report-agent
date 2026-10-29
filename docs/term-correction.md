# 按需术语修正

任务通过 `create_run(..., term_correction=True)` 或 CLI `--term-correction` 开启，Core 默认关闭。Local 与 Docker 使用同一实现，参数保存为任务输入，不改变报告模式、模型或原始转写缓存身份。调用方决定是否让用户选择开关。

## 判断与取证

同一 Pi Agent 读取完整转写与 `source-context.json` 中的标题、作者、简介和 URL；在阅读或写作遇到影响理解的名称、专业术语疑点时处理，不增加独立全文审查阶段。自身知识仅提出候选，先用元数据，证据不足才按需联网，不逐词搜索。

开启 correction 就同时提供 `record_term_review`、`lookup_term` 与 `fetch_term_source`，允许按需使用付费 Search/Fetch；关闭时不加载这组工具。没有独立的搜索许可开关，旧 `PI_TERM_EVIDENCE_ALLOW_PAID_SEARCH` 不再读取。开启不意味着每份报告必定搜索，已有元数据足够时可直接记录修正。

取证供应商通过 `TERM_SEARCH_PROVIDER=deepseek|openai` 切换，默认 `deepseek`，独立于报告生成模型。开启 correction 的新任务把选择保存为 `search_provider`；OpenAI 另保存 `search_model`，来自 `OPENAI_SEARCH_MODEL`，默认 `gpt-6.1-sol`。`create_run` 也接受同名参数覆盖默认值。修改配置只影响新任务；旧任务缺少字段时仍使用 DeepSeek。Service 修改 `.env` 后需重启进程使新默认值生效。

| 供应商 | 接口与凭据 | 返回证据 |
|---|---|---|
| DeepSeek | `/v1/tools/search`、`/v1/tools/fetch`；优先 `DEEPSEEK_SEARCH_API_KEY`，否则 `DEEPSEEK_API_KEY` | 来源标题、摘要和可用网页正文 |
| OpenAI | `/v1/responses` 配置 `web_search`，low reasoning；优先 `OPENAI_SEARCH_API_KEY`，否则 `OPENAI_API_KEY` | 来源标题、URL 和独立的 `analysis` 模型摘要；`content` 留空 |

两者均不使用 `PI_API_KEY` 或 `DASHSCOPE_API_KEY`，不自动切换供应商。缺少对应凭据时工具记录 `missing_evidence_credentials`。OpenAI 搜索包含一次独立模型请求，模型 token 与搜索分别计费；不复用报告模型的会话。

- `lookup_term(query, unit_ids)` 只发送具体 query，最多保存五条来源；DeepSeek 使用 `need_content=true`，OpenAI 从引用与 `web_search_call.action.sources` 提取来源。元数据与附近原文只交给当前 Agent 核对，不向搜索发送整份转写。
- `fetch_term_source(source_id)` 仅核对本任务搜索已返回的来源，不接受任意 URL。DeepSeek 返回正文；OpenAI 请求打开指定 URL，只有响应包含已完成且 URL 匹配的 `open_page` 才接受其来源标题，否则记录 `source_not_opened`。OpenAI 的摘要不进入原文引用校验，来源标题不足以支持纠正时保留原词。
- Search 与 Fetch 每任务合计最多三次，开始请求即计数，错误也占次数；单次含读取响应最多 30 秒，不重试或换供应商。OpenAI 每请求设 `max_tool_calls=1`、`max_output_tokens=1500` 和 `store=false`，分别记录外层请求数、内置工具数、搜索动作数和模型 token 用量。三个工具沿用 Pi 串行执行。
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

staging 保存任务开关、搜索供应商/模型和辨名所需的标题、作者、简介；Pi 在隔离工作目录初始化修正记录。仅当任务开启时，可信控制端把所选供应商的独立搜索 Key 写入现有 `.generation-config/term-search-key`（0600），通过现有只读配置挂载供容器启动脚本读取为对应供应商的搜索变量；Key 不放在命令、任务输入或产物中。配置沿用任务结束后的清理。该凭据与模型凭据一样可被任务进程读取，不宣称拥有额外的秘密隔离。

三个修正文档列入既有输出白名单，成功退出后回传并保存；仍拒绝符号链接和非普通文件，不回传配置。

## 验证与限制

已有离线检查覆盖两供应商的开关、任务配置快照、来源和位置校验、工具协议、预算、存储，以及 Docker staging、凭据范围和产物导出；Service 合成测试覆盖 HTTP 提交与 Core worker 渲染、保存流程。

2026-10-29 使用 `gpt-6.1-sol` 完成真实 Web Search → 来源保存 → 标题引用校验 → 纠正版转写小样。第二次真实请求打开已返回来源时，OpenAI 报告重定向目的页不可抓取，工具返回 `no_sources`，未伪造正文。本地记录在 `.local/evals/openai-search-20261029/`。

同日以相同短句和查询将搜索模型切换为 `gpt-6-luna`，真实搜索与纠正工具链通过，原始转写保持不变；纠正条目由测试脚本给定，未测试主 Agent 的自主纠正判断。环境变量示例使用 Luna 覆盖代码默认搜索模型；未设置 `OPENAI_SEARCH_MODEL` 时仍按上述默认值运行。记录在 `.local/evals/openai-search-luna-20261029/`。

随后完成本机 Local Web → Linux amd64 Docker → Sol 主 Agent / Luna 搜索 → HTML 与 PNG 交付验证。Sol 自主调用搜索、引用官方来源并纠正合成字幕中的名称；任务达到 `RENDERED`，最终 `stopReason=stop` 和 `agent_settled` 均已确认，临时容器与网络正常清理。当前 Docker Desktop 配置代理，结果仅证明该网络配置可用。输入为 75 秒合成字幕，未验证真实视频 ASR、普遍纠正质量或 Windows/公网部署；记录见 `.local/evals/openai-docker-e2e-20261029/validation.md`。

接口依据：[OpenAI Web Search](https://developers.openai.com/api/docs/guides/tools-web-search)、[GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol)（2026-10-29 核对）。

历史真实接口、小样、冻结转写报告与按需对照保留在忽略的 `.local/evals/term-correction-deepseek-20261022/`、`.local/evals/term-correction-full-transcript-20261022/`、`.local/evals/term-trigger-strategy-20261023/` 和 `.local/evals/term-trigger-qwen-pair-20261023/`。单会话按需比两段流程耗时更低，但两个追加样本没有稳定正文质量赢家；首次搜索实际仍在正文写入前。不能用修正数量、渲染成功或模型信心代替准确率，也不能把历史模型与旧 Skill 的结果作为当前设置的质量证明。
