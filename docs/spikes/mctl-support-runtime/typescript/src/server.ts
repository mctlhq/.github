// A2A 1.0 HTTP+JSON server for the mctl-support runtime spike (.github#249).
// SDK-given: REST transport, agent card, auth hook (UserBuilder), call context
// (ServerCallContext), Postgres task store, request handler, cancel plumbing.
// Hand-written: bearer parsing, trace middleware, messageId idempotency,
// context-ownership refusal, the fake tool loop (agent.ts).
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import express, { type Request, type RequestHandler } from 'express';
import pg from 'pg';
import { Kysely, PostgresDialect, sql } from 'kysely';
import type { AgentCard, SendMessageRequest, Task, Message } from '@a2a-js/sdk';
import { A2AError, TaskNotFoundError } from '@a2a-js/sdk/errors';
import {
  DefaultExecutionEventBusManager,
  DefaultRequestHandler,
  defaultServerCallContextBuilder,
  type ServerCallContext,
  type ServerCallContextBuilder,
  type TaskStore,
  type User,
} from '@a2a-js/sdk/server';
import { DatabaseTaskStore } from '@a2a-js/sdk/server/database';
import { agentCardHandler, restHandler, type UserBuilder } from '@a2a-js/sdk/server/express';
import { SupportExecutor } from './agent.js';

const PORT = Number(process.env.PORT ?? 8021);
const REPLICA = process.env.REPLICA ?? 'a';
const DATABASE_URL = process.env.DATABASE_URL ?? 'postgres://spike:spike@127.0.0.1:55432/spike_ts';
const REST_BASE = '/v1';

// ---------------------------------------------------------------- auth hook
// The SDK calls the UserBuilder inside its REST router before any handler
// method runs; a throw here becomes the HTTP error response, so the executor
// is never reached for an unauthenticated call.

class SpikeUser implements User {
  constructor(private readonly principal: string) {}
  get isAuthenticated(): boolean {
    return true;
  }
  get userName(): string {
    return this.principal;
  }
}

/** A2AError with REST transport context so the SDK's restStatusFor() yields 401. */
class UnauthenticatedError extends A2AError {
  readonly transport = 'rest' as const;
  readonly statusCode = 401;
  readonly headers = { 'WWW-Authenticate': 'Bearer' };
  constructor(message: string) {
    super({ message });
    this.name = 'UnauthenticatedError';
  }
}

/** `Bearer spike-<principal>` -> principal; anything else is refused. */
export function principalOf(authorization: string | undefined): string | undefined {
  const m = /^Bearer\s+spike-(\S+)$/i.exec(authorization ?? '');
  return m?.[1];
}

const userBuilder: UserBuilder = async (req: Request) => {
  const principal = principalOf(req.header('authorization'));
  if (!principal) throw new UnauthenticatedError('Authorization: Bearer spike-<principal> is required');
  return new SpikeUser(principal);
};

// SDK hook for the per-call context: we keep the default (headers in state)
// and add which replica served the call, readable by the executor.
const contextBuilder: ServerCallContextBuilder = (opts) => {
  const ctx = defaultServerCallContextBuilder(opts);
  ctx.state.set('replica', REPLICA);
  return ctx;
};

// ------------------------------------------------------------ trace middleware
// OTel-style hook: one line per request. Express has no SDK-side server
// interceptor in @a2a-js/sdk 1.3.0 (CallInterceptor is client-only), so this
// is plain Express middleware mounted ahead of the SDK router.
const trace: RequestHandler = (req, res, next) => {
  const started = process.hrtime.bigint();
  const principal = principalOf(req.header('authorization')) ?? '-';
  let taskId = /\/tasks\/([^/:]+)/.exec(req.path)?.[1] ?? '-';
  const json = res.json.bind(res);
  res.json = (body: unknown) => {
    // message:send answers {task: {...}} (or {message: {...}}); tasks/:id answers the task itself.
    const b = (body ?? {}) as { task?: { id?: string }; id?: string; status?: unknown };
    const t = b.task?.id ?? (b.status !== undefined ? b.id : undefined);
    if (taskId === '-' && typeof t === 'string') taskId = t;
    return json(body);
  };
  res.on('finish', () => {
    const ms = Number(process.hrtime.bigint() - started) / 1e6;
    console.log(`trace task=${taskId} principal=${principal} replica=${REPLICA} ${req.method} ${req.originalUrl} -> ${res.statusCode} ${ms.toFixed(1)}ms`);
  });
  next();
};

// ------------------------------------------------------------------- store
async function openStore(): Promise<{ store: TaskStore; db: Kysely<unknown>; pool: pg.Pool }> {
  // Schema: the SDK ships `a2a-db` (kysely migrations with a shared lock, safe
  // to run from both replicas at once). It is a CLI, not a function export.
  const cli = fileURLToPath(new URL('../node_modules/@a2a-js/sdk/dist/cli/a2a_db.js', import.meta.url));
  execFileSync(process.execPath, [cli, 'upgrade', '--url', DATABASE_URL, '--store', 'tasks'], { stdio: 'inherit' });

  const pool = new pg.Pool({ connectionString: DATABASE_URL, max: 5 });
  const db = new Kysely<unknown>({ dialect: new PostgresDialect({ pool }) });
  // Hand-written: the SDK has no messageId -> task index, so idempotency needs our own table.
  // Under an advisory lock: two replicas starting at once would otherwise race
  // Postgres' CREATE TABLE IF NOT EXISTS (pg_type unique violation).
  await db.transaction().execute(async (trx) => {
    await sql`select pg_advisory_xact_lock(hashtext('spike_message_tasks'))`.execute(trx);
    await sql`create table if not exists spike_message_tasks (
      tenant varchar(255) not null, owner varchar(255) not null, message_id varchar(255) not null,
      task_id varchar(36), created_at timestamptz not null default now(),
      primary key (tenant, owner, message_id))`.execute(trx);
  });
  return { store: new DatabaseTaskStore(db), db, pool };
}

