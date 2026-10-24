import { useEffect, useRef, useState } from "react";
import { Download, FileArchive, X } from "lucide-react";
import { api } from "../api";
import { ProjectInfo } from "./ProjectControls";
import type { DesignReadiness } from "../types";
import ReadinessChecklist from "./ReadinessChecklist";
interface ExportInfo {
  projectId: string;
  hasBoard: boolean;
  ready: boolean;
  files: { name: string; label: string; url: string; size: number }[];
  report?: { summary?: string; warnings?: string[] };
  readiness?: DesignReadiness | null;
}
export default function ExportDialog({
  project,
  disabled,
  onBusy,
  request = 0,
  onReady,
}: {
  project: ProjectInfo | null;
  disabled?: boolean;
  onBusy: (busy: boolean) => void;
  request?: number;
  onReady: (ready: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const [info, setInfo] = useState<ExportInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const currentProject = useRef(project?.id);
  currentProject.current = project?.id;
  useEffect(() => {
    setOpen(false);
    setInfo(null);
    setError("");
    setBusy(false);
  }, [project?.id]);
  useEffect(() => {
    if (request > 0) setOpen(true);
  }, [request]);
  useEffect(() => {
    if (!open || !project) return;
    let active = true;
    void fetch(api(`/api/exports?project=${encodeURIComponent(project.id)}`))
      .then(async (response) => {
        const data = await response.json();
        if (!response.ok)
          throw new Error(data.error || "Export information unavailable");
        return data;
      })
      .then((data) => {
        if (active) setInfo(data);
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [open, project?.id]);
  async function generate() {
    if (!project) return;
    const id = project.id;
    setBusy(true);
    onBusy(true);
    setError("");
    try {
      const response = await fetch(api("/api/exports"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ projectId: project.id }),
      });
      const result = await response.json();
      if (currentProject.current !== id) return;
      setInfo(result);
      onReady(result.ready === true);
      if (!response.ok)
        throw new Error(
          result.error || result.report?.summary || "Export failed",
        );
    } catch (e) {
      if (currentProject.current === id)
        setError(e instanceof Error ? e.message : "Export failed");
    } finally {
      if (currentProject.current === id) {
        setBusy(false);
        onBusy(false);
      }
    }
  }
  return (
    <>
      <button
        className="project-action"
        disabled={disabled || !project}
        onClick={() => setOpen(true)}
      >
        <FileArchive size={15} />
        Export
      </button>
      {open && (
        <div className="project-backdrop" onClick={() => setOpen(false)}>
          <section
            className="project-dialog export-dialog"
            role="dialog"
            aria-modal="true"
            aria-label="Export manufacturing files"
            onClick={(e) => e.stopPropagation()}
          >
            <header>
              <h2>Export · {project?.name}</h2>
              <button
                aria-label="Close export dialog"
                onClick={() => setOpen(false)}
              >
                <X size={18} />
              </button>
            </header>
            <p>
              {busy
                ? "Checking DRC and generating files…"
                : info?.ready
                  ? "Files match the current board."
                  : info?.hasBoard
                    ? "Generate Gerbers, drill, BOM and JLCPCB placement files. Native DRC must pass first."
                    : "Create a board before exporting."}
            </p>
            <ReadinessChecklist readiness={info?.readiness ?? null} />
            {info?.ready && (
              <div className="export-files">
                {info.files.map((file) => (
                  <a key={file.name} href={api(file.url)} download>
                    <Download size={17} />
                    <span>{file.label}</span>
                    <small>{Math.ceil(file.size / 1024)} KB</small>
                  </a>
                ))}
              </div>
            )}
            {error && (
              <p className="project-error" role="alert">
                {error}
              </p>
            )}
            <footer>
              <button onClick={() => setOpen(false)}>Close</button>
              <button
                className="primary"
                disabled={busy || !info?.hasBoard}
                onClick={() => void generate()}
              >
                {busy
                  ? "Exporting…"
                  : info?.ready
                    ? "Regenerate files"
                    : "Generate files"}
              </button>
            </footer>
          </section>
        </div>
      )}
    </>
  );
}
