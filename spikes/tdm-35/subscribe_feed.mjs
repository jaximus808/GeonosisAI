// TDM-35 proof: build AgentFeed v1 in Node from SpacetimeDB subscriptions ONLY, with the same
// apps/web normaliser (normalize.ts) and generated bindings the agents panel uses.
//
//   node spikes/tdm-35/subscribe_feed.mjs <host> <db> <out.json> [timeout_s]
//
// Prints one line per change of the droid's status / step / event count, writes the last feed to
// <out.json>, and exits once every droid is done/failed (or after timeout_s, default 120).
import { registerHooks } from 'node:module';
import { writeFileSync } from 'node:fs';

// The generated bindings import relative files without an extension; Node needs '.ts'.
registerHooks({
  resolve(spec, ctx, next) {
    try {
      return next(spec, ctx);
    } catch (e) {
      if (spec.startsWith('.') && !spec.endsWith('.ts')) return next(spec + '.ts', ctx);
      throw e;
    }
  },
});

const [host, db, out, timeoutS = '120'] = process.argv.slice(2);
const web = new URL('../../apps/web/src/', import.meta.url);
const { DbConnection } = await import(new URL('module_bindings/index.ts', web).href);
const { buildAgentFeed, sessionInputFromRow, eventInputFromRow } = await import(new URL('lib/agentfeed/normalize.ts', web).href);

let last = '';
let feed = null;
let conn = null;
let changes = 0;

function rebuild() {
  feed = buildAgentFeed([...conn.db.agentSession.iter()].map(sessionInputFromRow), [...conn.db.eventLog.iter()].map(eventInputFromRow));
  const line = feed.agents
    .map((a) => `${a.key}[${a.status}] n=${a.event_count} step=${JSON.stringify(a.current_step.slice(0, 70))}`)
    .join(' | ');
  if (line !== last) {
    last = line;
    changes++;
    console.log(`[sub ${new Date().toISOString().slice(11, 19)}] ${line}`);
  }
  const droids = feed.agents.filter((a) => a.kind === 'droid');
  if (droids.length && droids.every((a) => a.status === 'done' || a.status === 'failed')) finish(0);
}

function finish(code) {
  if (feed) writeFileSync(out, JSON.stringify({ changes, feed }, null, 1));
  console.log(`[sub] ${changes} distinct feed states; wrote ${out}`);
  process.exit(code);
}

setTimeout(() => finish(feed ? 0 : 1), Number(timeoutS) * 1000);

DbConnection.builder()
  .withUri(host)
  .withDatabaseName(db)
  .onConnect((c) => {
    conn = c;
    for (const t of [c.db.agentSession, c.db.eventLog]) {
      t.onInsert(rebuild);
      if (t.onUpdate) t.onUpdate(rebuild);
    }
    c.subscriptionBuilder()
      .onApplied(() => {
        console.log('[sub] subscription applied');
        rebuild();
      })
      .subscribe(['SELECT * FROM agent_session', 'SELECT * FROM event_log']);
  })
  .onConnectError((_c, e) => {
    console.error('[sub] connect error', e?.message ?? e);
    finish(2);
  })
  .build();
