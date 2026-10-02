// Resolve only this task's model configuration on the trusted side, without API calls.
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
const [piRoot, agentDir] = process.argv.slice(2);
const { ModelRuntime } = await import(pathToFileURL(join(piRoot, 'dist/core/model-runtime.js')));
const { AuthStorage } = await import(pathToFileURL(join(piRoot, 'dist/core/auth-storage.js')));
let input = '';
for await (const chunk of process.stdin) input += chunk;
const selections = JSON.parse(input);
const runtime = await ModelRuntime.create({
  credentials: AuthStorage.create(join(agentDir, 'auth.json')),
  modelsPath: join(agentDir, 'models.json'), allowModelNetwork: false,
});
if (runtime.getError()) throw new Error('Invalid model configuration');
const literal = value => value.replaceAll('$', () => '$$').replace(/^!/, '$!');
const providers = {}, auth = {};
for (const selection of selections) {
  const model = runtime.getModel(selection.provider, selection.model);
  if (!model || runtime.isUsingOAuth(selection.provider))
    throw new Error('Task isolation requires a configured API-key model');
  const resolution = await runtime.getAuth(model);
  const key = selection.api_key || resolution?.auth.apiKey;
  if (!key) throw new Error('Missing task model API key');
  const definition = {};
  for (const field of ['id', 'name', 'api', 'baseUrl', 'reasoning', 'input', 'cost',
                       'contextWindow', 'maxTokens', 'compat', 'thinkingLevelMap',
                       'inputLimits', 'promptCache', 'samplingParams']) {
    if (model[field] !== undefined) definition[field] = model[field];
  }
  definition.baseUrl = resolution?.auth.baseUrl || model.baseUrl;
  if (resolution?.auth.headers) definition.headers = Object.fromEntries(
    Object.entries(resolution.auth.headers).map(([name, value]) => [name, literal(value)]));
  const provider = providers[selection.provider] ||= { models: [] };
  if (!provider.models.some(item => item.id === model.id)) provider.models.push(definition);
  auth[selection.provider] = { type: 'api_key', key: literal(key) };
}
process.stdout.write(JSON.stringify({ models: { providers }, auth }));
