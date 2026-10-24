// TDM-81: a minimal single-operator OAuth 2.1 authorization server for the claude.ai connector.
//
// claude.ai custom connectors cannot send a fixed bearer header (request headers are a limited
// beta), so they sign in with OAuth: https://claude.com/docs/connectors/building/authentication
//   - 401 + `WWW-Authenticate: Bearer resource_metadata=...` on /mcp starts the flow
//   - RFC 9728 protected resource metadata (resource = <public url>/mcp, one authorization server)
//   - RFC 8414 authorization server metadata (this same origin is the issuer)
//   - client registration: RFC 7591 DCR (`/register`) or a Client ID Metadata Document (CIMD,
//     client_id is an https URL on an allow-listed host, fetched at /authorize)
//   - authorization code + S256 PKCE only, refresh tokens rotated, form-urlencoded /token
//   - redirect URIs: https on an allow-listed host (claude.ai / claude.com by default) or loopback
//     on any port (Claude Code, MCP Inspector, the SDK test client)
// The consent page is gated by the operator passcode (RDF_OPERATOR_PASSCODE, the same secret and
// the same constant-time sha256 check as the web app's /ops/login, apps/web/src/lib/operator/session.ts).
// No passcode configured = no OAuth sign-in (fail closed); the static MCP_TOKEN bearer still works.
//
// Everything is stateless: registered clients, consent requests, codes and tokens are HMAC-signed
// blobs ("rdf1.<kind>.<payload>.<sig>"), so a restart or a second Fly machine keeps every grant.
// The in-memory part is only best-effort replay protection (single-use codes, refresh rotation with
// reuse detection that revokes the token family) and the passcode throttle. Rotating the signing key
// (MCP_OAUTH_SECRET, or MCP_TOKEN when it is unset) revokes every client and token at once.
import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import type { IncomingMessage, ServerResponse } from "node:http";

export const SCOPE = "mcp";
const SCOPES_SUPPORTED = [SCOPE, "offline_access"];
const TOKEN_PREFIX = "rdf1";

type FetchLike = typeof fetch;
type Row = Record<string, any>;

export interface OAuthOptions {
  /** RDF_OPERATOR_PASSCODE. Unset = the consent page refuses (503). */
  passcode: string | undefined;
  /** HMAC key for every signed blob. */
  signingKey: Buffer;
  /** MCP_PUBLIC_URL (e.g. https://rdf-mcp.fly.dev). Unset = derived from the request (Host / X-Forwarded-*). */
  publicUrl?: string;
  /** https redirect hosts accepted at registration (loopback is always accepted). "*" = any https host. */
  redirectHosts?: string[];
  /** Hosts whose https client_id URLs are fetched as Client ID Metadata Documents. Empty = CIMD off. */
  cimdHosts?: string[];
  accessTtlS?: number;
  refreshTtlS?: number;
  codeTtlS?: number;
  fetch?: FetchLike;
  now?: () => number;
  log?: (event: string, fields: Record<string, unknown>) => void;
}

/** The signing key: MCP_OAUTH_SECRET when set, else derived from MCP_TOKEN (domain-separated). */
export function deriveSigningKey(oauthSecret: string | undefined, mcpToken: string): Buffer {
  return createHmac("sha256", oauthSecret || mcpToken).update("rdf-mcp-oauth-v1").digest();
}

const b64u = (b: Buffer | string) => Buffer.from(b).toString("base64url");
const sha256 = (s: string) => createHash("sha256").update(s).digest();

/** Constant-time passcode check (same as the web app's checkPasscode). */
export function passcodeOk(candidate: unknown, passcode: string | undefined): boolean {
  if (!passcode || typeof candidate !== "string" || !candidate) return false;
  return timingSafeEqual(sha256(candidate), sha256(passcode));
}

function isLoopbackHost(hostname: string): boolean {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]" || hostname === "::1";
}

/** Exact match, except loopback redirects ignore the port (RFC 8252 7.3; Claude Code uses localhost too). */
export function redirectMatches(registered: string, requested: string): boolean {
  if (registered === requested) return true;
  let a: URL, b: URL;
  try {
    a = new URL(registered);
    b = new URL(requested);
  } catch {
    return false;
  }
  if (a.protocol !== "http:" || b.protocol !== "http:" || !isLoopbackHost(a.hostname) || a.hostname !== b.hostname) return false;
  return a.pathname === b.pathname && a.search === b.search && !b.hash;
}

