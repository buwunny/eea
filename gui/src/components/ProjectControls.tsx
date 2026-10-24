import { useEffect, useState } from "react";
import {
  FolderOpen,
  FolderPlus,
  ChevronLeft,
  ChevronRight,
  Folder,
  ArrowUp,
  X,
} from "lucide-react";
import { api } from "../api";
import "./projects.css";
export interface ProjectInfo {
  id: string;
  name: string;
  path: string;
}
interface FolderList {
  path: string;
  parent: string | null;
  folders: { name: string; path: string }[];
}
export default function ProjectControls({
  project,
  onSelected,
  disabled,
}: {
  project: ProjectInfo | null;
  onSelected: (project: ProjectInfo) => void;
  disabled?: boolean;
}) {
  const [mode, setMode] = useState<"new" | "open" | null>(null);
  const [name, setName] = useState("");
  const [directory, setDirectory] = useState("");
  const [recent, setRecent] = useState<{ name: string; path: string }[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [folders, setFolders] = useState<FolderList | null>(null);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(window.innerHeight < 650 ? 2 : 4);
  useEffect(() => {
    const resize = () => setPageSize(window.innerHeight < 650 ? 2 : 4);
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);
  useEffect(() => {
    if (!mode) return;
    let active = true;
    setError("");
    setFolders(null);
    setPage(0);
    setName("");
    void fetch(api("/api/projects"))
      .then(async (response) => {
        if (!response.ok)
          throw new Error(
            "Project service unavailable. Restart the EEA bridge after updating.",
          );
        return response.json();
      })
      .then((data) => {
        if (!active) return;
        setRecent(data.recent || []);
        setDirectory(mode === "new" ? data.defaultDirectory : data.active.path);
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [mode]);
  async function browse(path = directory) {
    setError("");
    try {
      const response = await fetch(
        api(`/api/projects/directories?path=${encodeURIComponent(path)}`),
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.error);
      setFolders(data);
      setDirectory(data.path);
      setPage(0);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Cannot browse folder");
    }
  }
  async function submit(path = directory) {
    setBusy(true);
    setError("");
    try {
      const response = await fetch(
        api(`/api/projects/${mode === "new" ? "new" : "open"}`),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(
            mode === "new"
              ? { name, parentPath: path, projectId: project?.id }
              : { path, projectId: project?.id },
          ),
        },
      );
      const data = await response.json();
      if (!response.ok)
        throw new Error(data.error || "Project could not be opened");
      onSelected(data.project);
      setMode(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Project operation failed");
    } finally {
      setBusy(false);
    }
  }
  const list = folders?.folders ?? recent;
  const pages = Math.max(1, Math.ceil(list.length / pageSize));
  const current = Math.min(page, pages - 1);
  return (
    <>
      <button
        className="project-action"
        disabled={disabled}
        onClick={() => setMode("new")}
      >
        <FolderPlus size={15} />
        New
      </button>
      <button
        className="project-action"
        disabled={disabled}
        onClick={() => setMode("open")}
      >
        <FolderOpen size={15} />
        Open
      </button>
      {mode && (
        <div
          className="project-backdrop"
          onClick={() => {
            if (!busy) setMode(null);
          }}
        >
          <section
            className="project-dialog"
            role="dialog"
            aria-modal="true"
            aria-label={mode === "new" ? "New project" : "Open project"}
            onClick={(e) => e.stopPropagation()}
          >
            <header>
              <h2>
                {folders
                  ? "Choose folder"
                  : mode === "new"
                    ? "New project"
                    : "Open project"}
              </h2>
              <button
                aria-label="Close project dialog"
                disabled={busy}
                onClick={() => setMode(null)}
              >
                <X size={18} />
              </button>
            </header>
            {!folders && mode === "new" && (
              <label>
                Project name
                <input
                  autoFocus
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="555 flasher"
                />
              </label>
            )}
            <label>
              {folders
                ? "Folder"
                : mode === "new"
                  ? "Parent folder"
                  : "Project folder"}
              <div className="project-path-field">
                <input
                  aria-label="Project folder path"
                  value={directory}
                  onChange={(e) => setDirectory(e.target.value)}
                  placeholder="/path/to/project"
                />
                <button disabled={busy} onClick={() => void browse()}>
                  {folders ? "Go" : "Browse"}
                </button>
              </div>
            </label>
            {folders ? (
              <>
                <div className="folder-navigation">
                  <button
                    disabled={!folders.parent}
                    onClick={() => void browse(folders.parent!)}
                  >
                    <ArrowUp size={14} />
                    Parent
                  </button>
                  <span>{list.length} folders</span>
                </div>
                <div className="project-list">
                  {list
                    .slice(current * pageSize, current * pageSize + pageSize)
                    .map((item) => (
                      <button
                        key={item.path}
                        onClick={() => void browse(item.path)}
                      >
                        <Folder size={16} />
                        <span>{item.name}</span>
                        <ChevronRight size={15} />
                      </button>
                    ))}
                  {!list.length && <p>No subfolders</p>}
                </div>
              </>
            ) : (
              mode === "open" && (
                <>
                  <div className="project-list-label">Recent projects</div>
                  <div className="project-list">
                    {recent
                      .slice(current * pageSize, current * pageSize + pageSize)
                      .map((item) => (
                        <button
                          key={item.path}
                          disabled={busy || item.path === project?.path}
                          title={item.path}
                          onClick={() => void submit(item.path)}
                        >
                          <FolderOpen size={16} />
                          <span>
                            <b>{item.name}</b>
                            <small>{item.path}</small>
                          </span>
                          <ChevronRight size={15} />
                        </button>
                      ))}
                    {!recent.length && (
                      <p>Choose an EEA project folder above.</p>
                    )}
                  </div>
                </>
              )
            )}
            {(folders || mode === "open") && pages > 1 && (
              <div className="project-pagination">
                <button
                  aria-label="Previous folders"
                  disabled={current === 0}
                  onClick={() => setPage(current - 1)}
                >
                  <ChevronLeft size={14} />
                </button>
                <span>
                  {current + 1} / {pages}
                </span>
                <button
                  aria-label="Next folders"
                  disabled={current === pages - 1}
                  onClick={() => setPage(current + 1)}
                >
                  <ChevronRight size={14} />
                </button>
              </div>
            )}
            {error && (
              <p className="project-error" role="alert">
                {error}
              </p>
            )}
            {!folders && (
              <p className="project-notice">
                Switching ends the current terminal session. Project files are
                kept.
              </p>
            )}
            <footer>
              <button
                disabled={busy}
                onClick={() => (folders ? setFolders(null) : setMode(null))}
              >
                {folders ? "Back" : "Cancel"}
              </button>
              <button
                className="primary"
                disabled={
                  busy ||
                  !directory ||
                  (!folders && mode === "new" && !name.trim())
                }
                onClick={() => (folders ? setFolders(null) : void submit())}
              >
                {busy
                  ? "Opening…"
                  : folders
                    ? "Use this folder"
                    : mode === "new"
                      ? "Create project"
                      : "Open project"}
              </button>
            </footer>
          </section>
        </div>
      )}
    </>
  );
}
