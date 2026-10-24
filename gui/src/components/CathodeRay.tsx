import { memo, useMemo } from "react";
import {
  Line,
  LineChart,
  ResponsiveContainer,
  CartesianGrid,
  XAxis,
  YAxis,
  Tooltip,
} from "recharts";
import { Simulation } from "../types";
function CathodeRay({
  simulation,
  passed,
}: {
  simulation: Simulation | null;
  passed: boolean | null;
}) {
  const data = useMemo(
    () =>
      simulation?.x_data.map((x, i) =>
        Object.fromEntries([
          ["x", x],
          ...Object.entries(simulation.signals).map(([key, values]) => [
            key,
            values[i],
          ]),
        ]),
      ) ?? [],
    [simulation],
  );
  return (
    <section className="instrument scope">
      <div className="panel-heading">
        <span>Simulation</span>
        <span className="tiny">
          {simulation?.analysis_type.toUpperCase() ?? "TRANSIENT"}
        </span>
      </div>
      <div className="scope-screen">
        {data.length ? (
          <div className="scope-plot">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart
                data={data}
                margin={{ top: 12, right: 12, left: -14, bottom: 0 }}
              >
                <CartesianGrid stroke="#25432c" strokeDasharray="2 4" />
                <XAxis
                  dataKey="x"
                  tick={{ fill: "#7d9774", fontSize: 12 }}
                  tickLine={false}
                  minTickGap={35}
                />
                <YAxis
                  tick={{ fill: "#7d9774", fontSize: 12 }}
                  tickLine={false}
                />
                <Tooltip
                  contentStyle={{
                    background: "#101a10",
                    border: "1px solid #6a8752",
                    fontSize: 11,
                  }}
                />
                {Object.keys(simulation!.signals).map((name, i) => (
                  <Line
                    key={name}
                    name={name}
                    type={i === 0 ? "stepAfter" : "monotone"}
                    dataKey={name}
                    dot={false}
                    stroke={i === 0 ? "#8fe66b" : "#d8aa48"}
                    strokeWidth={1.6}
                    isAnimationActive={false}
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <div className="scope-idle">No simulation</div>
        )}
        <div className="scope-legend">
          {Object.keys(simulation?.signals ?? {}).map((name) => (
            <span key={name}>● {name}</span>
          ))}
          <span>{simulation?.x_axis_label ?? "Time (ms)"}</span>
        </div>
      </div>
      <div className="scope-readouts">
        <div>
          <small>Frequency</small>
          <b>
            {simulation?.measurements.frequency_hz?.toFixed(2) ?? "—"}
            <em> Hz</em>
          </b>
        </div>
        <div>
          <small>Amplitude</small>
          <b>
            {simulation?.measurements.vpp?.toFixed(2) ?? "—"}
            <em> Vpp</em>
          </b>
        </div>
        <div>
          <small>Result</small>
          <b className={simulation ? (passed ? "green" : "warning") : ""}>
            {simulation
              ? simulation.verified === false || passed === null
                ? "ESTIMATE"
                : passed
                  ? "PASS"
                  : "REVISE"
              : "—"}
          </b>
        </div>
      </div>
    </section>
  );
}

export default memo(CathodeRay);