function escapeHtml(s: unknown): string {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

class OAuthError extends Error {
  readonly error: string;
  readonly status: number;
  constructor(error: string, description: string, status = 400) {
    super(description);
    this.error = error;
    this.status = status;
  }
}

interface ClientInfo {
  client_id: string;
  client_name: string;
  redirect_uris: string[];
  auth_method: "none" | "client_secret_post" | "client_secret_basic";
  kind: "dcr" | "cimd";
}

const MAX_FORM_BYTES = 64 * 1024;

async function readBody(req: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size > MAX_FORM_BYTES) throw new OAuthError("invalid_request", "request body too large", 413);
    chunks.push(chunk as Buffer);
  }
  return Buffer.concat(chunks).toString("utf8");
}

const NO_STORE = { "cache-control": "no-store", pragma: "no-cache" };

function sendJson(res: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}): void {
  res.writeHead(status, { "content-type": "application/json", ...NO_STORE, ...headers }).end(JSON.stringify(body));
}

function oauthJsonError(res: ServerResponse, e: OAuthError, headers: Record<string, string> = {}): void {
  sendJson(res, e.status, { error: e.error, error_description: e.message }, headers);
}

const PAGE_HEADERS = {
  "content-type": "text/html; charset=utf-8",
  ...NO_STORE,
  "x-frame-options": "DENY",
  "referrer-policy": "no-referrer",
  "x-content-type-options": "nosniff",
  // No form-action: Chrome applies it to the post-consent redirect to claude.ai.
  "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
};

function page(res: ServerResponse, status: number, title: string, bodyHtml: string): void {
  res.writeHead(status, PAGE_HEADERS).end(`<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title>
<style>
:root{color-scheme:light dark;--bg:#f6f6f4;--fg:#1b1b1b;--muted:#5d5d5a;--card:#fff;--line:#d9d9d4;--accent:#1f5f4a;--warn:#8a4b00}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#ececea;--muted:#a3a39e;--card:#1e1e1e;--line:#353533;--accent:#6cc4a1;--warn:#f0a548}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:30rem;margin:8vh auto;padding:0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px}
h1{font-size:1.25rem;margin:0 0 .75rem}
p{margin:.5rem 0}.muted{color:var(--muted);font-size:.9rem}.warn{color:var(--warn)}.err{color:#c0392b}
code{font-size:.95em;word-break:break-all}
label{display:block;margin:1rem 0 .25rem;font-weight:600}
input[type=password]{width:100%;box-sizing:border-box;padding:.6rem;border:1px solid var(--line);border-radius:8px;background:transparent;color:inherit;font:inherit}
.row{display:flex;gap:.75rem;margin-top:1.25rem}
button{flex:1;padding:.65rem;border-radius:8px;border:1px solid var(--line);font:inherit;cursor:pointer;background:transparent;color:inherit}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
</style></head><body><main><div class="card">${bodyHtml}</div></main></body></html>`);
}

export class OAuthServer {
  private readonly o: Required<Omit<OAuthOptions, "publicUrl" | "passcode" | "log">> & Pick<OAuthOptions, "publicUrl" | "passcode" | "log">;
  /** Consumed single-use ids (codes, rotated refresh tokens) -> expiry ms. */
  private readonly used = new Map<string, number>();
  /** Revoked token families (after a code or refresh-token replay) -> expiry ms. */
  private readonly revoked = new Map<string, number>();
  private readonly fails = new Map<string, { n: number; until: number }>();
  private globalFails = { n: 0, until: 0 };
  private readonly cimdCache = new Map<string, { at: number; info: ClientInfo }>();

  constructor(opts: OAuthOptions) {
    this.o = {
      redirectHosts: ["claude.ai", "claude.com"],
      cimdHosts: ["claude.ai", "claude.com"],
      accessTtlS: 3600,
      refreshTtlS: 30 * 24 * 3600,
      codeTtlS: 120,
      fetch: fetch,
      now: Date.now,
      ...Object.fromEntries(Object.entries(opts).filter(([, v]) => v !== undefined)),
    } as OAuthServer["o"];
    if (this.o.publicUrl) this.o.publicUrl = this.o.publicUrl.replace(/\/+$/, "");
  }

  get enabled(): boolean {
    return Boolean(this.o.passcode);
  }

