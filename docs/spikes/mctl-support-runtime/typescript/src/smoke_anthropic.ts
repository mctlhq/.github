// Real-Anthropic smoke for the runtime spike. Separate from the gate: runs the
// official Anthropic TS SDK tool runner (client.beta.messages.toolRunner) with
// the one fake tool. Needs ANTHROPIC_API_KEY in the environment; the key is
// read by the SDK itself and never printed or written anywhere.
import Anthropic from '@anthropic-ai/sdk';
import { betaTool } from '@anthropic-ai/sdk/helpers/beta/json-schema';
import { getServiceStatus } from './agent.js';

async function main(): Promise<number> {
  // ANTHROPIC_AUTH_TOKEN (Bearer) serves Anthropic-compatible endpoints such
  // as Hetzner inference together with ANTHROPIC_BASE_URL; the SDK reads both.
  if (!process.env.ANTHROPIC_API_KEY && !process.env.ANTHROPIC_AUTH_TOKEN) {
    console.log('skipped: no key');
    return 0;
  }
  const model = process.env.ANTHROPIC_MODEL ?? 'claude-sonnet-5-5';
  const client = new Anthropic();

  const tool = betaTool({
    name: 'get_service_status',
    description: 'Returns the current status of one service in the caller\'s tenant.',
    inputSchema: {
      type: 'object',
      properties: { service: { type: 'string', description: 'service name' } },
      required: ['service'],
      additionalProperties: false,
    },
    run: async ({ service }) => JSON.stringify(await getServiceStatus(service)),
  });

  // ANTHROPIC_THREAD=0 sends the full history every turn instead of the SDK's
  // default server-side thread; Anthropic-compatible endpoints without
  // threads (Hetzner inference) answer 400 otherwise.
  const runner = client.beta.messages.toolRunner({
    ...(process.env.ANTHROPIC_THREAD === '0' ? { thread: false } : {}),
    model,
    max_tokens: 300,
    max_iterations: 4,
    system: 'You are a read-only support agent. Use get_service_status for status questions and answer in one sentence.',
    messages: [{ role: 'user', content: 'status of svc-x' }],
    tools: [tool],
  });

  const final = await runner.runUntilDone();
  const text = final.content
    .filter((b): b is Extract<typeof b, { type: 'text' }> => b.type === 'text')
    .map((b) => b.text)
    .join('');
  console.log(`model=${model} stop_reason=${final.stop_reason}`);
  console.log(text);
  return 0;
}

main().then(
  (code) => process.exit(code),
  (err) => {
    console.error(`smoke failed: ${err instanceof Error ? err.message : String(err)}`);
    process.exit(1);
  },
);
