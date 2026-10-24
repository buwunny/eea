import { lazy, Suspense, useEffect, useState, useRef } from "react";
import type { Design } from "./Board3D";
import {
  Box,
  Layers,
  History,
  Maximize2,
  FlipHorizontal2,
  Download,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";
import { BoardFocus, Commit } from "../types";
import { artifact } from "../api";
import KiCanvasBoard, { BoardControls } from "./KiCanvasBoard";

const Board3D = lazy(() => import("./Board3D"));

declare global {
  namespace JSX {
    interface IntrinsicElements {
      "kicanvas-embed": React.DetailedHTMLProps<
        React.HTMLAttributes<HTMLElement>,
        HTMLElement
      > & { src: string; controls: string; controlslist?: string };
    }
  }
}
function DemoPlan() {
  return (
    <svg
      viewBox="0 0 540 410"
      className="demo-board"
      role="img"
      aria-label="Illustrative 555 demonstration board"
    >
      <defs>
        <pattern
          id="pcb-grid"
          width="20"
          height="20"
          patternUnits="userSpaceOnUse"
        >
          <circle cx="1" cy="1" r=".8" fill="#6a936b" opacity=".24" />
        </pattern>
      </defs>
      <rect
        x="26"
        y="29"
        width="488"
        height="345"
        rx="12"
        fill="#17392c"
        stroke="#c3a153"
        strokeWidth="2"
      />
      <rect
        x="26"
        y="29"
        width="488"
        height="345"
        rx="12"
        fill="url(#pcb-grid)"
      />
      {[
        [49, 52],
        [491, 52],
        [49, 351],
        [491, 351],
      ].map(([x, y]) => (
        <g key={`${x}${y}`}>
          <circle cx={x} cy={y} r="11" fill="#a79458" />
          <circle cx={x} cy={y} r="6" fill="#0f1d16" />
        </g>
      ))}
      <g fill="none" stroke="#ba954c" strokeWidth="4" strokeLinejoin="round">
        <path d="M103 145H170L210 185H245M103 200H155V288H405V225M275 212V299H370V200M285 174H340V103H422V163M310 200H370V243H430M245 213H196V103H275V155M245 200H180V250H215" />
        <path
          d="M103 175H138V320H455V91H310V170"
          stroke="#708e94"
          opacity=".65"
        />
      </g>
      <rect
        x="56"
        y="130"
        width="57"
        height="88"
        rx="4"
        fill="#b1a89b"
        stroke="#ded0aa"
        strokeWidth="2"
      />
      <rect x="56" y="140" width="12" height="68" fill="#1c2621" />
      <rect
        x="240"
        y="160"
        width="70"
        height="69"
        rx="3"
        fill="#20231f"
        stroke="#55584b"
      />
      <path d="M265 160a10 10 0 0 0 20 0" fill="#0b120e" />
      {[0, 1, 2, 3].flatMap((i) =>
        [230, 310].map((x) => (
          <rect
            key={`${x}${i}`}
            x={x}
            y={169 + i * 14}
            width="10"
            height="6"
            fill="#dbcb92"
          />
        )),
      )}
      {[
        [200, 96],
        [275, 96],
        [365, 192],
        [195, 246],
      ].map(([x, y], i) => (
        <g key={i}>
          <rect x={x} y={y} width="39" height="16" rx="2" fill="#a89770" />
          <rect x={x + 9} y={y} width="21" height="16" fill="#423a29" />
        </g>
      ))}
      <rect
        x="415"
        y="163"
        width="26"
        height="38"
        rx="3"
        fill="#d99f3c"
        stroke="#ffc463"
        strokeWidth="2"
      />
      <circle cx="428" cy="182" r="6" fill="#ffca6c" className="led-glow" />
      <g fill="#d0d1a6" fontFamily="monospace" fontSize="12">
        <text x="240" y="149">
          U1 · NE555
        </text>
        <text x="56" y="120">
          J1
        </text>
        <text x="201" y="88">
          R1
        </text>
        <text x="276" y="88">
          R2
        </text>
        <text x="364" y="184">
          R3
        </text>
        <text x="196" y="239">
          C1
        </text>
        <text x="416" y="152">
          D1
        </text>
        <text x="170" y="346" fontSize="13" letterSpacing="3">
          EEA / REV.01
        </text>
      </g>
      <g fill="#d3bf74" fontFamily="monospace" fontSize="10">
        <text x="475" y="211" transform="rotate(90 475 211)">
          50 × 40 mm
        </text>
      </g>
    </svg>
  );
}
export default function AetherScope({
  pcbUrl,
  modelUrl,
  demo,
  commits,
  onCheckout,
  selected,
  busy,
  focus,
  onClearFocus,
}: {
  pcbUrl: string | null;
  modelUrl: string | null;
  demo: boolean;
  commits: Commit[];
  onCheckout: (hash: string) => void;
  selected: string;
  busy: boolean;
  focus?: BoardFocus | null;
  onClearFocus?: () => void;
}) {
  const [view, setView] = useState<"2d" | "3d">("2d");
  useEffect(() => { if (focus) setView("2d"); }, [focus]);
  const [design, setDesign] = useState<Design | null>(null);
  useEffect(() => {
    let active = true;
    setDesign(null);
    // Same revision as the board (a ?rev= snapshot, or live).
    if (pcbUrl)
      void fetch(artifact(pcbUrl.replace("board.kicad_pcb", "design.json")))
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          if (active && d?.board_width_mm && Array.isArray(d.components))
            setDesign(d);
        })
        .catch(() => {});
    return () => {
      active = false;
    };
  }, [pcbUrl]);
  const boardControls = useRef<BoardControls>(null);
  const revisionSlider = useRef<HTMLInputElement>(null);
  const [scrubIndex, setScrubIndex] = useState<number | null>(null);
  const chronological = [...commits].reverse();
  const latestIndex = Math.max(chronological.length - 1, 0);
  const index =
    selected === "latest"
      ? latestIndex
      : chronological.findIndex((c) => c.hash === selected);
  const displayedIndex = scrubIndex ?? Math.max(index, 0);
  function checkoutIndex(i: number) {
    const hash = i === latestIndex ? "latest" : chronological[i]?.hash;
    if (hash && (busy || hash !== selected)) {
      setScrubIndex(i);
      onCheckout(hash);
    } else setScrubIndex(null);
  }
  // Native change also covers WebViews that only report the final value.
  useEffect(() => {
    const slider = revisionSlider.current;
    if (!slider) return;
    const commit = () => checkoutIndex(Number(slider.value));
    slider.addEventListener("change", commit);
    return () => slider.removeEventListener("change", commit);
  }, [commits, selected, busy, onCheckout]);
  useEffect(() => {
    if (!busy) setScrubIndex(null);
  }, [busy, selected, demo]);
  return (
    <section className="instrument board-panel">
      <div className="panel-heading board-toolbar">
        <span>
          <i className="fig">II.</i> Board
        </span>
        <div className="view-tabs">
          <button
            className={view === "2d" ? "active" : ""}
            onClick={() => setView("2d")}
          >
            <Layers size={13} /> 2D
          </button>
          <button
            className={view === "3d" ? "active" : ""}
            onClick={() => { onClearFocus?.(); setView("3d"); }}
          >
            <Box size={13} /> 3D
          </button>
        </div>
        <div className="board-actions">
          {view === "2d" && pcbUrl && !demo && (
            <>
              <button
                className="icon-button"
                aria-label="Fit board"
                title="Fit board"
                onClick={() => { onClearFocus?.(); boardControls.current?.fit(); }}
              >
                <Maximize2 size={15} />
              </button>
              <button
                className="icon-button"
                aria-label="Flip board"
                title="Flip board"
                onClick={() => boardControls.current?.flip()}
              >
                <FlipHorizontal2 size={15} />
              </button>
              <a
                className="icon-button"
                aria-label="Download PCB"
                title="Download PCB"
                href={artifact(pcbUrl)}
                download
              >
                <Download size={15} />
              </a>
            </>
          )}
        </div>
      </div>
      <div className="board-viewport">
        <div className="viewport-crosshair top-left" />
        <div className="viewport-crosshair bottom-right" />
        {view === "2d" ? (
          demo ? (
            <DemoPlan />
          ) : pcbUrl ? (
            <KiCanvasBoard ref={boardControls} src={artifact(pcbUrl)} focus={focus} />
          ) : (
            <div className="bench-empty">
              <div className="reticle">
                <Maximize2 size={47} />
              </div>
              <h2>No board yet</h2>
            </div>
          )
        ) : demo || modelUrl || design ? (
          <Suspense fallback={<div className="bench-empty">Loading 3D…</div>}><Board3D demo={demo} modelUrl={modelUrl} design={design} /></Suspense>
        ) : (
          <div className="bench-empty">
            <Box size={45} />
            <h2>No 3D model</h2>
          </div>
        )}
        <div className="viewport-caption">
          <span className="status-dot online" />
          {focus && !demo ? `${focus.label} · ${focus.x.toFixed(2)}, ${focus.y.toFixed(2)} mm` : demo
            ? "Demo board"
            : view === "3d" && design && !modelUrl
              ? "Approximate 3D preview"
              : pcbUrl
                ? "Live board"
                : ""}
          <span>{view === "3d" ? "" : ""}</span>
        </div>
      </div>
      <div className="history">
        <div className="history-label">
          <span>
            <History size={13} /> History
          </span>
          <button
            onClick={() => checkoutIndex(latestIndex)}
            disabled={demo || (!busy && selected === "latest")}
          >
            Latest
          </button>
        </div>
        <div className="history-navigation">
          <button
            className="icon-button"
            aria-label="Previous revision"
            title="Previous revision"
            disabled={demo || !commits.length || displayedIndex <= 0}
            onClick={() => checkoutIndex(displayedIndex - 1)}
          >
            <ChevronLeft size={16} />
          </button>
          <input
            ref={revisionSlider}
            aria-label="Board revision"
            type="range"
            min={0}
            max={latestIndex}
            value={displayedIndex}
            disabled={!chronological.length || demo}
            onChange={(e) => {
              const i = Number(e.target.value);
              setScrubIndex(i);
              checkoutIndex(i);
            }}
          />
          <button
            className="icon-button"
            aria-label="Next revision"
            title="Next revision"
            disabled={
              demo || !commits.length || displayedIndex >= latestIndex
            }
            onClick={() => checkoutIndex(displayedIndex + 1)}
          >
            <ChevronRight size={16} />
          </button>
        </div>
        <div className="history-caption">
          <span>
            {busy
              ? "Loading…"
              : displayedIndex === latestIndex
                ? "Latest"
                : chronological[displayedIndex]?.hash.slice(0, 8)}
          </span>
          <span>{demo ? "Demo" : `${commits.length} revisions`}</span>
        </div>
      </div>
    </section>
  );
}
