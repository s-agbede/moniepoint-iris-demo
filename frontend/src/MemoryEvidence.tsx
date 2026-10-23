import { useEffect, useState } from "react";
import { api, type Turn } from "./api";

type Search = { status: string; memories: unknown[]; error?: string | null };

export function MemoryEvidence({turn, customer}: {turn: Turn; customer: string}) {
  const [latest, setLatest] = useState<Search | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { setLatest(null); setError(""); }, [turn.turn_id, customer]);
  if (!turn.memory || (turn.memory.status === "disabled" && (!turn.memory_recording || turn.memory_recording.status === "disabled"))) {
    return <p className="fine">Long-term memory was off for this turn.</p>;
  }
  return <section className="memory-evidence">
    <div className="section-label">IRIS MEMORY</div>
    <p className="fine">Lookup: {turn.memory.status === "disabled" ? "not needed for this turn" : turn.memory.status}. Events: {turn.memory_recording?.status ?? "disabled"}.</p>
    {turn.memory.error && <p role="alert">{turn.memory.error}</p>}
    {turn.memory_recording?.error && <p role="alert">{turn.memory_recording.error}</p>}
    <details className="trace-item"><summary>Memories used for this answer ({turn.memory.memories.length})</summary>
      <pre>{JSON.stringify(turn.memory.memories, null, 2)}</pre>
    </details>
    <details className="trace-item"><summary>Redacted events recorded in Iris</summary>
      <pre>{JSON.stringify(turn.memory_recording?.events ?? [], null, 2)}</pre>
    </details>
    <p className="fine">Recording events does not mean extraction has finished. Refresh after your extraction interval to inspect customer observations across sessions.</p>
    <button className="secondary" disabled={loading} onClick={async () => {
      setLoading(true); setError("");
      try { setLatest(await api<Search>(`/support/sessions/${turn.session_id}/memory`, customer)); }
      catch(e) { setError(String(e)); }
      finally { setLoading(false); }
    }}>{loading ? "Checking Iris…" : "Check extracted observations"}</button>
    {error && <p role="alert">{error}</p>}
    {latest && <><p className="fine">{latest.status}: {latest.memories.length} matching observations. {latest.error}
      {latest.status === "ready" && latest.memories.length === 0 && " No support_observation memories found yet; extraction may still be pending."}</p>
      <pre>{JSON.stringify(latest.memories, null, 2)}</pre></>}
  </section>;
}
