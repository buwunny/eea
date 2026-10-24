import { Minus, Square, X } from "lucide-react";
async function windowAction(action: string) {
  if (!("__TAURI_INTERNALS__" in window)) return;
  try {
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    const win = getCurrentWindow();
    if (action === "minimize") await win.minimize();
    if (action === "toggle_maximize") await win.toggleMaximize();
    if (action === "close") await win.close();
  } catch (error) {
    console.warn("Window control unavailable", error);
  }
}
export default function BrassTitlebar() {
  return (
    <header className="titlebar" data-tauri-drag-region>
      <div className="brand" data-tauri-drag-region>
        <span className="maker-plate" data-tauri-drag-region>
          EEA
        </span>
        <span className="maker-line" data-tauri-drag-region>
          Electrical Engineer Anything
        </span>
      </div>
      <div className="window-controls">
        <button
          aria-label="Minimize window"
          onClick={() => windowAction("minimize")}
        >
          <Minus size={13} />
        </button>
        <button
          aria-label="Toggle maximize window"
          onClick={() => windowAction("toggle_maximize")}
        >
          <Square size={11} />
        </button>
        <button aria-label="Close window" onClick={() => windowAction("close")}>
          <X size={13} />
        </button>
      </div>
    </header>
  );
}
