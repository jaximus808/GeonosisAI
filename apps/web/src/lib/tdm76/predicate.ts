// TDM-76: a droid's WAITING_FOR predicate, in plain words, plus how close it is to true.
//
// Shape (TDM-30 / TDM-27 resume bridge): a leaf is {table, field?, op?, value?, agg?, where?, raw?}
// and every other key is an equality filter (e.g. ticket_id). Groups are {all:[...]} / {any:[...]}.
// Display only: the resume bridge (services/orchestrator/.../resume_bridge.py) is what evaluates it.

export type PredicateLeaf = {
  kind: 'leaf';
  table: string;
  field?: string;
  op: string;
  value?: unknown;
  agg?: string;
  filters: [string, unknown][];
  raw?: string;
};
export type PredicateGroup = { kind: 'all' | 'any'; children: PredicateNode[] };
export type PredicateNode = PredicateLeaf | PredicateGroup;

const RESERVED = new Set(['table', 'field', 'op', 'value', 'agg', 'where', 'raw', 'predicate']);

export function parsePredicate(json: string | undefined | null): PredicateNode | undefined {
  if (!json) return undefined;
  try {
    return toNode(JSON.parse(json));
  } catch {
    return undefined;
  }
}

export function toNode(o: unknown, depth = 0): PredicateNode | undefined {
  if (!o || typeof o !== 'object' || Array.isArray(o) || depth > 8) return undefined;
  const obj = o as Record<string, unknown>;
  for (const k of ['all', 'any'] as const) {
    if (Array.isArray(obj[k])) {
      const children = (obj[k] as unknown[]).map((c) => toNode(c, depth + 1)).filter(Boolean) as PredicateNode[];
      return { kind: k, children };
    }
  }
  if (typeof obj.table !== 'string') return undefined;
  const filters: [string, unknown][] = Object.entries(obj).filter(([k]) => !RESERVED.has(k));
  if (obj.where && typeof obj.where === 'object') filters.push(...Object.entries(obj.where as Record<string, unknown>));
  return {
    kind: 'leaf',
    table: obj.table,
    field: typeof obj.field === 'string' && obj.field ? obj.field : undefined,
    op: typeof obj.op === 'string' ? obj.op : '==',
    value: obj.value,
    agg: typeof obj.agg === 'string' ? obj.agg : undefined,
    filters,
    raw: typeof obj.raw === 'string' ? obj.raw : typeof obj.predicate === 'string' ? obj.predicate : undefined,
  };
}

export function leaves(n: PredicateNode | undefined): PredicateLeaf[] {
  if (!n) return [];
  return n.kind === 'leaf' ? [n] : n.children.flatMap(leaves);
}

// Nouns for the counters droids actually wait on. Anything else is spelled out from the names.
const COUNTER_NOUN: Record<string, string> = {
  'dataset.accepted_count': 'accepted episodes',
  'dataset.total_count': 'recorded episodes',
  'eval_run.completed': 'finished eval trials',
  'eval_run.successes': 'successful eval trials',
};
const TABLE_NOUN: Record<string, string> = {
  episode: 'episodes',
  review: 'reviews',
  eval_trial: 'eval trials',
  task: 'tasks',
  payment: 'payments',
  training_run: 'training runs',
  eval_run: 'eval runs',
  qa_result: 'QA results',
  dataset_offer: 'dataset offers',
  failure_cluster: 'failure clusters',
};

const words = (s: string) => s.replace(/_/g, ' ');
const fmtVal = (v: unknown) => (typeof v === 'string' ? v.replace(/_/g, ' ') : JSON.stringify(v));

function filterWords(l: PredicateLeaf): string {
  const f = l.filters.filter(([k]) => k !== 'ticket_id');
  if (f.length === 0) return '';
  return ' where ' + f.map(([k, v]) => (v === true ? words(k) : v === false ? `not ${words(k)}` : `${words(k)} is ${fmtVal(v)}`)).join(' and ');
}