  private log(event: string, fields: Record<string, unknown> = {}): void {
    this.o.log?.(event, fields);
  }

  private nowS(): number {
    return Math.floor(this.o.now() / 1000);
  }

  // ------------------------------------------------------------------ URLs

  /** Public origin of this server, e.g. https://rdf-mcp.fly.dev (no trailing slash). */
  baseUrl(req: IncomingMessage): string {
    if (this.o.publicUrl) return this.o.publicUrl;
    const first = (h: string | string[] | undefined) => (Array.isArray(h) ? h[0] : h)?.split(",")[0]?.trim();
    const proto = first(req.headers["x-forwarded-proto"]) || "http";
    const host = first(req.headers["x-forwarded-host"]) || req.headers.host || "localhost";
    return `${proto === "https" ? "https" : "http"}://${host}`;
  }

  resourceUrl(req: IncomingMessage): string {
    return `${this.baseUrl(req)}/mcp`;
  }

  prmUrl(req: IncomingMessage): string {
    return `${this.baseUrl(req)}/.well-known/oauth-protected-resource/mcp`;
  }

  /** The WWW-Authenticate value for a 401 on /mcp. */
  challenge(req: IncomingMessage, invalidToken: boolean): string {
    const err = invalidToken ? `error="invalid_token", error_description="The access token is missing, expired or revoked", ` : "";
    return `Bearer ${err}resource_metadata="${this.prmUrl(req)}", scope="${SCOPE}"`;
  }

  protectedResourceMetadata(req: IncomingMessage): Row {
    return {
      resource: this.resourceUrl(req),
      authorization_servers: [this.baseUrl(req)],
      scopes_supported: [SCOPE],
      bearer_methods_supported: ["header"],
      resource_name: "Robot Deployment Factory",
    };
  }

  authorizationServerMetadata(req: IncomingMessage): Row {
    const base = this.baseUrl(req);
    return {
      issuer: base,
      authorization_endpoint: `${base}/authorize`,
      token_endpoint: `${base}/token`,
      registration_endpoint: `${base}/register`,
      scopes_supported: SCOPES_SUPPORTED,
      response_types_supported: ["code"],
      response_modes_supported: ["query"],
      grant_types_supported: ["authorization_code", "refresh_token"],
      token_endpoint_auth_methods_supported: ["none", "client_secret_post", "client_secret_basic"],
      code_challenge_methods_supported: ["S256"],
      client_id_metadata_document_supported: this.o.cimdHosts.length > 0,
      authorization_response_iss_parameter_supported: true,
      service_documentation: `${base}/`,
    };
  }

  // ------------------------------------------------------------------ signed blobs

  private sign(kind: string, payload: Row): string {
    const body = `${TOKEN_PREFIX}.${kind}.${b64u(JSON.stringify(payload))}`;
    return `${body}.${createHmac("sha256", this.o.signingKey).update(body).digest("base64url")}`;
  }

  private verify(kind: string, value: unknown): Row | null {
    if (typeof value !== "string" || value.length > 8192) return null;
    const parts = value.split(".");
    if (parts.length !== 4 || parts[0] !== TOKEN_PREFIX || parts[1] !== kind) return null;
    const want = createHmac("sha256", this.o.signingKey).update(parts.slice(0, 3).join(".")).digest();
    const got = Buffer.from(parts[3], "base64url");
    if (got.length !== want.length || !timingSafeEqual(got, want)) return null;
    let payload: Row;
    try {
      payload = JSON.parse(Buffer.from(parts[2], "base64url").toString("utf8"));
    } catch {
      return null;
    }
    if (typeof payload.exp === "number" && payload.exp <= this.nowS()) return null;
    return payload;
  }

  private gc(): void {
    const now = this.o.now();
    for (const m of [this.used, this.revoked]) for (const [k, exp] of m) if (exp <= now) m.delete(k);
    for (const [k, f] of this.fails) if (f.until <= now) this.fails.delete(k);
  }

  /** Mark a single-use id; false if it was already used. */
  private consume(id: string, expS: number): boolean {
    this.gc();
    if (this.used.has(id)) return false;
    this.used.set(id, expS * 1000);
    return true;
  }

  private revokeFamily(fam: string): void {
    this.revoked.set(fam, this.o.now() + this.o.refreshTtlS * 1000);
    this.log("oauth.family_revoked", { fam: fam.slice(0, 8) });
  }

