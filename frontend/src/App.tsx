import { MemoryEvidence } from "./MemoryEvidence";
import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import PolicyEvidence from "./PolicyEvidence";
import { flushSync } from "react-dom";
import {
  ArrowUp,
  ArrowRight,
  Check,
  ChevronDown,
  Clock3,
  Code2,
  Layers3,
  MessageSquare,
  Plus,
  ShieldCheck,
  Sparkles,
  X,
  RotateCcw,
} from "lucide-react";
import { api, remember, remembered } from "./api";
import type {
  Approval,
  Customer,
  Pending,
  Session,
  SessionView,
  Turn,
} from "./api";

const seconds = (ms: number) => `${(ms / 1000).toFixed(1)} seconds`;
const label = (text: string) =>
  text.replaceAll("_", " ").replace(/^./, (s) => s.toUpperCase());
const sessionLabel = (session: Session) =>
  new Date(session.created_at).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

function Proposal({
  approval,
  active,
  busy,
  decide,
}: {
  approval: Approval;
  active: boolean;
  busy: boolean;
  decide: (decision: "approve" | "decline") => void;
}) {
  return (
    <div className="proposal">
      <div className="flex items-center gap-2">
        <ShieldCheck size={18} />
        <strong>Update your savings plan</strong>
        <span className="pill ml-auto">
          {active
            ? "Your approval needed"
            : label(
                approval.status === "pending"
                  ? "no_longer_active"
                  : approval.status,
              )}
        </span>
      </div>
      <p>Choose which payments automatically put money aside.</p>
      <div className="change">
        <div>
          <small>Currently enabled</small>
          <b>
            {approval.before_enabled_transaction_types.map(label).join(", ") ||
              "None"}
          </b>
        </div>
        <ArrowRight size={18} />
        <div>
          <small>After this change</small>
          <b>
            {approval.enabled_transaction_types.map(label).join(", ") || "None"}
          </b>
        </div>
      </div>
      <p className="fine">
        Applies to future eligible payments. Earlier transactions are not
        reprocessed.
      </p>
      {active && (
        <div className="flex gap-2">
          <button
            className="primary"
            disabled={busy}
            onClick={() => decide("approve")}
          >
            <Check size={15} />
            Approve change
          </button>
          <button
            className="secondary"
            disabled={busy}
            onClick={() => decide("decline")}
          >
            Decline
          </button>
        </div>
      )}
    </div>
  );
}

