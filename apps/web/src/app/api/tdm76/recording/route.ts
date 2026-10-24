// TDM-76: recorded droid sessions for the agents panel's replay mode.
//
//   GET /api/tdm76/recording              -> {"files": ["1.events.jsonl", ...]}
//   GET /api/tdm76/recording?file=<name>  -> that jsonl (TDM-30 event shape v1, one event per line)
//
// Read-only, from services/factory/work/state/ (or FACTORY_STATE_DIR). File names are
// allow-listed by pattern, so nothing outside that directory can be read. Recordings hold
// droid step text, never secrets (TDM-30 never logs keys or tokens).

import { readdir, readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';

const TDM76_STATE_DIR = process.env.FACTORY_STATE_DIR || resolve(process.cwd(), '../../services/factory/work/state');
const TDM76_FILE = /^\d{1,12}\.events\.jsonl$/;

export async function GET(req: Request) {
  const file = new URL(req.url).searchParams.get('file');
  if (!file) {
    try {
      const files = (await readdir(TDM76_STATE_DIR)).filter((f) => TDM76_FILE.test(f)).sort();
      return Response.json({ files });
    } catch {
      return Response.json({ files: [] });
    }
  }
  if (!TDM76_FILE.test(file)) return new Response('bad file name', { status: 400 });
  try {
    const body = await readFile(join(TDM76_STATE_DIR, file), 'utf8');
    return new Response(body, { headers: { 'content-type': 'application/x-ndjson; charset=utf-8', 'cache-control': 'no-store' } });
  } catch {
    return new Response('no such recording', { status: 404 });
  }
}