  /** Validate an OAuth access token for this resource. */
  verifyAccessToken(token: string, req: IncomingMessage): Row | null {
    const p = this.verify("at", token);
    if (!p || p.aud !== this.resourceUrl(req) || this.revoked.has(p.fam)) return null;
    return p;
  }

  // ------------------------------------------------------------------ clients

  private clientSecretFor(clientId: string): string {
    return createHmac("sha256", this.o.signingKey).update(`client-secret:${clientId}`).digest("base64url");
  }

  private checkRedirectUri(u: unknown): string {
    if (typeof u !== "string" || u.length > 2000) throw new OAuthError("invalid_redirect_uri", "redirect_uris must be URL strings");
    let url: URL;
    try {
      url = new URL(u);
    } catch {
      throw new OAuthError("invalid_redirect_uri", `not a URL: ${u}`);
    }
    if (url.hash) throw new OAuthError("invalid_redirect_uri", "redirect URIs must not have a fragment");
    if (url.protocol === "http:" && isLoopbackHost(url.hostname)) return u;
    if (url.protocol !== "https:") throw new OAuthError("invalid_redirect_uri", "redirect URIs must be https (or http on a loopback address)");
    const hosts = this.o.redirectHosts;
    if (!hosts.includes("*") && !hosts.includes(url.hostname.toLowerCase())) {
      throw new OAuthError("invalid_redirect_uri", `redirect host ${url.hostname} is not allowed on this server (allowed: ${hosts.join(", ")}, or loopback)`);
    }
    return u;
  }

  /** RFC 7591 registration: the client_id is a signed blob of the registration, so nothing is stored. */
  register(meta: Row): Row {
    const uris = meta.redirect_uris;
    if (!Array.isArray(uris) || uris.length === 0 || uris.length > 10) throw new OAuthError("invalid_redirect_uri", "redirect_uris must be a non-empty array");
    const redirect_uris = uris.map((u) => this.checkRedirectUri(u));
    const method = meta.token_endpoint_auth_method ?? "none";
    if (!["none", "client_secret_post", "client_secret_basic"].includes(method)) {
      throw new OAuthError("invalid_client_metadata", `token_endpoint_auth_method ${method} is not supported`);
    }
    const grants: string[] = meta.grant_types ?? ["authorization_code", "refresh_token"];
    if (!Array.isArray(grants) || grants.some((g) => g !== "authorization_code" && g !== "refresh_token")) {
      throw new OAuthError("invalid_client_metadata", "only authorization_code and refresh_token grants are supported");
    }
    const client_name = String(meta.client_name ?? "MCP client").slice(0, 100);
    const iat = this.nowS();
    const client_id = this.sign("cl", { n: client_name, r: redirect_uris, m: method, iat });
    const out: Row = {
      client_id,
      client_id_issued_at: iat,
      client_name,
      redirect_uris,
      grant_types: ["authorization_code", "refresh_token"],
      response_types: ["code"],
      token_endpoint_auth_method: method,
      scope: SCOPE,
    };
    if (method !== "none") {
      out.client_secret = this.clientSecretFor(client_id);
      out.client_secret_expires_at = 0;
    }
    this.log("oauth.client_registered", { client_name, redirect_hosts: redirect_uris.map((u) => new URL(u).host), auth_method: method });
    return out;
  }

  private async resolveClient(clientId: unknown): Promise<ClientInfo> {
    if (typeof clientId !== "string" || !clientId) throw new OAuthError("invalid_request", "client_id is required");
    if (clientId.startsWith("https://")) return this.resolveCimd(clientId);
    const p = this.verify("cl", clientId);
    if (!p) throw new OAuthError("invalid_client", "unknown client_id (register again)", 401);
    return { client_id: clientId, client_name: p.n, redirect_uris: p.r, auth_method: p.m, kind: "dcr" };
  }

