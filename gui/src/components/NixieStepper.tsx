import { STAGES, Telemetry } from "../types";
const labels = [
  "Architect",
  "Synthesize",
  "Simulate",
  "Autoroute",
  "Verify DRC",
  "Fabricate",
];
export default function NixieStepper({ telemetry }: { telemetry: Telemetry }) {
  return (
    <section className="instrument sequence">
      <div className="panel-heading">
        <span>01 / THE INVENTION SEQUENCE</span>
      </div>
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
              <small>
                {state === "completed"
                  ? "VERIFIED"
                  : state === "in_progress"
                    ? "ACTIVE"
                    : state === "failed"
                      ? "REVISE"
                      : "STANDBY"}
              </small>
            </div>
          );
        })}
      </div>
    </section>
  );
}
