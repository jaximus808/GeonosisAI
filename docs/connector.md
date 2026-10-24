# Claude connector (remote MCP): deploy, connect, test

`apps/mcp` is the Robot Deployment Factory's remote MCP server (TDM-80). TDM-81 added the sign-in that
claude.ai needs, a Fly deploy config and an end-to-end test. With it you can set up the harness from a
Claude chat.

- **Connector URL (after the Fly deploy):** `https://rdf-mcp.fly.dev/mcp`. If you deploy under a
  different app name, use `https://<app>.fly.dev/mcp` and set `MCP_PUBLIC_URL` to match.
- **Tools:** `create_ticket`, `ticket_status`, `approve_spend`, `get_report`, `list_robots`. Every
  human action through the connector is logged as `human:via_mcp`.

## How sign-in works

The connector accepts two kinds of credential on `POST /mcp`.

1. **A static bearer token, for local work, SDK scripts and Claude Code.** The client sends
   `Authorization: Bearer <MCP_TOKEN>`. This is the TDM-80 path, unchanged.
2. **OAuth 2.1, for claude.ai, Claude Desktop and mobile.** claude.ai custom connectors cannot send a
   fixed header: request headers are a beta for a limited set of organizations. They sign in with OAuth
   instead. `apps/mcp/src/oauth.ts` is a small authorization server for a single operator. It
   implements the parts Claude's client requires
   ([Anthropic's connector auth docs](https://claude.com/docs/connectors/building/authentication)):
   - An unauthenticated `/mcp` request gets a `401` with
     `WWW-Authenticate: Bearer resource_metadata="<url>/.well-known/oauth-protected-resource/mcp", scope="mcp"`.
   - The RFC 9728 protected resource metadata has `resource` set to exactly `<MCP_PUBLIC_URL>/mcp`, and
     one authorization server, which is this same origin.
   - The RFC 8414 metadata is at `/.well-known/oauth-authorization-server`. It advertises
     `code_challenge_methods_supported: ["S256"]`, a `registration_endpoint` and
     `client_id_metadata_document_supported: true`, with `none` listed as a token auth method.
   - Clients register through DCR (`POST /register`, RFC 7591) or through a Client ID Metadata
     Document. A CIMD is fetched only from `claude.ai` and `claude.com`.
   - Redirect URIs must be `https` on `claude.ai` or `claude.com`, which covers
     `https://claude.ai/api/mcp/auth_callback`. Loopback addresses on any port are also allowed, for
     Claude Code, the MCP Inspector and the test script. Other hosts are refused when the client
     registers.
   - The flow is authorization code with S256 PKCE. `/token` reads form-urlencoded bodies, returns
   `invalid_grant` errors that follow RFC 6749, and rotates the refresh token on every refresh.
   - **Consent:** `/authorize` shows a page that names the app and the host Claude will return to. The
     operator types `RDF_OPERATOR_PASSCODE` and clicks Approve. This is the same passcode, with the
     same constant-time check, as the web app's `/ops/login`. Wrong guesses are throttled to 8 per IP
     and 50 in total per 10 minutes. If no passcode is set, the page answers 503 and refuses.
   - **Stateless:** clients, codes and tokens are blobs signed with HMAC-SHA256, so restarts and extra
     machines keep every grant. Access tokens last 1 h (`MCP_OAUTH_ACCESS_TTL_S`) and refresh tokens
     30 days. Single-use codes and refresh-reuse detection run in memory only. A replayed code or
     refresh token revokes its whole token family on that machine.
   - **Revoke every connection:** rotate `MCP_OAUTH_SECRET`. If it is unset, the signing key comes
     from `MCP_TOKEN`, so rotating `MCP_TOKEN` also revokes everything.

## Env (Fly secrets / `[env]` in `apps/mcp/fly.toml`)

| Key | Where | Notes |
|---|---|---|
| `MCP_TOKEN` | secret | Static bearer token for SDK clients and Claude Code; ≥16 chars (`openssl rand -hex 32`). It also seeds the OAuth signing key when `MCP_OAUTH_SECRET` is unset. |
| `RDF_OPERATOR_PASSCODE` | secret | Gates the OAuth consent page. Same value as the web app. If unset, OAuth sign-in is refused. |
| `SPACETIME_TOKEN` | secret | Use the connector's **`mcp` service identity**, never the owner token (pre-run checklist). `approve_spend` accepts the `web`, `mcp` and `orchestrator` roles. |
| `ORCHESTRATOR_URL` | secret or env | The prod orchestrator from the TDM-5 Fly deploy, or a tunnel to the laptop's orchestrator. Set it as a secret with `fly secrets set`. |
| `MCP_PUBLIC_URL` | env | `https://rdf-mcp.fly.dev`. This exact origin becomes the issuer and the token audience. If unset, it comes from the request's Host / X-Forwarded-* headers, which is fine for tunnels. |
| `MCP_OAUTH_SECRET` | secret, optional | ≥32 chars. Rotating it revokes every OAuth client and token. |
| `MCP_OAUTH_REDIRECT_HOSTS` | env, optional | Default `claude.ai,claude.com`. Loopback is always allowed. `*` allows any host. |
| `MCP_OAUTH_CIMD_HOSTS` | env, optional | Default `claude.ai,claude.com`. Set `-` to turn CIMD off. |
| `MCP_OAUTH_ACCESS_TTL_S` | env, optional | Default 3600. |
| `SPACETIME_HOST`, `SPACETIME_DB_NAME`, `MCP_MAX_SPEND_CENTS`, `HOST`, `PORT` | env | Already set in `fly.toml`. |

## Deploy (TDM-5 / the user, not run tonight)

Run this from the repo root:

```sh
fly apps create rdf-mcp      # if the name is taken, pick another and update `app` + MCP_PUBLIC_URL in apps/mcp/fly.toml
fly secrets set --config apps/mcp/fly.toml \
  MCP_TOKEN=... RDF_OPERATOR_PASSCODE=... SPACETIME_TOKEN=<mcp identity token> \
  ORCHESTRATOR_URL=https://<orchestrator app>.fly.dev
fly deploy . --config apps/mcp/fly.toml --dockerfile apps/mcp/Dockerfile --ignorefile apps/mcp/Dockerfile.dockerignore
curl -s https://rdf-mcp.fly.dev/healthz          # {"ok":true,...,"oauth":"enabled"}
```

The image is `node:24-bookworm-slim`. It runs the `.ts` sources directly and holds only the lockfile,
`packages/env` and `apps/mcp`: no `.env`, no spikes. Check it locally with
`docker build -f apps/mcp/Dockerfile -t rdf-mcp .`.

## Check the deployed sign-in before using claude.ai

```sh
MCP_URL=https://rdf-mcp.fly.dev/mcp node apps/mcp/scripts/oauth-e2e.ts --auth-only
# Claude Code's identity (its published CIMD + a loopback redirect):
MCP_URL=https://rdf-mcp.fly.dev/mcp node apps/mcp/scripts/oauth-e2e.ts --auth-only \
  --cimd https://claude.ai/oauth/claude-code-client-metadata --redirect http://localhost:51234/callback
```

The script takes the passcode from `.env` and never prints it. Without `--auth-only` it also runs the
whole "set up the harness" flow and **creates a real ticket** on whatever backend that URL serves.

## Connect from claude.ai

1. **Add the connector.**
   - Pro / Max / Free: go to **Customize > Connectors** (<https://claude.ai/customize/connectors>) and
     click **Add custom connector**.
   - Team / Enterprise: an Owner goes to **Organization settings > Connectors**, clicks **Add**, then
     **Custom**, then **Web**. Members then connect from **Customize > Connectors**.
2. **Name:** `Robot Deployment Factory`.
3. **MCP server URL:** `https://rdf-mcp.fly.dev/mcp`. Include `/mcp`.
4. **Authentication.** If the dialog asks, choose **Sign in now**.
5. **OAuth client.** Leave the default, **Use Claude's published identity**. If sign-in fails with it,
   remove the connector and add it again with **Register automatically**. Leave the client ID and
   secret blank. Don't add request headers.
6. Click **Add**, then **Connect**. A popup opens our page, "Connect Claude?", and says you will go
   back to **claude.ai**. Type the operator passcode and click **Approve**. The popup closes and the
   connector shows as connected.
7. **In a chat:** click **+**, then **Connectors**, and turn on **Robot Deployment Factory**. Approve
   the tool calls as Claude asks.

You can't edit authentication settings later. To change them, remove the connector and add it again.

### Claude Code (optional)

- With OAuth: run `claude mcp add --transport http rdf https://rdf-mcp.fly.dev/mcp`, then `/mcp` in a
  session. Approve on our page with the passcode.
- With the static token:
  `claude mcp add --transport http rdf https://rdf-mcp.fly.dev/mcp --header "Authorization: Bearer $MCP_TOKEN"`.

## Example prompts (the demo)

1. "Set up a deployment for the YAM: put the cup in the bin, 80% reliable, $50 budget, 10 eval trials."
   This calls `create_ticket`, and the ticket appears on the dashboard.
2. "How is it going?" This calls `ticket_status`: the stage, the baseline or latest eval with its 95%
   CI, spend, and what the ticket is waiting on.
3. "Approve the data plan at $25." or "Approve $20 more." This calls `approve_spend`. Either it
   approves the plan, or it raises the budget, and the change is logged as `human:via_mcp`.
4. "Give me the report." This calls `get_report` and returns the report as markdown. Before the
   ticket ships, it is an interim report.

Check the audit trail with
`SELECT * FROM event_log WHERE actor = 'human:via_mcp'`. The rows are `human.run_settings_changed`
(budget) and `spend.approved` (plan).

## Proven locally (TDM-81, 2026-10-24)

The test stack was `make fake-stack` (port 3081, db `tdm81loop`), plus `rdf-orchestrator` on `:8081`,
plus `apps/mcp` on `:3181`. The client was the official MCP SDK, going through the full OAuth flow:
401, then PRM, then AS metadata, then DCR, then PKCE, then passcode consent, then token.

- `create_ticket` made ticket #1. `ticket_status` showed the baseline running. `approve_spend` raised
  the budget from $50 to $70. `get_report` returned an interim report, marked [FAKE]. After the
  baseline finished at 3/10 (30%, CI 11-60%), `approve_spend` approved data plan #1 for $25.
- `event_log` got `human.run_settings_changed` and `spend.approved` rows, both with actor
  `human:via_mcp`.
- The same flow passed over a cloudflared https quick tunnel, where the issuer came from the Host
  header. The tunnel was stopped afterwards.
- It also passed inside the Docker image, and with Claude Code's real CIMD document, which the server
  fetched from claude.ai.
- Unit tests: `pnpm --filter @rdf/mcp test` passes 45 tests, including `test/oauth.test.ts`.

## Troubleshooting

- **"Couldn't reach the MCP server":** check that `curl -si -X POST https://rdf-mcp.fly.dev/mcp`
  returns `401` with a `www-authenticate: Bearer resource_metadata=...` header. Also check that the
  `resource` in `/.well-known/oauth-protected-resource/mcp` is exactly the URL you pasted. If it isn't,
  fix `MCP_PUBLIC_URL`.
- **The popup says "redirect host ... is not allowed":** Claude used a callback host outside
  `MCP_OAUTH_REDIRECT_HOSTS`. Add that host, for example with
  `fly secrets set MCP_OAUTH_REDIRECT_HOSTS=claude.ai,claude.com,<host>`.
- **The consent page says sign-in is not set up:** `RDF_OPERATOR_PASSCODE` is missing on the Fly app.
- **Tools fail with `orchestrator_unreachable`:** `ORCHESTRATOR_URL` is wrong, or the orchestrator
  isn't reachable from Fly.
- **Sign everyone out:** `fly secrets set MCP_OAUTH_SECRET=$(openssl rand -hex 32)`.
