// TDM-114: clear a SIM robot's soft e-stop from the web (nobody stands at a sim arm). The orchestrator
// refuses real robots (409 real_robot): a person at the arm clears those. Operator login required.
import type { NextRequest } from 'next/server';
import { requireOperator } from '@/lib/operator/session';
import { TDM114_ID, tdm114Forward } from '@/lib/tdm114/server';

type Ctx = { params: Promise<{ id: string }> };

export async function POST(req: NextRequest, ctx: Ctx) {
  const denied = requireOperator(req);
  if (denied) return denied;
  const { id } = await ctx.params;
  if (!TDM114_ID.test(id)) return Response.json({ detail: 'bad robot id' }, { status: 404 });
  return tdm114Forward(`robots/${id}/estop/clear`, { method: 'POST', operator: true, by: req.headers.get('x-human-id') });
}
