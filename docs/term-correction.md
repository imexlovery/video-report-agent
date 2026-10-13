# 本地音频转写术语修正

本地 Pi 可显式开启名称与术语修正。代码与离线夹具已搭建；真实百炼接口、报告生成和人工语义验收待 API 额度恢复后进行。网站入口与 Docker 任务暂不支持此开关。

## 开启与取证

CLI 使用 `video-report generate '<Bilibili URL>' --term-correction`；嵌入者使用 `create_run(..., term_correction=True)`。默认关闭，开关保存为任务输入，不改变报告模式、报告模型或原始转写缓存身份。

同一个 Pi Agent 完整阅读原转写及元数据，在写提纲前检查名称冲突、语境异常和同名异写。元数据有明确支持时直接记录；不足时调用 `lookup_term`，再通过 `record_term_review` 记录判断。写作中也可处理新疑点，无独立全文修复调用。

取证模型由 `PI_TERM_EVIDENCE_PROVIDER` / `PI_TERM_EVIDENCE_MODEL` 指定，默认 `qwenai` / `qwen3.8-flash`；provider 需已经配置为百炼官方端点。扩展通过 Pi ModelRegistry 读取其配置和凭据，不使用报告模型的 Key 替代取证凭据，不保存 Key 或请求头。

调用对应业务空间的 `/api/v1/services/aigc/multimodal-generation/generation`，纯文本输入、SSE、关闭思考、`enable_search=true`，搜索选项为 `turbo`、`forced_search=true`、`enable_source=true`。DashScope 可返回检索来源；Chat Completions 不返回来源，多模态 Qwen 搜索需使用多模态端点和流式调用，见[百炼联网搜索](https://help.aliyun.com/zh/model-studio/web-search/)和[多模态接口](https://help.aliyun.com/zh/model-studio/vision/)。当前账号尚未实测这些接口约定。

每任务最多三次取证请求，单次 HTTP 请求含读取流最多 30 秒、1024 输出 tokens。发送查询、元数据与指定单元附近两条原文，上下文最多 6000 字符并记录截断。两个工具声明为 sequential，沿用 Pi 的串行工具执行以避免同轮预算和记录竞争。请求开始即占用预算，错误也计数，不重试或换供应商。保存 provider/model、累计 token 用量、实际检索计数和耗时；缺失用量保持未知。辅助用量当前不并入主报告模型的 `usage.json` 金额估算。

## 工具与修正记录

`lookup_term(query, unit_ids)` 接受一个查询及一至三个原始单元 ID，返回调用 ID、模型分析、来源标题/URL、用量和状态。分析并非网页原文。来源只返回标题时，直接证据仅使用标题；只有 URL 或生成的解释不足以支持修正。

`record_term_review(entries)` 每项包含：

- `unit_id/start/end/before` 与可选 `after`：在原单元正文中定位，Python 字符偏移、左闭右开，不包含 ID 与时间。
- `kind`：`person/organization/product/term`；`status`：`supported/candidate/unresolved`；以及语境 `reason`。
- `evidence`：`source_id` 与原文 `quote`；引用 `metadata:title/uploader/description` 或 lookup 返回的 source ID。
- 可选 `confidence`：`high/medium/low`，仅供参考，不代表校准概率或准确率。

supported 项要求定位准确、引文确实来自保存的元数据或已完成检索的来源标题，且包含候选名称。工具拒绝位置冲突、明显的数字/否定修正与无依据的替换。机械检查不验证对象身份和语义正确性；同一人物、话题与局部语境仍由 Agent 判断，效果需人工验收。

未确认项保留原文，不为存疑核心概念补定义。不做全文盲替换，不用联网改写数字、单位、公式、否定或作者观点。同一原始位置的最新判断决定修正版，历史记录保留，改为 unresolved 可撤回该位置的修正。报告归纳可使用修正版，逐字引用取原文或改为转述。

## 产物与存储

| 文件 | 内容 |
|---|---|
| `source-context.json` | 标题、作者、原始简介和视频 URL |
| `term-review.json` | 检索请求、来源、用量、失败及逐次修正判断 |
| `transcript.corrected.md` | 仅应用 supported 补丁的阅读视图，保留单元 ID 和时间 |

原始 ASR、canonical transcript 和 `transcript.md` 不修改。新文件由现有 `run_documents` 保存与导出，不新增数据库表，不进入 READY 转写缓存或传播到另一任务。不承诺 Agent 一定发现疑词或使用工具；来源绑定与完成渲染不证明修正正确。

## 待真实验收

1. 恢复额度后先以小请求确认已有凭据、模型与端点可用，检查真实 `search_info`、来源、检索计数和 token 用量。
2. 固定已有转写和生成配置，在独立目录比较开关关闭/开启的报告，不覆盖历史报告，不重跑 ASR。
3. 人工核对正确修正、同词不同义、同名歧义和未确认案例，统计正确修正与误改，不以覆盖率或模型信心当准确率。
4. 完成真实接口和语义验收后，再决定网站入口与 Docker 集成。离线夹具通过不等于上述验收完成。
