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
import { BoardFocus, Commit, DesignAdvice, DesignReadiness, DrcReport, emptyTelemetry, Simulation, Telemetry } from "./types";
import { demoSimulation, demoTelemetry } from "./demo";
import { api, wsUrl } from "./api";
import ProjectControls, { ProjectInfo } from "./components/ProjectControls";
import ExportDialog from "./components/ExportDialog";
import EnvironmentDoctor from "./components/EnvironmentDoctor";
import DrcInspector from "./components/DrcInspector";
import PowerInspector from "./components/PowerInspector";
import ReadinessChecklist from "./components/ReadinessChecklist";

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

/** One revision's build files, read straight from git; viewing it never touches workspace/. */
interface Snapshot {
  hash: string;
  pcb: string | null;
  simulation: Simulation | null;
  model: string | null;
  drc: DrcReport | null;
  design: DesignAdvice | null;
  readiness: DesignReadiness | null;
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
  const [inspectorTab, setInspectorTab] = useState<"checks" | "parts" | "power">(
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
  const [drc, setDrc] = useState<DrcReport | null>(null);
  const [designAdvice, setDesignAdvice] = useState<DesignAdvice | null>(null);
  const [readiness, setReadiness] = useState<DesignReadiness | null>(null);
  const [focus, setFocus] = useState<BoardFocus | null>(null);
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
  // Live board state keeps updating underneath while a past revision is on screen.
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const snapshotRequest = useRef(0);
  const selectedRef = useRef("latest");
  const lastRun = useRef<string>();
  const [error, setError] = useState("");
  const [demoStep, setDemoStep] = useState<number | null>(null);
  const [demoRunning, setDemoRunning] = useState(false);
  const [drcHistory, setDrcHistory] = useState<number[]>([]);
  const [unconnectedHistory, setUnconnectedHistory] = useState<number[]>([]);
  const demoTimer = useRef<ReturnType<typeof setInterval>>();
  const lastGitHead = useRef<string>();
  const isDemo = demoStep !== null;
  const telemetry = isDemo ? demoTelemetry(demoStep) : live;
  const viewing = selected !== "latest" && snapshot?.hash === selected ? snapshot : null;
  const shownPcb = viewing ? viewing.pcb : pcb;
  const shownModel = viewing ? viewing.model : model;
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
    : viewing
      ? viewing.simulation
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
    setDrc(null);
    setDesignAdvice(null);
    setReadiness(null);
    setFocus(null);
    setPcb(null);
    setModels({ glb: null, wrl: null });
    setCommits([]);
    setSelected("latest");
    selectedRef.current = "latest";
    snapshotRequest.current++;
    setSnapshot(null);
    setBusy(false);
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
        if (disposed) {
          socket.close();
          return;
        }
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
            // A new tool run means the user wants to watch it: leave history for the live board.
            // Keyed on start time, since a fast tool may only ever be seen already finished.
            const run = payload.activity?.started_at
              ? `${payload.activity.tool}@${payload.activity.started_at}`
              : undefined;
            if (
              run &&
              lastRun.current !== undefined &&
              run !== lastRun.current &&
              selectedRef.current !== "latest"
            )
              viewLatest();
            if (run) lastRun.current = run;
            if (payload.git_head !== lastGitHead.current) {
              lastGitHead.current = payload.git_head;
              void refreshHistory();
            }
          }
          if (file === "build/board.kicad_pcb") {
            setFocus(null);
            setPcb(payload);
            setExportReady(false);
            void refreshExports();
          }
          if (file.startsWith("manufacturing/")) void refreshExports();
          if (file === "build/sim_results.json") setSimulation(payload);
          if (file === "build/drc_report.json") setDrc(payload);
          if (file === "build/design.json") setDesignAdvice(payload);
          if (file === "build/readiness.json") setReadiness(payload);
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
    // Avoid opening and immediately aborting a socket during StrictMode's probe.
    const frame = requestAnimationFrame(connect);
    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      clearTimeout(timer);
      // A connecting socket closes in onopen, allowing a clean WS handshake.
      if (socket?.readyState === WebSocket.OPEN) socket.close();
    };
  }, []);
  useEffect(() => () => clearInterval(demoTimer.current), []);
  useEffect(() => {
    if (!error) return;
    const timer = setTimeout(() => setError(""), 6000);
    return () => clearTimeout(timer);
  }, [error]);
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
  function viewLatest() {
    setFocus(null);
    snapshotRequest.current++;
    selectedRef.current = "latest";
    setSelected("latest");
    setSnapshot(null);
    setBusy(false);
    setError("");
  }
  async function checkout(hash: string) {
    if (hash === "latest") return viewLatest();
    const projectId = projectRef.current?.id;
    // Only the newest request may land, however fast the slider moves.
    const request = ++snapshotRequest.current;
    const stale = () =>
      request !== snapshotRequest.current || projectId !== projectRef.current?.id;
    setBusy(true);
    setError("");
    try {
      const response = await fetch(api(`/api/git/snapshot/${hash}`));
      const info = await response.json();
      if (!response.ok) throw new Error(info.error ?? "Revision unavailable");
      const files = new Set<string>(info.files ?? []);
      const url = (file: string) =>
        files.has(file)
          ? `/artifacts/${file}?project=${encodeURIComponent(projectId ?? "")}&rev=${hash}`
          : null;
      const simUrl = url("build/sim_results.json");
      const simulation: Simulation | null = simUrl
        ? await fetch(api(simUrl))
            .then((r) => (r.ok ? r.json() : null))
            .catch(() => null)
        : null;
      const drcUrl = url("build/drc_report.json");
      const drc: DrcReport | null = drcUrl
        ? await fetch(api(drcUrl)).then(r => r.ok ? r.json() : null).catch(() => null)
        : null;
      const designUrl = url("build/design.json");
      const design: DesignAdvice | null = designUrl
        ? await fetch(api(designUrl)).then(r => r.ok ? r.json() : null).catch(() => null)
        : null;
      const readinessUrl = url("build/readiness.json");
      const readiness: DesignReadiness | null = readinessUrl
        ? await fetch(api(readinessUrl)).then(r => r.ok ? r.json() : null).catch(() => null)
        : null;
      if (stale()) return;
      selectedRef.current = hash;
      setSnapshot({
        hash,
        pcb: url("build/board.kicad_pcb"),
        simulation,
        model: url("build/board_3d.glb") ?? url("build/board_3d.wrl"),
        drc,
        design,
        readiness,
      });
      setFocus(null);
      setSelected(hash);
    } catch (e) {
      if (!stale()) setError(e instanceof Error ? e.message : "Revision unavailable");
    } finally {
      if (!stale()) setBusy(false);
    }
  }
  const drcReady = isDemo
    ? demoStep >= 1
    : typeof live.metrics.drc_errors === "number" &&
      ["completed", "failed"].includes(live.stages.DRC_VERIFICATION ?? "");
  const gaugeDrc = viewing ? viewing.drc?.drc_errors ?? null : telemetry.metrics.drc_errors;
  const gaugeUnconnected = viewing ? viewing.drc?.unconnected_items ?? null : telemetry.metrics.unconnected_items;
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
            <EnvironmentDoctor />
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
                pcbUrl={shownPcb}
                modelUrl={shownModel}
                demo={isDemo}
                commits={commits}
                selected={selected}
                busy={busy}
                focus={focus}
                onClearFocus={() => setFocus(null)}
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
                  {["Simulation", "Checks", "Parts", "Power"].map((label, i) => (
                    <button
                      key={label}
                      className={
                        (
                          activeInstrument === 0
                            ? i === 0
                            : i === (inspectorTab === "checks" ? 1 : inspectorTab === "parts" ? 2 : 3)
                        )
                          ? "active"
                          : ""
                      }
                      onClick={() => {
                        setActiveInstrument(i === 0 ? 0 : 1);
                        if (i) setInspectorTab(i === 1 ? "checks" : i === 2 ? "parts" : "power");
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
                    <button role="tab" aria-selected={inspectorTab === "power"} onClick={() => setInspectorTab("power")}>Power</button>
                  </div>
                  {inspectorTab === "checks" ? (
                    <section className="instrument verification">
                      <div className="gauges">
                        <PressureGauge
                          label="DRC errors"
                          value={gaugeDrc}
                          history={
                            isDemo
                              ? [4, 1, ...(demoStep >= 5 ? [0] : [])]
                              : viewing ? [] : drcHistory
                          }
                          ready={viewing ? viewing.drc?.verified === true : drcReady}
                        />
                        <PressureGauge
                          label="Unconnected"
                          value={gaugeUnconnected}
                          history={
                            isDemo
                              ? [12, ...(demoStep >= 4 ? [0] : [])]
                              : viewing ? [] : unconnectedHistory
                          }
                          ready={
                            viewing ? viewing.drc?.verified === true : isDemo
                              ? drcReady
                              : typeof live.metrics.unconnected_items ===
                                "number"
                          }
                        />
                      </div>
                      {!isDemo && <DrcInspector report={viewing ? viewing.drc : drc} pcbUrl={shownPcb} onFocus={(point) => { setFocus(point); setActivePane(1); }} />}
                      {!isDemo && <ReadinessChecklist readiness={viewing ? viewing.readiness : readiness} pcbUrl={shownPcb} design={viewing ? viewing.design : designAdvice} />}
                    </section>
                  ) : inspectorTab === "power" ? (
                    <PowerInspector design={isDemo ? null : viewing ? viewing.design : designAdvice} />
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
