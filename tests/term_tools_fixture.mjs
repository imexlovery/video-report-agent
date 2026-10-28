// Synthetic Pi tool harness. All HTTP requests are intercepted; no provider is contacted.
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
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
const api = {
  registerTool(tool) { tools.set(tool.name, tool); },
  async exec(command, args, options) {
    assert.ok(args.every(arg => Buffer.byteLength(arg, "utf8") < 128 * 1024));
    const result = spawnSync(command, args, { cwd: options.cwd, encoding: "utf8" });
    return { code: result.status, stdout: result.stdout, stderr: result.stderr };
  },
};
// The retired environment permission must not suppress tools in an enabled task.
process.env.PI_TERM_EVIDENCE_ALLOW_PAID_SEARCH = "0";
process.env.DEEPSEEK_SEARCH_API_KEY = "fixture-secret";
globalThis.fetch = async () => { throw new Error("unexpected HTTP before fixture"); };
extension.default(api);
assert.ok(tools.has("record_term_review"));
assert.ok(tools.has("lookup_term"));
assert.ok(tools.has("fetch_term_source"));
let calls = 0;
globalThis.fetch = async (url, options) => {
  calls++;
  assert.equal(new Headers(options.headers).get("Authorization"), "Bearer fixture-secret");
  const body = JSON.parse(options.body);
  assert.ok(!("model" in body));
  if (calls === 2) {
    assert.equal(url, "https://api.deepseek.com/v1/tools/fetch");
    assert.deepEqual(body, { url: "https://example.org/docs" });
    return Response.json({ title: "", content: "# Claude Code\n" + "合成开发工具指南。".repeat(30000) });
  }
  assert.equal(url, "https://api.deepseek.com/v1/tools/search");
  assert.deepEqual(body, { query: "AI产品 cloud 名称", count: 5, need_content: true });
  if (calls === 3) throw new DOMException("fixture timeout", "TimeoutError");
  return Response.json({ results: [{ title: "Claude 官方文档", url: "https://example.org/docs",
    snippet: "Claude Code", summary: "Claude Code 是开发工具。", content: "" },
    ...Array.from({ length: 4 }, (_, i) => ({ title: "合成来源", url: `https://example.org/${i}`,
      content: "合成检索正文".repeat(10000) }))] },
    { headers: { "x-request-id": "fixture-request" } });
};
const ctx = { cwd: process.env.TERM_TEST_RUN, modelRegistry: {
  find() { throw new Error("Search must not resolve an auxiliary model"); },
  getApiKeyAndHeaders() { throw new Error("Search must not reuse report credentials"); },
} };
const lookup = tools.get("lookup_term");
assert.equal(lookup.executionMode, "sequential");
assert.equal(tools.get("fetch_term_source").executionMode, "sequential");
assert.equal(tools.get("record_term_review").executionMode, "sequential");
const invoke = () => lookup.execute("fixture", { query: "AI产品 cloud 名称", unit_ids: ["unit-000003"] },
  new AbortController().signal, undefined, ctx);
const first = (await invoke()).details;
assert.equal(first.status, "completed");
assert.equal(first.provider, "deepseek");
assert.equal(first.operation, "search");
assert.equal(first.usage.requests, 1);
assert.equal(first.usage.billable_response, true);
assert.equal(first.request_id, "fixture-request");
assert.equal(first.sources[0].id, "lookup-001:source-1");
assert.equal(first.sources[0].summary, "Claude Code 是开发工具。");
assert.equal(first.sources[1].content.length, 12000);
assert.equal(first.sources[1].content_truncated, true);
assert.ok(first.context.includes("薄肌、训练与健康"));
const patch = { unit_id: "unit-000003", start: 0, end: 5, before: "cloud", after: "Claude Code",
  kind: "product", status: "supported", reason: "合成夹具明确指定这一处修正。",
  evidence: [{ source_id: first.sources[0].id, quote: "Claude Code 是开发工具。" }] };
await assert.rejects(() => tools.get("record_term_review").execute("fixture", { entries: [patch] },
  new AbortController().signal, undefined, ctx), /Evidence must quote/);
const fetched = (await tools.get("fetch_term_source").execute("fixture",
  { source_id: first.sources[0].id }, new AbortController().signal, undefined, ctx)).details;
assert.equal(fetched.operation, "fetch");
assert.equal(fetched.parent_source_id, first.sources[0].id);
assert.equal(fetched.sources[0].title, "");
assert.ok(fetched.sources[0].content.includes("Claude Code"));
assert.equal(fetched.sources[0].content.length, 12000);
assert.equal(fetched.sources[0].content_truncated, true);
patch.evidence = [{ source_id: fetched.sources[0].id, quote: "# Claude Code" }];
const recorded = await tools.get("record_term_review").execute("fixture", { entries: [patch] },
  new AbortController().signal, undefined, ctx);
assert.equal(recorded.details.supported, 1);
assert.ok(readFileSync(join(ctx.cwd, "transcript.corrected.md"), "utf8").includes("Claude Code帮我"));
const failure = (await invoke()).details;
assert.equal(failure.status, "failed");
assert.equal(failure.error_code, "timeout");
await assert.rejects(invoke, /budget exhausted/);
assert.equal(calls, 3);
assert.ok(!readFileSync(join(ctx.cwd, "term-review.json"), "utf8").includes("fixture-secret"));
globalThis.fetch = async () => new Response(null, { status: 402 });
await assert.rejects(() => extension.requestTermEvidence(first,
  new AbortController().signal), /http_402/);
delete process.env.DEEPSEEK_SEARCH_API_KEY;
delete process.env.DEEPSEEK_API_KEY;
await assert.rejects(() => extension.requestTermEvidence(first,
  new AbortController().signal), /missing_evidence_credentials/);
process.env.DEEPSEEK_API_KEY = "fixture-fallback-key";
globalThis.fetch = async (_url, options) => {
  assert.equal(new Headers(options.headers).get("Authorization"), "Bearer fixture-fallback-key");
  return Response.json({ results: [] });
};
const empty = await extension.requestTermEvidence(first, new AbortController().signal);
assert.equal(empty.status, "no_sources");
assert.equal(empty.usage.billable_response, false);
assert.ok(!readdirSync(ctx.cwd).some(name => name.startsWith(".term-tool-")));
console.log("offline term tools passed");
