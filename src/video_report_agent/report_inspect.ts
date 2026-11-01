import { Type } from "typebox";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

export default function (pi) {
  let calls = 0;
  pi.registerTool({
    name: "inspect_report",
    label: "Inspect report",
    description: "检查 report.html 的来源 ID、溢出、裁切和加载问题，并定位转述、元叙述、作者姓名及重复文本候选，保存草稿与截图。内容候选需结合原文判断，保留必要归属，不机械删词。最多两次；不验证语义真实性或归属正确性，不评分审美。",
    parameters: Type.Object({}),
    async execute(_id, _params, signal, _onUpdate, ctx) {
      if (calls >= 2) throw new Error("检查预算已用完。停止修订，诚实报告剩余问题。");
      const label = `review-${++calls}`;
      const result = await pi.exec(process.env.VIDEO_REPORT_PYTHON,
        ["-m", "video_report_agent.inspect_report", "--label", label],
        { signal, timeout: 90000 });
      if (result.code !== 0) throw new Error(`Inspection failed: ${result.stdout || result.stderr}`);
      const report = JSON.parse(result.stdout);
      const content: any[] = [{ type: "text", text: JSON.stringify(report) }];
      if (ctx.model?.input?.includes("image")) {
        for (const path of report.screenshots) {
          content.push({ type: "image", mimeType: "image/png",
            data: (await readFile(resolve(ctx.cwd, path))).toString("base64") });
        }
      }
      return { content, details: { label, imagesReturned: content.length - 1 } };
    },
  });
}
