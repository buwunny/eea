import { useEffect, useState } from "react";
import { Crosshair } from "lucide-react";
import type { BoardFocus, DrcReport } from "../types";
import { artifact } from "../api";
import { drcFindings, reportMatchesBoard } from "../lib/verification";

export default function DrcInspector({ report, pcbUrl, onFocus }: { report: DrcReport | null; pcbUrl: string | null; onFocus: (point: BoardFocus) => void }) {
  const [digest, setDigest] = useState<{ url: string; value: string } | null>(null);
  const [page, setPage] = useState(0);
  const [severity, setSeverity] = useState("all");
  const [hashError, setHashError] = useState("");
  const [checking, setChecking] = useState(false);
  useEffect(() => {
    let active = true;
    setDigest(null);
    setHashError("");
    setChecking(!!pcbUrl);
    if (pcbUrl) void fetch(artifact(pcbUrl)).then(response => {
      if (!response.ok) throw new Error("Board unavailable");
      return response.arrayBuffer();
    }).then(bytes => crypto.subtle.digest("SHA-256", bytes)).then(hash => {
      if (active) setDigest({ url: pcbUrl, value: [...new Uint8Array(hash)].map(value => value.toString(16).padStart(2, "0")).join("") });
    }).catch(() => { if (active) setHashError("Cannot verify report freshness. Reload the board or run DRC again."); })
      .finally(() => { if (active) setChecking(false); });
    return () => { active = false; };
  }, [pcbUrl]);
  useEffect(() => { setPage(0); }, [report, pcbUrl]);
  const allFindings = drcFindings(report);
  const findings = allFindings.filter(finding => severity === "all" || finding.severity === severity);
  const warnings = allFindings.filter(finding => finding.severity === "warning").length;
  const current = !!pcbUrl && digest?.url === pcbUrl && reportMatchesBoard(report, digest.value);
  const pages = Math.max(1, Math.ceil(findings.length / 3));
  const selectedPage = Math.min(page, pages - 1);
  return <div className="drc-inspector">
    <p className="tiny">{!report ? "Run DRC to inspect findings." : hashError || (checking ? "Checking report freshness…" : !current ? "Report does not match this board. Run DRC again." : report.verified ? `KiCad DRC · ${allFindings.length} findings · ${warnings} warnings` : `Geometric precheck · ${allFindings.length} findings · unverified`)}</p>
    {!!allFindings.length && <label className="drc-filters">Show findings<select aria-label="DRC severity" value={severity} onChange={event => { setSeverity(event.target.value); setPage(0); }}><option value="all">All</option><option value="error">Errors</option><option value="warning">Warnings</option><option value="exclusion">Exclusions</option></select></label>}
    {findings.slice(selectedPage * 3, selectedPage * 3 + 3).map((finding, i) => <button
      key={`${selectedPage}-${i}`} className="drc-finding" disabled={!current || !finding.position}
      title={finding.details || finding.title}
      onClick={() => finding.position && onFocus({ ...finding.position, label: finding.title, request: Date.now() })}
    ><Crosshair size={14} /><span><b>{finding.title}</b><small>{finding.details || (finding.position ? `${finding.position.x.toFixed(2)}, ${finding.position.y.toFixed(2)} mm` : "No location reported")}</small></span><em>{finding.severity}</em></button>)}
    {pages > 1 && <div className="finding-pages"><button disabled={!selectedPage} onClick={() => setPage(selectedPage - 1)}>Previous</button><span>{selectedPage + 1} / {pages}</span><button disabled={selectedPage + 1 >= pages} onClick={() => setPage(selectedPage + 1)}>Next</button></div>}
  </div>;
}
