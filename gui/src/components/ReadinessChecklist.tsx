import { useEffect, useState } from "react";
import type { DesignAdvice, DesignReadiness } from "../types";
import { artifact } from "../api";

const labels: Record<string, string> = {
  connectivity_clearance: "Connectivity & clearance",
  nominal_simulation: "Nominal simulation",
  placement_constraints: "Placement constraints",
  footprint_review: "Footprint review",
  assembly_sourcing: "Assembly sourcing",
  drc_warnings: "DRC warning review",
  drc_review: "DRC warning review",
  requirements: "Design requirements",
  timing_tolerance: "Timing tolerance estimate",
};

export default function ReadinessChecklist({ readiness, pcbUrl, design }: { readiness: DesignReadiness | null; pcbUrl?: string | null; design?: DesignAdvice | null }) {
  const [binding, setBinding] = useState<{ report: DesignReadiness; url: string; design?: DesignAdvice | null; status: string } | null>(null);
  useEffect(() => {
    let active = true;
    if (readiness && pcbUrl) {
      const hash = async (url: string) => {
        const response = await fetch(artifact(url));
        if (!response.ok) throw new Error("Artifact unavailable");
        const digest = await crypto.subtle.digest("SHA-256", await response.arrayBuffer());
        return [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, "0")).join("");
      };
      void Promise.all([hash(pcbUrl), hash(pcbUrl.replace("board.kicad_pcb", "design.json"))])
        .then(([boardHash, designHash]) => {
          if (active) setBinding({ report: readiness, url: pcbUrl, design, status: boardHash === readiness.board_sha256 && designHash === readiness.design_sha256 ? "current" : "stale" });
        }).catch(() => { if (active) setBinding({ report: readiness, url: pcbUrl, design, status: "unavailable" }); });
    }
    return () => { active = false; };
  }, [readiness, pcbUrl, design]);
  if (!readiness) return <p className="tiny readiness-idle">Run an MCP design operation to review readiness.</p>;
  if (pcbUrl === null) return <p className="tiny readiness-idle">No current board is available for readiness review.</p>;
  if (pcbUrl) {
    const status = binding?.report === readiness && binding?.url === pcbUrl && binding?.design === design ? binding.status : "checking";
    if (status !== "current") return <p className="tiny readiness-idle">{status === "checking" ? "Checking readiness freshness…" : status === "stale" ? "Readiness is stale. Refresh it with get_design_readiness." : "Cannot verify readiness freshness. Reload the board and refresh readiness."}</p>;
  }
  return <details className="readiness-checklist">
    <summary>Design readiness · {readiness.assembly_ready ? "assembly reviewed" : "review incomplete"}</summary>
    <ul>{Object.entries(readiness.checks ?? {}).map(([key, check]) => <li key={key}>
      <div><b>{labels[key] || key.replaceAll("_", " ")}</b><span className={check.status === "passed" ? "green" : "warning"}>{check.status.replaceAll("_", " ")}</span></div>
      {!!check.reasons.length && <small>{check.reasons.join(" · ")}</small>}
    </li>)}</ul>
    <p className="tiny">{readiness.fabrication_files_ready ? "Fabrication files available." : "Fabrication files not ready."} {readiness.fabrication_review_ready ? "Fabrication review complete." : "Fabrication review incomplete."}</p>
  </details>;
}