  private async resolveCimd(clientId: string): Promise<ClientInfo> {
    let url: URL;
    try {
      url = new URL(clientId);
    } catch {
      throw new OAuthError("invalid_client", "client_id is not a valid URL", 401);
    }
    if (!this.o.cimdHosts.includes(url.hostname.toLowerCase()) || url.protocol !== "https:" || url.hash || url.pathname === "/") {
      throw new OAuthError("invalid_client", `client metadata documents are only accepted from: ${this.o.cimdHosts.join(", ") || "(none)"}`, 401);
    }
    const hit = this.cimdCache.get(clientId);
    if (hit && this.o.now() - hit.at < 5 * 60_000) return hit.info;
    let doc: Row;
    try {
      const r = await this.o.fetch(clientId, { headers: { accept: "application/json" }, redirect: "error", signal: AbortSignal.timeout(5000) });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const text = await r.text();
      if (text.length > 16_384) throw new Error("document too large");
      doc = JSON.parse(text);
    } catch (e) {
      throw new OAuthError("invalid_client", `could not fetch the client metadata document (${(e as Error).message})`, 401);
    }
    if (doc.client_id !== clientId) throw new OAuthError("invalid_client", "client metadata document client_id does not match its URL", 401);
    const uris = Array.isArray(doc.redirect_uris) ? doc.redirect_uris.filter((u: unknown) => typeof u === "string") : [];
    if (!uris.length) throw new OAuthError("invalid_client", "client metadata document has no redirect_uris", 401);
    const info: ClientInfo = { client_id: clientId, client_name: String(doc.client_name ?? url.host).slice(0, 100), redirect_uris: uris, auth_method: "none", kind: "cimd" };
    this.cimdCache.set(clientId, { at: this.o.now(), info });
    return info;
  }

  private authenticateClient(client: ClientInfo, form: URLSearchParams, req: IncomingMessage): void {
    if (client.auth_method === "none") return;
    let secret = form.get("client_secret") ?? undefined;
    const basic = /^Basic\s+(.+)$/i.exec(req.headers.authorization ?? "");
    if (basic) {
      const [id, s] = Buffer.from(basic[1], "base64").toString("utf8").split(":");
      if (decodeURIComponent(id ?? "") !== client.client_id) throw new OAuthError("invalid_client", "client authentication failed", 401);
      secret = decodeURIComponent(s ?? "");
    }
    const want = this.clientSecretFor(client.client_id);
    if (!secret || !timingSafeEqual(sha256(secret), sha256(want))) throw new OAuthError("invalid_client", "client authentication failed", 401);
  }

  // ------------------------------------------------------------------ throttle

  private clientKey(req: IncomingMessage): string {
    const h = (n: string) => {
      const v = req.headers[n];
      return (Array.isArray(v) ? v[0] : v)?.split(",")[0]?.trim();
    };
    return h("fly-client-ip") || h("cf-connecting-ip") || h("x-forwarded-for") || req.socket.remoteAddress || "local";
  }

  private throttled(req: IncomingMessage): boolean {
    const now = this.o.now();
    if (this.globalFails.until > now && this.globalFails.n >= 50) return true;
    const f = this.fails.get(this.clientKey(req));
    return Boolean(f && f.until > now && f.n >= 8);
  }

  private noteFailure(req: IncomingMessage): void {
    const now = this.o.now();
    const k = this.clientKey(req);
    const f = this.fails.get(k);
    if (!f || f.until <= now) this.fails.set(k, { n: 1, until: now + 10 * 60_000 });
    else f.n += 1;
    if (this.globalFails.until <= now) this.globalFails = { n: 1, until: now + 10 * 60_000 };
    else this.globalFails.n += 1;
  }

  // ------------------------------------------------------------------ scope / resource

  private grantScope(requested: string | null | undefined): string {
    const asked = (requested ?? "").split(/\s+/).filter(Boolean);
    const granted = asked.filter((s) => SCOPES_SUPPORTED.includes(s));
    if (!granted.includes(SCOPE)) granted.unshift(SCOPE);
    return [...new Set(granted)].join(" ");
  }

  private checkResource(resource: string | null | undefined, req: IncomingMessage): void {
    if (!resource) return;
    const norm = (s: string) => s.replace(/\/+$/, "");
    if (norm(resource) !== norm(this.resourceUrl(req))) {
      throw new OAuthError("invalid_target", `this server only issues tokens for ${this.resourceUrl(req)}`);
    }
  }

  // ------------------------------------------------------------------ HTTP

