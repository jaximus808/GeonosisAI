// Which LLM backend produced a decision, and which one the orchestrator will use next.
//
// Shared by the server proxy (/api/llm-backend), the header chip, the /ops/settings page and the
// decision badges. Pure functions only (no React, no node APIs), so `node --test` can import it.
//
// Orchestrator contract (services/orchestrator/src/rdf_orchestrator/admin_llm_api.py):
//   GET /admin/llm  -> LlmBackendState
//   PUT /admin/llm  {backend, model?, by?} + X-RDF-Operator-Key -> LlmBackendState & {changed}
//                   403 "locked in production", 401 bad key, 503 no RDF_OPERATOR_KEY, 422 unknown backend

export const LLM_BACKENDS = ['claude_code', 'droid', 'api'] as const;
export type LlmBackend = (typeof LLM_BACKENDS)[number];

export const LLM_BACKEND_LABELS: Record<LlmBackend, string> = {
  claude_code: 'Claude Code (local)',
  droid: 'Factory droid',
  api: 'Claude API',
};

export interface LlmBackendState {
  backend: LlmBackend;
  model: string | null;
  effective_model: string;
  allowed: LlmBackend[];
  env: string;
  locked: boolean;
  source: 'default' | 'env' | 'runtime' | string;
  labels?: Partial<Record<LlmBackend, string>>;
  updated_at?: string | null;
  updated_by?: string | null;
  changed?: { from: string; to: string; model: string | null };
}

export function isLlmBackend(v: unknown): v is LlmBackend {
  return typeof v === 'string' && (LLM_BACKENDS as readonly string[]).includes(v);
}

const PROD_VALUES = new Set(['production', 'prod', 'cloud']);

/** True when an env map says this deployment is production (mirrors the orchestrator's cloud lock:
 *  RDF_DEPLOY_ENV / NEXT_PUBLIC_RDF_ENV in production|prod|cloud, or running on Fly). */
export function isProductionEnv(env: Record<string, string | undefined>): boolean {
  for (const k of ['RDF_DEPLOY_ENV', 'NEXT_PUBLIC_RDF_ENV']) {
    const v = env[k]?.trim().toLowerCase();
    if (v && PROD_VALUES.has(v)) return true;
  }
  return Boolean(env.FLY_APP_NAME?.trim());
}

/** Parse a GET/PUT body from the orchestrator; null when it is not a backend state. */
export function parseLlmBackendState(b: unknown): LlmBackendState | null {
  if (!b || typeof b !== 'object') return null;
  const o = b as Record<string, unknown>;
  if (!isLlmBackend(o.backend)) return null;
  return {
    backend: o.backend,
    model: typeof o.model === 'string' && o.model ? o.model : null,
    effective_model: typeof o.effective_model === 'string' ? o.effective_model : '',
    allowed: Array.isArray(o.allowed) ? o.allowed.filter(isLlmBackend) : [],
    env: typeof o.env === 'string' ? o.env : 'unknown',
    locked: o.locked === true,
    source: typeof o.source === 'string' ? o.source : 'unknown',
    labels: o.labels && typeof o.labels === 'object' ? (o.labels as LlmBackendState['labels']) : undefined,
    updated_at: typeof o.updated_at === 'string' ? o.updated_at : null,
    updated_by: typeof o.updated_by === 'string' ? o.updated_by : null,
    changed:
      o.changed && typeof o.changed === 'object'
        ? (o.changed as LlmBackendState['changed'])
        : undefined,
  };
}

export type LlmBadgeTone = 'api' | 'droid' | 'local' | 'unknown';

export interface LlmBadgeInfo {
  /** The backend named by the record, or null when the record does not say (older rows). */
  backend: LlmBackend | null;
  label: string;
  model: string | null;
  tone: LlmBadgeTone;
  /** One line, e.g. "Claude Code (local) · opus". */
  text: string;
}

/**
 * Badge for a decision record. `backend` comes from the record itself; a missing or unknown value is
 * shown as such and never assumed to be the API (a Claude Code or droid decision must not pass as an
 * API one). The model shown is the one that actually ran (`model`, then `served`, then `requested`).
 */
export function llmBadge(rec: {
  backend?: unknown;
  model?: unknown;
  served?: unknown;
  requested?: unknown;
} | null | undefined): LlmBadgeInfo {
  const str = (v: unknown) => (typeof v === 'string' && v.trim() ? v.trim() : null);
  const model = str(rec?.model) ?? str(rec?.served) ?? str(rec?.requested);
  const raw = str(rec?.backend);
  let backend: LlmBackend | null = null;
  let label: string;
  let tone: LlmBadgeTone;
  if (raw && isLlmBackend(raw)) {
    backend = raw;
    label = LLM_BACKEND_LABELS[raw];
    tone = raw === 'api' ? 'api' : raw === 'droid' ? 'droid' : 'local';
  } else if (raw) {
    label = `backend ${raw}`;
    tone = 'unknown';
  } else {
    label = 'backend n/a';
    tone = 'unknown';
  }
  return { backend, label, model, tone, text: model ? `${label} · ${model}` : label };
}
