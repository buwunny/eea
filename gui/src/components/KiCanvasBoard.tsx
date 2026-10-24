import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";

interface Viewer {
  document?: unknown;
  loaded?: { isOpen: boolean };
  page_opacity?: number;
  zoom_to_board: () => void;
  flip_view: () => void;
  draw: () => void;
}
export interface BoardControls {
  fit: () => void;
  flip: () => void;
}

/** Adapter for the pinned, local KiCanvas build in public/vendor/kicanvas. */
export default forwardRef<BoardControls, { src: string }>(
  function KiCanvasBoard({ src }, controls) {
    const embed = useRef<HTMLElement>(null);
    const viewer = useRef<Viewer>();
    const [loadError, setLoadError] = useState(false);
    useImperativeHandle(
      controls,
      () => ({
        fit() {
          if (viewer.current) {
            viewer.current.zoom_to_board();
            viewer.current.draw();
          }
        },
        flip() {
          viewer.current?.flip_view();
        },
      }),
      [],
    );
    useEffect(() => {
      let cancelled = false;
      let attempts = 0;
      let timer: ReturnType<typeof setTimeout>;
      viewer.current = undefined;
      setLoadError(false);
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
            app.viewer.page_opacity = 0;
            app.viewer.zoom_to_board();
            app.viewer.draw();
            viewer.current = app.viewer;
            return;
          }
        } catch {
          /* The custom element may still be initializing. */
        }
        if (++attempts < 100) timer = setTimeout(connect, 200);
        else setLoadError(true);
      }
      void customElements.whenDefined("kicanvas-embed").then(connect);
      return () => {
        cancelled = true;
        clearTimeout(timer);
        viewer.current = undefined;
      };
    }, [src]);
    return (
      <>
        <kicanvas-embed
          ref={embed}
          key={src}
          src={src}
          controls="basic"
          controlslist="nooverlay nodownload noflipview nofullscreen"
        />
        {loadError && (
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
