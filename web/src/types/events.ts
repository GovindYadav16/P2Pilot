// Mirrors backend/p2pilot/sse.py. Keep in sync; `python -m p2pilot.sse` prints the schema.

export type RunStarted = { type: "run_started"; run_id: string; conversation_id: string; skill: string };
export type Token = { type: "token"; text: string };
export type ToolStarted = { type: "tool_started"; call_id: string; tool: string; args: Record<string, unknown> };
export type ToolFinished = { type: "tool_finished"; call_id: string; tool: string; ok: boolean; summary: string };
export type Citation = {
  type: "citation";
  chunk_id: string;
  doc_title: string;
  page?: number;
  section?: string;
  quote: string;
};
export type TodoUpdated = { type: "todo_updated"; todos: Array<Record<string, unknown>> };
export type Interrupt = { type: "interrupt"; interrupt_id: string; kind: string; payload: Record<string, unknown> };
export type ArtifactCreated = { type: "artifact_created"; file_id: string; title: string; mime_type: string };
export type ErrorEvent = {
  type: "error";
  code:
    | "model_unavailable"
    | "budget_exceeded"
    | "forbidden"
    | "bad_request"
    | "tool_failed"
    | "internal_error";
  message: string;
  retryable: boolean;
};
export type RunFinished = { type: "run_finished"; run_id: string; status: "ok" | "error" | "interrupted" };

export type AgentEvent =
  | RunStarted
  | Token
  | ToolStarted
  | ToolFinished
  | Citation
  | TodoUpdated
  | Interrupt
  | ArtifactCreated
  | ErrorEvent
  | RunFinished;