export default function App() {
  const [customers, setCustomers] = useState<Customer[]>([]),
    [customer, setCustomer] = useState("customer-001");
  const [sessions, setSessions] = useState<Session[]>([]),
    [session, setSession] = useState<Session | null>(null);
  const [view, setView] = useState<SessionView | null>(null),
    [turns, setTurns] = useState<Turn[]>([]);
  const [memory, setMemory] = useState(false);
  const [history, setHistory] = useState(true);
  const [cache, setCache] = useState(true),
    [inspect, setInspect] = useState(true),
    [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState(""),
    [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(false),
    [error, setError] = useState("");
  const [pending, setPending] = useState<Pending | null>(null),
    [timings, setTimings] = useState<Record<string, number>>(() =>
      remembered("support-timings", {}),
    );
  const end = useRef<HTMLDivElement>(null);
  const generation = useRef(0);
  const activeTurn = turns.find((t) => t.turn_id === selected) ?? turns.at(-1);
  const person = customers.find((c) => c.id === customer);
  useEffect(() => {
    api<Customer[]>("/presenter/customers")
      .then(setCustomers)
      .catch((e) => setError(String(e)));
  }, []);
  useEffect(() => {
    const version = ++generation.current;
    setSession(null);
    setView(null);
    setTurns([]);
    setPending(null);
    setError("");
    setLoading(true);
    api<Session[]>("/support/sessions", customer)
      .then((result) => {
        if (version === generation.current) setSessions(result);
      })
      .catch((e) => {
        if (version === generation.current) setError(String(e));
      })
      .finally(() => {
        if (version === generation.current) setLoading(false);
      });
  }, [customer]);
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, busy]);
  async function openSession(id: string) {
    setLoading(true);
    setError("");
    setPending(null);
    try {
      const result = await api<SessionView>(
        `/support/sessions/${id}`,
        customer,
      );
      setSession(result.session);
      setView(result);
      setTurns(result.turns);
      setSelected(null);
      setPending(remembered<Pending | null>(`support-pending:${id}`, null));
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }
  async function createSession() {
    setLoading(true);
    setError("");
    try {
      const created = await api<Session>("/support/sessions", customer, {
        use_cache: cache,
        use_memory: memory,
        use_history: history,
      });
      setSession(created);
      setSessions((prev) => [created, ...prev]);
      setTurns([]);
      setView(null);
      setPending(null);
      setSelected(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }
  async function submit(action: Pending, retry = false) {
    setBusy(true);
    setError("");
    setPending(action);
    remember(`support-pending:${action.session}`, action);
    const started = performance.now();
    try {
      const result = await api<Turn>(
        `/support/sessions/${action.session}/${action.proposal ? `proposals/${action.proposal}/decision` : "messages"}`,
        action.customer,
        action.proposal
          ? { decision: action.decision, request_id: action.request_id }
          : { message: action.message, request_id: action.request_id },
      );
      flushSync(() => {
        setTurns((old) => [
          ...old.filter((t) => t.turn_id !== result.turn_id),
          result,
        ]);
        setSelected(result.turn_id);
      });
      await new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      );
      if (!retry) {
        setTimings((old) => {
          const updated = {
            ...old,
            [result.turn_id]: performance.now() - started,
          };
          remember("support-timings", updated);
          return updated;
        });
      }
      const current = await api<SessionView>(
        `/support/sessions/${action.session}`,
        action.customer,
      );
      setView(current);
      if (result.status !== "failed" && result.status !== "running") {
        setPending(null);
        remember(`support-pending:${action.session}`, null);
      } else
        setError(
          result.error ??
            "This turn did not finish. Retry to continue the same request.",
        );
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  function send() {
    if (!session || !draft.trim() || busy || pending) return;
    const message = draft.trim();
    setDraft("");
    void submit({
      customer,
      session: session.id,
      request_id: crypto.randomUUID(),
      message,
    });
  }
  function decide(proposal: string, decision: "approve" | "decline") {
    if (session)
      void submit({
        customer,
        session: session.id,
        request_id: crypto.randomUUID(),
        proposal,
        decision,
      });
  }
  const blocked = busy || loading;
  const currentApproval = view?.approval;
  return (
    <div className="app-shell">
      <header className="masthead">
        <div className="brand">
          <div className="brand-mark">
            m<span>↗</span>
          </div>
          <div>
            <strong>
              moniepoint<span className="brand-divider">/</span>
              <span className="font-normal">Support studio</span>
            </strong>
            <p>A clearer view of every answer.</p>
          </div>
        </div>
        <span className="demo-label">
          <span />
          Demo environment · fictional accounts
        </span>
      </header>
      <div className="workspace-bar">
        <div className="field">
          <label htmlFor="customer">CUSTOMER</label>
          <div className="select-wrap">
            <span className="avatar-small">
              {person?.display_name.charAt(0) ?? "A"}
            </span>
            <select
              id="customer"
              value={customer}
              disabled={blocked}
              onChange={(e) => setCustomer(e.target.value)}
            >
              {customers.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.display_name}
                </option>
              ))}
            </select>
            <ChevronDown size={14} />
          </div>
        </div>
        <div className="field history-field">
          <label htmlFor="history">CONVERSATION</label>
          <div className="select-wrap">
            <MessageSquare size={16} />
            <select
              id="history"
              disabled={blocked}
              value={session?.id ?? ""}
              onChange={(e) => {
                if (e.target.value) void openSession(e.target.value);
              }}
            >
              <option value="">Choose a session</option>
              {sessions.map((s) => (
                <option key={s.id} value={s.id}>
                  {sessionLabel(s)} · {s.id.slice(0, 5)}
                </option>
              ))}
            </select>
            <ChevronDown size={14} />
          </div>
        </div>
        <button
          className="secondary new-session"
          onClick={() => void createSession()}
          disabled={blocked}
        >
          <Plus size={16} />
          New session
        </button>
        <div className="bar-spacer" />
        <details className="demo-controls">
          <summary className="secondary">Demo controls <ChevronDown size={14} /></summary>
          <div className="demo-controls-panel">
            <strong>Context settings</strong>
            <p>Choose settings, then start a new session.</p>
        <div className="cache-control">
          <label className="switch-label">
            <input type="checkbox" checked={history}
              onChange={(e) => setHistory(e.target.checked)} />
            <span className="switch" />Short-term memory
          </label>
        </div>
        <div className="cache-control">
          <label className="switch-label">
            <input type="checkbox" checked={memory}
              onChange={(e) => setMemory(e.target.checked)} />
            <span className="switch" />Long-term memory
          </label>
        </div>
        <div className="cache-control">
          <label className="switch-label">
            <input
              type="checkbox"
              checked={cache}
              onChange={(e) => setCache(e.target.checked)}
            />
            <span className="switch" />
            Semantic cache
          </label>
        </div>
          </div>
        </details>
        <button
          className={`inspect-toggle ${inspect ? "enabled" : ""}`}
          aria-pressed={inspect}
          onClick={() => setInspect(!inspect)}
        >
          <Code2 size={17} />
          <span>Behind the answer</span>
        </button>
      </div>
      <main className={`workspace ${inspect ? "with-inspector" : ""}`}>
        <section className="chat-panel">
          <div className="panel-heading">
            <div className="flex items-center gap-3">
              <div className="agent-symbol">
                <Sparkles size={18} />
              </div>
              <div>
                <h1>Let’s get it sorted.</h1>
                <p>
                  {session
                    ? `Supporting ${person?.display_name ?? "your account"}`
                    : "Your support conversation starts here"}
                </p>
              </div>
            </div>
            {session && (
              <span className="session-cache">
                Cache {session.use_cache ? "on" : "off"} · Short-term memory {session.use_history ? "on" : "off"} · Long-term memory {session.use_memory ? "on" : "off"}
              </span>
            )}
          </div>
          <div className="conversation" aria-live="polite">
            {!turns.length &&
              !busy &&
              !pending &&
              !view?.messages.some((m) => m.role === "user") && (
                <div className="empty-state">
                  <div className="empty-icon">
                    <MessageSquare size={26} />
                  </div>
                  <h2>
                    A little context.
                    <br />A better answer.
                  </h2>
                  <p>
                    Ask about a payment, your savings settings,
                    <br />
                    or how a product works.
                  </p>
                  {!session && (
                    <button
                      className="primary"
                      disabled={blocked}
                      onClick={() => void createSession()}
                    >
                      Start a conversation
                      <ArrowRight size={16} />
                    </button>
                  )}
                  {session && (
                    <span className="empty-note">
                      Fresh conversation. Same customer account.
                    </span>
                  )}
                </div>
              )}
            {view &&
              view.turns.length === 0 &&
              view.messages.some((m) => m.role === "user") && (
                <div className="legacy">
                  <p className="fine">
                    Earlier conversation · timing and traces unavailable
                  </p>
                  {view.messages
                    .filter(
                      (m) =>
                        ["user", "assistant"].includes(m.role) && m.content,
                    )
                    .map((m, i) => (
                      <div key={i} className={`legacy-${m.role}`}>
                        {m.content}
                      </div>
                    ))}
                </div>
              )}
            {turns.map((turn) => (
              <article key={turn.turn_id} className="turn">
                {turn.event_kind === "message" ? (
                  <div className="user-message">
                    <span>You</span>
                    <div>{turn.message}</div>
                  </div>
                ) : (
                  <div className="decision-note">
                    <ShieldCheck size={14} />
                    You {turn.decision === "approve"
                      ? "approved"
                      : "declined"}{" "}
                    the proposed change
                  </div>
                )}
                <div className="assistant-message">
                  <div className="assistant-label">
                    <span className="tiny-mark">m</span>Moniepoint assistant
                  </div>
                  {turn.action && (
                    <div
                      className={`action-result ${turn.action.status === "applied" ? "success" : ""}`}
                    >
                      <ShieldCheck size={16} />
                      {turn.action.status === "applied"
                        ? "Change applied successfully"
                        : turn.action.status === "declined"
                          ? "Change declined — settings unchanged"
                          : `Change not applied${turn.action.error ? `: ${turn.action.error}` : ""}`}
                    </div>
                  )}
                  <div className="answer-text">
                    <Markdown>
                      {turn.answer ??
                        (turn.status === "failed"
                          ? "The explanation could not be completed. Check the action result above, then retry this request."
                          : "No written response was returned.")}
                    </Markdown>
                  </div>
                  {turn.approval && (
                    <Proposal
                      approval={
                        currentApproval?.id === turn.approval.id
                          ? currentApproval
                          : {
                              ...turn.approval,
                              status:
                                turns.find(
                                  (t) =>
                                    t.proposal_id === turn.approval?.id &&
                                    t.action,
                                )?.action?.status ?? turn.approval.status,
                            }
                      }
                      active={
                        [...turns]
                          .reverse()
                          .find((t) => t.approval?.id === turn.approval?.id)
                          ?.turn_id === turn.turn_id &&
                        currentApproval?.id === turn.approval.id &&
                        currentApproval.status === "pending" &&
                        currentApproval.presented
                      }
                      busy={blocked || !!pending}
                      decide={(decision) => decide(turn.approval!.id, decision)}
                    />
                  )}
                  <div className="answer-footer">
                    <span>
                      <Clock3 size={13} />
                      {timings[turn.turn_id] !== undefined
                        ? `Answered in ${seconds(timings[turn.turn_id])}`
                        : `Server processing: ${seconds(turn.timings.total_ms)}`}
                    </span>
                    <button
                      onClick={() => {
                        setSelected(turn.turn_id);
                        setInspect(true);
                      }}
                    >
                      <Layers3 size={13} />
                      {turn.trace.length} tool{" "}
                      {turn.trace.length === 1 ? "call" : "calls"}
                      <ArrowRight size={12} />
                    </button>
                  </div>
                </div>
              </article>
            ))}
            {pending?.message &&
              !turns.some((t) => t.request_id === pending.request_id) && (
                <div className="user-message">
                  <span>You</span>
                  <div>{pending.message}</div>
                </div>
              )}
            {busy && (
              <div className="thinking" role="status" aria-label="Preparing your response">
                <span className="pulse" />
                <span>Working on your request…</span>
                <small>The answer and its evidence will appear together.</small>
              </div>
            )}
            <div ref={end} />
          </div>
          <div className="composer-area">
            {error && (
              <div role="alert" className="error-banner">
                {error}
                {pending && (
                  <button
                    disabled={blocked}
                    onClick={() => void submit(pending, true)}
                  >
                    <RotateCcw size={14} />
                    Retry same request
                  </button>
                )}
              </div>
            )}
            {pending && !error && !busy && (
              <div className="error-banner">
                An unfinished request is saved.
                <button
                  disabled={blocked}
                  onClick={() => void submit(pending, true)}
                >
                  Resume request
                </button>
              </div>
            )}
            {view?.active_request_id && !pending && (
              <p className="fine">
                An unfinished request exists in this session. Resume it from the
                client that started it, or use a new session.
              </p>
            )}
            <form
              onSubmit={(e) => {
                e.preventDefault();
                send();
              }}
              className="composer"
            >
              <textarea
                aria-label="Message"
                placeholder={
                  session
                    ? "Tell us what happened…"
                    : "Start a session to send a message…"
                }
                value={draft}
                disabled={!session || blocked || !!pending}
                rows={2}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    send();
                  }
                }}
              />
              <button
                className="send-button"
                aria-label="Send message"
                disabled={!session || !draft.trim() || blocked || !!pending}
                type="submit"
              >
                <ArrowUp size={20} />
              </button>
            </form>
            <div className="composer-caption">
              <span>Enter to send · Shift + Enter for a new line</span>
              <span>
                <ShieldCheck size={12} />
                Changes always need your approval
              </span>
            </div>
          </div>
        </section>
        {inspect && (
          <aside className="inspector">
            <div className="inspector-header">
              <div>
                <div className="eyebrow">A LOOK UNDER THE HOOD</div>
                <h2>Behind the answer</h2>
              </div>
              <button
                aria-label="Hide behind the answer"
                className="icon-button"
                onClick={() => setInspect(false)}
              >
                <X size={18} />
              </button>
            </div>
            {session && (
              <div className="inspector-intro">
                <strong>{!session.use_history && !session.use_memory ? "No memory sent to the model" : "Model context"}</strong>
                <p>Short-term memory: {session.use_history ? "included" : "excluded"}. Long-term memory: {session.use_memory ? "enabled" : "excluded"}.</p>
                <p>The current message, instructions, live tools and approval state remain available. Visible chat is kept for inspection.</p>
              </div>
            )}
            <p className="inspector-intro">
              The checks, evidence, and time behind each response.
            </p>
            {activeTurn ? (
              <>
                <div className="metrics">
                  <div>
                    <small>BACKEND TIME</small>
                    <strong>
                      {(activeTurn.timings.total_ms / 1000).toFixed(2)}
                      <span>s</span>
                    </strong>
                  </div>
                  <div>
                    <small>TOOL CALLS</small>
                    <strong>
                      {activeTurn.trace.length.toString().padStart(2, "0")}
                    </strong>
                  </div>
                  <div>
                    <small>STATUS</small>
                    <b>{label(activeTurn.status)}</b>
                  </div>
                </div>
                <div className="section-label">
                  INVESTIGATION TRACE<span>{activeTurn.model}</span>
                </div>
                <div className="trace-list">
                  {activeTurn.trace.length === 0 && (
                    <p className="fine">No tools called for this response.</p>
                  )}
                  {activeTurn.trace.map((trace, i) => (
                    <details key={trace.call_id} className="trace-item">
                      <summary>
                        <span className="step-number">{i + 1}</span>
                        <span>
                          {label(trace.name)}
                          <small>
                            {trace.error ? "Needs attention" : "Completed"}
                          </small>
                        </span>
                        <span className="trace-time">
                          {(trace.duration_ms / 1000).toFixed(2)}s
                        </span>
                        <ChevronDown size={14} />
                      </summary>
                      <div className="trace-body">
                        {trace.name === "ask_policy_question" && (
                          <PolicyEvidence result={trace.result} />
                        )}
                        <small>ARGUMENTS</small>
                        <pre>{JSON.stringify(trace.arguments, null, 2)}</pre>
                        <small>
                          {trace.error ? "ERROR" : "RESULT / EVIDENCE"}
                        </small>
                        <pre>
                          {JSON.stringify(trace.result, null, 2)}
                        </pre>
                      </div>
                    </details>
                  ))}
                </div>
                <MemoryEvidence key={activeTurn.turn_id} turn={activeTurn} customer={customer} />
                <div className="timing-breakdown">
                  <div className="section-label">TIME BREAKDOWN</div>
                  {Object.entries(activeTurn.timings.stages_ms).map(
                    ([name, ms]) => (
                      <div key={name}>
                        <span>{label(name)}</span>
                        <b>{seconds(ms)}</b>
                      </div>
                    ),
                  )}
                  <p>
                    Backend timings exclude network and display time. The time
                    beneath the answer includes your browser’s wait.
                  </p>
                </div>
              </>
            ) : (
              <div className="inspector-empty">
                <Layers3 size={28} />
                <h3>Every answer has a story.</h3>
                <p>
                  Send a message to see which tools the agent uses and what it
                  finds.
                </p>
                <div className="empty-steps">
                  <span>
                    01 <b>Investigate</b>
                  </span>
                  <span>
                    02 <b>Verify</b>
                  </span>
                  <span>
                    03 <b>Explain</b>
                  </span>
                </div>
              </div>
            )}
            <div className="inspector-foot">
              <span className="live-dot" />
              Live service results · no scripted responses
            </div>
          </aside>
        )}
      </main>
      <footer className="page-footer">
        <span>MONIEPOINT × REDIS</span>
        <span>Support, with the evidence in view.</span>
      </footer>
    </div>
  );
}