  /** Handles the OAuth routes; returns false when the path is not one of them. */
  async handle(req: IncomingMessage, res: ServerResponse, url: URL): Promise<boolean> {
    const p = url.pathname;
    try {
      if (p === "/.well-known/oauth-protected-resource" || p === "/.well-known/oauth-protected-resource/mcp") {
        sendJson(res, 200, this.protectedResourceMetadata(req), { "cache-control": "public, max-age=300" });
        return true;
      }
      if (p === "/.well-known/oauth-authorization-server" || p === "/.well-known/openid-configuration") {
        sendJson(res, 200, this.authorizationServerMetadata(req), { "cache-control": "public, max-age=300" });
        return true;
      }
      if (p === "/register") {
        if (req.method !== "POST") return void sendJson(res, 405, { error: "invalid_request", error_description: "POST only" }, { allow: "POST" }), true;
        let meta: Row;
        try {
          meta = JSON.parse(await readBody(req));
        } catch (e) {
          throw e instanceof OAuthError ? e : new OAuthError("invalid_client_metadata", "body must be JSON");
        }
        sendJson(res, 201, this.register(meta ?? {}));
        return true;
      }
      if (p === "/authorize") {
        if (req.method === "GET") await this.authorizeGet(req, res, url.searchParams);
        else if (req.method === "POST") await this.authorizePost(req, res);
        else sendJson(res, 405, { error: "invalid_request" }, { allow: "GET, POST" });
        return true;
      }
      if (p === "/token") {
        if (req.method !== "POST") return void sendJson(res, 405, { error: "invalid_request", error_description: "POST only" }, { allow: "POST" }), true;
        await this.token(req, res);
        return true;
      }
      return false;
    } catch (e) {
      if (e instanceof OAuthError) {
        oauthJsonError(res, e, e.error === "invalid_client" && req.headers.authorization?.startsWith("Basic") ? { "www-authenticate": 'Basic realm="rdf-mcp"' } : {});
        return true;
      }
      throw e;
    }
  }

  /** Validate the authorization request. Client / redirect errors are shown, never redirected. */
  private async authRequest(params: URLSearchParams | Row, req: IncomingMessage) {
    const get = (k: string): string | undefined => {
      const v = params instanceof URLSearchParams ? params.get(k) : params[k];
      return v === null || v === undefined || v === "" ? undefined : String(v);
    };
    const client = await this.resolveClient(get("client_id"));
    let redirect = get("redirect_uri");
    if (!redirect) {
      if (client.redirect_uris.length !== 1) throw new OAuthError("invalid_request", "redirect_uri is required");
      redirect = client.redirect_uris[0];
    }
    if (!client.redirect_uris.some((r) => redirectMatches(r, redirect!))) throw new OAuthError("invalid_request", "redirect_uri is not registered for this client");
    if (client.kind === "cimd") this.checkRedirectUri(redirect);
    const ar = {
      client_id: client.client_id,
      client_name: client.client_name,
      redirect_uri: redirect,
      state: get("state"),
      scope: this.grantScope(get("scope")),
      resource: get("resource"),
      code_challenge: get("code_challenge"),
      code_challenge_method: get("code_challenge_method"),
      response_type: get("response_type"),
    };
    return ar;
  }

  private redirectWith(res: ServerResponse, redirectUri: string, params: Record<string, string | undefined>): void {
    const u = new URL(redirectUri);
    for (const [k, v] of Object.entries(params)) if (v !== undefined) u.searchParams.set(k, v);
    res.writeHead(302, { location: u.toString(), ...NO_STORE }).end();
  }

  private errorPage(res: ServerResponse, status: number, message: string): void {
    page(res, status, "Sign-in problem", `<h1>Can't connect</h1><p class="err">${escapeHtml(message)}</p><p class="muted">Robot Deployment Factory connector</p>`);
  }

  private async authorizeGet(req: IncomingMessage, res: ServerResponse, q: URLSearchParams): Promise<void> {
    let ar;
    try {
      ar = await this.authRequest(q, req);
    } catch (e) {
      if (e instanceof OAuthError) return this.errorPage(res, 400, e.message);
      throw e;
    }
    const iss = this.baseUrl(req);
    if (ar.response_type !== "code") return this.redirectWith(res, ar.redirect_uri, { error: "unsupported_response_type", state: ar.state, iss });
    if (!ar.code_challenge || ar.code_challenge_method !== "S256" || !/^[A-Za-z0-9_-]{43,128}$/.test(ar.code_challenge)) {
      return this.redirectWith(res, ar.redirect_uri, { error: "invalid_request", error_description: "S256 PKCE (code_challenge) is required", state: ar.state, iss });
    }
    try {
      this.checkResource(ar.resource, req);
    } catch (e) {
      return this.redirectWith(res, ar.redirect_uri, { error: "invalid_target", error_description: (e as Error).message, state: ar.state, iss });
    }
    if (!this.enabled) return this.errorPage(res, 503, "Operator sign-in is not set up on this server (RDF_OPERATOR_PASSCODE is unset).");
    this.consentPage(res, req, ar);
  }

