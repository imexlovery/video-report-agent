# 本地音频转写术语修正

本地 Pi 可显式开启名称与术语修正，默认仅使用标题、作者、简介和原转写。付费联网取证单独关闭，开启术语修正不会自动调用搜索服务。当前取证后端为 DeepSeek Search / Fetch，已通过离线工具、真实接口、单视频完整转写术语审查及冻结修正结果到报告的对照；同一会话连续取证与报告的净增耗时、更多语义样例尚待验证。网站入口与 Docker 任务暂不支持此开关。

## 开启与取证

CLI 使用 `video-report generate '<Bilibili URL>' --term-correction`；嵌入者使用 `create_run(..., term_correction=True)`。默认关闭，开关保存为任务输入，不改变报告模式、报告模型或原始转写缓存身份。

同一个 Pi Agent 完整阅读原转写及元数据，在写提纲前检查名称冲突、语境异常和同名异写。元数据有明确支持时直接通过 `record_term_review` 记录；不足时保留候选。写作中也可处理新疑点，无独立全文修复调用。

只有另外设置 `PI_TERM_EVIDENCE_ALLOW_PAID_SEARCH=1` 才注册 `lookup_term`、`fetch_term_source` 并允许外部请求；未设置或为0时不提供这两个工具，直接调用请求函数也会在 HTTP 前拒绝。模型、ASR 的优惠额度不代表覆盖搜索费用，不能只更换 provider 就认为无需现金结算。

请求固定发送至 `https://api.deepseek.com/v1/tools/search` 或 `/v1/tools/fetch`，使用 `DEEPSEEK_SEARCH_API_KEY`，未设置时读取 `DEEPSEEK_API_KEY`。凭据必须属于 DeepSeek 官方账号，不使用报告模型的 Key、`PI_API_KEY` 或 `DASHSCOPE_API_KEY` 替代，不保存 Key 或请求头。不经过 MCP、模型注册表或辅助 LLM，报告模型、ASR 与单 Pi Agent 流程不变。旧 `PI_TERM_EVIDENCE_PROVIDER` / `PI_TERM_EVIDENCE_MODEL` 不再使用，不会退回百炼搜索。

Search 只发送具体 query，默认返回5条结果且 `need_content=true`，优先利用搜索已返回的网页正文；元数据与指定单元附近两条原文保存为最多6000字符的上下文，随工具结果交给当前 Agent 对照，不发送完整转写。相关名称可合并查询，同词重复出现不要求重复搜索。仅当标题与已有正文缺失或不足时才 Fetch 指定来源，不自动抓取全部结果。每个来源正文最多保存12000字符，并标记截断；`snippet` / `summary` 只作线索，不当作原文。

Search 与 Fetch 每任务合计最多三次，单次 HTTP 请求含读取响应最多30秒。三个工具均为 sequential，沿用 Pi 的串行执行。请求开始即占用本地预算，错误也计数，不重试或换供应商。保存操作类型、来源、耗时、请求 ID（若返回）及 `usage.requests` / `billable_response`；后者按响应是否产生结果记录，不是账单对账结论。此费用不并入主报告模型的 `usage.json`。

