// TDM-94: server-side proxy for the seller page (sibling of /api/tdm64 and /api/tdm79).
//
// The orchestrator's CORS allows GET only, so the seller's POSTs go through the Next server. Only
// this allow-list is forwarded, and the orchestrator's error body comes back unchanged (the seller
// sees the server's own message, e.g. "not bought: the offer only buys 3 episode(s) ...").
//
//   GET  /api/tdm94/offers/{id}                -> GET  /offers/{id}   (summary + finalize_note)
//   POST /api/tdm94/offers/{id}/uploads/sign   -> POST same   seller only
//   POST /api/tdm94/offers/{id}/episodes       -> POST same   seller only (the upload manifest)
//   POST /api/tdm94/offers/{id}/delivered      -> POST same   seller only
//
// "Seller only": the browser sends its OWN SpacetimeDB identity token (Authorization: Bearer, the
// anonymous identity the provider keeps in localStorage; never the owner token). We check it with
// SpacetimeDB (GET /v1/identity/{id}/verify) and compare that identity with the offer's seller
// (dataset_offer.seller_id -> contributor.identity, read over public SQL). Accepting the offer does
// not come through here: the seller calls accept_dataset_offer from the browser as itself.
//
// Orchestrator base URL: RDF_ORCHESTRATOR_URL, else NEXT_PUBLIC_ORCHESTRATOR_URL, else http://localhost:8000.

import type { NextRequest } from 'next/server';
import { SPACETIME_DB_NAME, SPACETIME_HOST } from '@/lib/spacetime/config';

