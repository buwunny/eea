import { useEffect, useRef, useState } from "react";
import { Activity, FlaskConical, Radio, Square } from "lucide-react";
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

export default function App() {
  const [live, setLive] = useState<Telemetry>(emptyTelemetry);
  const [simulation, setSimulation] = useState<Simulation | null>(null);
  const [pcb, setPcb] = useState<string | null>(null);
  const [model, setModel] = useState<string | null>(null);
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
    : simulation;
  const refreshHistory = async () => {
    try {
      const response = await fetch(api("/api/git/log"));
      if (response.ok) {
        const data = await response.json();
        setCommits(data.commits ?? []);
      }
    } catch {
      /* Reconnect will refresh history. */
    }
  };
  useEffect(() => {
    let disposed = false;
    let socket: WebSocket;
    let timer: ReturnType<typeof setTimeout>;
    function connect() {
      socket = new WebSocket(wsUrl("/ws/telemetry"));
      socket.onopen = () => {
        setConnected(true);
        void refreshHistory();
      };
      socket.onmessage = (e) => {
        try {
          const message = JSON.parse(e.data);
          if (message.type !== "FILE_UPDATED") return;
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
                [...h, payload.metrics.drc_errors].slice(-8),
              );
            if (typeof payload.metrics?.unconnected_items === "number")
              setUnconnectedHistory((h) =>
                [...h, payload.metrics.unconnected_items].slice(-8),
              );
            void refreshHistory();
          }
          if (file === "build/board.kicad_pcb") setPcb(payload);
          if (file === "build/sim_results.json") setSimulation(payload);
          if (file === "build/board_3d.glb" || file === "build/board_3d.wrl")
            setModel(payload);
        } catch {
          /* Ignore incomplete files; next atomic update replaces them. */
        }
      };
      socket.onclose = () => {
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
            <div className="eyebrow">
              <span className="ornament">✦</span> THE INTELLIGENT CIRCUIT
              WORKBENCH
            </div>
            <h1>{telemetry.project_name}</h1>
          </div>
          <div className="connection">
            <Radio size={14} />
            <span className={connected ? "green" : ""}>
              {connected ? "LOCAL BRIDGE ONLINE" : "LOCAL BRIDGE OFFLINE"}
            </span>
          </div>
        </div>
        <div className="workbench">
          <TelegraphTerm />
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
                <b>
                  {telemetry.metrics.component_count
                    .toString()
                    .padStart(2, "0")}
                </b>
              </div>
              <div>
                <small>CONNECTED NETS</small>
                <b>{telemetry.metrics.net_count.toString().padStart(2, "0")}</b>
              </div>
              <div>
                <small>ESTIMATED COST</small>
                <b>${telemetry.metrics.estimated_cost_usd.toFixed(2)}</b>
              </div>
              <div>
                <small>INSTRUMENT MODE</small>
                <b className="mode-readout">{isDemo ? "DEMO" : "LIVE"}</b>
              </div>
            </div>
            <div className="bench-note">
              <Activity size={14} />
              <span>
                {error ||
                  (isDemo
                    ? "Demonstration sequence · simulated readings and illustrative geometry."
                    : telemetry.metrics.sim_summary ||
                      "Artifacts arrive through the filesystem. Your invention remains yours.")}
              </span>
            </div>
          </div>
          <aside>
            <NixieStepper telemetry={telemetry} />
            <CathodeRay
              simulation={sim}
              passed={telemetry.metrics.sim_passed}
            />
            <section className="instrument verification">
              <div className="panel-heading">
                <span>03 / VERIFICATION PRESSURE</span>
              </div>
              <div className="gauges">
                <PressureGauge
                  label="DRC VIOLATIONS"
                  value={telemetry.metrics.drc_errors}
                  history={
                    isDemo ? [4, 1, ...(demoStep >= 5 ? [0] : [])] : drcHistory
                  }
                  ready={drcReady}
                />
                <PressureGauge
                  label="UNCONNECTED NETS"
                  value={telemetry.metrics.unconnected_items}
                  history={
                    isDemo
                      ? [12, ...(demoStep >= 4 ? [0] : [])]
                      : unconnectedHistory
                  }
                  ready={drcReady}
                />
              </div>
            </section>
            <LedgerBOM
              parts={telemetry.bom}
              cost={telemetry.metrics.estimated_cost_usd}
              canDownload={!isDemo && live.stages.MANUFACTURING === "completed"}
            />
          </aside>
        </div>
      </main>
      <footer className="forge-footer">
        <div>
          <span className={`status-dot ${connected ? "online" : ""}`} />
          <span>
            {isDemo
              ? "DEMONSTRATION · NO WORKSPACE CHANGES"
              : "FILESYSTEM TELEMETRY · INTERACTIVE SHELL"}
          </span>
        </div>
        <div className="demo-controls">
          {isDemo && (
            <button className="exit-demo" onClick={endDemo}>
              <Square size={10} /> RETURN TO LIVE
            </button>
          )}
          <button
            className="demo-button"
            onClick={startDemo}
            disabled={demoRunning}
          >
            <FlaskConical size={14} />
            {demoRunning ? "INVENTION IN PROGRESS…" : "SIMULATE DEMO FLOW"}
          </button>
        </div>
      </footer>
    </div>
  );
}
