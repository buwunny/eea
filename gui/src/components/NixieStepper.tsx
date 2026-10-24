import ProgressGauge from "./ProgressGauge";
import { STAGES, Telemetry } from "../types";
const labels = ["Plan", "PCB", "Sim", "Route", "DRC", "Export"];
export default function NixieStepper({ telemetry }: { telemetry: Telemetry }) {
  const states = STAGES.map((st) => telemetry.stages[st] ?? "pending");
  const done = states.filter((st) => st === "completed").length;
  return (
    <section className="instrument sequence">
      <ProgressGauge done={done} total={STAGES.length} failed={states.includes("failed")} />
      <div className="tubes">
        {STAGES.map((stage, i) => {
          let state = telemetry.stages[stage] ?? "pending";
          // Older servers recorded partial routing as pending; preserve the actual result in the display.
          if (
            stage === "AUTOROUTING" &&
            stage === telemetry.pipeline_stage &&
            state === "pending" &&
            (telemetry.metrics.unconnected_items ?? 0) > 0
          )
            state = "failed";
          return (
            <div
              className={`tube-unit ${state}`}
              key={stage}
              title={`${labels[i]}: ${state}`}
            >
              <div className="tube">
                <span>{String(i + 1).padStart(2, "0")}</span>
              </div>
              <label>{labels[i]}</label>
            </div>
          );
        })}
      </div>
    </section>
  );
}
