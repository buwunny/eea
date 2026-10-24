import { Component, ReactNode, Suspense, useEffect, useState } from "react";
import { Canvas, useLoader } from "@react-three/fiber";
import { OrbitControls, ContactShadows, Html } from "@react-three/drei";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { VRMLLoader } from "three/examples/jsm/loaders/VRMLLoader.js";
import { Box3, Vector3, Object3D } from "three";
import { Box, Layers, RotateCw, History, Maximize2 } from "lucide-react";
import { Commit } from "../types";
import { artifact, api } from "../api";

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
const chips = [
  [-1.15, -0.6, 0.65, 0.38],
  [-0.55, 0.65, 0.35, 0.2],
  [0.15, 0.65, 0.35, 0.2],
  [1.1, 0.5, 0.3, 0.3],
  [0.8, -0.65, 0.5, 0.25],
];
function IllustrativeBoard() {
  return (
    <group rotation={[-Math.PI / 2, 0, 0]}>
      <mesh>
        <boxGeometry args={[4.4, 3.3, 0.12]} />
        <meshStandardMaterial
          color="#173d2a"
          metalness={0.3}
          roughness={0.55}
        />
      </mesh>
      {[-1, 1].flatMap((x) =>
        [-1, 1].map((y) => (
          <mesh key={`${x},${y}`} position={[x * 1.9, y * 1.35, 0.07]}>
            <ringGeometry args={[0.08, 0.16, 24]} />
            <meshStandardMaterial color="#cfa857" />
          </mesh>
        )),
      )}
      <mesh position={[0, 0, 0.2]}>
        <boxGeometry args={[0.8, 0.7, 0.3]} />
        <meshStandardMaterial color="#252322" />
      </mesh>
      {Array.from({ length: 4 }, (_, i) =>
        [-1, 1].map((s) => (
          <mesh key={`${i},${s}`} position={[-0.3 + i * 0.2, s * 0.47, 0.15]}>
            <boxGeometry args={[0.09, 0.25, 0.1]} />
            <meshStandardMaterial
              color="#d1c7a1"
              metalness={0.8}
              roughness={0.25}
            />
          </mesh>
        )),
      )}
      {chips.map(([x, y, w, h], i) => (
        <mesh key={i} position={[x, y, 0.15]}>
          <boxGeometry args={[w, h, 0.16]} />
          <meshStandardMaterial color={i === 3 ? "#de8d35" : "#393028"} />
        </mesh>
      ))}
      <mesh position={[-1.65, 0.1, 0.2]}>
        <boxGeometry args={[0.55, 0.8, 0.4]} />
        <meshStandardMaterial
          color="#b4aaa0"
          metalness={0.8}
          roughness={0.25}
        />
      </mesh>
      <Html position={[0, -1.18, 0.08]} transform scale={0.18}>
        <div
          style={{
            color: "#d3c98a",
            fontFamily: "monospace",
            whiteSpace: "nowrap",
          }}
        >
          EEA / 555
        </div>
      </Html>
    </group>
  );
}
interface Design {
  board_width_mm: number;
  board_height_mm: number;
  components: {
    ref: string;
    part_id: string;
    x_mm: number;
    y_mm: number;
    rotation_deg: number;
  }[];
}
function ProceduralBoard({ design }: { design: Design }) {
  const scale = 4.4 / Math.max(design.board_width_mm, design.board_height_mm);
  return (
    <group scale={scale}>
      <mesh position={[0, -0.8, 0]}>
        <boxGeometry
          args={[design.board_width_mm, 1.6, design.board_height_mm]}
        />
        <meshStandardMaterial color="#173d2a" roughness={0.65} />
      </mesh>
      {design.components.map((p) => {
        const isIc = ["NE555", "AMS1117_3V3"].includes(p.part_id);
        const isConnector =
          p.part_id.includes("USB") || p.part_id.includes("Header");
        const width = isIc ? 5 : isConnector ? 8 : 2;
        const depth = isIc ? 4 : isConnector ? 6 : 1.3;
        return (
          <group
            key={p.ref}
            position={[
              p.x_mm - design.board_width_mm / 2,
              0,
              p.y_mm - design.board_height_mm / 2,
            ]}
            rotation={[0, (-p.rotation_deg * Math.PI) / 180, 0]}
          >
            <mesh position={[0, isIc ? 1.1 : 0.6, 0]}>
              <boxGeometry args={[width, isIc ? 2.2 : 1.2, depth]} />
              <meshStandardMaterial
                color={
                  isConnector
                    ? "#a99e8a"
                    : p.part_id === "LED"
                      ? "#e7a243"
                      : "#302c25"
                }
                metalness={isConnector ? 0.75 : 0.2}
                roughness={0.45}
              />
            </mesh>
            <Html position={[0, 2.7, 0]} center distanceFactor={15}>
              <div className="part-label">{p.ref}</div>
            </Html>
          </group>
        );
      })}
    </group>
  );
}
function NativeModel({ url }: { url: string }) {
  const glb = /\.glb(?:\?|$)/i.test(url);
  return glb ? <Glb url={url} /> : <Vrml url={url} />;
}
function CenterObject({ object }: { object: Object3D }) {
  const model = object.clone();
  const box = new Box3().setFromObject(model);
  const size = box.getSize(new Vector3());
  const center = box.getCenter(new Vector3());
  model.position.sub(center);
  return (
    <group scale={4 / Math.max(size.x, size.y, size.z, 1e-8)}>
      <primitive object={model} />
    </group>
  );
}
function Glb({ url }: { url: string }) {
  const gltf = useLoader(GLTFLoader, url);
  return <CenterObject object={gltf.scene} />;
}
function Vrml({ url }: { url: string }) {
  const model = useLoader(VRMLLoader, url);
  return <CenterObject object={model} />;
}
class ModelBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <Html center>
        <div className="model-error">
          3D export could not be loaded.
          <br />
          Use the 2D board view.
        </div>
      </Html>
    ) : (
      this.props.children
    );
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
}: {
  pcbUrl: string | null;
  modelUrl: string | null;
  demo: boolean;
  commits: Commit[];
  onCheckout: (hash: string) => void;
  selected: string;
  busy: boolean;
}) {
  const [view, setView] = useState<"2d" | "3d">("2d");
  const [design, setDesign] = useState<Design | null>(null);
  useEffect(() => {
    let active = true;
    setDesign(null);
    if (pcbUrl)
      void fetch(api(`/artifacts/build/design.json?t=${Date.now()}`))
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
  const [rotate, setRotate] = useState(true);
  const [embedReady, setEmbedReady] = useState(
    Boolean(customElements.get("kicanvas-embed")),
  );
  useEffect(() => {
    let active = true;
    void customElements.whenDefined("kicanvas-embed").then(() => {
      if (active) setEmbedReady(true);
    });
    return () => {
      active = false;
    };
  }, []);
  const chronological = [...commits].reverse();
  const index =
    selected === "latest"
      ? chronological.length
      : chronological.findIndex((c) => c.hash === selected);
  return (
    <section className="instrument board-panel">
      <div className="panel-heading">
        <span>
          <Layers size={16} /> Board
        </span>
      </div>
      <div className="board-toolbar">
        <div className="view-tabs">
          <button
            className={view === "2d" ? "active" : ""}
            onClick={() => setView("2d")}
          >
            <Layers size={13} /> 2D
          </button>
          <button
            className={view === "3d" ? "active" : ""}
            onClick={() => setView("3d")}
          >
            <Box size={13} /> 3D
          </button>
        </div>
        <button
          className="icon-button"
          aria-label={
            rotate ? "Pause automatic rotation" : "Start automatic rotation"
          }
          onClick={() => setRotate(!rotate)}
          disabled={view !== "3d"}
        >
          <RotateCw size={15} />
        </button>
      </div>
      <div className="board-viewport">
        <div className="viewport-crosshair top-left" />
        <div className="viewport-crosshair bottom-right" />
        {view === "2d" ? (
          demo ? (
            <DemoPlan />
          ) : pcbUrl ? (
            <>
              <kicanvas-embed
                key={pcbUrl}
                src={artifact(pcbUrl)}
                controls="full"
                controlslist="nooverlay"
              />
              {!embedReady && (
                <div className="embed-notice">
                  Loading viewer…
                  <a href={artifact(pcbUrl)} download>
                    Download PCB
                  </a>
                </div>
              )}
            </>
          ) : (
            <div className="bench-empty">
              <div className="reticle">
                <Maximize2 size={47} />
              </div>
              <h2>No board yet</h2>
            </div>
          )
        ) : demo || modelUrl || design ? (
          <Canvas camera={{ position: [4, 5, 6], fov: 42 }}>
            <color attach="background" args={["#111710"]} />
            <ambientLight intensity={1.3} />
            <directionalLight position={[3, 7, 3]} intensity={2.5} />
            <Suspense fallback={<Html center>Loading 3D…</Html>}>
              <ModelBoundary key={modelUrl ?? "demo"}>
                {modelUrl && !demo ? (
                  <NativeModel url={artifact(modelUrl)} />
                ) : demo ? (
                  <IllustrativeBoard />
                ) : design ? (
                  <ProceduralBoard design={design} />
                ) : null}
              </ModelBoundary>
              <ContactShadows
                position={[0, -0.22, 0]}
                opacity={0.4}
                scale={12}
                blur={2}
              />
            </Suspense>
            <OrbitControls
              autoRotate={rotate}
              autoRotateSpeed={0.6}
              enablePan
              enableZoom
            />
          </Canvas>
        ) : (
          <div className="bench-empty">
            <Box size={45} />
            <h2>No 3D model</h2>
          </div>
        )}
        <div className="viewport-caption">
          <span className="status-dot online" />
          {demo
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
            onClick={() => onCheckout("latest")}
            disabled={busy || demo || selected === "latest"}
          >
            Latest
          </button>
        </div>
        <input
          aria-label="Board revision"
          type="range"
          min={0}
          max={chronological.length}
          value={Math.max(index, 0)}
          disabled={!chronological.length || busy || demo}
          onChange={(e) => {
            const i = Number(e.target.value);
            onCheckout(
              i === chronological.length ? "latest" : chronological[i].hash,
            );
          }}
        />
        <div className="history-caption">
          <span>
            {busy
              ? "Loading…"
              : selected === "latest"
                ? "Latest"
                : selected.slice(0, 8)}
          </span>
          <span>{demo ? "Demo" : `${commits.length} revisions`}</span>
        </div>
      </div>
    </section>
  );
}
