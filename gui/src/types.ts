export const STAGES = [
  "ARCHITECTURE",
  "SCHEMATIC_PCB",
  "SIMULATION",
  "AUTOROUTING",
  "DRC_VERIFICATION",
  "MANUFACTURING",
] as const;
export type Stage = (typeof STAGES)[number];
export interface Part {
  ref: string;
  value: string;
  footprint: string;
  lcsc?: string;
  price_usd: number;
}
export interface Commit {
  hash: string;
  message: string;
  timestamp: string;
}
export interface Telemetry {
  project_name: string;
  last_updated?: string;
  pipeline_stage: Stage;
  stages: Partial<
    Record<Stage, "pending" | "in_progress" | "completed" | "failed">
  >;
  metrics: {
    component_count: number;
    net_count: number;
    drc_errors: number | null;
    unconnected_items: number | null;
    estimated_cost_usd: number;
    sim_passed: boolean | null;
    sim_summary?: string;
  };
  bom: Part[];
  git_commits: Commit[];
}
export interface Simulation {
  verified?: boolean;
  backend?: string;
  analysis_type: string;
  x_axis_label: string;
  x_data: number[];
  signals: Record<string, number[]>;
  measurements: {
    frequency_hz?: number;
    vpp?: number;
    duty_cycle_pct?: number;
  };
}
export const emptyTelemetry: Telemetry = {
  project_name: "Untitled project",
  pipeline_stage: "ARCHITECTURE",
  stages: {},
  metrics: {
    component_count: 0,
    net_count: 0,
    drc_errors: 0,
    unconnected_items: 0,
    estimated_cost_usd: 0,
    sim_passed: false,
  },
  bom: [],
  git_commits: [],
};