按2026-10-22的[DeepSeek官方说明](https://api-docs.deepseek.com/zh-cn/guides/search/)，Search 与 Fetch 均为5元/千次成功且有结果的请求；搜索返回条数不改变单次价格，失败、空结果与超时不收费。[扣费规则](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)说明优先使用赠送余额。部分客户端服务端工具至11月1日前的临时免计费，不能直接套用于此处独立 HTTP 请求；不将该接口当作免费替代。

## 工具与修正记录

`lookup_term(query, unit_ids)` 接受1～128字符的查询及一至三个原始单元 ID，返回调用 ID、来源标题/URL、摘要、可用正文、请求用量和状态。`fetch_term_source(source_id)` 只读取本任务已返回的来源，保存父来源 ID 与 Markdown 正文，不接受任意 URL。只有 URL 或摘要不足以支持修正。

`record_term_review(entries)` 每项包含：

- `unit_id/start/end/before` 与可选 `after`：在原单元正文中定位，Python 字符偏移、左闭右开，不包含 ID 与时间。
- `kind`：`person/organization/product/term`；`status`：`supported/candidate/unresolved`；以及语境 `reason`。
- `evidence`：`source_id` 与原文 `quote`；引用 `metadata:title/uploader/description` 或 lookup 返回的 source ID。
- 可选 `confidence`：`high/medium/low`，仅供参考，不代表校准概率或准确率。

supported 项要求定位准确、引文确实来自保存的元数据、来源标题或 Search/Fetch 返回的 `content` 正文，且包含候选名称；搜索摘要和历史辅助解释不作为直接证据。请求失败或没有来源时不可引用。工具拒绝位置冲突、明显的数字/否定修正与无依据的替换。机械检查不验证对象身份和语义正确性；同一人物、话题与局部语境仍由 Agent 判断，效果需人工验收。

未确认项保留原文，不为存疑核心概念补定义。不做全文盲替换，不用联网改写数字、单位、公式、否定或作者观点。同一原始位置的最新判断决定修正版，历史记录保留，改为 unresolved 可撤回该位置的修正。报告归纳可使用修正版，逐字引用取原文或改为转述。

## 产物与存储

| 文件 | 内容 |
|---|---|
| `source-context.json` | 标题、作者、原始简介和视频 URL |
| `term-review.json` | 检索请求、来源、用量、失败及逐次修正判断 |
| `transcript.corrected.md` | 仅应用 supported 补丁的阅读视图，保留单元 ID 和时间 |

原始 ASR、canonical transcript 和 `transcript.md` 不修改。新文件由现有 `run_documents` 保存与导出，不新增数据库表，不进入 READY 转写缓存或传播到另一任务。不承诺 Agent 一定发现疑词或使用工具；来源绑定与完成渲染不证明修正正确。

## 验证范围与后续

- 2026-10-22 将后端替换为 DeepSeek Search / Fetch。离线夹具检查默认停用、独立鉴权、Search→Fetch→批量修正、摘要不可作为原文、空结果与错误、共享预算及正文截断。
- 用户明确允许最多0.01元后，真实调用一次Search与一次Fetch，返回5条结果和4490字符正文；在原视频两个已有单元中批量记录两处术语修正，原文本、ID和时间保留。没有调用报告模型或ASR，也没有生成完整报告；这仅验证接口、取证工具与局部修正样例，材料在忽略的 `.local/evals/term-correction-deepseek-20261022/`。
- 同日以相同完整冻结转写、`gpt-6.1-sol / medium` 对比“两次网络请求”与“仅合计0.1元费用预算”的术语审查阶段，分别正常结束。两组包含首次全文读取，不能当作完整报告的净增耗时；代表性位置的记录数也不是准确率。后续同query参数验证发现 Search 可直接返回所需正文，因此默认改为 `need_content=true`；该默认尚未重跑完整两组。材料在 `.local/evals/term-correction-full-transcript-20261022/`。
- 将上述 A/B 的修正版与已接受判断冻结，沿用历史基线 Skill/模板分别新建 `gpt-6.1-sol / medium / standard` 报告会话，两份均生成并渲染，报告阶段没有新增取证。A 恢复薄肌与 TRON，B 恢复 Lean 与 TRON、却省略薄肌三支点；其他多项已确认名称没有进入正文。修正数量不能作为成文准确率，已确认核心概念的保留仍需评估。该分阶段对照不替代同一会话完整流程的净增耗时测试；详细记录在上述目录的 `report-generation/comparison.md`。
- 2026-10-15 的历史百炼样例使用相同冻结转写及 `gpt-6.1-sol / medium / standard` 比较开关关闭/开启的报告；最终独立运行没有中途提示或续跑，使用两次搜索完成报告。该结果不代替当前 DeepSeek 后端的真实验证。
- 对已接受的名称补丁逐条对照原文、元数据和来源标题；存疑项保留，单元 ID、时间和原始文本未改变。检查 HTML/PNG 与实际文件的 SQLite 保存回读。真实材料和记录保留在忽略的 `.local/evals/`。
- 这些检查覆盖本地冻结转写到报告的路径，不包含新视频下载、ASR、网站准入、Docker 或部署；不把程序检查、AI审阅或模型信心当作真人标注准确率。
- 真实调用需配置 DeepSeek 官方凭据并确认可用额度与费用；此次试用仅在子进程开启，产品默认仍关闭付费入口。
- 检索预算限制辅助请求数，主 Agent 的判断与记录仍会增加生成轮次；单样例耗时不作为稳定性能基准。扩展到更多样例时仍需独立标注正确修正、误改与未解决项，再决定网站入口与 Docker 集成。
