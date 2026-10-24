# 可选文字稿来源与 B站字幕对照

Web 与 CLI 默认使用 `auto`：作者字幕轨 → B站 `ai-zh` → ASR。作者字幕指平台返回的非 AI 字幕轨；取得可用正文后直接形成 Transcript，不继续下载下一路正文，不进行多路融合，也不运行 OCR。作者轨不存在或不可用时尝试 `ai-zh`；AI 字幕需要登录或获取失败时才进入 ASR，并在此时检查 ASR 凭证。

嵌入调用用 `transcript_mode="auto"` 启用同一策略。既有显式 ASR-only 与旧版 Fusion 调用保持兼容。Local 高级设置或 CLI 的 `--sources` 可主动选择单一路径或多路对照／融合：

| 标识 | 来源 |
|---|---|
| `asr` | 当前配置的语音识别后端 |
| `subtitle_track` | B站非 AI 字幕轨；提供本地字幕文件时使用该文件 |
| `bilibili_ai` | B站中文 AI 自动字幕 `ai-zh` |

OCR 在 Web 暂时关闭；Core 的旧 CLI 接口保留。以下规则仅描述手动选源：列表第一项是首选主来源。所有选中的来源都会采集；主来源不可用时，选择列表中下一个可用来源。手动模式未选择的 ASR 不会被隐式调用。仅选择字幕时，不下载媒体、不抽取音频，也不要求 ASR 凭证。

主来源提供 Canonical Transcript 的时间轴。每条字幕保留毫秒时间戳和原始 cue 序号；unit ID 及 `transcript.md` 的时间范围继续供后续报告回查。没有 ASR 时 `asr_text` 为 null，来源明确记录为字幕。辅助来源使用已有的保守局部纠正规则，不自动补写主来源缺失的整句，不代表多个来源已经被完整合并。

## 登录与提取

本地可通过 `BILIBILI_SUBTITLE_BROWSER=edge`（或 yt-dlp 支持的其他浏览器）读取已有 B站登录态，也可由环境提供 `BILIBILI_SESSDATA`。默认不读取浏览器。Cookie 只留在内存中，仅发送到 B站 API 域，不写入任务、缓存、日志或前端，也不发给字幕 CDN。

先取得指定分 P 的 aid/cid/时长，再查询 WBI 签名播放器接口与新版 Protobuf 字幕接口。播放器响应必须与请求的 aid/cid 一致。两者返回同一轨道时保留签名播放器地址，避免新版不可直接下载的地址覆盖可用地址。下载签名 URL 不持久化。

`SKIPPED` 表示前一路成功、此路正文未获取。`OFF`、`LOGIN_REQUIRED`、`ABSENT`、`FAILED`、`READY` 分别表示未选择、需要登录、已查询但未发现、获取/解析失败、取得可用正文。仅发现轨道不计为成功。时间戳必须有限、有序且在视频合理范围内；空白 cue 跳过并记录数量，非空 cue 保留原始序号。首尾时间、尾部缺口及字幕时间覆盖比用于诊断；静音、音乐会产生合理间隔，覆盖到结尾也不能证明没有中间漏句。

## 只生成 Transcript

从 Core 仓库执行：

```bash
BILIBILI_SUBTITLE_BROWSER=edge uv run video-report \
  --runs .local/evals/subtitle-trial/runs \
  transcribe 'https://www.bilibili.com/video/BVxxxxxxxxxx/'
```

以上命令采用自动路由。需要主动指定来源时，添加 `--sources bilibili_ai`，或改为 `subtitle_track bilibili_ai`、`bilibili_ai asr` 或 `asr`。`bilibili_ai asr` 会同时取得 AI 字幕和 ASR，以 AI 字幕为首选主来源，不是只在字幕失败时才运行 ASR。

`transcribe` 复用受监督 Worker、存储和缓存，完成状态为 `TRANSCRIPT_READY`，不启动 Pi、报告或截图。`generate` 也接受 `--sources`，但会继续生成报告。

字幕正文存于 `bilibili-subtitles.json`，与来源事件、规范单元、manifest 和 `transcript.md` 一起入库。新缓存身份包括路由模式、实际采用的来源、字幕正文及导入内容；自动路由、手动多路与旧 ASR-only 缓存各自独立。无 ASR 的转写不再要求 `asr.json` 才能验证和复用。

## 与已有 ASR 对照

真实输入只放在忽略的 `.local/evals/`。manifest 示例：

```json
{"videos":[{"url":"https://www.bilibili.com/video/BVxxxxxxxxxx/","asr_path":"reference.asr.json"}]}
```

```bash
BILIBILI_SUBTITLE_BROWSER=edge uv run video-report audit-subtitles \
  --manifest .local/evals/subtitle-trial/manifest.json \
  --output .local/evals/subtitle-trial/audit
```

命令逐个查询，输出可获取状态、覆盖信息、字幕 Canonical Transcript，以及按一分钟时间窗排列的 AI/已有 ASR 文本，不新跑 ASR 或报告。时间窗边界可能造成表面差异，文本相似度也不是准确率；判定谁更准确时，应回查整份文本和原视频的声音或画面字幕，分别检查数字、术语、条件句和遗漏。

## 接口依据

Protobuf 字段结构参考 [bilibili-ai-subtitle](https://github.com/ccBilly-aipm/bilibili-ai-subtitle)（MIT）；同时核对已安装 yt-dlp 和 [bili-subtitle 协议笔记](https://github.com/ymt200120/bili-subtitle/blob/main/docs/PROTOCOL.md)中的签名及分 P 绑定。这些播放器接口不是承诺稳定的官方开放 API；实际可用性以返回的轨道及正文为准。