/** "waiting for 3 accepted episodes", "waiting until training run status is done". */
export function leafWords(l: PredicateLeaf): string {
  const key = `${l.table}.${l.field ?? ''}`;
  const n = typeof l.value === 'number' ? l.value : Number(l.value);
  const numeric = l.value !== undefined && l.value !== null && l.value !== '' && Number.isFinite(n);
  const countLike = l.agg === 'count' || COUNTER_NOUN[key] !== undefined || (l.field ?? '').endsWith('_count');
  if (countLike && numeric && (l.op === '>=' || l.op === '>')) {
    const noun =
      COUNTER_NOUN[key] ??
      (l.agg === 'count' ? (TABLE_NOUN[l.table] ?? `${words(l.table)} rows`) : words((l.field ?? '').replace(/_count$/, 's')));
    const target = l.op === '>' ? n + 1 : n;
    return `waiting for ${target} ${noun}${filterWords(l)}`;
  }
  if (!l.field && !l.agg) return `waiting for a ${words(l.table)} row${filterWords(l)}`;
  const subject = l.agg ? `${l.agg} of ${words(l.table)}${l.field ? ` ${words(l.field)}` : ''}` : `${words(l.table)} ${words(l.field!)}`;
  const opw: Record<string, string> = {
    '==': 'is', '!=': 'is no longer', '>=': 'reaches', '>': 'goes above', '<': 'drops below', '<=': 'drops to',
  };
  return `waiting until ${subject} ${opw[l.op] ?? l.op} ${fmtVal(l.value)}${filterWords(l)}`;
}

export function predicateWords(n: PredicateNode | undefined): string {
  if (!n) return 'waiting';
  if (n.kind === 'leaf') return leafWords(n);
  const parts = n.children.map((c) => predicateWords(c).replace(/^waiting (for|until) /, ''));
  return `waiting for ${parts.join(n.kind === 'all' ? ' and ' : ' or ')}`;
}

/** Target number when the leaf is a "reach N" counter, for a progress fraction. */
export function leafTarget(l: PredicateLeaf): number | undefined {
  const n = typeof l.value === 'number' ? l.value : Number(l.value);
  if (!Number.isFinite(n)) return undefined;
  if (l.op === '>=') return n;
  if (l.op === '>') return n + 1;
  return undefined;
}

const camel = (s: string) => s.replace(/_([a-z0-9])/g, (_m, c: string) => c.toUpperCase());

function num(v: unknown): number | undefined {
  if (typeof v === 'number') return v;
  if (typeof v === 'bigint') return Number(v);
  if (typeof v === 'boolean') return v ? 1 : 0;
  if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))) return Number(v);
  return undefined;
}

function eqLoose(a: unknown, b: unknown): boolean {
  const na = num(a), nb = num(b);
  if (na !== undefined && nb !== undefined) return na === nb;
  if (a && typeof a === 'object' && 'tag' in (a as object)) return (a as { tag: string }).tag === b; // sum-type enums
  return a === b;
}

/**
 * Current value of a leaf over live rows (TS bindings rows, camelCase fields). Mirrors the bridge:
 * count -> number of matching rows; sum/min/max over the field; plain field -> the best matching
 * row's value (any row satisfying the predicate is enough, so take the max for >=/>).
 */
export function leafCurrent(l: PredicateLeaf, rows: readonly object[]): number | undefined {
  const sel = rows.filter((r) =>
    l.filters.every(([k, v]) => {
      const rec = r as Record<string, unknown>;
      const key = k in rec ? k : camel(k);
      return key in rec && eqLoose(rec[key], v);
    }),
  );
  if (l.agg === 'count') return sel.length;
  if (!l.field) return sel.length;
  const vals = sel.map((r) => num((r as Record<string, unknown>)[camel(l.field!)])).filter((v): v is number => v !== undefined);
  if (vals.length === 0) return undefined;
  if (l.agg === 'sum') return vals.reduce((a, b) => a + b, 0);
  if (l.agg === 'min' || l.op === '<' || l.op === '<=') return Math.min(...vals);
  return Math.max(...vals);
}

/** camelCase accessor name of a snake_case table (bindings convention). */
export const tableAccessor = camel;
