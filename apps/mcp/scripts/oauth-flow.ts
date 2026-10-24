// TDM-81: drive the connector's OAuth flow with the official MCP SDK client, the way claude.ai does:
// 401 -> protected resource metadata -> authorization server metadata -> DCR (or CIMD) -> PKCE
// authorize -> operator consent page -> code -> token. The "browser" step (the consent page) is
// done here with fetch: GET the page, POST the passcode, read the 302 back to the redirect URI.
// Used by test/oauth.test.ts and scripts/oauth-e2e.ts. The passcode is passed in, never printed.
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { UnauthorizedError, type OAuthClientProvider } from "@modelcontextprotocol/sdk/client/auth.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import type { OAuthClientInformationMixed, OAuthClientMetadata, OAuthTokens } from "@modelcontextprotocol/sdk/shared/auth.js";

export class MemoryOAuthProvider implements OAuthClientProvider {
  readonly redirectUrl: string;
  readonly clientMetadataUrl?: string;
  client?: OAuthClientInformationMixed;
  saved?: OAuthTokens;
  verifier = "";
  authUrl?: URL;

  constructor(opts: { redirectUrl?: string; clientName?: string; clientMetadataUrl?: string } = {}) {
    this.redirectUrl = opts.redirectUrl ?? "http://127.0.0.1:33418/callback";
    this.clientMetadataUrl = opts.clientMetadataUrl;
    this.name = opts.clientName ?? "rdf-mcp oauth test client";
  }

  private readonly name: string;

  get clientMetadata(): OAuthClientMetadata {
    return { client_name: this.name, redirect_uris: [this.redirectUrl], grant_types: ["authorization_code", "refresh_token"], response_types: ["code"], token_endpoint_auth_method: "none" };
  }
  clientInformation() {
    return this.client;
  }
  saveClientInformation(c: OAuthClientInformationMixed) {
    this.client = c;
  }
  tokens() {
    return this.saved;
  }
  saveTokens(t: OAuthTokens) {
    this.saved = t;
  }
  redirectToAuthorization(u: URL) {
    this.authUrl = u;
  }
  saveCodeVerifier(v: string) {
    this.verifier = v;
  }
  codeVerifier() {
    return this.verifier;
  }
}

export interface ConsentResult {
  status: number;
  location?: string;
  code?: string;
  html?: string;
}

/** Play the operator: open the consent page and submit it. */
export async function consent(authUrl: URL, passcode: string, decision: "approve" | "deny" = "approve"): Promise<ConsentResult> {
  const pageRes = await fetch(authUrl, { redirect: "manual" });
  const html = await pageRes.text();
  const m = /name="request" value="([^"]+)"/.exec(html);
  if (pageRes.status !== 200 || !m) return { status: pageRes.status, html, location: pageRes.headers.get("location") ?? undefined };
  const request = m[1].replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">");
  const post = await fetch(new URL("/authorize", authUrl), {
    method: "POST",
    redirect: "manual",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ request, passcode, decision }).toString(),
  });
  const location = post.headers.get("location") ?? undefined;
  const code = location ? (new URL(location).searchParams.get("code") ?? undefined) : undefined;
  return { status: post.status, location, code, html: post.status === 302 ? undefined : await post.text() };
}

/**
 * Connect an SDK client to `mcpUrl` through the full OAuth flow. Returns the connected client and
 * the provider (holding the client registration and tokens).
 */
export async function connectWithOAuth(mcpUrl: string, passcode: string, provider = new MemoryOAuthProvider()) {
  const url = new URL(mcpUrl);
  const first = new StreamableHTTPClientTransport(url, { authProvider: provider });
  const probe = new Client({ name: "rdf-oauth-flow", version: "0" });
  let needsAuth = false;
  try {
    await probe.connect(first);
  } catch (e) {
    if (!(e instanceof UnauthorizedError)) throw e;
    needsAuth = true;
  }
  if (needsAuth) {
    if (!provider.authUrl) throw new Error("server answered 401 but the SDK produced no authorization URL");
    const c = await consent(provider.authUrl, passcode);
    if (!c.code) throw new Error(`consent did not return a code (HTTP ${c.status}${c.location ? `, location ${new URL(c.location).searchParams.get("error")}` : ""})`);
    await first.finishAuth(c.code);
    const client = new Client({ name: "rdf-oauth-flow", version: "0" });
    await client.connect(new StreamableHTTPClientTransport(url, { authProvider: provider }));
    return { client, provider };
  }
  return { client: probe, provider };
}
