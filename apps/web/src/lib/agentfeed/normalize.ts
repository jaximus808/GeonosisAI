// TDM-35: AgentFeed v1, the one normalised, ordered stream per agent for the agents panel.
//
// Mirror of services/orchestrator/src/rdf_orchestrator/agents_feed.py (GET /agents). Keep the two in
// step: services/orchestrator/tests/test_tdm35_agents_feed.py runs this file under Node against the
// same golden output. Pure and import-free on purpose (Node strips the types and runs it as is).
// Shape and mapping tables: services/factory/README.md, "Agents panel feed (TDM-35)".

export const FEED_VERSION = 1;
export const TEXT_MAX = 300;
export const DEFAULT_LIMIT = 20;
const SERVICE_ACTIVE_US = 5 * 60 * 1_000_000;

export type FeedKind =
  | 'started' | 'step' | 'tool_call' | 'tool_result' | 'message'
  | 'waiting' | 'resumed' | 'done' | 'failed' | 'error' | 'info';

export type FeedEvent = {
  /** 'ev:<event_log id>' live, 'rec:<agent_session_id>:<line>' when replayed from a recording. */
  id: string;
  agent_key: string;
  ts_us: number;
  kind: FeedKind;
  /** Source event type, e.g. 'droid.tool_call', 'planner.data_plan', 'agent.resumed'. */
  type: string;
  actor: string;
  /** One line, at most 300 characters: show as is. */
  text: string;
  tool: string | null;
  ticket_id: number | null;
  turn: number | null;
  is_error: boolean;
  replay: boolean;
  data: Record<string, unknown>;
};

export type AgentUsage = {
  credits_total: number;
  turns: number;
  input_tokens: number;
  output_tokens: number;
  seconds: number;
};

export type AgentView = {
  /** 'session:<agent_session_id>' or 'service:<planner|clustering|reviewer|qa|dispatcher|eval|flywheel>'. */
  key: string;
  kind: 'droid' | 'session' | 'service';
  label: string;
  role: string;
  agent_session_id: number | null;
  ticket_id: number | null;
  /** agent_session.status for sessions; 'active' | 'idle' | 'error' for services. */
  status: string;
  current_step: string;
  last_update_us: number;
  factory_session_id: string | null;
  factory_session_ids: string[];
  waiting_for: unknown;
  /** From droid.turn_completed (credits = Factory credits, cloud: factoryCredits). Null if no turn finished. */
  usage: AgentUsage | null;
  replay: boolean;
  event_count: number;
  /** Oldest -> newest, the last `limit`. */
  events: FeedEvent[];
};

export type AgentFeed = {
  v: number;
  replay: boolean;
  generated_at_us: number;
  agents: AgentView[];
  replay_source?: string[];
  replay_reason?: string;
};

/** agent_session row in plain snake_case form (what the hook converts the bindings row into). */
export type SessionInput = {
  id: number;
  ticket_id?: number;
  role?: string;
  name?: string;
  factory_session_id?: string | null;
  factory_session_ids_json?: string;
  status?: string;
  current_step?: string;
  waiting_for_json?: string | null;
  last_update_us?: number;
};

/** event_log row in plain form. */
export type EventInput = {
  id: number | string;
  ts_us: number;
  actor: string;
  type: string;
  payload_json?: string;
  payload?: Record<string, unknown>;
};

/** Structural shape of the generated `agentSession` row (module_bindings) the hook receives. */
export type AgentSessionRowLike = {
  id: bigint | number;
  ticketId: bigint | number;
  role: string;
  name: string;
  factorySessionId?: string | null;
  factorySessionIdsJson: string;
  status: string;
  currentStep: string;
  waitingForJson?: string | null;
  lastUpdate?: { microsSinceUnixEpoch: bigint };
};

/** Structural shape of the generated `eventLog` row. */
export type EventLogRowLike = {
  id: bigint | number;
  ts?: { microsSinceUnixEpoch: bigint };
  actor: string;
  type: string;
  payloadJson: string;
};

const micros = (t: { microsSinceUnixEpoch: bigint } | undefined) => (t ? Number(t.microsSinceUnixEpoch) : 0);

