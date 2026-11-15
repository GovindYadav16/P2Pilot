import { useEffect, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import type { Session } from "@supabase/supabase-js";
import { API_URL, AUTH_DISABLED, supabase } from "./supabase";
import { getJson, streamChat, streamResume, uploadFile } from "./sse";
import type { AgentEvent } from "./types/events";

type Step = { id: string; tool: string; status: "running" | "ok" | "failed"; summary?: string };
type Finding = { code: string; severity: string; message: string };
type Approval = {
  conversationId: string;
  invoiceNumber: string;
  vendor: string;
  total: string;
  findings: Finding[];
  state: "pending" | "sending" | "done";
};
type ChatMessage = {
  role: "user" | "assistant" | "error";
  text: string;
  steps?: Step[];
  approval?: Approval;
};
type PendingRow = {
  invoice_id: string;
  invoice_number: string;
  vendor_name: string | null;
  total: string;
  findings: Finding[];
  thread_id: string;
};

const TOOL_LABELS: Record<string, string> = {
  files_read_text: "Read the PDF",
  extract_invoice: "Extract invoice fields",
  erp_save_invoice: "Save invoice",
  erp_get_purchase_order: "Look up the purchase order",
  erp_find_possible_duplicates: "Check for duplicates",
  erp_record_decision: "Record decision",
  erp_record_review: "Record reviewer decision",
  erp_post_invoice: "Post to ERP",
  tickets_create: "Open exception ticket",
};

function Login() {
  const [email, setEmail] = useState("clerk@acme.test");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const { error } = await supabase!.auth.signInWithPassword({ email, password });
    if (error) setError(error.message);
  }

  return (
    <form className="card login" onSubmit={submit}>
      <h1>P2Pilot</h1>
      <label>
        Email
        <input value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" />
      </label>
      <label>
        Password
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
        />
      </label>
      {error && <p className="error">{error}</p>}
      <button type="submit">Sign in</button>
      <p className="muted small">Demo users: clerk@, approver@, auditor@, admin@ acme.test</p>
    </form>
  );
}

function Steps({ steps }: { steps: Step[] }) {
  return (
    <ol className="steps">
      {steps.map((s) => (
        <li key={s.id} className={`step ${s.status}`}>
          <span className="dot" aria-hidden="true" />
          <span>{TOOL_LABELS[s.tool] ?? s.tool}</span>
          {s.summary && <span className="muted"> · {s.summary}</span>}
        </li>
      ))}
    </ol>
  );
}

