export default function PressureGauge({
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
  const angle = -120 + Math.min((value ?? 0) / 12, 1) * 240;
  return (
    <div className="gauge-wrap">
      <div className="gauge">
        <svg
          viewBox="0 0 150 100"
          aria-label={`${label}: ${ready ? value : "awaiting verification"}`}
          role="img"
        >
          <path
            d="M 24 75 A 54 54 0 1 1 126 75"
            fill="none"
            stroke="#514630"
            strokeWidth="5"
          />
          <path
            d="M 108 27 A 54 54 0 0 1 126 75"
            fill="none"
            stroke="#933d29"
            strokeWidth="5"
          />
          {Array.from({ length: 13 }, (_, i) => {
            const a = ((150 + i * 20) * Math.PI) / 180;
            return (
              <line
                key={i}
                x1={75 + 45 * Math.cos(a)}
                y1={57 + 45 * Math.sin(a)}
                x2={75 + 50 * Math.cos(a)}
                y2={57 + 50 * Math.sin(a)}
                stroke="#ab966b"
                strokeWidth="1"
              />
            );
          })}
          <text x="30" y="86">
            0
          </text>
          <text x="115" y="86">
            12
          </text>
          <g transform={`rotate(${angle} 75 57)`}>
            <path d="M 72 61 L 75 15 L 78 61 Z" fill="#c46239" />
          </g>
          <circle cx="75" cy="57" r="6" fill="#ac8b4b" />
          <text className="gauge-number" x="75" y="89" textAnchor="middle">
            {ready ? value : "—"}
          </text>
        </svg>
      </div>
      <label>{label}</label>
      <small className={ready && value === 0 ? "green" : ""}>
        {ready
          ? `${history.slice(-4).join(" → ")}${value === 0 ? " · Clear" : ""}`
          : "Not checked"}
      </small>
    </div>
  );
}
