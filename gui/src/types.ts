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
  mpn?: string;
  /** JLCPCB parts library: base = basic, expand = extended (assembly fee per unique part). */
  library_type?: "base" | "expand" | string | null;
  tier?: "curated" | "imported";
  footprint_review?: string;
  spice?: "curated" | "generic" | "none";
  stock?: number;
}
export interface Commit {
  hash: string;
  message: string;
  timestamp: string;
}
export interface Telemetry {
  project_name: string;
  last_updated?: string;
  git_head?: string;
  warnings?: string[];
  readiness?: DesignReadiness;
  activity?: {
    tool: string;
    status: "running" | "completed" | "warning" | "failed";
    message: string;
    started_at: string;
    updated_at: string;
    elapsed_seconds: number;
  };
  pipeline_stage: Stage;
  stages: Partial<
    Record<Stage, "pending" | "in_progress" | "completed" | "failed">
  >;
  metrics: {
    component_count: number;
    net_count: number;
    drc_errors: number | null;
    drc_warnings?: number | null;
    drc_exclusions?: number | null;
    unconnected_items: number | null;
    estimated_cost_usd: number;
    sim_passed: boolean | null;
    sim_summary?: string;
  };
  bom: Part[];
  /** Date of the catalog price/stock snapshot (YYYY-MM-DD). */
  sourcing_data_date?: string | null;
  git_commits: Commit[];
}
export interface Simulation {
  verified?: boolean;
  sim_passed?: boolean;
  functional_verified?: boolean;
  coverage_complete?: boolean;
  generic_model_components?: { ref: string; part_id: string; template: string }[];
  unmodeled_components?: string[];
  excluded_components?: string[];
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
export interface BoardFinding {
  type?: string;
  severity?: string;
  description?: string;
  position?: { x: number; y: number };
  items?: { description?: string; pos?: { x: number; y: number } }[];
}
export interface DrcReport {
  verified?: boolean;
  backend?: string;
  board_sha256?: string;
  drc_errors?: number | null;
  drc_warnings?: number | null;
  drc_exclusions?: number | null;
  unconnected_items?: number | null;
  violations?: BoardFinding[];
  unconnected?: BoardFinding[];
  exclusions?: BoardFinding[];
}
export interface BoardFocus { x: number; y: number; label: string; request: number }
export interface DesignAdvice {
  placement_checks?: { status: string; issues: { message: string; component?: string; capacitor?: string }[] };
  electrical_checks?: { issues: { code: string; component: string | null; net: string | null; message: string; severity: string }[] };
  power_budget?: {
    rails: { net: string; voltage_v: number; provenance: string; known_current_ma: number; max_current_ma: number | null; unknown_loads: string[] }[];
    resistor_dissipation: { component: string; power_w: number }[];
    regulators: { component: string; known_load_dissipation_w: number | null }[];
    unknown_loads: string[];
  };
  footprint_reviews?: { component: string; status: string }[];
}
export interface DesignReadiness {
  board_sha256?: string;
  design_sha256?: string;
  fabrication_files_ready: boolean;
  fabrication_review_ready: boolean;
  assembly_ready: boolean;
  checks: Record<string, { status: "passed" | "needs_attention" | "incomplete"; reasons: string[] }>;
}
export const emptyTelemetry: Telemetry = {
  project_name: "Untitled project",
  pipeline_stage: "ARCHITECTURE",
  stages: {},
  metrics: {
    component_count: 0,
    net_count: 0,
    drc_errors: null,
    unconnected_items: null,
    estimated_cost_usd: 0,
    sim_passed: null,
  },
  bom: [],
  git_commits: [],
};
