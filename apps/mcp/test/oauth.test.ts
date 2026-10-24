// TDM-81: the OAuth authorization server, end to end with the official SDK client (DCR + PKCE +
// consent), plus the edges claude.ai depends on: discovery documents, the 401 challenge, redirect
// allow-listing, passcode failures, single-use codes, refresh rotation with reuse detection, CIMD,
// and the static MCP_TOKEN bearer still working.
import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { after, before, describe, it } from "node:test";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { OrchestratorClient, SpacetimeHttp } from "../src/backend.ts";
import { createMcpHttpServer } from "../src/http.ts";
import { deriveSigningKey, OAuthServer, redirectMatches } from "../src/oauth.ts";
import { connectWithOAuth, consent, MemoryOAuthProvider } from "../scripts/oauth-flow.ts";

const TOKEN = "static-token-0123456789abcdef";
const PASS = "operator-pass-" + randomBytes(6).toString("hex");
const CIMD_URL = "https://claude.ai/oauth/test-client-metadata";
const events: { event: string; fields: Record<string, unknown> }[] = [];

let backend: Server, mcp: Server, base: string, mcpUrl: string;

before(async () => {
  backend = createServer((req, res) => {
    if (req.url === "/v1/database/testdb/sql") {
      return res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify([{ schema: { elements: [{ name: { some: "id" } }, { name: { some: "name" } }, { name: { some: "embodiment" } }, { name: { some: "status" } }] }, rows: [[1, "fake-yam-1", "i2rt_yam", "online"]] }]));
    }
    res.writeHead(404, { "content-type": "application/json" }).end('{"detail":"Not Found"}');
  });
  await new Promise<void>((r) => backend.listen(0, "127.0.0.1", r));
  const backendUrl = `http://127.0.0.1:${(backend.address() as AddressInfo).port}`;
  const cimdFetch = (async (u: string | URL | Request) => {
    if (String(u) === CIMD_URL) {
      return new Response(JSON.stringify({ client_id: CIMD_URL, client_name: "Claude (test CIMD)", redirect_uris: ["http://localhost/callback", "http://127.0.0.1/callback"], token_endpoint_auth_method: "none" }), { headers: { "content-type": "application/json" } });
    }
    return new Response("nope", { status: 404 });
  }) as typeof fetch;
  const oauth = new OAuthServer({ passcode: PASS, signingKey: deriveSigningKey(undefined, TOKEN), fetch: cimdFetch, log: (event, fields) => events.push({ event, fields }) });
  mcp = createMcpHttpServer({
    token: TOKEN,
    oauth,
    deps: { orchestrator: new OrchestratorClient(backendUrl), spacetime: new SpacetimeHttp(backendUrl, "testdb", "stdb-secret"), maxSpendCents: 50_000 },
  });
  await new Promise<void>((r) => mcp.listen(0, "127.0.0.1", r));
  base = `http://127.0.0.1:${(mcp.address() as AddressInfo).port}`;
  mcpUrl = `${base}/mcp`;
});

after(() => {
  mcp.close();
  backend.close();
});

const rpc = (headers: Record<string, string> = {}) => ({
  method: "POST",
  headers: { "content-type": "application/json", accept: "application/json, text/event-stream", ...headers },
  body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }),
});

const pkce = () => {
  const verifier = randomBytes(32).toString("base64url");
  return { verifier, challenge: createHash("sha256").update(verifier).digest("base64url") };
};

async function register(redirect = "http://127.0.0.1:5555/cb", extra: Record<string, unknown> = {}) {
  const r = await fetch(`${base}/register`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ client_name: "t", redirect_uris: [redirect], ...extra }) });
  return { status: r.status, body: (await r.json()) as Record<string, any> };
}

function authUrl(clientId: string, redirect: string, challenge: string, extra: Record<string, string> = {}) {
  const u = new URL(`${base}/authorize`);
  for (const [k, v] of Object.entries({ response_type: "code", client_id: clientId, redirect_uri: redirect, code_challenge: challenge, code_challenge_method: "S256", state: "st-1", scope: "mcp", resource: mcpUrl, ...extra })) u.searchParams.set(k, v);
  return u;
}

const tokenReq = (form: Record<string, string>) =>
  fetch(`${base}/token`, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: new URLSearchParams(form).toString() });