  private consentPage(res: ServerResponse, req: IncomingMessage, ar: Row, error?: string, status = 200): void {
    const signed = this.sign("cs", { ...ar, client_name: undefined, exp: this.nowS() + 600 });
    const redirectHost = new URL(ar.redirect_uri).host;
    const loopback = isLoopbackHost(new URL(ar.redirect_uri).hostname);
    const known = /(^|\.)claude\.(ai|com)$/.test(new URL(ar.redirect_uri).hostname);
    page(
      res,
      status,
      "Connect to Robot Deployment Factory",
      `<h1>Connect ${escapeHtml(ar.client_name)}?</h1>
<p>This lets the app create tickets, read their status and reports, and approve <strong>test-mode</strong> spend on the Robot Deployment Factory at <code>${escapeHtml(this.baseUrl(req))}</code>.</p>
<p>After you approve, you go back to <strong><code>${escapeHtml(redirectHost)}</code></strong>.</p>
${loopback ? `<p class="warn">That is an app on this computer (a loopback address). Approve only if you just started the sign-in from Claude Code, the MCP Inspector or a test script.</p>` : known ? "" : `<p class="warn">That is not claude.ai. Approve only if you started this sign-in yourself.</p>`}
${error ? `<p class="err" role="alert">${escapeHtml(error)}</p>` : ""}
<form method="post" action="/authorize">
<input type="hidden" name="request" value="${escapeHtml(signed)}">
<label for="passcode">Operator passcode</label>
<input id="passcode" name="passcode" type="password" autocomplete="current-password" autofocus>
<div class="row"><button type="submit" name="decision" value="deny">Deny</button><button class="primary" type="submit" name="decision" value="approve">Approve</button></div>
</form>
<p class="muted">Only the factory operator can approve. Access lasts until revoked (rotate MCP_OAUTH_SECRET to revoke every connection).</p>`,
    );
  }

  private async authorizePost(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const ctype = String(req.headers["content-type"] ?? "");
    if (!ctype.startsWith("application/x-www-form-urlencoded")) return this.errorPage(res, 415, "Expected a form post.");
    const form = new URLSearchParams(await readBody(req));
    const cs = this.verify("cs", form.get("request"));
    if (!cs) return this.errorPage(res, 400, "This sign-in page expired. Start the connection again from Claude.");
    let ar: Row;
    try {
      ar = await this.authRequest(cs, req); // re-checks the client and redirect
    } catch (e) {
      if (e instanceof OAuthError) return this.errorPage(res, 400, e.message);
      throw e;
    }
    ar.scope = cs.scope;
    const iss = this.baseUrl(req);
    if (form.get("decision") !== "approve") {
      this.log("oauth.consent_denied", { client_name: ar.client_name });
      return this.redirectWith(res, ar.redirect_uri, { error: "access_denied", error_description: "The operator denied access", state: ar.state, iss });
    }
    if (!this.enabled) return this.errorPage(res, 503, "Operator sign-in is not set up on this server.");
    if (this.throttled(req)) {
      this.log("oauth.consent_throttled", {});
      return this.consentPage(res, req, ar, "Too many wrong passcodes. Wait 10 minutes and try again.", 429);
    }
    if (!passcodeOk(form.get("passcode"), this.o.passcode)) {
      this.noteFailure(req);
      this.log("oauth.consent_bad_passcode", { client_name: ar.client_name });
      return this.consentPage(res, req, ar, "Wrong passcode.", 401);
    }
    const jti = b64u(randomBytes(16));
    const code = this.sign("ac", {
      jti,
      cid: ar.client_id,
      ru: ar.redirect_uri,
      cc: ar.code_challenge,
      sc: ar.scope,
      aud: this.resourceUrl(req),
      exp: this.nowS() + this.o.codeTtlS,
    });
    this.log("oauth.consent_approved", { client_name: ar.client_name, redirect_host: new URL(ar.redirect_uri).host });
    this.redirectWith(res, ar.redirect_uri, { code, state: ar.state, iss });
  }

