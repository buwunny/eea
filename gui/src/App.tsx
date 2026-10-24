import { useEffect, useRef, useState } from "react";
import { FlaskConical, Radio, Square } from "lucide-react";
import SplitView from "./components/SplitView";
import ToolActivity from "./components/ToolActivity";
import BrassTitlebar from "./components/BrassTitlebar";
import TelegraphTerm from "./components/TelegraphTerm";
import AetherScope from "./components/AetherScope";
import CathodeRay from "./components/CathodeRay";
import NixieStepper from "./components/NixieStepper";
import PressureGauge from "./components/PressureGauge";
import LedgerBOM from "./components/LedgerBOM";
import { Commit, emptyTelemetry, Simulation, Telemetry } from "./types";
import { demoSimulation, demoTelemetry } from "./demo";
import { api, wsUrl } from "./api";
import ProjectControls, { ProjectInfo } from "./components/ProjectControls";
import ExportDialog from "./components/ExportDialog";

/** Tally-counter readout: each digit sits in its own number wheel. */
function Counter({ text }: { text: string }) {
  return (
    <b aria-label={text}>
      {[...text].map((c, i) =>
        /\d/.test(c) ? (
          <span className="wheel" key={i} aria-hidden>
            {c}
          </span>
        ) : (
          <span key={i} aria-hidden>
            {c}
          </span>
        ),
      )}
    </b>
  );
}