/** Subscription row (camelCase, bigint, Timestamp) -> SessionInput. */
export function sessionInputFromRow(r: AgentSessionRowLike): SessionInput {
  return {
    id: Number(r.id),
    ticket_id: Number(r.ticketId),
    role: r.role,
    name: r.name,
    factory_session_id: r.factorySessionId ?? null,
    factory_session_ids_json: r.factorySessionIdsJson,
    status: r.status,
    current_step: r.currentStep,
    waiting_for_json: r.waitingForJson ?? null,
    last_update_us: micros(r.lastUpdate),
  };
}

/** Subscription row -> EventInput. */
export function eventInputFromRow(r: EventLogRowLike): EventInput {
  return { id: Number(r.id), ts_us: micros(r.ts), actor: r.actor, type: r.type, payload_json: r.payloadJson };
}

type Svc = [key: string, label: string, prefixes: string[], actors: string[]];
const SERVICES: Svc[] = [
  ['clustering', 'Failure clustering', ['diagnose.', 'planner.failure_cluster'], ['orchestrator.diagnose']],
  ['planner', 'Planner', ['planner.'], ['planner', 'orchestrator.planner']],
  ['reviewer', 'Reviewer', ['review.'], ['reviewer', 'orchestrator.reviewer']],
  ['qa', 'Dataset QA', ['qa.'], ['orchestrator.dataset_qa', 'orchestrator.llm_qa']],
  ['dispatcher', 'Dispatcher', ['dispatch.'], ['dispatcher', 'orchestrator:dispatcher']],
  ['eval', 'Eval runner', ['eval.'], ['orchestrator:eval']],
  ['flywheel', 'Flywheel', ['flywheel.'], []],
];
const SERVICE_LABELS: Record<string, string> = Object.fromEntries(SERVICES.map(([k, l]) => [k, l]));
const TEST_PREFIXES = ['test.', 'infra.', 'spike.', 'probe.'];
const NON_AGENT_ACTOR_PREFIXES = ['human:', 'contributor:', 'robot:', 'fake-robot:'];

const DROID_KIND: Record<string, FeedKind> = {
  session_started: 'started',
  session_restarted: 'started',
  turn_started: 'step',
  tool_call: 'tool_call',
  tool_result: 'tool_result',
  assistant_text: 'message',
  permission_denied: 'error',
  waiting_for: 'waiting',
  error: 'error',
};
const DROID_DUPLICATES = new Set(['agent.step', 'agent.session_updated', 'agent.waiting_for']);

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => !!v && typeof v === 'object' && !Array.isArray(v);
const truthy = (v: unknown) =>
  !(v === undefined || v === null || v === false || v === 0 || v === '' ||
    (Array.isArray(v) && v.length === 0) || (isObj(v) && Object.keys(v).length === 0));
const starts = (s: string, prefixes: string[]) => prefixes.some((p) => s.startsWith(p));

export function clip(s: unknown, n: number = TEXT_MAX): string {
  const t = (s === undefined || s === null ? '' : String(s)).split(/\s+/).filter(Boolean).join(' ');
  const cps = Array.from(t);
  return cps.length <= n ? t : cps.slice(0, n - 1).join('') + '…';
}

function toInt(v: unknown): number | null {
  if (v === undefined || v === null || v === '') return null;
  if (typeof v === 'bigint') return Number(v);
  if (typeof v === 'boolean') return v ? 1 : 0;
  if (typeof v === 'number') return Number.isFinite(v) ? Math.trunc(v) : null;
  if (typeof v === 'string' && /^\s*[-+]?\d+\s*$/.test(v)) return parseInt(v, 10);
  return null;
}

function num(v: unknown): number {
  if (!truthy(v)) return 0;
  const n = typeof v === 'bigint' ? Number(v) : Number(v);
  return Number.isFinite(n) ? n : 0;
}

function parseJson(s: unknown): unknown {
  if (typeof s !== 'string' || !s) return isObj(s) || Array.isArray(s) ? s : null;
  try {
    return JSON.parse(s);
  } catch {
    return null;
  }
}

