import { STAGES, Telemetry, Simulation } from "./types";
export const demoSimulation: Simulation = {
  analysis_type: "transient",
  x_axis_label: "Time (ms)",
  x_data: Array.from({ length: 160 }, (_, i) => i * 12.5),
  signals: {
    "V(OUT)": Array.from({ length: 160 }, (_, i) =>
      i % 40 < 22 ? 4.98 : 0.02,
    ),
    "V(THR)": Array.from(
      { length: 160 },
      (_, i) => 1.67 + Math.abs((i % 40) / 20 - 1) * 1.66,
    ),
  },
  measurements: { frequency_hz: 2.0, vpp: 4.96, duty_cycle_pct: 55 },
  generic_model_components: [{ ref: "Q1", part_id: "MMBT3904", template: "npn" }],
  unmodeled_components: ["J1"],
};
export function demoTelemetry(step: number): Telemetry {
  return {
    project_name: "USB-C 555 Pulse Generator",
    pipeline_stage: STAGES[Math.min(step, 5)],
    stages: Object.fromEntries(
      STAGES.map((s, i) => [
        s,
        i < step
          ? "completed"
          : i === step
            ? step === 2
              ? "failed"
              : step === 6
                ? "completed"
                : "in_progress"
            : "pending",
      ]),
    ),
    metrics: {
      component_count: 8,
      net_count: 6,
      drc_errors: step >= 5 ? 0 : step >= 3 ? 1 : 4,
      unconnected_items: step >= 4 ? 0 : 12,
      estimated_cost_usd: 4.15,
      sim_passed: step >= 3,
      sim_summary:
        step === 2
          ? "Frequency outside target. Adjusting timing resistor…"
          : "Oscillation verified at 2.00 Hz (target: 2.0 Hz)",
    },
    sourcing_data_date: "2026-10-23",
    bom: [
      {
        ref: "U1",
        value: "NE555",
        footprint: "SOIC-8",
        lcsc: "C7593",
        price_usd: 0.18,
        mpn: "NE555DR",
        library_type: "base",
        tier: "curated",
        footprint_review: "reviewed",
        spice: "curated",
        stock: 120000,
      },
      {
        ref: "J1",
        value: "USB-C",
        footprint: "HRO TYPE-C",
        price_usd: 0.67,
        tier: "imported",
        footprint_review: "unreviewed",
        spice: "none",
      },
      {
        ref: "Q1",
        value: "MMBT3904",
        footprint: "SOT-23",
        lcsc: "C20526",
        price_usd: 0.03,
        mpn: "MMBT3904",
        library_type: "expand",
        tier: "imported",
        footprint_review: "unreviewed",
        spice: "generic",
        stock: 54000,
      },
      {
        ref: "R1",
        value: step >= 3 ? "10 kΩ" : "1 kΩ",
        footprint: "0805",
        price_usd: 0.02,
        tier: "curated",
        footprint_review: "reviewed",
        spice: "curated",
      },
      { ref: "R2", value: "330 kΩ", footprint: "0805", price_usd: 0.02, tier: "curated", footprint_review: "reviewed", spice: "curated" },
      { ref: "C1", value: "1 µF", footprint: "0805", price_usd: 0.08, tier: "curated", footprint_review: "reviewed", spice: "curated" },
      { ref: "C2", value: "100 nF", footprint: "0805", price_usd: 0.03, tier: "curated", footprint_review: "reviewed", spice: "curated" },
      {
        ref: "D1",
        value: "Amber LED",
        footprint: "0805",
        price_usd: 0.05,
        tier: "curated",
        footprint_review: "reviewed",
        spice: "curated",
      },
      { ref: "R3", value: "1 kΩ", footprint: "0805", price_usd: 0.02, tier: "curated", footprint_review: "reviewed", spice: "curated" },
    ],
    git_commits: [],
  };
}