/** Register, consent and redeem: returns the token response. */
async function fullGrant(redirect = "http://127.0.0.1:5555/cb") {
  const { body: client } = await register(redirect);
  const { verifier, challenge } = pkce();
  const c = await consent(authUrl(client.client_id, redirect, challenge), PASS);
  assert.ok(c.code, `no code: ${c.status}`);
  const t = await tokenReq({ grant_type: "authorization_code", code: c.code!, redirect_uri: redirect, client_id: client.client_id, code_verifier: verifier, resource: mcpUrl });
  return { client, code: c.code!, verifier, res: t, body: (await t.json()) as Record<string, any> };
}

describe("discovery", () => {
  it("answers an unauthenticated /mcp with 401 and a resource_metadata pointer", async () => {
    const r = await fetch(mcpUrl, rpc());
    assert.equal(r.status, 401);
    assert.equal(r.headers.get("www-authenticate"), `Bearer resource_metadata="${base}/.well-known/oauth-protected-resource/mcp", scope="mcp"`);
    const bad = await fetch(mcpUrl, rpc({ authorization: "Bearer rdf1.at.bogus.sig" }));
    assert.equal(bad.status, 401);
    assert.match(bad.headers.get("www-authenticate")!, /error="invalid_token"/);
  });

  it("serves RFC 9728 protected resource metadata whose resource is the MCP URL", async () => {
    for (const path of ["/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"]) {
      const m = (await (await fetch(base + path)).json()) as Record<string, any>;
      assert.equal(m.resource, mcpUrl);
      assert.deepEqual(m.authorization_servers, [base]);
    }
  });

  it("serves RFC 8414 metadata with S256, DCR and CIMD", async () => {
    const m = (await (await fetch(`${base}/.well-known/oauth-authorization-server`)).json()) as Record<string, any>;
    assert.equal(m.issuer, base);
    assert.deepEqual(m.code_challenge_methods_supported, ["S256"]);
    assert.equal(m.registration_endpoint, `${base}/register`);
    assert.equal(m.client_id_metadata_document_supported, true);
    assert.ok(m.token_endpoint_auth_methods_supported.includes("none"));
    assert.ok(m.scopes_supported.includes("offline_access"));
  });

  it("answers CORS preflights", async () => {
    const r = await fetch(mcpUrl, { method: "OPTIONS" });
    assert.equal(r.status, 204);
    assert.equal(r.headers.get("access-control-allow-origin"), "*");
    assert.match(r.headers.get("access-control-expose-headers")!, /www-authenticate/);
  });
});

describe("SDK client end to end", () => {
  it("connects through DCR + PKCE + passcode consent and calls a tool", async () => {
    const { client, provider } = await connectWithOAuth(mcpUrl, PASS);
    const names = (await client.listTools()).tools.map((t) => t.name);
    assert.ok(names.includes("create_ticket"));
    const r = (await client.callTool({ name: "list_robots", arguments: {} })) as { content: { text: string }[] };
    assert.match(r.content[0].text, /fake-yam-1/);
    assert.ok(provider.saved?.refresh_token, "refresh token issued");
    assert.ok(provider.saved?.access_token.startsWith("rdf1.at."));
    await client.close();
  });

  it("keeps the static MCP_TOKEN bearer working", async () => {
    const c = new Client({ name: "static", version: "0" });
    await c.connect(new StreamableHTTPClientTransport(new URL(mcpUrl), { requestInit: { headers: { authorization: `Bearer ${TOKEN}` } } }));
    assert.ok((await c.listTools()).tools.length >= 5);
    await c.close();
  });

  it("never logs the passcode or tokens", () => {
    const dump = JSON.stringify(events);
    assert.ok(!dump.includes(PASS));
    assert.ok(!dump.includes("rdf1.at."));
    assert.ok(events.some((e) => e.event === "oauth.consent_approved"));
  });
});

