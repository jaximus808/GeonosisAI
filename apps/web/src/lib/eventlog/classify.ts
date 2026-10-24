// How the event-log UI groups rows (TDM-77). Display-only: nothing here changes a row.

/**
 * Who wrote the row, from the actor string.
 * Convention (TDM-15): orchestrator | planner | reviewer | dispatcher | droid:<role> |
 * robot:<id> | human:<contributor_id> | human:via_mcp. Reducers also write the caller's
 * service role (owner, orchestrator, web, ...), `contributor:<id>` and `spacetime`.
 */
export type ActorKind = 'human' | 'agent' | 'system';

export function actorKind(actor: string): ActorKind {
  if (actor.startsWith('human:') || actor.startsWith('contributor:')) return 'human';
  if (actor === 'spacetime') return 'system';
  return 'agent';
}

export const ACTOR_KIND_LABEL: Record<ActorKind, string> = {
  human: 'Person',
  agent: 'Agent or service',
  system: 'Database module',
};

/** Rows written while building and testing, not by the factory itself. */
export const TEST_TYPE_PREFIXES = ['test.', 'infra.', 'spike.', 'probe.'] as const;

/**
 * Why a row counts as a test row, or undefined for a real one. Checked in order: the type
 * prefix, an actor segment starting with `test.`, then a top-level payload string starting
 * with `test.` (e.g. a robot named test.tdm20-bot). Rows on a test ticket are marked by the
 * caller (see useEventLog).
 */
export function testReason(type: string, actor: string, p: Record<string, unknown> | undefined): string | undefined {
  const prefix = TEST_TYPE_PREFIXES.find((x) => type.startsWith(x));
  if (prefix) return `type starts with ${prefix}`;
  if (actor.split(':').some((seg) => seg.startsWith('test.') || seg.startsWith('test-'))) return 'actor is a test identity';
  if (p) {
    for (const [k, v] of Object.entries(p)) {
      if (typeof v === 'string' && (v.startsWith('test.') || v.startsWith('test-'))) return `${k} starts with test`;
    }
  }
  return undefined;
}

export function isTestEvent(type: string): boolean {
  return TEST_TYPE_PREFIXES.some((p) => type.startsWith(p));
}

/** Parsed payload, or undefined when payload_json is not a JSON object. */
export function parsePayload(payloadJson: string): Record<string, unknown> | undefined {
  if (!payloadJson || payloadJson[0] !== '{') return undefined;
  try {
    const v = JSON.parse(payloadJson);
    return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : undefined;
  } catch {
    return undefined;
  }
}

/** The ticket a row belongs to, from payload `ticket_id` (or `ticketId`). 0 means none. */
export function payloadTicketId(p: Record<string, unknown> | undefined): string | undefined {
  if (!p) return undefined;
  const v = p.ticket_id ?? p.ticketId;
  if (typeof v === 'number' || typeof v === 'string' || typeof v === 'bigint') {
    const s = String(v);
    return s && s !== '0' ? s : undefined;
  }
  return undefined;
}

/** Short one-line preview of a payload for the timeline row. */
export function payloadPreview(payloadJson: string, p: Record<string, unknown> | undefined): string {
  if (!p) return payloadJson;
  return Object.entries(p)
    .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join('  ');
}
