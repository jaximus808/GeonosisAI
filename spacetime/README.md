# spacetime/ — SpacetimeDB module

TypeScript module, published to the database **`robo-factory-a706j`** on **Maincloud**
(`https://maincloud.spacetimedb.com`). CLI version 2.13.0. Dashboard: https://spacetimedb.com/robo-factory-a706j

The full table set (TDM-8) is documented in **`schema.md`**: 20 tables, all public except the
`service_identity` allow-list. `event_log` (id, ts, actor, type, payload_json) and
`append_event(actor, type, payload_json)` are unchanged. `CLAUDE.md` here is the SpacetimeDB-generated
SDK reference for agents writing reducers.

```
spacetime.json           # module-path, server=maincloud, database=robo-factory-a706j
spacetimedb/src/schema.ts # all tables (default export = schema)
spacetimedb/src/index.ts  # module entry: re-exports the schema, init + auth reducers
spacetimedb/src/enums.ts  # allowed string-enum values, defaults, oneOf()
spacetimedb/src/auth.ts   # requireService / requireRole / requireOwner (ctx.sender checks)
```

## Setup and publish

```sh
curl -sSf https://install.spacetimedb.com | sh   # installs ~/.local/bin/spacetime
spacetime login                                   # browser login; token goes to ~/.config/spacetime/cli.toml
cd spacetime/spacetimedb && npm install && cd ..
spacetime build
spacetime publish -s maincloud robo-factory-a706j --delete-data=never --yes=remote,migrate
```

`--yes=remote,migrate` skips only the "publish to a non-local server?" prompt and the migration prompt.
If a schema change would break clients or delete data, the publish stops instead of going ahead. Do
**not** pass `--delete-data` / `-c` unless you mean to wipe the database: the event log is demo evidence.

Inspect the database with `spacetime describe -s maincloud robo-factory-a706j`, `spacetime logs -s maincloud robo-factory-a706j`
and `spacetime sql -s maincloud robo-factory-a706j "SELECT * FROM event_log"`.

## Generate client bindings (TypeScript)

```sh
spacetime generate --lang typescript --out-dir ../apps/web/src/module_bindings --module-path ./spacetimedb
```

Re-run this after every schema change. In the client, columns and reducer args are camelCase
(`payload_json` becomes `payloadJson`), and the `spacetimedb` npm package should match the CLI version (2.13.x):

```ts
import { DbConnection } from './module_bindings';
DbConnection.builder()
  .withUri('https://maincloud.spacetimedb.com')
  .withDatabaseName('robo-factory-a706j')
  .withToken(token)                       // optional; omit for an anonymous identity
  .onConnect((conn) => {
    conn.db.eventLog.onInsert((_ctx, row) => console.log(row));
    conn.subscriptionBuilder().subscribe('SELECT * FROM event_log');
    conn.reducers.appendEvent({ actor: 'web', type: 'hello', payloadJson: '{}' });
  })
  .build();
```

## HTTP API (Python services)

Base is `$SPACETIME_HOST/v1/database/$SPACETIME_DB_NAME`. Auth header: **`Authorization: Bearer $SPACETIME_TOKEN`**.

**Call a reducer:** `POST /call/<reducer>` with `Content-Type: application/json`. The body is the args as a
JSON array in declaration order, or as an object keyed by the **server-side** arg names (snake_case):

```sh
curl -X POST "$SPACETIME_HOST/v1/database/$SPACETIME_DB_NAME/call/append_event" \
  -H "Authorization: Bearer $SPACETIME_TOKEN" -H "Content-Type: application/json" \
  -d '{"actor":"planner","type":"plan.decision","payload_json":"{\"choice\":\"recovery\"}"}'
```

A success returns 200 with an empty body. Bad args return 400 with a message. An error thrown by the reducer also returns a non-2xx status.

**SQL query:** `POST /sql` with the raw SQL as the body:

```sh
curl -X POST "$SPACETIME_HOST/v1/database/$SPACETIME_DB_NAME/sql" \
  -H "Authorization: Bearer $SPACETIME_TOKEN" -d 'SELECT * FROM event_log'
```

The response is `[{"schema": {...}, "rows": [[id, [ts_micros], actor, type, payload_json], ...]}]`, so a
`timestamp` comes back as a 1-element array of microseconds since the Unix epoch.

```python
import os, requests
H = {"Authorization": f"Bearer {os.environ['SPACETIME_TOKEN']}"}
BASE = f"{os.environ['SPACETIME_HOST']}/v1/database/{os.environ['SPACETIME_DB_NAME']}"
requests.post(f"{BASE}/call/append_event", json=["planner", "plan.decision", '{"choice":"recovery"}'], headers=H).raise_for_status()
rows = requests.post(f"{BASE}/sql", data="SELECT * FROM event_log", headers=H).json()[0]["rows"]
```

### Auth notes (important for the schema)

- `SPACETIME_TOKEN` in `.env` is the CLI login token, which is the **database owner's** identity. Any
  service that uses it calls reducers as the owner (`ctx.sender` = owner).
- **Requests without a token are accepted too.** The server gives the caller a new anonymous identity,
  so anyone on the internet can call any reducer and read any `public: true` table. Reducers that matter
  (claim_task, payments, review verdicts) must check `ctx.sender` against an allowlist or a role table,
  and sensitive tables should not be `public`.
- To give each service or agent its own identity, call `POST https://maincloud.spacetimedb.com/v1/identity`
  (no auth). It returns `{"identity", "token"}`. Store the token and use it as that service's Bearer token.