describe("registration", () => {
  it("accepts claude.ai's callback and loopback, refuses other hosts and plain http", async () => {
    assert.equal((await register("https://claude.ai/api/mcp/auth_callback")).status, 201);
    assert.equal((await register("https://claude.com/api/mcp/auth_callback")).status, 201);
    assert.equal((await register("http://localhost:6274/oauth/callback")).status, 201);
    const evil = await register("https://evil.example/cb");
    assert.equal(evil.status, 400);
    assert.equal(evil.body.error, "invalid_redirect_uri");
    assert.equal((await register("http://claude.ai/cb")).status, 400);
  });

  it("issues a client secret for confidential registrations and checks it", async () => {
    const redirect = "http://127.0.0.1:5555/cb";
    const { body: client } = await register(redirect, { token_endpoint_auth_method: "client_secret_post" });
    assert.ok(client.client_secret);
    const { verifier, challenge } = pkce();
    const c = await consent(authUrl(client.client_id, redirect, challenge), PASS);
    const noSecret = await tokenReq({ grant_type: "authorization_code", code: c.code!, redirect_uri: redirect, client_id: client.client_id, code_verifier: verifier });
    assert.equal(noSecret.status, 401);
    const c2 = await consent(authUrl(client.client_id, redirect, challenge), PASS);
    const ok = await tokenReq({ grant_type: "authorization_code", code: c2.code!, redirect_uri: redirect, client_id: client.client_id, client_secret: client.client_secret, code_verifier: verifier });
    assert.equal(ok.status, 200);
  });
});

describe("authorize + token", () => {
  it("matches loopback redirects on any port, other redirects exactly", () => {
    assert.ok(redirectMatches("http://localhost/callback", "http://localhost:3118/callback"));
    assert.ok(redirectMatches("http://127.0.0.1:1/callback", "http://127.0.0.1:9/callback"));
    assert.ok(!redirectMatches("http://localhost/callback", "http://localhost:3118/other"));
    assert.ok(!redirectMatches("https://claude.ai/api/mcp/auth_callback", "https://claude.ai/api/mcp/auth_callback2"));
    assert.ok(!redirectMatches("http://localhost/callback", "http://127.0.0.1/callback"));
  });

  it("shows an error page (no redirect) for an unregistered redirect_uri", async () => {
    const { body: client } = await register();
    const r = await fetch(authUrl(client.client_id, "http://127.0.0.1:5555/other", pkce().challenge), { redirect: "manual" });
    assert.equal(r.status, 400);
    assert.match(await r.text(), /not registered/);
  });

  it("requires S256 PKCE", async () => {
    const redirect = "http://127.0.0.1:5555/cb";
    const { body: client } = await register(redirect);
    const u = authUrl(client.client_id, redirect, "x");
    u.searchParams.delete("code_challenge");
    const r = await fetch(u, { redirect: "manual" });
    assert.equal(r.status, 302);
    assert.equal(new URL(r.headers.get("location")!).searchParams.get("error"), "invalid_request");
  });

  it("refuses a wrong passcode and redirects a denial with access_denied", async () => {
    const redirect = "http://127.0.0.1:5555/cb";
    const { body: client } = await register(redirect);
    const u = authUrl(client.client_id, redirect, pkce().challenge);
    const wrong = await consent(u, "not-the-passcode");
    assert.equal(wrong.status, 401);
    assert.match(wrong.html!, /Wrong passcode/);
    const denied = await consent(u, "", "deny");
    assert.equal(denied.status, 302);
    const loc = new URL(denied.location!);
    assert.equal(loc.searchParams.get("error"), "access_denied");
    assert.equal(loc.searchParams.get("state"), "st-1");
  });

  it("returns state and iss with the code, and the consent page names the redirect host", async () => {
    const redirect = "https://claude.ai/api/mcp/auth_callback";
    const { body: client } = await register(redirect);
    const u = authUrl(client.client_id, redirect, pkce().challenge);
    const html = await (await fetch(u)).text();
    assert.match(html, /<code>claude\.ai<\/code>/);
    const c = await consent(u, PASS);
    const loc = new URL(c.location!);
    assert.equal(loc.origin + loc.pathname, redirect);
    assert.equal(loc.searchParams.get("state"), "st-1");
    assert.equal(loc.searchParams.get("iss"), base);
  });

  it("rejects a bad PKCE verifier, and a replayed code revokes its tokens", async () => {
    const g = await fullGrant();
    assert.equal(g.res.status, 200);
    assert.equal(g.body.token_type, "Bearer");
    assert.equal(g.body.scope, "mcp");
    assert.equal((await fetch(mcpUrl, rpc({ authorization: `Bearer ${g.body.access_token}` }))).status, 200);
    const replay = await tokenReq({ grant_type: "authorization_code", code: g.code, redirect_uri: "http://127.0.0.1:5555/cb", client_id: g.client.client_id, code_verifier: g.verifier });
    assert.equal(replay.status, 400);
    assert.equal(((await replay.json()) as any).error, "invalid_grant");
    assert.equal((await fetch(mcpUrl, rpc({ authorization: `Bearer ${g.body.access_token}` }))).status, 401, "family revoked after replay");

    const { body: client } = await register();
    const { challenge } = pkce();
    const c = await consent(authUrl(client.client_id, "http://127.0.0.1:5555/cb", challenge), PASS);
    const bad = await tokenReq({ grant_type: "authorization_code", code: c.code!, redirect_uri: "http://127.0.0.1:5555/cb", client_id: client.client_id, code_verifier: pkce().verifier });
    assert.equal(((await bad.json()) as any).error, "invalid_grant");
  });

  it("rotates refresh tokens and revokes the family when an old one is replayed", async () => {
    const g = await fullGrant();
    const r1 = await tokenReq({ grant_type: "refresh_token", refresh_token: g.body.refresh_token, client_id: g.client.client_id });
    assert.equal(r1.status, 200);
    const t1 = (await r1.json()) as Record<string, any>;
    assert.notEqual(t1.refresh_token, g.body.refresh_token);
    assert.equal((await fetch(mcpUrl, rpc({ authorization: `Bearer ${t1.access_token}` }))).status, 200);
    const reuse = await tokenReq({ grant_type: "refresh_token", refresh_token: g.body.refresh_token, client_id: g.client.client_id });
    assert.equal(((await reuse.json()) as any).error, "invalid_grant");
    assert.equal((await fetch(mcpUrl, rpc({ authorization: `Bearer ${t1.access_token}` }))).status, 401);
    const after = await tokenReq({ grant_type: "refresh_token", refresh_token: t1.refresh_token, client_id: g.client.client_id });
    assert.equal(((await after.json()) as any).error, "invalid_grant");
  });

  it("refuses tokens for another resource and unsupported grants", async () => {
    const { body: client } = await register();
    const { verifier, challenge } = pkce();
    const c = await consent(authUrl(client.client_id, "http://127.0.0.1:5555/cb", challenge), PASS);
    const other = await tokenReq({ grant_type: "authorization_code", code: c.code!, redirect_uri: "http://127.0.0.1:5555/cb", client_id: client.client_id, code_verifier: verifier, resource: "https://other.example/mcp" });
    assert.equal(((await other.json()) as any).error, "invalid_target");
    const cc = await tokenReq({ grant_type: "client_credentials" });
    assert.equal(((await cc.json()) as any).error, "unsupported_grant_type");
  });

  it("accepts a Client ID Metadata Document from an allow-listed host (Claude Code's loopback redirect)", async () => {
    const redirect = "http://localhost:47011/callback";
    const { verifier, challenge } = pkce();
    const c = await consent(authUrl(CIMD_URL, redirect, challenge), PASS);
    assert.ok(c.code, `status ${c.status}`);
    const t = await tokenReq({ grant_type: "authorization_code", code: c.code!, redirect_uri: redirect, client_id: CIMD_URL, code_verifier: verifier });
    assert.equal(t.status, 200);
    const off = await fetch(authUrl("https://evil.example/client.json", redirect, challenge), { redirect: "manual" });
    assert.equal(off.status, 400);
  });
});

