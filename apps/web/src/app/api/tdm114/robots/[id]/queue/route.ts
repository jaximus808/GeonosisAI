// TDM-114: GET /api/tdm114/robots/{id}/queue -> GET /robots/{id}/queue (who holds the robot, what's next). Read-only.
import type { NextRequest } from 'next/server';
import { TDM114_ID, tdm114Forward } from '@/lib/tdm114/server';

type Ctx = { params: Promise<{ id: string }> };

export async function GET(_req: NextRequest, ctx: Ctx) {
  const { id } = await ctx.params;
  if (!TDM114_ID.test(id)) return Response.json({ detail: 'bad robot id' }, { status: 404 });
  return tdm114Forward(`robots/${id}/queue`, { method: 'GET' });
}
