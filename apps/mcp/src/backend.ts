// HTTP clients for what the connector talks to:
//   - the orchestrator's HTTP API (tickets, settings, flywheel, report), and
//   - SpacetimeDB directly for two things the orchestrator has no route for: the
//     `approve_spend` reducer and the robot list. The SpacetimeDB token stays on this
//     server; nothing about it is ever returned to the MCP client.

export class BackendError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: unknown;

  constructor(status: number, code: string, message: string, detail?: unknown) {
    super(message);
    this.name = "BackendError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

type FetchLike = typeof fetch;

/** FastAPI error bodies come as {detail: {code, message, ...}}, {detail: [{loc, msg}]} or {detail: "..."}. */
export function errorFromBody(status: number, body: unknown, fallback: string): BackendError {
  const detail = (body as { detail?: unknown } | null)?.detail ?? body;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const d = detail as { code?: unknown; message?: unknown };
    if (typeof d.message === "string") {
      return new BackendError(status, typeof d.code === "string" ? d.code : `http_${status}`, d.message, detail);
    }
  }
  if (Array.isArray(detail)) {
    const msgs = detail.map((e: { loc?: unknown[]; msg?: string }) =>
      `${(e.loc ?? []).filter((p) => p !== "body").join(".") || "request"}: ${e.msg ?? JSON.stringify(e)}`,
    );
    return new BackendError(status, `http_${status}`, msgs.join("; "), detail);
  }
  if (typeof detail === "string" && detail.trim()) return new BackendError(status, `http_${status}`, detail, detail);
  return new BackendError(status, `http_${status}`, fallback, detail);
}

export class OrchestratorClient {
  readonly baseUrl: string;
  private readonly fetchImpl: FetchLike;
  private readonly timeoutMs: number;

  constructor(baseUrl: string, opts: { fetch?: FetchLike; timeoutMs?: number } = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.fetchImpl = opts.fetch ?? fetch;
    this.timeoutMs = opts.timeoutMs ?? 120_000; // POST /tickets makes a Claude call
  }

  async request<T = unknown>(
    method: string,
    path: string,
    opts: { body?: unknown; headers?: Record<string, string> } = {},
  ): Promise<{ status: number; body: T; contentType: string }> {
    let res: Response;
    try {
      res = await this.fetchImpl(this.baseUrl + path, {
        method,
        headers: { accept: "application/json", ...(opts.body !== undefined ? { "content-type": "application/json" } : {}), ...opts.headers },
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch (e) {
      throw new BackendError(502, "orchestrator_unreachable", `Could not reach the orchestrator at ${this.baseUrl} (${(e as Error).message}).`);
    }
    const contentType = res.headers.get("content-type") ?? "";
    const text = await res.text();
    let body: unknown = text;
    if (contentType.includes("json") && text) {
      try {
        body = JSON.parse(text);
      } catch {
        body = text;
      }
    }
    if (!res.ok) throw errorFromBody(res.status, body, `orchestrator ${method} ${path} -> HTTP ${res.status}`);
    return { status: res.status, body: body as T, contentType };
  }

  createTicket(body: Record<string, unknown>) {
    return this.request<Record<string, any>>("POST", "/tickets", { body }).then((r) => r.body);
  }

  getTicket(id: number) {
    return this.request<Record<string, any>>("GET", `/tickets/${id}`).then((r) => r.body);
  }

  getSettings(id: number) {
    return this.request<Record<string, any>>("GET", `/tickets/${id}/settings`).then((r) => r.body);
  }

  /** PATCH run settings; the orchestrator logs it to event_log as actor `human:<humanId>`. */
  patchSettings(id: number, patch: Record<string, unknown>, humanId: string) {
    return this.request<Record<string, any>>("PATCH", `/tickets/${id}/settings`, { body: patch, headers: { "x-human-id": humanId } }).then((r) => r.body);
  }

  /** What the flywheel sees (decision + reason). Optional: null when the route or the ticket is missing. */
  async getFlywheel(id: number): Promise<Record<string, any> | null> {
    try {
      return (await this.request<Record<string, any>>("GET", `/flywheel/tickets/${id}`)).body;
    } catch (e) {
      if (e instanceof BackendError && e.status !== 502) return null;
      throw e;
    }
  }

  /** TDM-71 report JSON; include_markdown adds the rendered `markdown` field. */
  getReport(id: number) {
    return this.request<unknown>("GET", `/tickets/${id}/report?include_markdown=true`);
  }
}

/** Minimal SpacetimeDB HTTP client: reducer calls and SQL as the connector's own identity. */
export class SpacetimeHttp {
  private readonly base: string;
  private readonly token: string;
  private readonly fetchImpl: FetchLike;

  constructor(host: string, db: string, token: string, opts: { fetch?: FetchLike } = {}) {
    this.base = `${host.replace(/\/+$/, "")}/v1/database/${encodeURIComponent(db)}`;
    this.token = token;
    this.fetchImpl = opts.fetch ?? fetch;
  }

  private async post(path: string, body: string, contentType: string): Promise<string> {
    let res: Response;
    try {
      res = await this.fetchImpl(this.base + path, {
        method: "POST",
        headers: { authorization: `Bearer ${this.token}`, "content-type": contentType },
        body,
        signal: AbortSignal.timeout(30_000),
      });
    } catch (e) {
      throw new BackendError(502, "spacetime_unreachable", `Could not reach SpacetimeDB (${(e as Error).message}).`);
    }
    const text = await res.text();
    if (!res.ok) {
      // 530 = the reducer refused (SenderError); its message is the reason.
      const code = res.status === 530 ? "reducer_rejected" : `spacetime_${res.status}`;
      throw new BackendError(res.status === 530 ? 422 : 502, code, text.trim() || `SpacetimeDB HTTP ${res.status}`);
    }
    return text;
  }

  async callReducer(name: string, args: unknown[]): Promise<void> {
    await this.post(`/call/${encodeURIComponent(name)}`, JSON.stringify(args), "application/json");
  }

  /** Rows of the last statement as objects. Only use for columns with plain types (no options/sums). */
  async sql(query: string): Promise<Record<string, unknown>[]> {
    const parsed = JSON.parse(await this.post("/sql", query, "text/plain")) as {
      schema: { elements: { name: string | { some: string } | null }[] };
      rows: unknown[][];
    }[];
    const last = parsed[parsed.length - 1];
    if (!last) return [];
    const names = last.schema.elements.map((e, i) =>
      typeof e.name === "string" ? e.name : e.name && typeof e.name === "object" && "some" in e.name ? e.name.some : `col${i}`,
    );
    return last.rows.map((row) => Object.fromEntries(names.map((n, i) => [n, row[i]])));
  }
}