describe("no passcode configured", () => {
  it("fails closed on the consent page, static bearer unaffected", async () => {
    const oauth = new OAuthServer({ passcode: undefined, signingKey: deriveSigningKey(undefined, TOKEN) });
    const s = createMcpHttpServer({ token: TOKEN, oauth, deps: { orchestrator: new OrchestratorClient("http://127.0.0.1:1"), spacetime: new SpacetimeHttp("http://127.0.0.1:1", "x", "y"), maxSpendCents: 1 } });
    await new Promise<void>((r) => s.listen(0, "127.0.0.1", r));
    const b = `http://127.0.0.1:${(s.address() as AddressInfo).port}`;
    try {
      const reg = (await (await fetch(`${b}/register`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ redirect_uris: ["http://127.0.0.1:1/cb"] }) })).json()) as any;
      const u = new URL(`${b}/authorize`);
      for (const [k, v] of Object.entries({ response_type: "code", client_id: reg.client_id, redirect_uri: "http://127.0.0.1:1/cb", code_challenge: pkce().challenge, code_challenge_method: "S256" })) u.searchParams.set(k, v);
      assert.equal((await fetch(u)).status, 503);
      assert.equal((await fetch(`${b}/mcp`, rpc({ authorization: `Bearer ${TOKEN}` }))).status, 200);
      assert.equal(((await (await fetch(`${b}/healthz`)).json()) as any).oauth, "no_passcode");
    } finally {
      s.close();
    }
  });
});
