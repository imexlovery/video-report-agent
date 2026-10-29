import { Type } from "typebox";
import { spawn } from "node:child_process";
import { access, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { rmSync } from "node:fs";
import { join } from "node:path";

const placeholder = "<!-- video-report-diagram -->";

export default function (pi) {
  let called = false;
  pi.on("tool_call", event => {
    const target = event.toolName === "read" ? event.input.path
      : event.toolName === "bash" ? event.input.command : "";
    if (typeof target === "string" && /(?:diagram\.md|report_diagram\.ts|assets\/diagram|inspection\/[^\s;]*\.html)/.test(target)) {
      return { block: true, reason: "图示源码和会话由独立工具管理，不回读。只放置工具返回的标记；inspect_report 的 diagram_count 与 PNG 预览可确认嵌入和视觉效果。" };
    }
  });
  pi.registerTool({
    name: "generate_diagram",
    label: "Generate diagram",
    description: "把一个确需图示的解释目标交给独立 Pi 会话。每份报告最多一次；只返回插入标记，不把绘图代码或过程带入主上下文。",
    parameters: Type.Object({
      goal: Type.String({ description: "图示帮助读者理解什么关系或疑问" }),
      context: Type.String({ description: "只写待画的对象、关系、数值和成立条件；不用“某人说”等来源引导句，人物属于图示对象或必要归属时保留；来源定位用 source_units，不夹带取材说明、绘制要求或声明" }),
      source_units: Type.Array(Type.String(), { minItems: 1, maxItems: 24 }),
      caption: Type.String({ description: "简短图名，只标明图示主题，不写小结、结论、备注或来源转述" }),
    }),
    async execute(_id, params, signal, _update, ctx) {
      if (called) throw new Error("每份报告最多一个绘图任务，不再调用绘图。");
      if (!ctx.model) throw new Error("没有可用的绘图模型。");
      const lines = (await readFile(join(ctx.cwd, "transcript.md"), "utf8")).split("\n");
      const units = new Map(lines.map(line => [line.match(/^\[([^ |]+) \|/)?.[1], line]));
      if (params.source_units.some(id => !units.has(id))) throw new Error("图示来源 ID 不存在。");
      called = true;
      const directory = join(ctx.cwd, "assets", "diagram");
      await mkdir(directory, { recursive: true });
      const request = { ...params, source: params.source_units.map(id => units.get(id)) };
      // Exclusive creation also enforces the limit after a parent model recovery.
      await writeFile(join(directory, "request.json"), JSON.stringify(request), { flag: "wx" });
      const config = join(ctx.cwd, ".diagram-config");
      await mkdir(config);
      const agentDir = process.env.PI_CODING_AGENT_DIR;
      try {
        for (const name of ["models.json", "auth.json"]) {
          const source = join(agentDir, name);
          try { await access(source); } catch { continue; }
          await symlink(source, join(config, name));
        }
        await writeFile(join(config, "settings.json"), JSON.stringify({
          retry: { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } },
        }));
        const args = ["--mode", "rpc", "--provider", ctx.model.provider, "--model", ctx.model.id,
          "--thinking", pi.getThinkingLevel(), "--no-tools", "--no-extensions", "--no-skills",
          "--no-context-files", "--no-prompt-templates", "--no-themes", "--offline",
          "--session-dir", join(directory, "sessions"), "--append-system-prompt",
          await readFile(join(ctx.cwd, "diagram.md"), "utf8"),
          "--extension", join(ctx.cwd, "request_trace.ts")];
        // The project's 0.85 pin and the local 1.0 install expose different auth methods.
        const key = ctx.modelRegistry.getApiKeyAndHeaders
          ? (await ctx.modelRegistry.getApiKeyAndHeaders(ctx.model)).apiKey
          : await ctx.modelRegistry.getApiKey(ctx.model);
        if (key) args.push("--api-key", key);
        let lastAssistant;
        await new Promise<void>((resolve, reject) => {
          const child = spawn("pi", args, { cwd: directory,
            env: { ...process.env, PI_CODING_AGENT_DIR: config }, stdio: ["pipe", "pipe", "pipe"] });
          let buffer = "";
          let settled = false;
          let failure;
          const stop = () => child.kill("SIGKILL");
          const terminate = () => {
            stop(); rmSync(config, { recursive: true, force: true }); process.exit(143);
          };
          const timer = setTimeout(() => { failure = new Error("绘图会话超过 180 秒。"); stop(); }, 180000);
          signal?.addEventListener("abort", stop, { once: true });
          process.once("SIGTERM", terminate);
          if (signal?.aborted) stop();
          child.stdin.on("error", () => {});
          child.stderr.resume(); // Provider errors are kept out of the tool response.
          child.stdout.setEncoding("utf8");
          child.stdout.on("data", chunk => {
            buffer += chunk;
            let end;
            while ((end = buffer.indexOf("\n")) >= 0) {
              const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
              try {
                let event = JSON.parse(line);
                if (event.type === "extension_ui_request" && event.method === "notify"
                    && event.message?.startsWith("video-report-trace:")) {
                  event = JSON.parse(event.message.slice("video-report-trace:".length));
                }
                if (["request_start", "request_first_response", "agent_settled"].includes(event.type)
                    || event.type === "message_end" && event.message?.role === "assistant") {
                  ctx.ui.notify("video-report-diagram:" + JSON.stringify(event), "info");
                }
                if (event.type === "message_end" && event.message?.role === "assistant") lastAssistant = event.message;
                if (event.type === "response" && event.success === false) {
                  failure = new Error("绘图会话无法开始。"); stop();
                }
                if (event.type === "agent_settled") { settled = true; stop(); }
              } catch { failure = new Error("绘图会话返回了无效事件。"); stop(); }
            }
          });
          const cleanup = () => {
            clearTimeout(timer);
            signal?.removeEventListener("abort", stop);
            process.removeListener("SIGTERM", terminate);
          };
          child.once("error", error => { cleanup(); reject(error); });
          child.once("close", () => {
            cleanup();
            if (failure) reject(failure);
            else if (signal?.aborted || !settled || lastAssistant?.stopReason !== "stop") {
              reject(new Error("绘图会话未正常完成。"));
            } else resolve();
          });
          child.stdin.write(JSON.stringify({ type: "prompt", message: JSON.stringify(request) }) + "\n");
        });
        const svg = lastAssistant.content.filter(part => part.type === "text").map(part => part.text).join("\n").trim();
        await writeFile(join(directory, "diagram.svg"), svg);
        return { content: [{ type: "text", text: JSON.stringify({ placeholder,
          caption: params.caption, source_units: params.source_units }) }] };
      } finally { await rm(config, { recursive: true, force: true }); }
    },
  });
}