const TDM94_ORCH = (
  process.env.RDF_ORCHESTRATOR_URL ||
  process.env.NEXT_PUBLIC_ORCHESTRATOR_URL ||
  'http://localhost:8000'
).replace(/\/$/, '');
const TDM94_STDB_HTTP = SPACETIME_HOST.replace(/^ws(s?):\/\//, 'http$1://').replace(/\/$/, '');
const TDM94_ID = /^\d{1,19}$/;
const TDM94_SELLER_ACTIONS = new Set(['uploads/sign', 'episodes', 'delivered']);
const TDM94_MAX_BODY = 1_000_000;

type Ctx = { params: Promise<{ path: string[] }> };

function tdm94Json(status: number, detail: unknown) {
  return Response.json({ detail }, { status, headers: { 'cache-control': 'no-store' } });
}

async function tdm94Forward(method: string, path: string, body?: string) {
  try {
    const res = await fetch(`${TDM94_ORCH}/${path}`, {
      method,
      headers: { 'content-type': 'application/json' },
      body,
      cache: 'no-store',
      signal: AbortSignal.timeout(60_000),
    });
    return new Response(await res.text(), {
      status: res.status,
      headers: { 'content-type': res.headers.get('content-type') ?? 'application/json', 'cache-control': 'no-store' },
    });
  } catch (e) {
    return tdm94Json(502, { code: 'orchestrator_unreachable', message: `Orchestrator not reachable at ${TDM94_ORCH} (${(e as Error).message})` });
  }
}

/** Rows of a public SQL query as objects (SpacetimeDB returns positional arrays plus a schema). */
async function tdm94Sql(query: string): Promise<Record<string, unknown>[]> {
  const res = await fetch(`${TDM94_STDB_HTTP}/v1/database/${SPACETIME_DB_NAME}/sql`, {
    method: 'POST',
    body: query,
    cache: 'no-store',
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) throw new Error(`SpacetimeDB SQL ${res.status}`);
  const [out] = (await res.json()) as { schema: { elements: { name: { some?: string } }[] }; rows: unknown[][] }[];
  const names = out.schema.elements.map((e) => e.name.some ?? '');
  return out.rows.map((r) => Object.fromEntries(names.map((n, i) => [n, r[i]])));
}

/** Option<Identity> from SQL JSON: [0, ["0x…"]] is some, [1, []] is none. */
function tdm94IdentityHex(v: unknown): string | null {
  if (!Array.isArray(v) || v[0] !== 0) return null;
  const inner = Array.isArray(v[1]) ? v[1][0] : v[1];
  return typeof inner === 'string' ? inner.replace(/^0x/, '').toLowerCase() : null;
}

function tdm94TokenIdentity(token: string): string | null {
  try {
    const part = token.split('.')[1];
    const json = JSON.parse(Buffer.from(part.replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString('utf8'));
    return typeof json.hex_identity === 'string' && /^[0-9a-f]{64}$/i.test(json.hex_identity) ? json.hex_identity.toLowerCase() : null;
  } catch {
    return null;
  }
}

/** null when the caller is the offer's seller, else the refusal to send back. */
async function tdm94RequireSeller(req: NextRequest, offerId: string): Promise<Response | null> {
  const auth = req.headers.get('authorization') ?? '';
  const token = auth.startsWith('Bearer ') ? auth.slice(7).trim() : '';
  const identity = token ? tdm94TokenIdentity(token) : null;
  if (!identity) {
    return tdm94Json(401, { code: 'not_signed_in', message: 'This browser has no SpacetimeDB identity yet. Reload the page and try again.' });
  }
  try {
    const ok = await fetch(`${TDM94_STDB_HTTP}/v1/identity/${identity}/verify`, {
      headers: { authorization: `Bearer ${token}` },
      cache: 'no-store',
      signal: AbortSignal.timeout(10_000),
    });
    if (ok.status !== 204 && ok.status !== 200) {
      return tdm94Json(401, { code: 'bad_identity', message: 'SpacetimeDB did not accept this browser identity.' });
    }
    const [offer] = await tdm94Sql(`SELECT * FROM dataset_offer WHERE id = ${offerId}`);
    if (!offer) return tdm94Json(404, { code: 'offer_not_found', message: `No dataset offer ${offerId}.` });
    const [seller] = await tdm94Sql(`SELECT * FROM contributor WHERE id = ${Number(offer.seller_id)}`);
    if (!seller || tdm94IdentityHex(seller.identity) !== identity) {
      return tdm94Json(403, {
        code: 'not_the_seller',
        message: `Only the seller of offer ${offerId} (${seller?.name ?? `contributor ${String(offer.seller_id)}`}) can upload to it.`,
      });
    }
    return null;
  } catch (e) {
    return tdm94Json(502, { code: 'spacetime_unreachable', message: `Could not check the seller (${(e as Error).message}).` });
  }
}

export async function GET(_req: NextRequest, ctx: Ctx) {
  const parts = (await ctx.params).path;
  if (parts.length === 2 && parts[0] === 'offers' && TDM94_ID.test(parts[1])) return tdm94Forward('GET', parts.join('/'));
  return tdm94Json(404, 'not proxied');
}

export async function POST(req: NextRequest, ctx: Ctx) {
  const parts = (await ctx.params).path;
  if (parts.length < 3 || parts[0] !== 'offers' || !TDM94_ID.test(parts[1])) return tdm94Json(404, 'not proxied');
  const action = parts.slice(2).join('/');
  if (!TDM94_SELLER_ACTIONS.has(action)) return tdm94Json(404, 'not proxied');
  const text = await req.text();
  if (text.length > TDM94_MAX_BODY) return tdm94Json(413, { code: 'too_large', message: 'Manifest is over 1 MB.' });
  let body: unknown = {};
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    return tdm94Json(422, { code: 'invalid_json', message: 'Send a JSON object.' });
  }
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return tdm94Json(422, { code: 'invalid_json', message: 'Send a JSON object.' });
  }
  const refused = await tdm94RequireSeller(req, parts[1]);
  if (refused) return refused;
  return tdm94Forward('POST', `offers/${parts[1]}/${action}`, action === 'delivered' ? '{}' : JSON.stringify(body));
}
