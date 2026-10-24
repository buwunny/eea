const MAX = 12;
/** Dial angle in SVG degrees: 0 at lower-left (150°), MAX at lower-right (390°). */
const angleOf = (v: number) => 150 + (Math.min(Math.max(v, 0), MAX) / MAX) * 240;
const polar = (deg: number, r: number) => {
  const a = (deg * Math.PI) / 180;
  return [60 + r * Math.cos(a), 60 + r * Math.sin(a)];
};
function arc(from: number, to: number, r: number) {
  const [x1, y1] = polar(angleOf(from), r);
  const [x2, y2] = polar(angleOf(to), r);
  const large = angleOf(to) - angleOf(from) > 180 ? 1 : 0;
  return `M ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2}`;
}

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
  const needle = angleOf(ready ? (value ?? 0) : 0) - 270;
  return (
    <div className="gauge-wrap">
      <div className="gauge">
        <svg
          viewBox="0 0 120 120"
          aria-label={`${label}: ${ready ? value : "awaiting verification"}`}
          role="img"
        >
          <circle cx="60" cy="60" r="57" fill="#0e0b09" />
          <circle
            cx="60"
            cy="60"
            r="56"
            fill="none"
            stroke="#8f7445"
            strokeWidth="1"
          />
          <circle
            cx="60"
            cy="60"
            r="53"
            fill="none"
            stroke="#352c23"
            strokeWidth="0.75"
          />
          <path
            d={arc(9, MAX, 46)}
            fill="none"
            stroke="#c4473a"
            strokeWidth="2"
          />
          {Array.from({ length: MAX * 2 + 1 }, (_, i) => {
            const major = i % 2 === 0;
            const [x1, y1] = polar(angleOf(i / 2), major ? 41 : 44);
            const [x2, y2] = polar(angleOf(i / 2), 48);
            return (
              <line
                key={i}
                x1={x1}
                y1={y1}
                x2={x2}
                y2={y2}
                stroke={i / 2 >= 9 ? "#c4473a" : "#b8a685"}
                strokeWidth={major ? 1.2 : 0.6}
                opacity={major ? 1 : 0.6}
              />
            );
          })}
          {[3, 6, 9, 12].map((v) => {
            const [x, y] = polar(angleOf(v), 33);
            return (
              <text
                key={v}
                x={x}
                y={y + 3.5}
                textAnchor="middle"
                fontSize="11"
                fill={v >= 9 ? "#d0685a" : undefined}
              >
                {v}
              </text>
            );
          })}
          <text
            className="gauge-number"
            x="60"
            y="92"
            textAnchor="middle"
          >
            {ready ? value : "—"}
          </text>
          <g
            style={{
              transform: `rotate(${needle}deg)`,
              transformOrigin: "60px 60px",
              transition: "transform 0.8s cubic-bezier(.3,1.6,.5,1)",
            }}
          >
            <line
              x1="60"
              y1="68"
              x2="60"
              y2="19"
              stroke="#ffa53d"
              strokeWidth="1.6"
              strokeLinecap="round"
              style={{ filter: "drop-shadow(0 0 2px #ff8a2a99)" }}
            />
          </g>
          <circle cx="60" cy="60" r="3.5" fill="#cfa95f" />
          <circle cx="60" cy="60" r="1.2" fill="#0e0b09" />
        </svg>
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
