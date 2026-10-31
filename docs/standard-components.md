# Standard 组件选型

Standard 的内容整理、布局与 HTML 编写由同一个 Pi Agent 完成。`pi.py` 按模式复制 Skill 与唯一模板，在 Standard 的任务 prompt 中明确要求先读 `modes/standard.md` 的选型表和模板结构示例。该表包含信息关系、必需字段、正例、误用与降级；无需额外布局调用。Brief 不加载此表，保持其独立模式与模板。

## 组件取舍

下表对应候选实验页的 12 项。模板路径均为 `src/video_report_agent/skills/video-report/assets/report-template.html`（简称模板），使用契约在相邻 `modes/standard.md`。

| 候选 | 已有对应实现及路径 | 原能力是否足够 | 决定 | 理由 |
|---|---|---|---|---|
| 数值对照 | 模板 `.stats/.stat` | 是 | 复用 | 对象、数值、单位和条件同行，无须专用双数值组件 |
| 数量横条 | 模板 `.bars/.bar-highlight` | 是 | 复用 | 共用零点与刻度已具备；补齐可比口径选型说明 |
| 输入—任务—输出 | 模板 table / steps | 关系可写清，缺专用开放结构 | 新增 `.mapping` | 每行独立映射，局部浅蓝处理节点，输入输出留在白纸上 |
| 一因多果 | 模板 comparison / 列表 | 内容可承载，关系不显式 | 合并为 `.branches` | 与解释分叉共用开放条目，明确可能作用与实际条件 |
| 阶段路线 | 模板 `.steps` | 纵向能力足够 | 扩展 `.steps-route` | 短阶段横向扫读；长内容退回原步骤，不换皮肤 |
| 解释分叉 | 模板 comparison / 嵌套列表 | 缺开放关系结构 | 合并为 `.branches` | 不再增加一套卡片；连接线不编码概率或确证因果 |
| 分类点阵 | 模板 bars / table-compact | 是 | 暂不采用 | 计数已有清楚表达，手写点数增加一致性负担 |
| 研究证据卡 | 模板 evidence-rows / stats | 长依据足够，摘要字段指导不足 | 复用 + `.definitions` | 样本、结果、条件、范围保持相邻，无须再包带底色卡片 |
| 解释行 | 模板 `.evidence-rows` | 是 | 复用 | 已有长解释双区布局，补强替代表格的判断依据 |
| 轻量比较矩阵 | 模板 table / table-details | 是 | 复用 | 共享多维数据仍用表格，不仅为灰表头新造变体 |
| 条件金额 | 模板 stats / 列表 | 是 | 复用 | 保留最高、支付条件及币种，不能用面积暗示已到账 |
| 留白结论 | 模板 p / strong | 是 | 复用 | 普通结论正文加粗即可，避免新增装饰性框 |

开放式 `.definitions` 同时支持名称解释和证据摘要。它与 `.evidence-rows` 的区别是前者没有强制的“判断—依据”字段。所有新结构均有模板内的完整 HTML 示例；CSS 无外部依赖，沿用原字体、纸面、蓝橙变量。核心内容不依赖折叠、悬停或横向滚动。

## 生成、检查与保真

首稿前按信息关系选表达，整篇检查并入既有集中检查：相邻重型组件、连续色块、表格的实际行列维度、卡片长段落、图表与正文重复。相同结构重复使用相同组件有比较收益；没有次数或种类配额。检查是模型编辑判断，不声称自动证明语义保真。

`report_content.py` 只处理归属程序的简介区；`diagram.py` 嵌入图示；`report_image.py` 在 CSP 限制下截图，未发现将组件统一改写为表格的回退。`inspect_report.py` 检查布局、资源与来源绑定，不替模型选择组件。入口与 Profile 都是模型指导，不是另一个组件路由器。

主报告仍复用模板；复杂 SVG 沿用 `generate_diagram` 与独立绘图会话。新结构只替代适合的简单关系，不覆盖复杂图示能力。事实、单位、分母、时间、范围与 `data-source-*` 随内容迁移；条件不足时回到文字，不补数据。

## 公开设计参考

2026-10-31 查看 [Intelligent UI 功能说明](https://help.openai.com/en/articles/20001598-intelligent-ui-in-chatgpt)：公开描述按问题选择文字、视觉与交互，比较可并排，简单问题可直接用文本。该资料未公开内部选型实现。这里采用按信息关系选形式的原则。

[Apps SDK UI](https://github.com/openai/apps-sdk-ui) 提供 MIT 许可的 React 组件和 Tailwind 设计变量；[组件文档](https://openai.github.io/apps-sdk-ui/) 为 Storybook。它是公开应用设计系统，未提供完整 Intelligent UI 内部编排源码。本次未复制其代码或引入依赖。

[官方 UI 规范](https://developers.openai.com/plugins/concepts/ui-guidelines) 中的清晰层级、原生字体、稳定间距、不嵌套滚动和克制色彩适合长篇报告；采用这些原则，用 VReport 的原有视觉语言实现。交互卡片、轮播及操作按钮不适合静态报告导出，未引入。
