import type { DesignAdvice } from "../types";

export default function PowerInspector({ design }: { design: DesignAdvice | null }) {
  const budget = design?.power_budget;
  const issues = design?.electrical_checks?.issues ?? [];
  return <section className="instrument power-inspector">
    <p className="tiny">Design estimates · incomplete · unverified</p>
    {!budget ? <p>Synthesize a design to inspect power and electrical advice.</p> : <>
      {!budget.rails.length && <p>No rail voltages identified. Declare net voltage_v to estimate loads.</p>}
      {budget.rails.map(rail => <div className="power-rail" key={rail.net}>
        <div><b>{rail.net}</b><span>{rail.voltage_v.toPrecision(3)} V</span></div>
        <p>{rail.known_current_ma.toPrecision(3)} mA known{rail.max_current_ma != null ? ` / ${rail.max_current_ma} mA capacity` : " · capacity unknown"}</p>
        <small>{rail.provenance === "declared" ? "Declared voltage" : "Nominal voltage hint"}{rail.unknown_loads.length ? ` · unknown draw: ${rail.unknown_loads.join(", ")}` : ""}</small>
      </div>)}
      {budget.regulators.map(regulator => <p key={regulator.component}>{regulator.component} dissipation: {regulator.known_load_dissipation_w == null ? "unknown" : `${regulator.known_load_dissipation_w.toPrecision(3)} W from known loads`}</p>)}
      {budget.resistor_dissipation.length > 0 && <details><summary>Resistor dissipation</summary>{budget.resistor_dissipation.map(item => <p key={item.component}>{item.component}: {item.power_w.toPrecision(3)} W</p>)}</details>}
      {budget.unknown_loads.length > 0 && <p className="tiny">Unknown consumption: {budget.unknown_loads.join(", ")}. Known loads exclude unsolved dynamic and regulator input currents.</p>}
    </>}
    {!!issues.length && <details open><summary>Electrical advice ({issues.length})</summary><ul>{issues.map((issue, i) => <li key={i}><b>{issue.component || issue.net || "Design"}</b> · {issue.message}</li>)}</ul></details>}
    {!!design?.footprint_reviews?.length && <p className="tiny">Footprint geometry requires review against the manufacturer drawing before fabrication.</p>}
  </section>;
}
