import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import type { BoardFocus } from "../types";

interface Box {
  x: number; y: number; w: number; h: number;
  copy: () => Box;
  grow: (amount: number) => Box;
}

interface Viewer {
  document?: unknown;
  loaded?: { isOpen: boolean };
  page_opacity?: number;
  zoom_to_board: () => void;
  flip_view: () => void;
  draw: () => void;
  viewport?: { camera: { bbox: Box } };
  select?: (box: Box | null) => void;
}
export interface BoardControls {
  fit: () => void;
  flip: () => void;
}

function BoardFrame({
  src,
  visible,
  onReady,
  onError,
}: {
  src: string;
  visible: boolean;
  onReady: (src: string, viewer: Viewer) => void;
  onError: (src: string) => void;
}) {
  const embed = useRef<HTMLElement>(null);
  useEffect(() => {
    let cancelled = false;
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout>;
    let frame: number;
    function connect() {
      if (cancelled) return;
      try {
        const app = embed.current?.shadowRoot?.querySelector(
          "kc-board-app",
        ) as (HTMLElement & { viewer?: Viewer }) | undefined;
        if (
          app?.shadowRoot &&
          !app.shadowRoot.querySelector("#eea-viewer-style")
        ) {
          const style = document.createElement("style");
          style.id = "eea-viewer-style";
          style.textContent =
            "kc-viewer-bottom-toolbar { display:none !important; }";
          app.shadowRoot.append(style);
        }
        if (app?.viewer?.document && app.viewer.loaded?.isOpen) {
          const readyViewer = app.viewer;
          readyViewer.page_opacity = 0;
          readyViewer.zoom_to_board();
          readyViewer.draw();
          // Reveal only after the fitted canvas has had a frame to paint.
          frame = requestAnimationFrame(() => {
            if (!cancelled) onReady(src, readyViewer);
          });
          return;
        }
      } catch {
        /* The custom element may still be initializing. */
      }
      if (++attempts < 400) timer = setTimeout(connect, 50);
      else onError(src);
    }
    void customElements.whenDefined("kicanvas-embed").then(connect);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      cancelAnimationFrame(frame);
    };
  }, [src, onReady, onError]);
  return (
    <kicanvas-embed
      ref={embed}
      src={src}
      style={{ visibility: visible ? "visible" : "hidden" }}
      aria-hidden={!visible}
      controls="basic"
      controlslist="nooverlay nodownload noflipview nofullscreen"
    />
  );
}

/** Keep the current board visible while a replacement initializes offscreen. */
export default forwardRef<BoardControls, { src: string; focus?: BoardFocus | null }>(
  function KiCanvasBoard({ src, focus }, controls) {
    const requested = useRef(src);
    requested.current = src;
    const viewer = useRef<Viewer>();
    const [displayed, setDisplayed] = useState<string | null>(null);
    const [failed, setFailed] = useState<string | null>(null);
    const ready = useCallback((loadedSrc: string, loadedViewer: Viewer) => {
      if (loadedSrc !== requested.current) return;
      viewer.current = loadedViewer;
      setDisplayed(loadedSrc);
      setFailed(null);
    }, []);
    const fail = useCallback((failedSrc: string) => {
      if (failedSrc === requested.current) setFailed(failedSrc);
    }, []);
    useEffect(() => {
      if (!focus || displayed !== src || !viewer.current?.viewport) return;
      const box = viewer.current.viewport.camera.bbox.copy();
      box.x = focus.x - 0.6; box.y = focus.y - 0.6; box.w = 1.2; box.h = 1.2;
      viewer.current.viewport.camera.bbox = box.grow(4);
      viewer.current.select?.(box);
      viewer.current.draw();
    }, [focus, displayed, src]);
    useImperativeHandle(
      controls,
      () => ({
        fit() {
          viewer.current?.select?.(null);
          viewer.current?.zoom_to_board();
          viewer.current?.draw();
        },
        flip() {
          viewer.current?.flip_view();
        },
      }),
      [],
    );
    const sources = displayed && displayed !== src ? [displayed, src] : [src];
    return (
      <>
        {sources.map((source) => (
          <BoardFrame
            key={source}
            src={source}
            visible={source === displayed}
            onReady={ready}
            onError={fail}
          />
        ))}
        {failed === src && (
          <div className="embed-notice">
            Viewer unavailable.{" "}
            <a href={src} download>
              Download PCB
            </a>
          </div>
        )}
      </>
    );
  },
);
