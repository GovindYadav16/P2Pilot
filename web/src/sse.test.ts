import { describe, expect, it } from "vitest";
import { SSEParser } from "./sse";

const frame = (obj: object) => `event: ${(obj as { type: string }).type}\ndata: ${JSON.stringify(obj)}\n\n`;

describe("SSEParser", () => {
  it("parses whole frames", () => {
    const p = new SSEParser();
    const events = p.push(frame({ type: "token", text: "Hi" }) + frame({ type: "token", text: " there" }));
    expect(events).toEqual([
      { type: "token", text: "Hi" },
      { type: "token", text: " there" },
    ]);
  });

  it("handles a frame split across network chunks, even mid-line", () => {
    const p = new SSEParser();
    const raw = frame({ type: "run_finished", run_id: "r1", status: "ok" });
    const cut = 17;
    expect(p.push(raw.slice(0, cut))).toEqual([]);
    expect(p.push(raw.slice(cut))).toEqual([{ type: "run_finished", run_id: "r1", status: "ok" }]);
  });

  it("handles CRLF line endings", () => {
    const p = new SSEParser();
    const raw = frame({ type: "token", text: "x" }).replace(/\n/g, "\r\n");
    expect(p.push(raw)).toEqual([{ type: "token", text: "x" }]);
  });

  it("ignores comment/heartbeat frames", () => {
    const p = new SSEParser();
    expect(p.push(": keep-alive\n\n")).toEqual([]);
  });

  it("keeps whitespace inside token text", () => {
    const p = new SSEParser();
    expect(p.push(frame({ type: "token", text: "  two spaces" }))).toEqual([
      { type: "token", text: "  two spaces" },
    ]);
  });
});
