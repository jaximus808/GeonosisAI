// TDM-113: text job offers (iMessage via Inkbox). Pure helpers, unit-tested in test/tdm113-texts.test.ts.
//
// The consent text is shown next to the checkbox and sent back verbatim with the opt-in; the orchestrator
// (services/orchestrator/src/rdf_orchestrator/text_offers.py CONSENT_TEXT) refuses any other text, so a
// number is only ever stored with the exact consent the person saw.

export const TDM113_CONSENT_TEXT = 'GeonosisAI will text you paid robot jobs. Msg frequency varies. Reply STOP to stop.';

export type Tdm113Route = { target: string; operator: boolean };

// release / takeover: one person on two devices (Give back control, a second device opening the job link);
// login_code / login: phone sign-in (a 6-digit code by text; the browser adopts that contributor's identity)
const TDM113_POST = new Set(['optin', 'state', 'resend', 'optout', 'release', 'takeover', 'login_code', 'login']);

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

/** TDM-57 + TDM-113: should a teleop page that just opened ask the orchestrator to move the claim to this device?
 *  - held by this contributor but not a generation this browser drove (the text YES claim, or the same person's
 *    other device): yes, so this device gets a fresh generation and the other one stops
 *  - someone else holds it: yes once; the orchestrator only agrees when it is the same person (a merged account)
 *  - free / closed / already ours: no */
export function tdm57TakeoverNeeded(state: string, gen: number | undefined, storedGen: number | undefined): boolean {
  if (state === 'held') return gen !== undefined && storedGen !== gen;
  return state === 'view_only';
}

/** 6 digits, spaces / dashes ignored. */
export function tdm113CodeOk(raw: string): string | null {
  const d = raw.replace(/[\s-]/g, '');
  return /^\d{6}$/.test(d) ? d : null;
}
