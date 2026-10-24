import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { ChevronLeft, ChevronRight, Download } from "lucide-react";
import { Part } from "../types";
export default function LedgerBOM({
  parts,
  cost,
  canDownload,
  projectId,
  canExport,
  exporting,
  onExport,
}: {
  parts: Part[];
  cost: number;
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
        Math.max(1, Math.floor((entry.contentRect.height - 34) / 32)),
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
                      {p.value}
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
