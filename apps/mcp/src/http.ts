// Streamable HTTP endpoint (stateless: a fresh McpServer + transport per POST, JSON responses).
//   POST /mcp      MCP JSON-RPC, requires `Authorization: Bearer <MCP_TOKEN>`
//   GET  /healthz  liveness (no auth, no secrets)
// Stateless mode keeps the server horizontally scalable (Fly) and needs no session store;
// GET/DELETE /mcp (SSE stream / session end) answer 405 as the spec allows.
import { createHash, timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";

import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";

import { buildServer, type ToolDeps } from "./tools.ts";

const MAX_BODY_BYTES = 1_000_000;

function digest(s: string): Buffer {
  return createHash("sha256").update(s).digest();
}

/** Constant-time bearer check (hash both sides so lengths always match). */
export function bearerOk(header: string | undefined, token: string): boolean {
  const m = /^Bearer\s+(.+)$/i.exec(header ?? "");
  if (!m) return false;
  return timingSafeEqual(digest(m[1].trim()), digest(token));
}

function sendJson(res: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}): void {
  res.writeHead(status, { "content-type": "application/json", ...headers }).end(JSON.stringify(body));
}

function rpcError(res: ServerResponse, status: number, code: number, message: string, headers: Record<string, string> = {}): void {
  sendJson(res, status, { jsonrpc: "2.0", error: { code, message }, id: null }, headers);
}

async function readJson(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size > MAX_BODY_BYTES) throw new RangeError("body too large");
    chunks.push(chunk as Buffer);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

export interface HttpOptions {
  token: string;
  deps: ToolDeps;
  log?: (event: string, fields: Record<string, unknown>) => void;
}

export function createMcpHttpServer({ token, deps, log = () => {} }: HttpOptions): Server {
  return createServer(async (req, res) => {
    const url = new URL(req.url ?? "/", "http://localhost");
    try {
      if (url.pathname === "/healthz" || url.pathname === "/") {
        return sendJson(res, 200, { ok: true, service: "rdf-mcp", mcp_path: "/mcp" });
      }
      if (url.pathname !== "/mcp") return sendJson(res, 404, { error: "not found" });

      if (!bearerOk(req.headers.authorization, token)) {
        log("http.unauthorized", { method: req.method, has_auth: Boolean(req.headers.authorization) });
        return rpcError(res, 401, -32001, "Unauthorized: send Authorization: Bearer <MCP_TOKEN>", {
          "www-authenticate": 'Bearer realm="rdf-mcp"',
        });
      }
      if (req.method !== "POST") {
        return rpcError(res, 405, -32000, "Method not allowed: this server is stateless; POST JSON-RPC to /mcp.", { allow: "POST" });
      }

      let body: unknown;
      try {
        body = await readJson(req);
      } catch (e) {
        return rpcError(res, e instanceof RangeError ? 413 : 400, -32700, e instanceof RangeError ? "Request body too large" : "Parse error: body must be JSON");
      }

      const server = buildServer(deps);
      const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
      res.on("close", () => {
        void transport.close();
        void server.close();
      });
      await server.connect(transport);
      await transport.handleRequest(req, res, body);
    } catch (e) {
      log("http.error", { error: (e as Error).message });
      if (!res.headersSent) rpcError(res, 500, -32603, "Internal server error");
      else res.end();
    }
  });
}
