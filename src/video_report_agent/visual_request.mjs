// One multimodal request through the report model's Pi runtime and authentication.
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';

const [piRoot, agentDir] = process.argv.slice(2);
const { ModelRuntime } = await import(pathToFileURL(join(piRoot, 'dist/core/model-runtime.js')));
const { AuthStorage } = await import(pathToFileURL(join(piRoot, 'dist/core/auth-storage.js')));
const { SettingsManager } = await import(pathToFileURL(join(piRoot, 'dist/core/settings-manager.js')));
const { applyHttpProxySettings, configureHttpDispatcher } = await import(
  pathToFileURL(join(piRoot, 'dist/core/http-dispatcher.js')));
let input = '';
for await (const chunk of process.stdin) input += chunk;
const request = JSON.parse(input);
let requestCount = 0;
try {
  const settings = SettingsManager.create(process.cwd(), agentDir, { projectTrusted: false });
  applyHttpProxySettings(settings.getGlobalSettings().httpProxy);
  configureHttpDispatcher();
  const runtime = await ModelRuntime.create({
    credentials: AuthStorage.create(join(agentDir, 'auth.json')),
    modelsPath: join(agentDir, 'models.json'), allowModelNetwork: false,
  });
  if (runtime.getError()) throw new Error('Invalid model configuration');
  const model = runtime.getModel(request.provider, request.model);
  if (!model) throw new Error('Unknown report model');
  if (!model.input.includes('image')) {
    process.stdout.write(JSON.stringify({ error: 'image_input_required', request_count: 0 }));
    process.exit(1);
  }
  requestCount = 1;
  let response = await runtime.completeSimple(model, request.context, {
    maxTokens: request.max_tokens, maxRetries: 0,
    reasoning: request.thinking === 'off' ? undefined : request.thinking,
    ...(request.api_key ? { apiKey: request.api_key } : {}),
    signal: AbortSignal.timeout(180000),
  });
  if (['error', 'aborted'].includes(response.stopReason)) {
    // Provider error bodies can echo authentication data. Retain only safe usage/status.
    response = { role: 'assistant', provider: model.provider, model: model.id, content: [],
      usage: response.usage, stopReason: response.stopReason, timestamp: response.timestamp };
  }
  process.stdout.write(JSON.stringify({ response, request_count: requestCount }));
} catch {
  // Do not expose provider errors or credentials through subprocess diagnostics.
  process.stdout.write(JSON.stringify({ error: 'visual_request_failed', request_count: requestCount }));
  process.exitCode = 1;
}
