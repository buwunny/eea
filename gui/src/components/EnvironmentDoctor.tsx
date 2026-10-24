import { useEffect, useRef, useState } from "react";
import { Stethoscope, X } from "lucide-react";
import { api } from "../api";

interface Check { name: string; available: boolean; version?: string; fallback?: string; detail?: string }
interface Report { python: string; tools: Check[]; modules: Check[]; freerouting_jar: boolean; capabilities: Record<string, boolean>; summary: string }

export default function EnvironmentDoctor() {
  const [open, setOpen] = useState(false);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState("");
  const [checking, setChecking] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const request = useRef(0);
  async function inspect() {
    const id = ++request.current;
    setChecking(true); setError(""); setReport(null);
    try {
      const response = await fetch(api("/api/environment"));
      const value = await response.json();
      if (!response.ok) throw new Error(value.error || "Environment check failed");
      if (id === request.current) setReport(value);
    } catch (error) {
      if (id === request.current) setError(error instanceof Error ? error.message : "Environment check failed");
    } finally { if (id === request.current) setChecking(false); }
  }
  useEffect(() => {
    if (open) { dialog.current?.showModal(); void inspect(); }
    return () => { request.current++; };
  }, [open]);
  return <>
    <button className="project-action" onClick={() => setOpen(true)} title="Check installed engineering tools"><Stethoscope size={15} /> Tools</button>
    {open && <dialog ref={dialog} className="project-dialog environment-dialog" onClose={() => setOpen(false)}>
      <header><h2>Environment doctor</h2><button aria-label="Close environment doctor" onClick={() => dialog.current?.close()}><X size={18} /></button></header>
      <div className="diagnostic-scroll" aria-busy={checking}>
        {checking && <p role="status">Checking engineering tools…</p>}
        {error && <p className="project-error" role="alert">{error}</p>}
        {report && <>
          <p>Python {report.python} · {report.summary}</p>
          <ul className="doctor-checks">{[...report.tools, ...report.modules, { name: "Freerouting JAR", available: report.freerouting_jar }].map(check => <li key={check.name}>
            <div><b>{check.name}</b><span className={check.available ? "green" : "warning"}>{check.available ? "Available" : "Missing"}</span></div>
            {check.version && <small>{check.version}</small>}
            {!check.available && check.fallback && <small>{check.fallback}</small>}
            {check.detail && <small>{check.detail}</small>}
          </li>)}</ul>
        </>}
      </div>
      <footer><button onClick={() => dialog.current?.close()}>Close</button><button disabled={checking} onClick={() => void inspect()}>Check again</button></footer>
    </dialog>}
  </>;
}