function ApprovalCard({
  approval,
  onDecide,
}: {
  approval: Approval;
  onDecide: (a: Approval, approve: boolean, comment: string) => void;
}) {
  const [comment, setComment] = useState("");
  const busy = approval.state !== "pending";
  return (
    <div className="approval">
      <div className="approval-head">
        <strong>Approval needed</strong>
        <span className="muted">
          {approval.invoiceNumber} · {approval.vendor} · ₹
          {Number(approval.total).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
        </span>
      </div>
      <ul className="findings">
        {approval.findings.map((f, i) => (
          <li key={i} className={f.severity}>
            <code>{f.code}</code> {f.message}
          </li>
        ))}
      </ul>
      <input
        placeholder="Comment for the audit trail (optional)"
        value={comment}
        onChange={(e) => setComment(e.target.value)}
        disabled={busy}
      />
      <div className="row">
        <button disabled={busy} onClick={() => onDecide(approval, true, comment)}>
          Approve and post
        </button>
        <button className="danger" disabled={busy} onClick={() => onDecide(approval, false, comment)}>
          Reject
        </button>
      </div>
      <p className="muted small">Approvers only, and not the person who submitted the invoice.</p>
    </div>
  );
}

function Chat({ token, email }: { token?: string; email?: string }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [conversationId, setConversationId] = useState<string>();
  const [pending, setPending] = useState<PendingRow[] | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  // Braces matter: the effect must return nothing. Newer browsers make scrollIntoView()
  // return a Promise, and React would try to call that as a cleanup function.
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const updateLast = (fn: (m: ChatMessage) => ChatMessage) =>
    setMessages((ms) => {
      const copy = [...ms];
      copy[copy.length - 1] = fn(copy[copy.length - 1]);
      return copy;
    });

  /** Feed one agent run's events into the last (assistant) message. */
  async function consume(events: AsyncGenerator<AgentEvent>, onStart?: (conv: string) => void): Promise<boolean> {
    let conv = "";
    let ok = true;
    for await (const ev of events) {
      switch (ev.type) {
        case "run_started":
          conv = ev.conversation_id;
          onStart?.(conv);
          break;
        case "token":
          updateLast((m) => ({ ...m, text: m.text + ev.text }));
          break;
        case "tool_started":
          updateLast((m) => ({
            ...m,
            steps: [...(m.steps ?? []), { id: ev.call_id, tool: ev.tool, status: "running" }],
          }));
          break;
        case "tool_finished":
          updateLast((m) => ({
            ...m,
            steps: (m.steps ?? []).map((s) =>
              s.id === ev.call_id ? { ...s, status: ev.ok ? "ok" : "failed", summary: ev.summary } : s,
            ),
          }));
          break;
        case "interrupt": {
          const p = ev.payload as Record<string, unknown>;
          updateLast((m) => ({
            ...m,
            approval: {
              conversationId: conv,
              invoiceNumber: String(p.invoice_number ?? ""),
              vendor: String(p.vendor_name ?? ""),
              total: String(p.total ?? "0"),
              findings: (p.findings as Finding[]) ?? [],
              state: "pending",
            },
          }));
          break;
        }
        case "error":
          ok = false;
          // Any step still running when the run fails did not finish: show it as failed.
          updateLast((m) => ({
            ...m,
            steps: m.steps?.map((s) => (s.status === "running" ? { ...s, status: "failed" } : s)),
          }));
          setMessages((ms) => [...ms, { role: "error", text: ev.message }]);
          break;
      }
    }
    return ok;
  }

  async function run(
    userText: string,
    events: () => AsyncGenerator<AgentEvent>,
    onStart?: (c: string) => void,
  ): Promise<boolean> {
    setBusy(true);
    setMessages((m) => [...m, { role: "user", text: userText }, { role: "assistant", text: "" }]);
    let ok = false;
    try {
      ok = await consume(events(), onStart);
    } catch {
      setMessages((m) => [...m, { role: "error", text: "Lost connection to the server." }]);
    } finally {
      // Drop the assistant bubble if nothing was streamed into it (e.g. a refused request).
      setMessages((ms) => ms.filter((m) => !(m.role === "assistant" && !m.text && !m.steps?.length && !m.approval)));
      setBusy(false);
    }
    return ok;
  }

  async function send(e: FormEvent) {
    e.preventDefault();
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    await run(text, () => streamChat(API_URL, { message: text, conversation_id: conversationId }, token), setConversationId);
  }

  async function onFile(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file || busy) return;
    try {
      setBusy(true);
      const { file_id } = await uploadFile(API_URL, file, token);
      setBusy(false);
      // Each invoice gets its own run (its own LangGraph thread).
      await run(`Process invoice ${file.name}`, () =>
        streamChat(API_URL, { message: `Process ${file.name}`, skill: "invoice-processing", file_id }, token),
      );
    } catch (err) {
      setBusy(false);
      setMessages((m) => [...m, { role: "error", text: (err as Error).message }]);
    }
  }

  async function decide(a: Approval, approve: boolean, comment: string) {
    const mark = (state: Approval["state"]) =>
      setMessages((ms) =>
        ms.map((m) => (m.approval?.conversationId === a.conversationId ? { ...m, approval: { ...m.approval, state } } : m)),
      );
    mark("sending");
    const ok = await run(`${approve ? "Approve" : "Reject"} ${a.invoiceNumber}${comment ? `: ${comment}` : ""}`, () =>
      streamResume(API_URL, a.conversationId, { approve, comment }, token),
    );
    // Refused (e.g. wrong role) or failed: keep the card usable so someone else can decide.
    mark(ok ? "done" : "pending");
    if (ok) setPending(null);
  }

  async function loadPending() {
    try {
      setPending(await getJson<PendingRow[]>(`${API_URL}/approvals`, token));
    } catch (err) {
      setMessages((m) => [...m, { role: "error", text: (err as Error).message }]);
    }
  }

  return (
    <div className="chat">
      <header>
        <strong>P2Pilot</strong>
        <button className="link" onClick={loadPending}>
          Approvals
        </button>
        <span className="muted">{email ?? "dev mode"}</span>
        {supabase && (
          <button className="link" onClick={() => supabase!.auth.signOut()}>
            Sign out
          </button>
        )}
      </header>
      <main>
        {pending && (
          <div className="pending">
            <div className="approval-head">
              <strong>Waiting for approval ({pending.length})</strong>
              <button className="link" onClick={() => setPending(null)}>
                Close
              </button>
            </div>
            {pending.length === 0 && <p className="muted">Nothing is waiting for review.</p>}
            {pending.map((r) => (
              <ApprovalCard
                key={r.invoice_id}
                approval={{
                  conversationId: r.thread_id,
                  invoiceNumber: r.invoice_number,
                  vendor: r.vendor_name ?? "Unknown vendor",
                  total: String(r.total),
                  findings: r.findings,
                  state: "pending",
                }}
                onDecide={decide}
              />
            ))}
          </div>
        )}
        {messages.length === 0 && !pending && (
          <p className="muted">Ask about an invoice, a PO or a policy, or attach an invoice PDF to process it.</p>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            {m.steps && m.steps.length > 0 && <Steps steps={m.steps} />}
            {m.text || (busy && i === messages.length - 1 && !m.steps ? "…" : "")}
            {m.approval && <ApprovalCard approval={m.approval} onDecide={decide} />}
          </div>
        ))}
        <div ref={bottom} />
      </main>
      <form className="composer" onSubmit={send}>
        <input ref={fileInput} type="file" accept="application/pdf" hidden onChange={onFile} />
        <button type="button" className="secondary" disabled={busy} onClick={() => fileInput.current?.click()}>
          Attach invoice
        </button>
        <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="Message P2Pilot" disabled={busy} />
        <button type="submit" disabled={busy || !input.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}

export default function App() {
  const [session, setSession] = useState<Session | null>(null);

  useEffect(() => {
    if (!supabase) return;
    supabase.auth.getSession().then(({ data }) => setSession(data.session));
    const { data } = supabase.auth.onAuthStateChange((_e, s) => setSession(s));
    return () => data.subscription.unsubscribe();
  }, []);

  if (AUTH_DISABLED) return <Chat />;
  if (!supabase) return <p className="error">Set VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY in web/.env</p>;
  if (!session) return <Login />;
  return <Chat token={session.access_token} email={session.user.email} />;
}
