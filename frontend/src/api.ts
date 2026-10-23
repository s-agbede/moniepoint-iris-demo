export interface Customer {
  id: string;
  display_name: string;
}
export interface Session {
  id: string;
  customer_id: string;
  created_at: string;
  use_cache: boolean;
  use_memory: boolean;
  use_history: boolean;
}
export interface Approval {
  id: string;
  status: string;
  presented: boolean;
  before_enabled_transaction_types: string[];
  enabled_transaction_types: string[];
}
export interface Trace {
  call_id: string;
  name: string;
  arguments: unknown;
  result: unknown;
  error: boolean;
  duration_ms: number;
}
export interface Turn {
  memory?: { status: string; memories: unknown[]; error?: string | null };
  memory_recording?: { status: string; events: unknown[]; error?: string | null };
  session_id: string;
  turn_id: string;
  request_id: string;
  message: string;
  answer: string | null;
  status: string;
  error: string | null;
  model: string;
  trace: Trace[];
  approval: Approval | null;
  event_kind: "message" | "decision";
  proposal_id: string | null;
  decision: "approve" | "decline" | null;
  action: { status: string; error?: string } | null;
  timings: { total_ms: number; stages_ms: Record<string, number> };
}
export interface SessionView {
  session: Session;
  turns: Turn[];
  messages: { role: string; content: string | null }[];
  approval: Approval | null;
  active_request_id: string | null;
}
export interface Pending {
  customer: string;
  session: string;
  request_id: string;
  message?: string;
  proposal?: string;
  decision?: "approve" | "decline";
}
export async function api<T>(
  path: string,
  customer?: string,
  body?: unknown,
): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      "Content-Type": "application/json",
      ...(customer ? { "X-Demo-Customer-Id": customer } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data: unknown = await response.json();
  if (!response.ok) {
    const object = data as { detail?: unknown; error?: string };
    throw new Error(
      typeof object.detail === "string"
        ? object.detail
        : (object.error ?? `Request failed (${response.status})`),
    );
  }
  return data as T;
}
export function remembered<T>(key: string, fallback: T): T {
  try {
    return (JSON.parse(localStorage.getItem(key) ?? "null") as T) ?? fallback;
  } catch {
    return fallback;
  }
}
export function remember(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* Storage is optional; server history remains authoritative. */
  }
}
