# Video Report Agent · 公开核心

简体中文 | [English](README.en.md)

把视频转写整理成可以扫读、精读和保存的 HTML 阅读报告。

## 先看成品

<a href="docs/examples/report.png"><img src="docs/images/report-preview.png" alt="阅读报告预览，点击查看完整长图" width="460"></a>

[示例 HTML](docs/examples/report.html) · [完整 PNG 长图](docs/examples/report.png)

在 GitHub 上下载示例 HTML，用浏览器打开即可阅读。Standard 报告默认跟随系统配色，也可以在页面里切换浅色和深色。

## 选择使用方式

| 你想怎么用 | 从这里开始 |
|---|---|
| 想在线生成报告 | [打开 VReport](https://vreport.tri4t.xyz) |
| 已有转写、字幕或长篇文本 | [下载 Skill ZIP v0.7.0](https://github.com/imexlovery/video-report-agent/releases/download/skill-zip-v0.7.0/video-report-skill-v0.7.0.zip)，交给自己的 Coding Agent |
| 有 B 站、YouTube、抖音或小红书的单视频链接，想在本地运行 | [使用完整 CLI](#方式二运行完整-cli-流水线)，生成转写或报告 |

## 方式一：交给自己的 Coding Agent

用一个已配置模型、能读写本地文件的 Coding Agent 即可试用。

1. <a name="2-获取完整-skill-目录"></a>下载并解压上面的 Skill ZIP，保留完整的 `video-report/` 目录。
2. <a name="1-准备完整转写"></a><a name="3-发送生成指令"></a>在解压目录打开 Coding Agent，用包里的示例转写试跑。发送下面这段指令：

   ```text
   读取 video-report/SKILL.md，使用 standard 模式。
   完整阅读 examples/transcript.md，将示例转写作为只读输入。
   保留重要条件、数字和归属，生成自包含 HTML，写入 output/report.html。
   ```

3. 用浏览器打开 `output/report.html`。

<a name="模板-skill-如何工作"></a>[报告 Skill](src/video_report_agent/skills/video-report/SKILL.md) 为 Standard 和 Brief 提供各自的阅读规则与模板。换成自己的转写时，替换输入路径即可。想先看主要结论和依据，可把 `standard` 改成 `brief`。详见 [Skill ZIP 使用说明](docs/skill-bundle/README.md)。

## 方式二：运行完整 CLI 流水线

<a name="安装"></a>准备 Python 3.12、uv、FFmpeg 和 Node.js 22.19.0 或更新版本，按指南[安装 CLI 和运行依赖](docs/cli.md#安装)，再<a name="配置模型与转写"></a>[配置报告模型和转写](docs/cli.md#配置模型与转写)。

<a name="生成与查看"></a>在仓库目录运行下面的命令，把示例地址换成你的视频链接：

```sh
uv run video-report generate 'https://www.bilibili.com/video/BV...'
```

完成后打开 `runs/<run-id>/report.html`。只需要文字稿时，使用 [`transcribe`](docs/cli.md#生成与查看)。

## 开发与进一步阅读

- [CLI 使用指南](docs/cli.md) · <a name="选择字幕与转写来源"></a>[字幕与转写来源](docs/cli.md#选择字幕与转写来源) · <a name="统一链接检查与用户电脑取材"></a>[链接检查和本机取材](docs/cli.md#统一链接检查与用户电脑取材) · <a name="抖音与小红书取材"></a>[抖音与小红书取材](docs/cli.md#抖音与小红书取材)
- <a name="报告流程中可选的工具与能力"></a>[可选工具与开关](docs/cli.md#报告流程中可选的工具与能力) · <a name="术语纠错的搜索服务配置"></a>[术语搜索配置](docs/cli.md#术语纠错的搜索服务配置) · <a name="运行与数据约定"></a>[运行与数据](docs/cli.md#运行与数据约定)
- [Core 项目文档](docs/README.md) · [报告评测](evals/report-review/README.md)

公开 Core 维护生成流水线和报告 Skill，网站、账户与额度由独立的 Web Service 维护。
