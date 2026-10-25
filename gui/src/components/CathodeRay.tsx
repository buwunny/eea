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
import { simulationResult } from "../lib/verification";
const tickStyle = {
  fill: "#6f8566",
  fontSize: 10,
  fontFamily: "IBM Plex Mono, monospace",
};
/** Short axis labels: 264.5894406 -> 265, 0.0123 -> 0.012 */
function tidy(v: number) {
  if (!Number.isFinite(v)) return "";
  const a = Math.abs(v);
  if (a >= 100) return Math.round(v).toString();
  if (a >= 10) return (Math.round(v * 10) / 10).toString();
  return Number(v.toPrecision(2)).toString();
}
function CathodeRay({
  simulation,
  passed,
}: {
  simulation: Simulation | null;
  passed: boolean | null;
}) {
  const result = simulationResult(simulation, passed);
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
        <span>
          <i className="fig">III.</i> Simulation
        </span>
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
                <CartesianGrid stroke="#3f5c33" strokeOpacity={0.35} />
                <XAxis
                  dataKey="x"
                  type="number"
                  domain={["dataMin", "dataMax"]}
                  tick={tickStyle}
                  tickFormatter={tidy}
                  tickLine={false}
                  axisLine={false}
                  minTickGap={35}
                />
                <YAxis
                  tick={tickStyle}
                  tickFormatter={tidy}
                  tickLine={false}
                  axisLine={false}
                  width={44}
                />
                <Tooltip
                  formatter={(v) => tidy(Number(v))}
                  labelFormatter={(v) => tidy(Number(v))}
                  contentStyle={{
                    background: "#241e18",
                    border: "1px solid #4a3e31",
                    borderRadius: 6,
                    color: "#ece2cf",
                    fontFamily: "IBM Plex Mono, monospace",
                    fontSize: 12,
                  }}
                />
                {Object.keys(simulation!.signals).map((name, i) => (
                  <Line
                    key={name}
                    name={name}
                    type="linear"
                    dataKey={name}
                    dot={false}
                    stroke={i === 0 ? "#8fe079" : "#f0c27a"}
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
      {!!simulation?.generic_model_components?.length && (
        <p
          className="model-plaque"
          title="These parts are simulated with generic approximate models, not vendor models. Treat results as indicative."
          aria-label={`Approximate models in use: ${simulation.generic_model_components.map((g) => `${g.ref} ${g.template}`).join(", ")}. Results are indicative.`}
        >
          APPROXIMATE MODELS:{" "}
          {simulation.generic_model_components
            .map((g) => `${g.ref} ${g.template}`)
            .join(", ")}
        </p>
      )}
      {!!simulation?.unmodeled_components?.length && (
        <p
          className="model-plaque unmodeled"
          title="These functional components have no SPICE model and are excluded, so the simulation does not cover them."
          aria-label={`Not simulated, no model: ${simulation.unmodeled_components.join(", ")}`}
        >
          NOT SIMULATED: {simulation.unmodeled_components.join(", ")}
        </p>
      )}
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
          <b className={simulation ? (result === "PASS" ? "green" : "warning") : ""} title={result === "PARTIAL" ? "Simulation excludes functional components or has incomplete source coverage" : undefined}>
            {result}
          </b>
        </div>
      </div>
    </section>
  );
}

export default memo(CathodeRay);
