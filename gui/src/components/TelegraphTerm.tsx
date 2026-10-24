import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { Copy, TerminalSquare } from "lucide-react";
import { wsUrl } from "../api";
const prompts = [
  [
    "555 LED Flasher",
    "Design a USB-C powered 555 LED flasher at 2 Hz using the curated parts. Simulate, route, verify DRC, and export manufacturing files.",
  ],
  [
    "USB-C 3.3 V LDO",
    "Design a USB-C powered AMS1117 3.3 V regulator board using the curated parts. Verify and export manufacturing files.",
  ],
];
export default function TelegraphTerm() {
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
        fontFamily: '"SFMono-Regular", Consolas, monospace',
        fontSize: 15,
        cursorBlink: true,
        theme: {
          background: "#101722",
          foreground: "#e0e7ef",
          cursor: "#ff9912",
          selectionBackground: "#6b512b",
          black: "#101722",
          green: "#b3c881",
          yellow: "#ffbb4e",
        },
        scrollback: 3000,
      });
      const fit = new FitAddon();
      terminal.loadAddon(fit);
      terminal.open(host.current);
      fit.fit();
      let socket: WebSocket | undefined;
      let timer: ReturnType<typeof setTimeout>;
      let disposed = false;
      function connect() {
        socket = new WebSocket(wsUrl("/ws/terminal"));
        socket.onopen = () => {
          socket?.send(
            JSON.stringify({
              type: "resize",
              cols: terminal.cols,
              rows: terminal.rows,
            }),
          );
        };
        socket.onmessage = (e) => {
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
        socket?.close();
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
          <TerminalSquare size={16} /> Terminal
        </span>
        <span className={`status-dot ${connected ? "online" : ""}`} />
      </div>
      <div className="terminal-path">
        <span>~/workspace</span>
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
