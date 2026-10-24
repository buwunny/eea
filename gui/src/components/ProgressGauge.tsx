/** Brass dial showing completed pipeline stages; sweeps 240° from lower-left to lower-right. */
const angleOf = (v: number, max: number) => 150 + (Math.min(Math.max(v, 0), max) / max) * 240;
const polar = (deg: number, r: number) => {
  const a = (deg * Math.PI) / 180;
  return [60 + r * Math.cos(a), 60 + r * Math.sin(a)];
};

export default function ProgressGauge({
  done,
  total,
  failed,
}: {
  done: number;
  total: number;
  failed: boolean;
}) {
  const needle = angleOf(done, total) - 270;
  return (
    <div className="progress-gauge">
      <svg
        viewBox="0 0 120 120"
        role="img"
        aria-label={`Pipeline progress: ${done} of ${total} stages complete`}
      >
        <circle cx="60" cy="60" r="57" fill="#0e0b09" />
        <circle cx="60" cy="60" r="56" fill="none" stroke="#8f7445" strokeWidth="1" />
        <circle cx="60" cy="60" r="53" fill="none" stroke="#352c23" strokeWidth="0.75" />
        {Array.from({ length: total + 1 }, (_, i) => {
          const [x1, y1] = polar(angleOf(i, total), 41);
          const [x2, y2] = polar(angleOf(i, total), 48);
          const [tx, ty] = polar(angleOf(i, total), 31);
          return (
            <g key={i}>
              <line x1={x1} y1={y1} x2={x2} y2={y2} stroke={i === total ? "#6fcf8f" : "#b8a685"} strokeWidth="1.2" />
              <text x={tx} y={ty + 3.5} textAnchor="middle" fontSize="10">{i}</text>
            </g>
          );
        })}
        <text className="gauge-number" x="60" y="92" textAnchor="middle">
          {done}/{total}
        </text>
        <g
          style={{
            transform: `rotate(${needle}deg)`,
            transformOrigin: "60px 60px",
            transition: "transform 0.8s cubic-bezier(.3,1.6,.5,1)",
          }}
        >
          <line
            x1="60" y1="68" x2="60" y2="19"
            stroke={failed ? "#d0685a" : done === total ? "#6fcf8f" : "#ffa53d"}
            strokeWidth="1.6"
            strokeLinecap="round"
          />
        </g>
        <circle cx="60" cy="60" r="3.5" fill="#cfa95f" />
        <circle cx="60" cy="60" r="1.2" fill="#0e0b09" />
      </svg>
    </div>
  );
}
