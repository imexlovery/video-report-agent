# Video Report Skill

把完整转写、字幕或其他长篇文本整理成可以直接用浏览器打开的 HTML 阅读报告。需要一个能读写本地文件的 Coding Agent，以及它已经配置好的模型。

[示例转写](examples/transcript.md) · [示例 HTML](examples/report.html)

在 GitHub 上点击 HTML 会看到源码；下载包解压后，用浏览器打开即可阅读成品。

## 解压后有什么

```text
README.md                  使用说明
video-report/              完整 Skill，请保留目录结构
  SKILL.md                 入口
  modes/                   Standard / Brief 两种阅读模式
  references/              按内容关系选用的编辑参考
  assets/                  两种模式各自的 HTML 模板
examples/
  transcript.md            合成示例转写
  report.html              对应的 Standard 成品，直接用浏览器打开
```

## 三步开始

1. 解压到一个本地目录。先用浏览器打开 `examples/report.html` 查看成品。
2. 在这个目录打开 Coding Agent，提供完整转写。先试用时，可以直接使用 `examples/transcript.md`。
3. 把下面这段指令发给 Agent：

```text
读取 video-report/SKILL.md，按其要求生成阅读报告。
使用 standard 模式，只读取该模式指定的模式文件和模板。
完整阅读 examples/transcript.md，将自包含 HTML 写入 output/report.html。
保留关键条件、数字口径、归属和不确定性，不修改原始转写或示例成品。
只有来源时间可靠时才显示章节时间；缺失的来源信息不要编造。
如有浏览器工具，检查页面布局；否则说明仅完成静态检查。
```

完成后用浏览器打开 `output/report.html`。换成自己的内容时，把输入路径改为自己的 Markdown、TXT 或 SRT 文件；需要精简速览时，把 `standard` 改成 `brief`。

## 安装到自己的 Agent（可选）

支持 Skills 的 Agent 可以按自身约定安装完整的 `video-report/` 目录。其他能读写本地文件的 Coding Agent 直接读取 `video-report/SKILL.md` 即可。请同时保留 `modes/`、`references/` 和 `assets/`，只复制入口文件会缺少生成规则与模板。

## 使用范围

- 这个包处理你已经准备好的完整文本，无需安装本项目的 Python 后端、Pi 或网站。
- 从视频链接自动下载、转写并生成报告，请使用源码仓库中的[完整 CLI 安装说明](https://github.com/imexlovery/video-report-agent#方式二运行完整-cli-流水线)。
- HTML 可独立阅读；PNG 导出取决于你的 Agent 是否具备浏览器或截图工具。
- 生成报告使用 Agent 当前配置的模型，可能产生模型费用；文本处理遵循该 Agent 和模型服务的设置。
- 示例转写为虚构演示材料，示例 HTML 用于展示 Standard 输出；实际生成的措辞和布局会随模型及输入变化。

[源码仓库](https://github.com/imexlovery/video-report-agent) · [在线使用](https://vreport.tri4t.xyz/)
