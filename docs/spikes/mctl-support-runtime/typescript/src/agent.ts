// Deterministic fake tool loop for the mctl-support runtime spike (.github#249).
// No model, no network: the "model" is a fixed policy over the user text, the
// one tool is get_service_status(service). See ../README.md for the contract.
import { Role, TaskState, type Part, type Task } from '@a2a-js/sdk';
import {
  AgentEvent,
  type AgentExecutor,
  type ExecutionEventBus,
  type RequestContext,
  type TaskStore,
} from '@a2a-js/sdk/server';

const SLOW_TOOL_MS = 20_000;
const CANCEL_POLL_MS = 500;

export interface ServiceStatus {
  service: string;
  status: 'degraded';
  replicas: number;
}

/** The one tool of the loop. The slow variant sleeps but honours the signal. */
export async function getServiceStatus(
  service: string,
  opts: { slow?: boolean; signal?: AbortSignal } = {},
): Promise<ServiceStatus> {
  if (opts.slow) {
    await sleep(SLOW_TOOL_MS, opts.signal);
  }
  return { service, status: 'degraded', replicas: 1 };
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError(signal));
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    function onAbort() {
      clearTimeout(timer);
      reject(abortError(signal!));
    }
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

function abortError(signal: AbortSignal): Error {
  const err = new Error(String(signal.reason ?? 'aborted'));
  err.name = 'AbortError';
  return err;
}

function textOf(parts: Part[] | undefined): string {
  return (parts ?? [])
    .map((p) => (p.content?.$case === 'text' ? p.content.value : ''))
    .join('')
    .trim();
}

function textPart(text: string): Part {
  return { content: { $case: 'text', value: text }, metadata: undefined, filename: '', mediaType: 'text/plain' };
}

/** The fake model's policy: which tool call (if any) a user text triggers. */
type Plan = { kind: 'status'; service: string; slow: boolean } | { kind: 'again' };

function plan(text: string): Plan {
  const slow = /^slow\s+(\S+)/i.exec(text);
  if (slow) return { kind: 'status', service: slow[1], slow: true };
  const status = /^status of\s+(\S+)/i.exec(text);
  if (status) return { kind: 'status', service: status[1], slow: false };
  return { kind: 'again' };
}

export class SupportExecutor implements AgentExecutor {
  private readonly inflight = new Map<string, AbortController>();

  constructor(
    private readonly store: TaskStore,
    private readonly replica: string,
  ) {}

  /**
   * Number of user turns in this context, counted from the transcript restored
   * from the shared store (scoped to the caller by the store itself), plus the
   * current message.
   */
  private async countTurns(ctx: RequestContext): Promise<number> {
    const page = await this.store.list(
      {
        tenant: ctx.context.tenant ?? '',
        contextId: ctx.contextId,
        status: TaskState.TASK_STATE_UNSPECIFIED,
        pageToken: '',
        pageSize: 100,
        statusTimestampAfter: undefined,
      },
      ctx.context,
    );
    let turns = 0;
    for (const task of page.tasks) {
      if (task.id === ctx.taskId) continue;
      turns += (task.history ?? []).filter((m) => m.role === Role.ROLE_USER).length;
    }
    return turns + 1;
  }

  async execute(ctx: RequestContext, bus: ExecutionEventBus): Promise<void> {
    const { taskId, contextId } = ctx;
    const principal = ctx.context.user?.userName ?? 'anonymous';
    const text = textOf(ctx.userMessage.parts);
    const now = () => new Date().toISOString();

    // First event must be the task (the SDK enforces this ordering).
    const task: Task = {
      id: taskId,
      contextId,
      status: { state: TaskState.TASK_STATE_SUBMITTED, message: undefined, timestamp: now() },
      artifacts: [],
      history: [],
      metadata: { replica: this.replica },
    };
    bus.publish(AgentEvent.task(task));

    const turn = await this.countTurns(ctx);
    const controller = new AbortController();
    this.inflight.set(taskId, controller);

    bus.publish(
      AgentEvent.statusUpdate({
        taskId,
        contextId,
        status: { state: TaskState.TASK_STATE_WORKING, message: undefined, timestamp: now() },
        metadata: undefined,
      }),
    );

    // Cross-replica cancel: the SDK's event bus is in-process, so a cancel that
    // landed on the other replica is only visible in the store. Poll it while a
    // tool is running and abort the signal when the task turned canceled there.
    const poll = setInterval(() => {
      this.store
        .load(taskId, ctx.context)
        .then((t) => {
          if (t?.status?.state === TaskState.TASK_STATE_CANCELED) controller.abort('canceled via store');
        })
        .catch(() => undefined);
    }, CANCEL_POLL_MS);

    try {
      const p = plan(text);
      let answer: string;
      if (p.kind === 'status') {
        const result = await getServiceStatus(p.service, { slow: p.slow, signal: controller.signal });
        answer = `${result.service} is ${result.status} (${result.replicas} replica); principal=${principal}; turn ${turn}`;
      } else {
        answer = `turn ${turn}; principal=${principal}`;
      }
      bus.publish(
        AgentEvent.artifactUpdate({
          taskId,
          contextId,
          artifact: { artifactId: 'answer', name: 'answer', description: '', parts: [textPart(answer)], metadata: undefined, extensions: [] },
          append: false,
          lastChunk: true,
          metadata: undefined,
        }),
      );
      bus.publish(
        AgentEvent.statusUpdate({
          taskId,
          contextId,
          status: { state: TaskState.TASK_STATE_COMPLETED, message: undefined, timestamp: now() },
          metadata: undefined,
        }),
      );
    } catch (err) {
      if ((err as Error).name === 'AbortError') {
        // The loop observed the cancel inside the turn: record it on the task.
        bus.publish(
          AgentEvent.statusUpdate({
            taskId,
            contextId,
            status: { state: TaskState.TASK_STATE_CANCELED, message: undefined, timestamp: now() },
            metadata: { loop_stopped: true, stopped_on: this.replica },
          }),
        );
      } else {
        bus.publish(
          AgentEvent.statusUpdate({
            taskId,
            contextId,
            status: { state: TaskState.TASK_STATE_FAILED, message: undefined, timestamp: now() },
            metadata: { error: String((err as Error).message ?? err) },
          }),
        );
      }
    } finally {
      clearInterval(poll);
      this.inflight.delete(taskId);
    }
  }

  /** Same-replica cancel: the SDK calls this when the task's bus lives here. */
  async cancelTask(taskId: string, bus: ExecutionEventBus): Promise<void> {
    const controller = this.inflight.get(taskId);
    if (controller) {
      // execute() publishes the canceled status with loop_stopped once the tool unwinds.
      controller.abort('canceled via bus');
      return;
    }
    bus.publish(
      AgentEvent.statusUpdate({
        taskId,
        contextId: '',
        status: { state: TaskState.TASK_STATE_CANCELED, message: undefined, timestamp: new Date().toISOString() },
        metadata: { loop_stopped: true, stopped_on: this.replica },
      }),
    );
  }
}
