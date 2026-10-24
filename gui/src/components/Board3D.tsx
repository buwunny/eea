import { Component, ReactNode, Suspense } from "react";
import { Canvas, useLoader } from "@react-three/fiber";
import { OrbitControls, ContactShadows, Html } from "@react-three/drei";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { VRMLLoader } from "three/examples/jsm/loaders/VRMLLoader.js";
import { Box3, Vector3, Object3D } from "three";
import { artifact } from "../api";
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
export interface Design {
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

export default function Board3D({ demo, modelUrl, design }: { demo: boolean; modelUrl: string | null; design: Design | null }) {
  return (
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
            <OrbitControls enablePan enableZoom />
          </Canvas>
  );
}
