// TDM-108: job-drop short link. Every social post / share card links (and its QR code points) to
// /j/{task}/{channel}, which lands on the marketplace deep link /marketplace?task={task}&src={channel}.
// The marketplace remembers src and reports it with the claim (POST /api/tdm108/claim-src).
//
// A short path survives the demo-public edge's /join?next=... redirect intact (a query string with
// several & parameters would not), and is short enough to type off a card.
// A ?code= (RDF_DROP_SHOW_ACCESS_CODE=1) is for /join's prefill only and is dropped here.
// The Location is relative, so it stays on whatever host the phone used (tunnel or LAN).

import { tdm108ShortTarget } from '@/lib/tdm108/proxy';

type Ctx = { params: Promise<{ task: string; src: string }> };

export async function GET(_req: Request, ctx: Ctx) {
  const { task, src } = await ctx.params;
  const target = tdm108ShortTarget(task, src) ?? '/marketplace';
  return new Response(null, { status: 302, headers: { location: target, 'cache-control': 'no-store' } });
}
