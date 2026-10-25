// TDM-114: operator actions on a ticket's robot.
//   POST /api/tdm114/tickets/{id}/robot/claim_now -> POST /tickets/{id}/robot/claim_now (run this ticket's eval now)
//   POST /api/tdm114/tickets/{id}/robot/resume    -> POST /tickets/{id}/robot/resume    (end a robot-reassigned hold)
// Operator login required; the orchestrator logs robot.reassigned / robot.claim_now / robot.resume_requested.
import type { NextRequest } from 'next/server';
import { requireOperator } from '@/lib/operator/session';
import { TDM114_ID, tdm114Forward } from '@/lib/tdm114/server';

type Ctx = { params: Promise<{ id: string; action: string }> };

export async function POST(req: NextRequest, ctx: Ctx) {
  const denied = requireOperator(req);
  if (denied) return denied;
  const { id, action } = await ctx.params;
  if (!TDM114_ID.test(id) || (action !== 'claim_now' && action !== 'resume')) return Response.json({ detail: 'not found' }, { status: 404 });
  return tdm114Forward(`tickets/${id}/robot/${action}`, { method: 'POST', operator: true, by: req.headers.get('x-human-id') });
}
