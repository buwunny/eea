import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { Copy } from "lucide-react";
import { wsUrl } from "../api";
const prompts = [
  ["555 Flasher", "Build a USB-C 555 LED flasher at 2 Hz, ready to manufacture."],
  ["3.3 V Supply", "Build a USB-C to 3.3 V power board with a power LED."],
  ["ESP32-C3", "Build an ESP32-C3 board with USB-C, on 4 layers."],
];
export default function TelegraphTerm({
  workspacePath,
}: {
  workspacePath?: string;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [connected, setConnected] = useState(false);
  const [copied, setCopied] = useState("");
  useEffect(() => {
    if (!host.current) return;
    // Defer opening until the effect survives React StrictMode’s setup/cleanup probe.
    let cleanup = () => {};
    const frame = requestAnimationFrame(() => {
      if (!host.current) return;
      const terminal = new Terminal({
        fontFamily: "monospace",
        fontSize: 14,
        lineHeight: 1.15,
        cursorBlink: true,
        theme: {
          background: "#0e0b09",
          foreground: "#e6dcc8",
          cursor: "#cfa95f",
          cursorAccent: "#0e0b09",
          selectionBackground: "#4a3e31",
          black: "#1a110b",
          red: "#e0654f",
          green: "#a9c46c",
          yellow: "#ffb84a",
          blue: "#8fa9c9",
          magenta: "#c98fa0",
          cyan: "#86c2a8",
          white: "#ecd9b0",
          brightBlack: "#7a6447",
          brightRed: "#ff8a6e",
          brightGreen: "#c8e08a",
          brightYellow: "#ffd27a",
          brightBlue: "#b0c6e0",
          brightMagenta: "#e2adbd",
          brightCyan: "#a8dcc3",
          brightWhite: "#fff3d2",
        },
        scrollback: 3000,
      });
      const fit = new FitAddon();
      terminal.loadAddon(fit);
      terminal.open(host.current);
      fit.fit();
      // IBM Plex Mono is bundled but loads asynchronously; swap it in once ready so xterm measures the right cell size.
      void document.fonts
        .load('14px "IBM Plex Mono"')
        .then(() => {
          if (disposed) return;
          terminal.options.fontFamily = '"IBM Plex Mono", monospace';
          fit.fit();
          if (socket?.readyState === WebSocket.OPEN)
            socket.send(
              JSON.stringify({
                type: "resize",
                cols: terminal.cols,
                rows: terminal.rows,
              }),
            );
        })
        .catch(() => {});
      let socket: WebSocket | undefined;
      let timer: ReturnType<typeof setTimeout>;
      let disposed = false;
      function connect() {
        socket = new WebSocket(wsUrl("/ws/terminal"));
        socket.onopen = () => {
          if (disposed) {
            socket?.close();
            return;
          }
          socket?.send(
            JSON.stringify({
              type: "resize",
              cols: terminal.cols,
              rows: terminal.rows,
            }),
          );
        };
        socket.onmessage = (e) => {
          if (disposed) return;
          try {
            const m = JSON.parse(e.data);
            if (m.type === "output") terminal.write(m.data);
            if (m.type === "status") {
              setConnected(m.status === "connected");
              if (m.message) terminal.writeln(`\r\n${m.message}`);
            }
          } catch {
            terminal.write(e.data);
          }
        };
        socket.onclose = () => {
          if (disposed) return;
          setConnected(false);
          if (!disposed) timer = setTimeout(connect, 3000);
        };
        socket.onerror = () => socket?.close();
      }
      connect();
      const input = terminal.onData((data) => {
        if (socket?.readyState === WebSocket.OPEN)
          socket.send(JSON.stringify({ type: "input", data }));
      });
      const observer = new ResizeObserver(() => {
        fit.fit();
        if (socket?.readyState === WebSocket.OPEN)
          socket.send(
            JSON.stringify({
              type: "resize",
              cols: terminal.cols,
              rows: terminal.rows,
            }),
          );
      });
      observer.observe(host.current);
      cleanup = () => {
        disposed = true;
        clearTimeout(timer);
        if (socket?.readyState === WebSocket.OPEN) socket.close();
        observer.disconnect();
        input.dispose();
        terminal.dispose();
      };
    });
    return () => {
      cancelAnimationFrame(frame);
      cleanup();
    };
  }, []);
  return (
    <section className="instrument terminal-panel">
      <div className="panel-heading">
        <span>
          <i className="fig">I.</i> Terminal
        </span>
        <span className={`status-dot ${connected ? "online" : ""}`} />
      </div>
      <div className="terminal-path">
        <span title={workspacePath}>{workspacePath || "~/workspace"}</span>
        <span>{connected ? "Connected" : "Offline"}</span>
      </div>
      <div ref={host} className="terminal-host" />
      <div className="prompt-chips">
        {prompts.map(([name, prompt]) => (
          <button
            key={name}
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(prompt);
                setCopied(name);
                setTimeout(() => setCopied(""), 1800);
              } catch {
                setCopied("Clipboard unavailable");
              }
            }}
          >
            <span>{copied === name ? "Copied" : name}</span>
            <Copy size={12} />
          </button>
        ))}
        {copied === "Clipboard unavailable" && (
          <small role="status">Clipboard unavailable in this browser.</small>
        )}
      </div>
    </section>
  );
}