  private issueTokens(fam: string, cid: string, scope: string, aud: string): Row {
    const now = this.nowS();
    const access_token = this.sign("at", { jti: b64u(randomBytes(12)), fam, cid, scope, aud, iat: now, exp: now + this.o.accessTtlS });
    const refresh_token = this.sign("rt", { jti: b64u(randomBytes(12)), fam, cid, scope, aud, iat: now, exp: now + this.o.refreshTtlS });
    return { access_token, token_type: "Bearer", expires_in: this.o.accessTtlS, refresh_token, scope };
  }

  private async token(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const ctype = String(req.headers["content-type"] ?? "");
    const raw = await readBody(req);
    let form: URLSearchParams;
    if (ctype.startsWith("application/x-www-form-urlencoded")) form = new URLSearchParams(raw);
    else if (ctype.startsWith("application/json")) {
      // Lenient: some clients send JSON. The spec (and Claude) use form encoding.
      try {
        form = new URLSearchParams(Object.entries(JSON.parse(raw) as Row).map(([k, v]) => [k, String(v)]));
      } catch {
        throw new OAuthError("invalid_request", "body must be application/x-www-form-urlencoded");
      }
    } else throw new OAuthError("invalid_request", "body must be application/x-www-form-urlencoded");

    let clientId = form.get("client_id") ?? undefined;
    const basic = /^Basic\s+(.+)$/i.exec(req.headers.authorization ?? "");
    if (!clientId && basic) clientId = decodeURIComponent(Buffer.from(basic[1], "base64").toString("utf8").split(":")[0] ?? "");
    const grant = form.get("grant_type");

    if (grant === "authorization_code") {
      const client = await this.resolveClient(clientId);
      this.authenticateClient(client, form, req);
      const c = this.verify("ac", form.get("code"));
      if (!c || c.cid !== client.client_id) throw new OAuthError("invalid_grant", "the authorization code is invalid or expired");
      if (!this.consume(`ac:${c.jti}`, c.exp)) {
        this.revokeFamily(c.jti); // a replayed code revokes what the first redemption got
        throw new OAuthError("invalid_grant", "the authorization code was already used");
      }
      if ((form.get("redirect_uri") ?? c.ru) !== c.ru) throw new OAuthError("invalid_grant", "redirect_uri does not match the authorization request");
      const verifier = form.get("code_verifier");
      if (!verifier || !/^[A-Za-z0-9._~-]{43,128}$/.test(verifier) || b64u(sha256(verifier)) !== c.cc) {
        throw new OAuthError("invalid_grant", "PKCE verification failed");
      }
      this.checkResource(form.get("resource"), req);
      if (c.aud !== this.resourceUrl(req)) throw new OAuthError("invalid_grant", "the code was issued for a different resource");
      this.log("oauth.token_issued", { grant: "authorization_code", client_name: client.client_name });
      return sendJson(res, 200, this.issueTokens(c.jti, client.client_id, c.sc, c.aud));
    }

    if (grant === "refresh_token") {
      const r = this.verify("rt", form.get("refresh_token"));
      if (!r || this.revoked.has(r.fam)) throw new OAuthError("invalid_grant", "the refresh token is invalid, expired or revoked");
      if (clientId && clientId !== r.cid) throw new OAuthError("invalid_grant", "the refresh token was issued to another client");
      const client = await this.resolveClient(r.cid);
      this.authenticateClient(client, form, req);
      this.checkResource(form.get("resource"), req);
      if (r.aud !== this.resourceUrl(req)) throw new OAuthError("invalid_grant", "the refresh token was issued for a different resource");
      if (!this.consume(`rt:${r.jti}`, r.exp)) {
        this.revokeFamily(r.fam); // rotation reuse detection (OAuth 2.1 4.3.1)
        throw new OAuthError("invalid_grant", "the refresh token was already used");
      }
      let scope = r.scope as string;
      const asked = form.get("scope");
      if (asked) {
        const have = new Set(scope.split(" "));
        const want = asked.split(/\s+/).filter(Boolean);
        if (want.some((s) => !have.has(s))) throw new OAuthError("invalid_scope", "cannot widen scope on refresh");
        scope = want.join(" ");
      }
      this.log("oauth.token_issued", { grant: "refresh_token", client_name: client.client_name });
      return sendJson(res, 200, this.issueTokens(r.fam, r.cid, scope, r.aud));
    }

    throw new OAuthError("unsupported_grant_type", "grant_type must be authorization_code or refresh_token");
  }
}
