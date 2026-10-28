# 关键帧 → 带时间戳的画面描述：独立最小实验

验证画面能补充哪些字幕未说出的信息，并在独立目录生成纯字幕与增强报告对照；不接入 Core/Service 流水线。
脚本只使用 Python 标准库和已有 FFmpeg/ffprobe；不安装新依赖。

## 运行

在 Core 仓库中执行，输出目录必须是尚不存在的、已忽略的 `.local/evals/` 子目录：

```sh
uv run python evals/sparse-visual/experiment.py prepare \
  --video /absolute/path/video.mp4 \
  --transcript /absolute/path/transcript.md \
  --units /absolute/path/canonical-transcript.jsonl \
  --output .local/evals/sparse-visual/example --frames 9

# DEEPSEEK_API_KEY 由本地环境提供，切勿写入命令行、实验材料或提交。
uv run python evals/sparse-visual/experiment.py describe \
  --output .local/evals/sparse-visual/example
```

`prepare` 不调用模型。它按等分时间窗的中心取样，不声称选中了语义关键画面；
通过解码后的 PTS 记录实际时间，保留 FFmpeg 日志和未缩小的独立图片。
输入视频和转写必须使用同一播放时间轴。

`describe` 将同一批原图和每帧附近 ±15 秒的字幕一次性发给官方 DeepSeek Flash，
关闭思考模式，限制输出为 6,144 tokens。它不使用工具、重试或自动切换模型。
同一目录只能提交一次描述请求；失败或输出不完整时保留已有响应，不自动付费重跑。

## 产物与核对

- `transcript.md` / `canonical-transcript.jsonl`：原始材料的逐字节副本，保持来源 ID。
- `visual/manifest.json` / `visual/frames/`：采样参数、实际时间戳、帧 ID、图片和附近字幕。
- `visual/observations.jsonl`：模型派生观察，单独保存来源性质与不确定项，不写回字幕。
- `parallel.md`：原图、附近原字幕、描述与候选增量的并排阅读材料。
- `visual/request.json`：模型参数和原始提示；图片以本地路径替代大段 base64，仅留一份原图。
- `visual/response.json` / `usage.json` / `invocation.json`：原始响应、供应商用量和一次请求的耗时。

候选增量只代表模型对附近字幕的判断。人工逐条核对原图和完整转写，区分：
全文未说出的具体信息、仅附近没说但全文已说、纠正 ASR 的文字、重复信息、视觉误读。
只保留可直接看清的事实；单帧不能证明操作过程、先后变化或软件真实能力。

真实视频、转写、图片和结果全部留在忽略的 `.local/evals/`；不提交到公开样例。

## 完整报告对照

`compare_reports.py` 复用已有 baseline 目录中的原字幕、`input.json`、Skill、模板、
模式文件与 `invocation.json` 的模型命令。`--config` 指向已有模型目录与凭据的隔离 Pi 配置。

```sh
uv run python evals/sparse-visual/compare_reports.py \
  --baseline /absolute/path/frozen-baseline \
  --config /absolute/path/isolated-pi-config \
  --output .local/evals/sparse-visual/control

uv run python evals/sparse-visual/compare_reports.py \
  --baseline /absolute/path/frozen-baseline \
  --config /absolute/path/isolated-pi-config \
  --output .local/evals/sparse-visual/enhanced \
  --observations .local/evals/sparse-visual/example/visual/observations.jsonl
```

两组使用相同的补充来源读取指令，对照组的 `visual-observations.jsonl` 为空，
增强组保留描述文本；报告模型只读取文本。原字幕、模型、思考档位、模式、Skill 与模板一致。
复用现有 RPC 消费端，等待 `agent_settled` 和最终正常结束后填入原始简介、渲染 PNG，
在 `run-status.json` 保存完整报告生成与渲染耗时、所有已返回请求的 token 和请求数。
增强任务总量还须累加媒体获取、抽帧和描述阶段的耗时与用量。