export default function App() {
  const [project, setProject] = useState<ProjectInfo | null>(null);
  const projectRef = useRef<ProjectInfo | null>(null);
  const [exportBusy, setExportBusy] = useState(false);
  const [exportReady, setExportReady] = useState(false);
  const [exportRequest, setExportRequest] = useState(0);
  const [compact, setCompact] = useState(window.innerWidth < 1050);
  const [shortWindow, setShortWindow] = useState(window.innerHeight < 740);
  const [activeInstrument, setActiveInstrument] = useState(0);
  const [activePane, setActivePane] = useState(1);
  const [inspectorTab, setInspectorTab] = useState<"checks" | "parts">(
    "checks",
  );
  useEffect(() => {
    const query = matchMedia("(max-width: 1049px)");
    const heightQuery = matchMedia("(max-height: 739px)");
    const change = () => setCompact(query.matches);
    const heightChange = () => setShortWindow(heightQuery.matches);
    query.addEventListener("change", change);
    heightQuery.addEventListener("change", heightChange);
    return () => {
      query.removeEventListener("change", change);
      heightQuery.removeEventListener("change", heightChange);
    };
  }, []);
  const [live, setLive] = useState<Telemetry>(emptyTelemetry);
  const [simulation, setSimulation] = useState<Simulation | null>(null);
  const [pcb, setPcb] = useState<string | null>(null);
  const [models, setModels] = useState<{
    glb: string | null;
    wrl: string | null;
  }>({ glb: null, wrl: null });
  const model = models.glb ?? models.wrl;
  const [connected, setConnected] = useState(false);
  const [commits, setCommits] = useState<Commit[]>([]);
  const [selected, setSelected] = useState("latest");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [demoStep, setDemoStep] = useState<number | null>(null);
  const [demoRunning, setDemoRunning] = useState(false);
  const [drcHistory, setDrcHistory] = useState<number[]>([]);
  const [unconnectedHistory, setUnconnectedHistory] = useState<number[]>([]);
  const demoTimer = useRef<ReturnType<typeof setInterval>>();
  const lastGitHead = useRef<string>();
  const isDemo = demoStep !== null;
  const telemetry = isDemo ? demoTelemetry(demoStep) : live;
  const sim = isDemo
    ? demoStep >= 2
      ? {
          ...demoSimulation,
          measurements: {
            ...demoSimulation.measurements,
            frequency_hz: demoStep === 2 ? 20 : 2,
          },
        }
      : null
    : live.stages.SIMULATION === "pending"
      ? null
      : simulation;
  const refreshHistory = async () => {
    try {
      const response = await fetch(api("/api/git/log"));
      if (response.ok) {
        const data = await response.json();
        if (data.projectId && data.projectId !== projectRef.current?.id) return;
        setCommits(data.commits ?? []);
      }
    } catch {
      /* Reconnect will refresh history. */
    }
  };
  const refreshExports = async () => {
    const id = projectRef.current?.id;
    if (!id) return;
    try {
      const response = await fetch(
        api(`/api/exports?project=${encodeURIComponent(id)}`),
      );
      const info = await response.json();
      if (response.ok && id === projectRef.current?.id)
        setExportReady(info.ready === true);
    } catch {
      /* Next file update retries. */
    }
  };
  function selectProject(next: ProjectInfo) {
    if (projectRef.current?.id === next.id) return;
    projectRef.current = next;
    setProject(next);
    setLive(emptyTelemetry);
    setSimulation(null);
    setPcb(null);
    setModels({ glb: null, wrl: null });
    setCommits([]);
    setSelected("latest");
    setDrcHistory([]);
    setUnconnectedHistory([]);
    setError("");
    setExportReady(false);
    setExportBusy(false);
    lastGitHead.current = undefined;
    clearInterval(demoTimer.current);
    setDemoStep(null);
    setDemoRunning(false);
    void refreshHistory();
    void refreshExports();
  }
  useEffect(() => {
    let disposed = false;
    let socket: WebSocket;
    let timer: ReturnType<typeof setTimeout>;
    function connect() {
      socket = new WebSocket(wsUrl("/ws/telemetry"));
      socket.onopen = () => {
        if (disposed) return;
        setConnected(true);
        void refreshHistory();
      };
      socket.onmessage = (e) => {
        if (disposed) return;
        try {
          const message = JSON.parse(e.data);
          if (message.type === "PROJECT_CHANGED") {
            selectProject(message.project);
            return;
          }
          if (message.type !== "FILE_UPDATED") return;
          if (message.projectId && message.projectId !== projectRef.current?.id)
            return;
          const { file, payload } = message;
          if (file === "telemetry.json" && payload) {
            setLive({
              ...emptyTelemetry,
              ...payload,
              metrics: { ...emptyTelemetry.metrics, ...payload.metrics },
              stages: payload.stages ?? {},
              bom: payload.bom ?? [],
              git_commits: payload.git_commits ?? [],
            });
            if (typeof payload.metrics?.drc_errors === "number")
              setDrcHistory((h) =>
                h.at(-1) === payload.metrics.drc_errors
                  ? h
                  : [...h, payload.metrics.drc_errors].slice(-8),
              );
            if (typeof payload.metrics?.unconnected_items === "number")
              setUnconnectedHistory((h) =>
                h.at(-1) === payload.metrics.unconnected_items
                  ? h
                  : [...h, payload.metrics.unconnected_items].slice(-8),
              );
            if (payload.git_head !== lastGitHead.current) {
              lastGitHead.current = payload.git_head;
              void refreshHistory();
            }
          }
          if (file === "build/board.kicad_pcb") {
            setPcb(payload);
            setExportReady(false);
            void refreshExports();
          }
          if (file.startsWith("manufacturing/")) void refreshExports();
          if (file === "build/sim_results.json") setSimulation(payload);
          if (file === "build/board_3d.glb")
            setModels((current) => ({ ...current, glb: payload }));
          if (file === "build/board_3d.wrl")
            setModels((current) => ({ ...current, wrl: payload }));
        } catch {
          /* Ignore incomplete files; next atomic update replaces them. */
        }
      };
      socket.onclose = () => {
        if (disposed) return;
        setConnected(false);
        if (!disposed) timer = setTimeout(connect, 2500);
      };
      socket.onerror = () => socket.close();
    }
    connect();
    return () => {
      disposed = true;
      clearTimeout(timer);
      socket?.close();
    };
  }, []);
  useEffect(() => () => clearInterval(demoTimer.current), []);
  function startDemo() {
    clearInterval(demoTimer.current);
    setDemoStep(0);
    setDemoRunning(true);
    let step = 0;
    demoTimer.current = setInterval(() => {
      step++;
      setDemoStep(step);
      if (step >= 6) {
        clearInterval(demoTimer.current);
        setDemoRunning(false);
      }
    }, 1900);
  }
  function endDemo() {
    clearInterval(demoTimer.current);
    setDemoStep(null);
    setDemoRunning(false);
  }
  async function checkout(hash: string) {
    setBusy(true);
    setError("");
    try {
      const response = await fetch(api(`/api/git/checkout/${hash}`), {
        method: "POST",
      });
      const result = await response.json();
      if (!response.ok)
        throw new Error(result.error ?? "Revision could not be restored");
      setSelected(hash);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Revision unavailable");
    } finally {
      setBusy(false);
    }
  }
  const drcReady = isDemo
    ? demoStep >= 1
    : typeof live.metrics.drc_errors === "number" &&
      ["completed", "failed"].includes(live.stages.DRC_VERIFICATION ?? "");
  return (
    <div className="forge">
      <BrassTitlebar />
      <main>
        <div className="workbench-heading">
          <div>
            <h1 title={project?.path}>
              {["The Aether-Forge", "EEA"].includes(telemetry.project_name)
                ? project?.name || "Untitled project"
                : telemetry.project_name}
            </h1>
          </div>
          <div className="project-actions">
            <ProjectControls
              project={project}
              onSelected={selectProject}
              disabled={exportBusy || !connected}
            />
            <ExportDialog
              project={project}
              disabled={isDemo || !connected}
              onBusy={setExportBusy}
              request={exportRequest}
              onReady={setExportReady}
            />
          </div>
          <div className="connection">
            <Radio size={14} />
            <span className={connected ? "green" : ""}>
              {connected ? "Bridge connected" : "Bridge disconnected"}
            </span>
          </div>
        </div>
        <ToolActivity
          telemetry={telemetry}
          connected={connected}
          demo={isDemo}
        />
        <nav className="pane-tabs" aria-label="Workspace panels">
          {["Terminal", "Board", "Instruments"].map((label, i) => (
            <button
              key={label}
              className={activePane === i ? "active" : ""}
              onClick={() => setActivePane(i)}
            >
              {label}
            </button>
          ))}
        </nav>
        <div className="workbench">
          <SplitView
            storageKey="eea-columns"
            initial={[27, 46, 27]}
            compactIndex={compact ? activePane : undefined}
          >
            <TelegraphTerm
              key={project?.id || "initial"}
              workspacePath={project?.path}
            />
            <div className="center-column">
              <AetherScope
                pcbUrl={pcb}
                modelUrl={model}
                demo={isDemo}
                commits={commits}
                selected={selected}
                busy={busy}
                onCheckout={(hash) => void checkout(hash)}
              />
              <div className="board-metrics">
                <div>
                  <small>COMPONENTS</small>
                  <Counter
                    text={telemetry.metrics.component_count
                      .toString()
                      .padStart(2, "0")}
                  />
                </div>
                <div>
                  <small>NETS</small>
                  <Counter
                    text={telemetry.metrics.net_count
                      .toString()
                      .padStart(2, "0")}
                  />
                </div>
                <div>
                  <small>EST. COST</small>
                  <Counter
                    text={`$${telemetry.metrics.estimated_cost_usd.toFixed(2)}`}
                  />
                </div>
              </div>
              {error && (
                <div className="bench-note" role="alert">
                  {error}
                </div>
              )}
            </div>
            <aside>
              <NixieStepper telemetry={telemetry} />
              {shortWindow && (
                <nav className="instrument-nav" aria-label="Instruments">
                  {["Simulation", "Checks", "Parts"].map((label, i) => (
                    <button
                      key={label}
                      className={
                        (
                          activeInstrument === 0
                            ? i === 0
                            : i === (inspectorTab === "checks" ? 1 : 2)
                        )
                          ? "active"
                          : ""
                      }
                      onClick={() => {
                        setActiveInstrument(i === 0 ? 0 : 1);
                        if (i) setInspectorTab(i === 1 ? "checks" : "parts");
                      }}
                    >
                      {label}
                    </button>
                  ))}
                </nav>
              )}
              <SplitView
                direction="vertical"
                storageKey="eea-instruments"
                initial={[52, 48]}
                compactIndex={shortWindow ? activeInstrument : undefined}
              >
                <CathodeRay
                  simulation={sim}
                  passed={telemetry.metrics.sim_passed}
                />
                <div className="inspector">
                  <div
                    className={`inspector-tabs ${shortWindow ? "hidden-tabs" : ""}`}
                    role="tablist"
                    aria-label="Inspector"
                  >
                    <button
                      role="tab"
                      aria-selected={inspectorTab === "checks"}
                      onClick={() => setInspectorTab("checks")}
                    >
                      Checks
                    </button>
                    <button
                      role="tab"
                      aria-selected={inspectorTab === "parts"}
                      onClick={() => setInspectorTab("parts")}
                    >
                      Parts ({telemetry.bom.length})
                    </button>
                  </div>
                  {inspectorTab === "checks" ? (
                    <section className="instrument verification">
                      <div className="gauges">
                        <PressureGauge
                          label="DRC errors"
                          value={telemetry.metrics.drc_errors}
                          history={
                            isDemo
                              ? [4, 1, ...(demoStep >= 5 ? [0] : [])]
                              : drcHistory
                          }
                          ready={drcReady}
                        />
                        <PressureGauge
                          label="Unconnected"
                          value={telemetry.metrics.unconnected_items}
                          history={
                            isDemo
                              ? [12, ...(demoStep >= 4 ? [0] : [])]
                              : unconnectedHistory
                          }
                          ready={
                            isDemo
                              ? drcReady
                              : typeof live.metrics.unconnected_items ===
                                "number"
                          }
                        />
                      </div>
                    </section>
                  ) : (
                    <LedgerBOM
                      parts={telemetry.bom}
                      cost={telemetry.metrics.estimated_cost_usd}
                      canDownload={!isDemo && exportReady}
                      projectId={project?.id}
                      canExport={!isDemo && !!pcb && !!project && connected}
                      exporting={exportBusy}
                      onExport={() => setExportRequest((value) => value + 1)}
                    />
                  )}
                </div>
              </SplitView>
            </aside>
          </SplitView>
        </div>
      </main>
      <footer className="forge-footer">
        <div>
          <span className={`status-dot ${connected ? "online" : ""}`} />
          <span>{isDemo ? "Demo · sample data" : "EEA"}</span>
        </div>
        <div className="demo-controls">
          {isDemo && (
            <button className="exit-demo" onClick={endDemo}>
              <Square size={10} /> Exit demo
            </button>
          )}
          <button
            className="demo-button"
            onClick={startDemo}
            disabled={demoRunning}
          >
            <FlaskConical size={14} />
            {demoRunning ? "Running demo…" : "Run demo"}
          </button>
        </div>
      </footer>
    </div>
  );
}
