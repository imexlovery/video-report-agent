// Synthetic Responses API + real Pi extension/Python evidence flow; no live requests.
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
process.env.TERM_SEARCH_PROVIDER = "openai";
process.env.OPENAI_SEARCH_MODEL = "gpt-6.1-sol";
process.env.OPENAI_SEARCH_API_KEY = "openai-fixture-secret";
process.env.DEEPSEEK_SEARCH_API_KEY = "must-not-use-deepseek";
const tools = new Map();
extension.default({
  registerTool(tool) { tools.set(tool.name, tool); },
  async exec(command, args, options) {
    const result = spawnSync(command, args, { cwd: options.cwd, encoding: "utf8" });
    return { code: result.status, stdout: result.stdout };
  },
});
const ctx = { cwd: process.env.TERM_TEST_RUN };
const invoke = (name, params) => tools.get(name).execute("fixture", params,
  new AbortController().signal, undefined, ctx).then(result => result.details);
const url = "https://example.org/claude-code";
const analysis = "Claude Code is a coding tool. This sentence is model synthesis.";
const payload = (action = { type: "search", sources: [{ type: "url", url }] }) => ({
  status: "completed", model: "gpt-6.1-sol",
  output: [
    { type: "web_search_call", status: "completed", action },
    { type: "message", content: [{ type: "output_text", text: analysis, annotations: [
      { type: "url_citation", title: "Claude Code overview", url },
      { type: "url_citation", title: "unsafe", url: "file:///private/key" },
    ] }] },
  ],
  usage: { input_tokens: 120, output_tokens: 40, total_tokens: 160 },
});
let requests = 0;
globalThis.fetch = async (endpoint, options) => {
  requests++;
  assert.equal(endpoint, "https://api.openai.com/v1/responses");
  assert.equal(new Headers(options.headers).get("Authorization"), "Bearer openai-fixture-secret");
  const body = JSON.parse(options.body);
  assert.equal(body.model, "gpt-6.1-sol");
  assert.equal(body.max_tool_calls, 1);
  assert.equal(body.tool_choice, "required");
  assert.equal(body.store, false);
  assert.deepEqual(body.include, ["web_search_call.action.sources"]);
  assert.ok(!options.body.includes("薄肌")); // Only the query/URL leaves the run.
  if (requests === 3) return new Response(null, { status: 429 });
  if (requests === 2) {
    assert.ok(body.input.includes(url));
    return Response.json(payload({ type: "open_page", url }));
  }
  assert.equal(body.input, "Anthropic cloud coding tool name");
  return Response.json(payload(), { headers: { "x-request-id": "openai-request" } });
};
const query = { query: "Anthropic cloud coding tool name", unit_ids: ["unit-000003"] };
const first = await invoke("lookup_term", query);
assert.equal(first.provider, "openai");
assert.equal(first.model, "gpt-6.1-sol");
assert.equal(first.status, "completed");
assert.equal(first.request_id, "openai-request");
assert.equal(first.usage.web_search_calls, 1);
assert.equal(first.usage.input_tokens, 120);
assert.equal(first.sources.length, 1);
assert.equal(first.sources[0].content, "");
assert.equal(first.analysis, analysis);
const patch = { unit_id: "unit-000003", start: 0, end: 5, before: "cloud", after: "Claude Code",
  kind: "product", status: "supported", reason: "Synthetic context identifies the coding tool.",
  evidence: [{ source_id: first.sources[0].id, quote: analysis }] };
await assert.rejects(() => invoke("record_term_review", { entries: [patch] }), /Evidence must quote/);
const fetched = await invoke("fetch_term_source", { source_id: first.sources[0].id });
assert.equal(fetched.status, "completed");
assert.equal(fetched.operation, "fetch");
assert.equal(fetched.sources[0].content, "");
patch.evidence = [{ source_id: fetched.sources[0].id, quote: "Claude Code overview" }];
assert.equal((await invoke("record_term_review", { entries: [patch] })).supported, 1);
assert.ok(readFileSync(join(ctx.cwd, "transcript.corrected.md"), "utf8").includes("Claude Code帮我"));
const failure = await invoke("lookup_term", query);
assert.equal(failure.error_code, "http_429");
assert.equal(failure.provider, "openai");
await assert.rejects(() => invoke("lookup_term", query), /budget exhausted/);
assert.equal(requests, 3);
assert.ok(!readFileSync(join(ctx.cwd, "term-review.json"), "utf8").includes("fixture-secret"));

const request = lookup => extension.requestTermEvidence(lookup, new AbortController().signal);
globalThis.fetch = async () => Response.json(payload());
assert.equal((await request({ operation: "fetch", url })).error_code, "source_not_opened");
globalThis.fetch = async () => Response.json({ ...payload(), output: payload().output.slice(1) });
assert.equal((await request(query)).error_code, "search_not_performed");
globalThis.fetch = async () => Response.json({ ...payload(), status: "incomplete",
  incomplete_details: { reason: "max_output_tokens" } });
assert.equal((await request(query)).status, "incomplete");
globalThis.fetch = async () => Response.json({ status: "completed", output: [payload().output[0]] });
const uncited = await request(query);
assert.equal(uncited.sources.length, 1); // Complete source list without a message.
assert.equal(uncited.sources[0].title, ""); // Never promote a URL to source-title evidence.
globalThis.fetch = async () => { throw new DOMException("fixture timeout", "TimeoutError"); };
await assert.rejects(() => request(query), { name: "TimeoutError" });
delete process.env.OPENAI_SEARCH_API_KEY;
delete process.env.OPENAI_API_KEY;
await assert.rejects(() => request(query), /missing_evidence_credentials/);
process.env.OPENAI_API_KEY = "fallback-openai-key";
globalThis.fetch = async (_url, options) => {
  assert.equal(new Headers(options.headers).get("Authorization"), "Bearer fallback-openai-key");
  return Response.json(payload());
};
assert.equal((await request(query)).status, "completed");
process.env.TERM_SEARCH_PROVIDER = "unknown";
await assert.rejects(() => request(query), /invalid_search_provider/);
console.log("offline OpenAI term tools passed");