/** Value -> text, identical to agents_feed._f. */
function f(v: unknown): string {
  if (v === undefined || v === null) return '?';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

export const isDroidRole = (role: string) => role.endsWith('_droid');

export function sessionLabel(role: string, sid: number, name = ''): string {
  const base = isDroidRole(role) ? role.slice(0, -'_droid'.length) + ' droid' : role.replaceAll('_', ' ');
  return name ? `${name} (${base} #${sid})` : `${base} #${sid}`;
}

export function serviceFor(actor: string, type: string): string | null {
  if (starts(type, TEST_PREFIXES) || starts(actor, NON_AGENT_ACTOR_PREFIXES)) return null;
  for (const [key, , prefixes] of SERVICES) if (starts(type, prefixes)) return key;
  for (const [key, , , actors] of SERVICES) if (actors.includes(actor)) return key;
  return null;
}

function isErrorType(type: string): boolean {
  const tail = type.split('.').pop() ?? '';
  return tail === 'error' || tail === 'blocked' || tail.endsWith('_failed') || tail.endsWith('_error');
}

function scalars(p: Obj, skip: string[]): string {
  const bits: string[] = [];
  for (const [k, v] of Object.entries(p)) {
    if (skip.includes(k) || v === null || v === undefined || v === '' || typeof v === 'object') continue;
    if (typeof v === 'string' && Array.from(v).length > 60) continue;
    bits.push(`${k}=${f(v)}`);
    if (bits.length === 4) break;
  }
  return bits.join(', ');
}

export function serviceText(type: string, p: Obj): string {
  const g = (k: string) => p[k];
  if (type === 'planner.data_plan') {
    return clip(`data plan: ${f(g('amount'))} × ${f(g('data_type'))} (iteration ${f(g('iteration'))}, ` +
      `est $${(num(g('est_cost_cents')) / 100).toFixed(2)}) — ${f(g('reasoning') || '')}`);
  }
  if (type === 'planner.failure_cluster') {
    return clip(`failure cluster "${f(g('label'))}" (${f(g('count'))} trials, iteration ${f(g('iteration'))})`);
  }
  if (type === 'diagnose.clusters') {
    const cl = g('clusters');
    const n = Array.isArray(cl) ? cl.length : 0;
    return clip(`${n} failure cluster${n !== 1 ? 's' : ''}` + (truthy(g('eval_run_id')) ? ` from eval ${f(g('eval_run_id'))}` : ''));
  }
  if (type === 'review.accepted' || type === 'review.rejected') {
    const verdict = type.split('.')[1];
    return clip(`episode ${f(g('episode_id'))} ${verdict}` + (truthy(g('reason')) ? `: ${f(g('reason'))}` : ''));
  }
  if (truthy(g('error'))) return clip(`${type}: ${f(g('error'))}`);
  if (truthy(g('reason')) && typeof g('reason') === 'string') return clip(`${type}: ${g('reason')}`);
  const rest = scalars(p, ['sender']);
  return clip(type + (rest ? ` (${rest})` : ''));
}

function agentEventKind(type: string, p: Obj): [FeedKind, string] {
  switch (type) {
    case 'agent.session_created':
      return ['info', clip(`session created (${f(p.role)}) ${f(p.name || '')}`)];
    case 'agent.session_updated': {
      const to = p.to;
      const kind: FeedKind = to === 'done' ? 'done' : to === 'failed' ? 'failed' : 'info';
      return [kind, clip(`status ${f(p.from)} → ${f(to)}` + (truthy(p.current_step) ? `: ${f(p.current_step)}` : ''))];
    }
    case 'agent.waiting_for': {
      const pred = p.predicate;
      const raw = isObj(pred) ? pred.raw : pred;
      return ['waiting', clip(`waiting for ${f(raw || pred)}`)];
    }
    case 'agent.resumed':
      return ['resumed', clip(`resumed (${f(p.mode)})` + (truthy(p.reason) ? `: ${f(p.reason)}` : ''))];
    case 'agent.step':
      return ['step', clip(f(p.step || ''))];
    case 'agent.resume_delivering':
      return ['info', 'resume bridge: delivering wake-up'];
    case 'agent.resume_delivered':
      return ['resumed', clip('resume bridge: delivered' + (truthy(p.protocol) ? ` (${f(p.protocol)})` : ''))];
    case 'agent.resume_failed':
    case 'agent.wait_error':
      return ['error', clip(`${type}: ${f(p.error || p.predicate || '')}`)];
    default:
      return ['info', clip(type)];
  }
}

/** One event_log row (or recorded jsonl line) -> FeedEvent, or null when it is not agent activity. */
export function normalizeEvent(ev: EventInput, replay = false): FeedEvent | null {
  const type = String(ev.type || '');
  const actor = String(ev.actor || '');
  let p: unknown = ev.payload;
  if (p === undefined || p === null) p = parseJson(ev.payload_json);
  const P: Obj = isObj(p) ? p : {};
  const eid = typeof ev.id === 'string' ? ev.id : `ev:${ev.id}`;
  const ts = toInt(ev.ts_us) || 0;
  const base = {
    id: eid, ts_us: ts, type, actor, tool: null as string | null, turn: null as number | null,
    ticket_id: toInt(P.ticket_id) || null, is_error: false, replay,
  };

  if (type.startsWith('droid.')) {
    const dk = type.slice('droid.'.length);
    const sid = toInt(P.agent_session_id);
    if (sid === null) return null;
    const data: Obj = isObj(P.data) ? P.data : {};
    let kind: FeedKind | undefined;
    if (dk === 'turn_completed') {
      const st = data.status;
      kind = st === 'done' ? 'done' : st === 'failed' ? 'failed' : 'info';
    } else {
      kind = DROID_KIND[dk];
    }
    if (!kind) return null;
    const isErr = kind === 'error' || kind === 'failed' || (dk === 'tool_result' && truthy(data.is_error));
    return {
      ...base, agent_key: `session:${sid}`, kind, text: clip(P.text || dk),
      ts_us: toInt(P.ts_us) || ts,
      tool: (P.tool as string | null | undefined) ?? null, turn: toInt(P.turn), is_error: isErr,
      data: { ...data, droid_kind: dk, role: P.role ?? null, seq: P.seq ?? null, mode: P.mode ?? null,
        factory_session_id: P.factory_session_id ?? null },
    };
  }

  if (type.startsWith('agent.')) {
    const sid = toInt(P.session_id);
    if (sid === null) return null;
    const [kind, text] = agentEventKind(type, P);
    return { ...base, agent_key: `session:${sid}`, kind, text, is_error: kind === 'error' || kind === 'failed', data: P };
  }

  const svc = serviceFor(actor, type);
  if (svc === null) return null;
  const err = isErrorType(type);
  return { ...base, agent_key: `service:${svc}`, kind: err ? 'error' : 'step', text: serviceText(type, P), is_error: err, data: P };
}

function sessionView(s: SessionInput, replay: boolean): AgentView {
  const sid = Number(s.id);
  const role = String(s.role || '');
  const ids = parseJson(s.factory_session_ids_json);
  return {
    key: `session:${sid}`, kind: isDroidRole(role) ? 'droid' : 'session',
    label: sessionLabel(role, sid, String(s.name || '')), role,
    agent_session_id: sid, ticket_id: toInt(s.ticket_id) || null,
    status: String(s.status || 'unknown'), current_step: String(s.current_step || ''),
    last_update_us: toInt(s.last_update_us) || 0,
    factory_session_id: s.factory_session_id || null,
    factory_session_ids: Array.isArray(ids) ? (ids as string[]) : [],
    waiting_for: parseJson(s.waiting_for_json),
    usage: null, replay, event_count: 0, events: [],
  };
}

function serviceView(key: string, replay: boolean): AgentView {
  return {
    key: `service:${key}`, kind: 'service', label: SERVICE_LABELS[key] ?? key, role: key,
    agent_session_id: null, ticket_id: null, status: 'idle', current_step: '',
    last_update_us: 0, factory_session_id: null, factory_session_ids: [], waiting_for: null,
    usage: null, replay, event_count: 0, events: [],
  };
}

function orderCmp(a: FeedEvent, b: FeedEvent): number {
  if (a.ts_us !== b.ts_us) return a.ts_us - b.ts_us;
  const ta = a.id.split(':').pop() ?? '';
  const tb = b.id.split(':').pop() ?? '';
  const na = /^\d+$/.test(ta) ? parseInt(ta, 10) : 0;
  const nb = /^\d+$/.test(tb) ? parseInt(tb, 10) : 0;
  if (na !== nb) return na - nb;
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

function inferStatus(evs: FeedEvent[]): string {
  for (let i = evs.length - 1; i >= 0; i--) {
    const x = evs[i];
    if (x.type === 'droid.turn_completed') return String(x.data.status || 'paused');
    if (x.kind === 'done' || x.kind === 'failed') return x.kind;
    if (x.kind === 'waiting') return 'waiting_for';
  }
  return 'running';
}

export type BuildOptions = { ticketId?: number | null; limit?: number; replay?: boolean; nowUs?: number };

/** agent_session rows + event_log rows -> AgentFeed. Same ordering and grouping as GET /agents. */
export function buildAgentFeed(sessions: Iterable<SessionInput>, events: Iterable<EventInput>, opts: BuildOptions = {}): AgentFeed {
  const ticketId = opts.ticketId ?? null;
  const limit = opts.limit ?? DEFAULT_LIMIT;
  const replay = opts.replay ?? false;
  const now = opts.nowUs ?? Date.now() * 1000;
  const agents = new Map<string, AgentView>();
  for (const s of sessions) {
    const v = sessionView(s, replay);
    agents.set(v.key, v);
  }
  const per = new Map<string, FeedEvent[]>();
  for (const raw of events) {
    const e = normalizeEvent(raw, replay);
    if (!e) continue;
    const list = per.get(e.agent_key);
    if (list) list.push(e);
    else per.set(e.agent_key, [e]);
  }
  for (const [key, list] of per) {
    let evs = list.sort(orderCmp);
    if (!agents.has(key)) {
      if (key.startsWith('service:')) agents.set(key, serviceView(key.slice('service:'.length), replay));
      else {
        const sid = parseInt(key.slice('session:'.length), 10);
        const first = evs.find((x) => x.type.startsWith('droid.'));
        const role = String((first ? first.data.role : null) || '');
        agents.set(key, sessionView({ id: sid, role, status: 'unknown' }, replay));
      }
    }
    const a = agents.get(key)!;
    if (a.kind === 'droid' || evs.some((x) => x.type.startsWith('droid.'))) evs = evs.filter((x) => !DROID_DUPLICATES.has(x.type));
    if (ticketId !== null && a.kind === 'service') evs = evs.filter((x) => x.ticket_id === ticketId);
    per.set(key, evs);
  }

  for (const [key, a] of agents) {
    const evs = per.get(key) ?? [];
    const usage: AgentUsage = { credits_total: 0, turns: 0, input_tokens: 0, output_tokens: 0, seconds: 0 };
    let hasUsage = false;
    let creditSum = 0;
    for (const x of evs) {
      if (x.type === 'droid.turn_completed') {
        const d = x.data;
        const tok: Obj = isObj(d.tokens) ? d.tokens : {};
        hasUsage = true;
        usage.turns += 1;
        creditSum += num(d.credits);
        usage.credits_total = Math.max(usage.credits_total, num(d.credits_total), creditSum);
        usage.input_tokens += Math.trunc(num(d.input_tokens || tok.input_tokens));
        usage.output_tokens += Math.trunc(num(d.output_tokens || tok.output_tokens));
        usage.seconds += num(d.seconds); // unrounded: same float sum as agents_feed.py
      }
      if (a.kind !== 'service' && a.ticket_id === null && x.ticket_id) a.ticket_id = x.ticket_id;
    }
    a.usage = hasUsage ? usage : null;
    a.event_count = evs.length;
    a.events = limit > 0 ? evs.slice(-limit) : [];
    if (evs.length) {
      const last = evs[evs.length - 1];
      a.last_update_us = Math.max(a.last_update_us, last.ts_us);
      if (a.kind === 'service') {
        a.current_step = last.text;
        a.status = last.is_error ? 'error' : now - last.ts_us <= SERVICE_ACTIVE_US ? 'active' : 'idle';
      } else if (a.status === 'unknown') {
        a.status = inferStatus(evs);
        a.current_step = a.current_step || last.text;
      }
    }
  }

  let out = [...agents.values()];
  if (ticketId !== null) out = out.filter((a) => (a.kind === 'service' ? a.event_count > 0 : a.ticket_id === ticketId));
  const group = { droid: 0, session: 1, service: 2 } as const;
  out.sort((a, b) => group[a.kind] - group[b.kind] || b.last_update_us - a.last_update_us || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
  return { v: FEED_VERSION, replay, generated_at_us: now, agents: out };
}
