import type { AgentEvent } from "./types/events";

/**
 * Incremental parser for text/event-stream.
 * Network chunks can split an event anywhere (even mid-line), so we buffer until
 * a blank line ends the event. Only `data:` lines are used; `event:` is informational
 * because every payload carries its own `type`.
 */
export class SSEParser {
  private buffer = "";

  push(chunk: string): AgentEvent[] {
    // Normalise the whole buffer so a CRLF split across two chunks is still caught.
    this.buffer = (this.buffer + chunk).replace(/\r\n/g, "\n");
    const events: AgentEvent[] = [];
    let end: number;
    while ((end = this.buffer.indexOf("\n\n")) !== -1) {
      const frame = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 2);
      const data = frame
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).replace(/^ /, ""))
        .join("\n");
      if (data) events.push(JSON.parse(data) as AgentEvent);
    }
    return events;
  }
}

function authHeaders(token?: string): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function errorMessage(resp: Response): Promise<string> {
  try {
    const body = await resp.json();
    if (typeof body?.detail === "string") return body.detail;
  } catch {
    /* not JSON */
  }
  return `Request failed (${resp.status}).`;
}

/**
 * POST JSON and yield SSE events as they arrive.
 * Uses fetch + ReadableStream rather than EventSource, because EventSource cannot
 * send a POST body or an Authorization header.
 */
export async function* streamPost(
  url: string,
  body: unknown,
  token?: string,
  signal?: AbortSignal,
): AsyncGenerator<AgentEvent> {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream", ...authHeaders(token) },
    body: JSON.stringify(body),
    signal,
  });
  if (!resp.ok || !resp.body) {
    yield {
      type: "error",
      code: resp.status === 401 || resp.status === 403 ? "forbidden" : "bad_request",
      message: resp.status === 401 ? "Please sign in again." : await errorMessage(resp),
      retryable: resp.status >= 500,
    };
    return;
  }
  const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();
  const parser = new SSEParser();
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    for (const event of parser.push(value)) yield event;
  }
}

export function streamChat(
  apiUrl: string,
  body: { message: string; conversation_id?: string; skill?: string; file_id?: string },
  token?: string,
  signal?: AbortSignal,
) {
  return streamPost(`${apiUrl}/chat`, body, token, signal);
}

export function streamResume(
  apiUrl: string,
  conversationId: string,
  body: { approve: boolean; comment?: string },
  token?: string,
) {
  return streamPost(`${apiUrl}/runs/${encodeURIComponent(conversationId)}/resume`, body, token);
}

/** Upload a file with multipart FormData; the browser sets the boundary header itself. */
export async function uploadFile(apiUrl: string, file: File, token?: string): Promise<{ file_id: string }> {
  const form = new FormData();
  form.append("file", file);
  const resp = await fetch(`${apiUrl}/files`, { method: "POST", headers: authHeaders(token), body: form });
  if (!resp.ok) throw new Error(await errorMessage(resp));
  return resp.json();
}

export async function getJson<T>(url: string, token?: string): Promise<T> {
  const resp = await fetch(url, { headers: authHeaders(token) });
  if (!resp.ok) throw new Error(await errorMessage(resp));
  return resp.json();
}
