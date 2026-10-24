import { api } from "../api";
import { Download } from "lucide-react";
import { Part } from "../types";
export default function LedgerBOM({
  parts,
  cost,
  canDownload,
}: {
  parts: Part[];
  cost: number;
  canDownload: boolean;
}) {
  return (
    <section className="instrument ledger">
      <div className="panel-heading">
        <span>04 / BILL OF MATERIALS</span>
        <span className="tiny">{parts.length} ITEMS</span>
      </div>
      <div className="ledger-table">
        <table>
          <thead>
            <tr>
              <th>REF.</th>
              <th>COMPONENT</th>
              <th>USD</th>
            </tr>
          </thead>
          <tbody>
            {parts.length ? (
              parts.map((p) => (
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
                  No components on the bench
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="ledger-total">
        <span>ESTIMATED ASSEMBLY</span>
        <b>${cost.toFixed(2)}</b>
      </div>
      {canDownload ? (
        <a
          className="download-button"
          href={api("/artifacts/gerbers.zip")}
          download
        >
          <Download size={14} /> DOWNLOAD JLCPCB GERBERS
        </a>
      ) : (
        <button className="download-button" disabled>
          <Download size={14} /> GERBERS AWAITING EXPORT
        </button>
      )}
    </section>
  );
}
