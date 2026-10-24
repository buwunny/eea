export default function NixieCounter({
  label,
  value,
  history,
  ready,
}: {
  label: string;
  value: number | null;
  history: number[];
  ready: boolean;
}) {
  const digits = ready ? String(value ?? 0).padStart(2, "0") : "--";
  const state = !ready ? "idle" : value === 0 ? "clear" : "fault";
  return (
    <div className="counter-wrap">
      <div
        className={`nixie-counter ${state}`}
        role="img"
        aria-label={`${label}: ${ready ? value : "awaiting verification"}`}
      >
        {digits.split("").map((d, i) => (
          <span key={i}>{d}</span>
        ))}
      </div>
      <label>{label}</label>
      <small className={ready && value === 0 ? "green" : ""}>
        {ready
          ? `${history.slice(-4).join(" → ")}${value === 0 ? " · clear" : ""}`
          : "not yet checked"}
      </small>
    </div>
  );
}
