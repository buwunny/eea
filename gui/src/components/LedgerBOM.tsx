import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { ChevronLeft, ChevronRight, Download } from "lucide-react";
import { Part } from "../types";
/** Row height in px; must match `.ledger td` in steampunk.css. */
const ROW_HEIGHT = 60;
const HEAD_HEIGHT = 34;
const NO_CODE_FREE = new Set(["R", "C"]);

interface Stamp {
  text: string;
  tone: "warn" | "info" | "ok";
  hint: string;
}
function stamps(p: Part): Stamp[] {
  const out: Stamp[] = [];
  if (p.tier === "imported")
    out.push({
      text: "IMPORTED",
      tone: "warn",
      hint: "Imported from the KiCad library: not hand-reviewed",
    });
  if (p.footprint_review && p.footprint_review !== "reviewed" && p.footprint_review !== "verified")
    out.push({
      text: "UNREVIEWED",
      tone: "warn",
      hint: `Footprint review status: ${p.footprint_review}. Check it against the manufacturer drawing before ordering boards.`,
    });
  if (p.spice === "generic")
    out.push({
      text: "GENERIC MODEL",
      tone: "info",
      hint: "Simulated with a generic approximate model, not a vendor model",
    });
  if (!p.lcsc && !NO_CODE_FREE.has(p.ref.replace(/[0-9]+$/, "")))
    out.push({
      text: "NO CODE",
      tone: "warn",
      hint: "No LCSC order code: this part cannot be ordered through JLCPCB assembly yet",
    });
  if (p.library_type === "expand")
    out.push({
      text: "EXTENDED",
      tone: "info",
      hint: "JLCPCB extended part: each unique extended part adds a per-part assembly fee",
    });
  else if (p.library_type === "base")
    out.push({
      text: "BASIC",
      tone: "ok",
      hint: "JLCPCB basic part: no extended-part assembly fee",
    });
  return out;
}
export default function LedgerBOM({
  parts,
  cost,
  sourcingDate,
  layerCount,
  canDownload,
  projectId,
  canExport,
  exporting,
  onExport,
}: {
  parts: Part[];
  cost: number;
  sourcingDate?: string | null;
  layerCount?: number;
  canDownload: boolean;
  projectId?: string;
  canExport?: boolean;
  exporting?: boolean;
  onExport?: () => void;
}) {
  const table = useRef<HTMLDivElement>(null);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(4);
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) =>
      setPageSize(
        Math.max(1, Math.floor((entry.contentRect.height - HEAD_HEIGHT) / ROW_HEIGHT)),
      ),
    );
    if (table.current) observer.observe(table.current);
    return () => observer.disconnect();
  }, []);
  const pages = Math.max(1, Math.ceil(parts.length / pageSize));
  const current = Math.min(page, pages - 1);
  return (
    <section className="instrument ledger">
      <div ref={table} className="ledger-table">
        <table>
          <thead>
            <tr>
              <th>Ref</th>
              <th>Part</th>
              <th>USD</th>
            </tr>
          </thead>
          <tbody>
            {parts.length ? (
              parts
                .slice(current * pageSize, (current + 1) * pageSize)
                .map((p) => (
                  <tr key={p.ref}>
                    <td>{p.ref}</td>
                    <td title={`${p.footprint}${p.lcsc ? " · " + p.lcsc : ""}`}>
                      <span className="part-value">{p.value}</span>
                      {p.mpn && <span className="part-mpn">{p.mpn}</span>}
                      <span className="stamps">
                        {stamps(p).map((b) => (
                          <span
                            key={b.text}
                            className={`stamp ${b.tone}`}
                            title={b.hint}
                            aria-label={`${b.text}: ${b.hint}`}
                          >
                            {b.text}
                          </span>
                        ))}
                      </span>
                    </td>
                    <td>{p.price_usd.toFixed(2)}</td>
                  </tr>
                ))
            ) : (
              <tr>
                <td colSpan={3} className="empty-row">
                  No parts
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <p className="ledger-note">
        {layerCount ? `${layerCount}-layer PCB · ` : ""}
        {sourcingDate
          ? `Prices are estimates from ${sourcingDate}`
          : "Prices are estimates"}
      </p>
      <div className="ledger-controls">
        <span>
          Est. <b>${cost.toFixed(2)}</b>
        </span>
        <div className="pagination">
          <button
            aria-label="Previous parts page"
            disabled={current === 0}
            onClick={() => setPage(current - 1)}
          >
            <ChevronLeft size={16} />
          </button>
          <span>
            {current + 1} / {pages}
          </span>
          <button
            aria-label="Next parts page"
            disabled={current === pages - 1}
            onClick={() => setPage(current + 1)}
          >
            <ChevronRight size={16} />
          </button>
        </div>
      </div>
      {canDownload ? (
        <a
          className="download-button"
          href={api(
            `/artifacts/manufacturing/gerbers.zip?project=${encodeURIComponent(projectId || "")}`,
          )}
          download
        >
          <Download size={16} />
          Gerbers (.zip)
        </a>
      ) : (
        <button
          className="download-button"
          disabled={!canExport || exporting}
          onClick={onExport}
        >
          <Download size={16} />
          {exporting ? "Exporting…" : "Export files"}
        </button>
      )}
    </section>
  );
}
