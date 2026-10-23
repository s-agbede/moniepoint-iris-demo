/** A small human-readable view; the complete tool result remains inspectable. */
export default function PolicyEvidence({ result }: { result: unknown }) {
  if (!result || typeof result !== "object") return null;
  const data = result as Record<string, unknown>;
  const trace = Array.isArray(data.trace)
    ? data.trace.filter((item): item is string => typeof item === "string")
    : [];
  const sources = Array.isArray(data.sources) ? data.sources : [];
  return (
    <div className="policy-evidence">
      {typeof data.answer_source === "string" && (
        <p>
          <b>Answer source:</b>{" "}
          {data.answer_source === "cache"
            ? "Verified semantic cache"
            : "Generated from policy evidence"}
        </p>
      )}
      {trace.length > 0 && (
        <p>
          <b>Cache decisions:</b>{" "}
          {trace.map((item) => item.replaceAll("_", " ")).join(" → ")}
        </p>
      )}
      {sources.map((source: unknown, index: number) => {
        if (!source || typeof source !== "object") return null;
        const item = source as Record<string, unknown>;
        return (
          <p key={index}>
            ↗ {String(item.title ?? item.id ?? "Policy source")}
          </p>
        );
      })}
    </div>
  );
}
