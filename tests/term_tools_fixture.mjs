// Synthetic Pi tool harness. All HTTP requests are intercepted; no provider is contacted.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";

const root = process.env.TERM_TEST_PI_ROOT;
const require = createRequire(join(root, "package.json"));
const { createJiti } = await import(pathToFileURL(join(root, "dist/core/extensions/jiti-loader.js")));
const jiti = createJiti(import.meta.url, { moduleCache: false, alias: {
  typebox: require.resolve("typebox"),
} });
const extension = await jiti.import(process.env.TERM_TEST_EXTENSION);
const tools = new Map();
extension.default({
  registerTool(tool) { tools.set(tool.name, tool); },
  async exec(command, args, options) {
    const result = spawnSync(command, args, { cwd: options.cwd, encoding: "utf8" });
    return { code: result.status, stdout: result.stdout, stderr: result.stderr };
  },
});
let calls = 0;
globalThis.fetch = async (url, options) => {
  calls++;
  assert.equal(url, "https://fixture.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation");
  assert.equal(options.headers.get("X-DashScope-SSE"), "enable");
  const body = JSON.parse(options.body);
  assert.equal(body.model, "qwen3.8-flash");
  assert.equal(body.parameters.enable_thinking, false);
  assert.equal(body.parameters.max_tokens, 1024);
  assert.deepEqual(body.parameters.search_options,
    { search_strategy: "turbo", forced_search: true, enable_source: true });
  assert.ok(body.input.messages[0].content[0].text.includes("薄肌、训练与健康"));
  if (calls === 3) throw new DOMException("fixture timeout", "TimeoutError");
  const sources = calls === 1 ? [{ index: 1, title: "Claude 官方文档", url: "https://example.org/docs" }] : [];
  const chunks = [
    { output: { search_info: { search_results: sources } }, request_id: "fixture-request" },
    { output: { choices: [{ message: { content: [{ text: "辅助解释" }] }, finish_reason: "null" }] },
      usage: { input_tokens: 100, output_tokens: 2, plugins: { search: { count: 1 } } } },
    { output: { choices: [{ message: { content: [{ text: "，请核对语境。" }] }, finish_reason: "stop" }] },
      usage: { input_tokens: 100, output_tokens: 8 } },
  ];
  const stream = chunks.map(c => `event:result\r\ndata:${JSON.stringify(c)}\r\n\r\n`).join("");
  const bytes = new TextEncoder().encode(stream);
  return new Response(new ReadableStream({ start(controller) {
    for (let i = 0; i < bytes.length; i += 13) controller.enqueue(bytes.slice(i, i + 13));
    controller.close();
  } }));
};
const ctx = { cwd: process.env.TERM_TEST_RUN, modelRegistry: {
  find(provider, model) {
    assert.equal(provider, "qwenai");
    assert.equal(model, "qwen3.8-flash");
    return { id: model, baseUrl: "https://fixture.cn-beijing.maas.aliyuncs.com/compatible-mode/v1" };
  },
  async getApiKeyAndHeaders() { return { ok: true, apiKey: "fixture-secret", headers: {} }; },
} };
const lookup = tools.get("lookup_term");
assert.equal(lookup.executionMode, "sequential");
assert.equal(tools.get("record_term_review").executionMode, "sequential");
const invoke = () => lookup.execute("fixture", { query: "AI产品 cloud 名称", unit_ids: ["unit-000003"] },
  new AbortController().signal, undefined, ctx);
const first = (await invoke()).details;
assert.equal(first.status, "completed");
assert.equal(first.provider, "qwenai");
assert.equal(first.model, "qwen3.8-flash");
assert.equal(first.usage.input_tokens, 100);
assert.equal(first.usage.output_tokens, 8); // cumulative counters are not summed
assert.equal(first.usage.plugins.search.count, 1);
assert.equal(first.sources[0].id, "lookup-001:source-1");
assert.equal(first.analysis, "辅助解释，请核对语境。");
const patch = { unit_id: "unit-000003", start: 0, end: 5, before: "cloud", after: "Claude",
  kind: "product", status: "supported", reason: "合成夹具明确指定这一处修正。",
  evidence: [{ source_id: first.sources[0].id, quote: "Claude 官方文档" }] };
const recorded = await tools.get("record_term_review").execute("fixture", { entries: [patch] },
  new AbortController().signal, undefined, ctx);
assert.equal(recorded.details.supported, 1);
assert.ok(readFileSync(join(ctx.cwd, "transcript.corrected.md"), "utf8").includes("Claude帮我"));
assert.equal((await invoke()).details.status, "no_sources");
const failure = (await invoke()).details;
assert.equal(failure.status, "failed");
assert.equal(failure.error_code, "timeout");
await assert.rejects(invoke, /budget exhausted/);
assert.equal(calls, 3);
assert.ok(!readFileSync(join(ctx.cwd, "term-review.json"), "utf8").includes("fixture-secret"));
assert.throws(() => extension.dashscopeEndpoint("https://example.org/compatible-mode/v1"),
  /unsupported_bailian_endpoint/);
globalThis.fetch = async () => new Response(null, { status: 402 });
await assert.rejects(() => extension.requestTermEvidence(
  ctx.modelRegistry.find("qwenai", "qwen3.8-flash"), { apiKey: "fixture-secret" }, first,
  new AbortController().signal), /http_402/);
console.log("offline term tools passed");
