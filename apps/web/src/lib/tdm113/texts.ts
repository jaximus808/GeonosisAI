// TDM-113: text job offers (iMessage via Inkbox). Pure helpers, unit-tested in test/tdm113-texts.test.ts.
//
// The consent text is shown next to the checkbox and sent back verbatim with the opt-in; the orchestrator
// (services/orchestrator/src/rdf_orchestrator/text_offers.py CONSENT_TEXT) refuses any other text, so a
// number is only ever stored with the exact consent the person saw.

export const TDM113_CONSENT_TEXT = 'GeonosisAI will text you paid robot jobs. Msg frequency varies. Reply STOP to stop.';

export type Tdm113Route = { target: string; operator: boolean };

const TDM113_POST = new Set(['optin', 'state', 'resend', 'optout']);

/** /api/tdm113/<path> -> the orchestrator path, else null. Contributor routes carry the browser's own identity token. */
export function tdm113Route(method: 'GET' | 'POST', parts: string[]): Tdm113Route | null {
  if (parts.length !== 1) return null;
  if (method === 'GET') return parts[0] === 'status' ? { target: 'texts/status', operator: true } : null;
  return TDM113_POST.has(parts[0]) ? { target: `texts/${parts[0]}`, operator: false } : null;
}

/** Where a text claimant lands from /j/<task>/text: the task is already theirs, so straight to its work page. */
export function tdm113WorkTarget(task: string | number, type: unknown, status: unknown): string | null {
  if (status !== 'claimed') return null;
  if (type === 'reset') return `/reset/${task}`;
  if (type === 'clean_teleop' || type === 'recovery_takeover') return `/teleop/${task}`;
  return null;
}

/** US-first phone check for the form (the server normalizes and validates again). */
export function tdm113PhoneOk(raw: string): boolean {
  const s = raw.trim();
  const digits = s.replace(/\D/g, '');
  if (s.startsWith('+')) return digits.length >= 8 && digits.length <= 15 && !digits.startsWith('0');
  return digits.length === 10 || (digits.length === 11 && digits.startsWith('1'));
}
