import { STAGES, Telemetry } from "../types";
const labels = ["Plan", "PCB", "Sim", "Route", "DRC", "Export"];
export default function NixieStepper({ telemetry }: { telemetry: Telemetry }) {
  return (
    <section className="instrument sequence">
      <div className="tubes">
        {STAGES.map((stage, i) => {
          const state = telemetry.stages[stage] ?? "pending";
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