// ---------------------------------------------------------- request handler
function isTask(r: Message | Task): r is Task {
  return 'status' in r && 'artifacts' in r;
}

class SpikeRequestHandler extends DefaultRequestHandler {
  constructor(
    card: AgentCard,
    private readonly store: TaskStore,
    private readonly pool: pg.Pool,
    executor: SupportExecutor,
  ) {
    super(card, store, executor, new DefaultExecutionEventBusManager());
  }

  override async sendMessage(params: SendMessageRequest, context: ServerCallContext): Promise<Message | Task> {
    const msg = params.message;
    if (!msg?.messageId) return super.sendMessage(params, context);
    const scope = { tenant: context.tenant ?? '', owner: context.user?.userName ?? 'unknown' };

    // A context can only be continued by the principal that owns a task in it.
    // The store is owner-scoped, so a foreign context and a nonexistent one
    // look identical here: 404 either way, no existence leak.
    if (msg.contextId && !msg.taskId) {
      const page = await this.store.list(
        { tenant: scope.tenant, contextId: msg.contextId, status: 0, pageToken: '', pageSize: 1, historyLength: 0, statusTimestampAfter: undefined },
        context,
      );
      if (page.totalSize === 0) throw new TaskNotFoundError(`Context not found: ${msg.contextId}`);
    }

    // Idempotency on messageId per (tenant, owner): reserve the id first so a
    // concurrent duplicate on the other replica waits for the winner's task.
    const reserve = await this.pool.query(
      `insert into spike_message_tasks (tenant, owner, message_id) values ($1, $2, $3)
       on conflict (tenant, owner, message_id) do nothing returning message_id`,
      [scope.tenant, scope.owner, msg.messageId],
    );
    if (reserve.rowCount === 0) {
      for (let i = 0; i < 20; i++) {
        const row = await this.pool.query<{ task_id: string | null }>(
          'select task_id from spike_message_tasks where tenant=$1 and owner=$2 and message_id=$3',
          [scope.tenant, scope.owner, msg.messageId],
        );
        const taskId = row.rows[0]?.task_id;
        if (taskId) {
          const existing = await this.store.load(taskId, context);
          if (existing) return existing;
          break;
        }
        await new Promise((r) => setTimeout(r, 100));
      }
      throw new A2AError({ message: `messageId ${msg.messageId} is already being processed` });
    }

    const result = await super.sendMessage(params, context);
    if (isTask(result)) {
      await this.pool.query('update spike_message_tasks set task_id=$4 where tenant=$1 and owner=$2 and message_id=$3', [
        scope.tenant,
        scope.owner,
        msg.messageId,
        result.id,
      ]);
    }
    return result;
  }
}

// ------------------------------------------------------------------- card
const agentCard: AgentCard = {
  name: 'mctl-support (spike, typescript)',
  description: 'Read-only tenant support agent prototype: deterministic fake tool loop over get_service_status.',
  version: '0.0.1',
  provider: undefined,
  supportedInterfaces: [{ url: `http://127.0.0.1:${PORT}${REST_BASE}`, protocolBinding: 'HTTP+JSON', protocolVersion: '1.0', tenant: '' }],
  capabilities: { streaming: false, pushNotifications: false, extensions: [] },
  securitySchemes: {
    bearer: { scheme: { $case: 'httpAuthSecurityScheme', value: { description: 'Bearer spike-<principal>', scheme: 'bearer', bearerFormat: 'opaque' } } },
  },
  securityRequirements: [{ schemes: { bearer: { list: [] } } }],
  defaultInputModes: ['text/plain'],
  defaultOutputModes: ['text/plain'],
  skills: [
    {
      id: 'service-status',
      name: 'Service status',
      description: 'Answers "status of <service>" with the service status for the caller\'s tenant.',
      tags: ['support', 'read-only'],
      examples: ['status of api', 'and again?'],
      inputModes: [],
      outputModes: [],
      securityRequirements: [],
    },
  ],
  signatures: [],
};

// ------------------------------------------------------------------- main
async function main(): Promise<void> {
  const { store, pool } = await openStore();
  const handler = new SpikeRequestHandler(agentCard, store, pool, new SupportExecutor(store, REPLICA));

  const app = express();
  app.disable('x-powered-by');
  app.use(trace);
  app.use('/.well-known/agent-card.json', agentCardHandler({ agentCardProvider: handler }));
  app.use(REST_BASE, restHandler({ requestHandler: handler, userBuilder, contextBuilder }));

  const server = app.listen(PORT, '0.0.0.0', () => {
    console.log(`replica=${REPLICA} listening on :${PORT} rest=${REST_BASE} runtime=${runtimeName()}`);
  });
  const stop = () => server.close(() => pool.end().finally(() => process.exit(0)));
  process.on('SIGINT', stop);
  process.on('SIGTERM', stop);
}

function runtimeName(): string {
  const v = process.versions as Record<string, string | undefined>;
  return v.bun ? `bun ${v.bun}` : `node ${v.node}`;
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
