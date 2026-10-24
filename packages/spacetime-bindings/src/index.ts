// @rdf/spacetime-bindings (TDM-16): the generated SpacetimeDB bindings plus a small client factory.
//
//   import { createSpacetimeClient, tables } from '@rdf/spacetime-bindings';
//   const conn = createSpacetimeClient({
//     subscriptions: ['SELECT * FROM ticket'],
//     onApplied: (conn) => console.log([...conn.db.ticket.iter()]),
//   });
//   conn.db.ticket.onInsert((_ctx, row) => console.log(row.taskDescription));
//
// `src/module_bindings/` is generated: regenerate it with `pnpm gen:bindings` from the repo root
// (TS + Python in one command) after every schema change. Columns and reducer args are camelCase.
//
// Reconnect: the SDK's withAutomaticReconnect() (1 s -> 30 s backoff) is on by default. The SDK
// keeps every subscription and callback across reconnects and replays the subscriptions itself,
// so `subscriptions` are sent once, on the first connect. The *initial* connection is not retried
// by the SDK; handle `onConnectError` (or call createSpacetimeClient again) if that matters.
// Tokens: never ship SPACETIME_TOKEN (the owner token) to a browser. Browsers connect anonymously
// and keep the server-issued token (see apps/web/src/lib/spacetime/provider.tsx).
import { DbConnection, type DbConnectionBuilder } from './module_bindings';

export * from './module_bindings';

export const DEFAULT_SPACETIME_HOST = 'https://maincloud.spacetimedb.com';
export const DEFAULT_SPACETIME_DB = 'robo-factory-a706j';

type ReconnectOptions = NonNullable<Parameters<DbConnectionBuilder['withAutomaticReconnect']>[0]>;
type OnConnect = Parameters<DbConnectionBuilder['onConnect']>[0];
type OnReconnect = Parameters<DbConnectionBuilder['onAutomaticReconnect']>[0];
type OnDisconnect = Parameters<DbConnectionBuilder['onDisconnect']>[0];
type OnConnectError = Parameters<DbConnectionBuilder['onConnectError']>[0];

export interface SpacetimeClientOptions {
  /** Server URL. Default: SPACETIME_HOST (Node only) or maincloud. */
  host?: string;
  /** Database name. Default: SPACETIME_DB_NAME (Node only) or robo-factory-a706j. */
  databaseName?: string;
  /** Identity token. Omit for a fresh anonymous identity; persist the one onConnect gives you. */
  token?: string;
  /** SQL queries subscribed once on first connect (replayed by the SDK after reconnects). */
  subscriptions?: string[];
  /** Called each time the subscription set is applied (first connect). */
  onApplied?: (conn: DbConnection) => void;
  /** Subscription error (bad query, revoked access). */
  onSubscriptionError?: (error: unknown) => void;
  /** Reconnect policy; `false` disables automatic reconnect. Default: SDK defaults (1 s -> 30 s). */
  reconnect?: ReconnectOptions | false;
  onConnect?: OnConnect;
  onReconnect?: OnReconnect;
  onDisconnect?: OnDisconnect;
  onConnectError?: OnConnectError;
}

function nodeEnv(name: string): string | undefined {
  const p = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process;
  const v = p?.env?.[name];
  return v && v.trim() ? v.trim() : undefined;
}

/** A configured builder (for React's SpacetimeDBProvider, which wants a builder, not a connection). */
export function spacetimeConnectionBuilder(opts: SpacetimeClientOptions = {}): DbConnectionBuilder {
  const host = opts.host ?? nodeEnv('SPACETIME_HOST') ?? DEFAULT_SPACETIME_HOST;
  const db = opts.databaseName ?? nodeEnv('SPACETIME_DB_NAME') ?? DEFAULT_SPACETIME_DB;
  let b = DbConnection.builder().withUri(host).withDatabaseName(db).withToken(opts.token);
  if (opts.reconnect !== false) b = b.withAutomaticReconnect(opts.reconnect || undefined);

  const subs = opts.subscriptions ?? [];
  b = b.onConnect((conn, identity, token) => {
    if (subs.length) {
      conn
        .subscriptionBuilder()
        .onApplied(() => opts.onApplied?.(conn))
        .onError((ctx) => opts.onSubscriptionError?.((ctx as { event?: unknown }).event ?? ctx))
        .subscribe(subs);
    }
    opts.onConnect?.(conn, identity, token);
  });
  if (opts.onReconnect) b = b.onAutomaticReconnect(opts.onReconnect);
  if (opts.onDisconnect) b = b.onDisconnect(opts.onDisconnect);
  if (opts.onConnectError) b = b.onConnectError(opts.onConnectError);
  return b;
}

/** Build and open a connection with automatic reconnect and the given subscriptions. */
export function createSpacetimeClient(opts: SpacetimeClientOptions = {}): DbConnection {
  return spacetimeConnectionBuilder(opts).build();
}
