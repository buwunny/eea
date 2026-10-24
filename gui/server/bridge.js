import http from "node:http";
import path from "node:path";
import fs from "node:fs/promises";
import { createReadStream } from "node:fs";
import { fileURLToPath } from "node:url";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import chokidar from "chokidar";
import { WebSocketServer, WebSocket } from "ws";

const exec = promisify(execFile);
const here = path.dirname(fileURLToPath(import.meta.url));
export const workspace = path.resolve(
  process.env.EEA_WORKSPACE || path.join(here, "../../workspace"),
);
const port = Number(process.env.EEA_BRIDGE_PORT || 3001);
const trackedFiles = [
  "telemetry.json",
  "build/board.kicad_pcb",
  "build/sim_results.json",
  "build/board_3d.wrl",
  "build/board_3d.glb",
  "build/drc_report.json",
];
const jsonFiles = new Set([
  "telemetry.json",
  "build/sim_results.json",
  "build/drc_report.json",
]);
const contentTypes = {
  ".json": "application/json",
  ".csv": "text/csv",
  ".zip": "application/zip",
  ".glb": "model/gltf-binary",
  ".wrl": "model/vrml",
  ".kicad_pcb": "text/plain",
};

export function allowedOrigin(origin) {
  if (!origin) return true;
  if (origin === "tauri://localhost" || origin === "http://tauri.localhost")
    return true;
  try {
    const url = new URL(origin);
    return (
      ["http:", "https:"].includes(url.protocol) &&
      ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
    );
  } catch {
    return false;
  }
}

export async function artifactPath(root, relative) {
  if (
    !relative ||
    relative.includes("\0") ||
    relative.includes("\\") ||
    relative.split("/").some((p) => p === ".." || p.startsWith("."))
  )
    return null;
  const candidates =
    relative.startsWith("build/") || relative.startsWith("manufacturing/")
      ? [path.resolve(root, relative)]
      : ["build", "manufacturing"].map((dir) =>
          path.resolve(root, dir, relative),
        );
  for (const candidate of candidates) {
    try {
      const real = await fs.realpath(candidate);
      const realRoot = await fs.realpath(root);
      const allowed = (
        await Promise.all(
          ["build", "manufacturing"].map((dir) =>
            fs.realpath(path.join(root, dir)).catch(() => null),
          ),
        )
      ).filter((base) => base && base.startsWith(realRoot + path.sep));
      if (
        allowed.some((base) => base && real.startsWith(base + path.sep)) &&
        (await fs.stat(real)).isFile()
      )
        return real;
    } catch {
      /* Missing artifacts are normal before synthesis. */
    }
  }
  return null;
}

function send(socket, value) {
  if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(value));
}
function response(res, status, value) {
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
  });
  res.end(JSON.stringify(value));
}
export async function startBridge({
  workspace: bridgeWorkspace = workspace,
  port: bridgePort = port,
} = {}) {
  const workspace = path.resolve(bridgeWorkspace);
  const git = async (...args) =>
    (
      await exec("git", args, {
        cwd: workspace,
        timeout: 15000,
        maxBuffer: 1024 * 1024,
      })
    ).stdout;
  let checkoutQueue = Promise.resolve();
  await fs.mkdir(workspace, { recursive: true });
  let pty;
  try {
    pty = await import("node-pty");
  } catch (error) {
    console.warn(`Terminal unavailable: ${error.message}`);
  }
  const server = http.createServer(async (req, res) => {
    if (!allowedOrigin(req.headers.origin))
      return response(res, 403, { error: "Origin denied" });
    if (req.headers.origin)
      res.setHeader("Access-Control-Allow-Origin", req.headers.origin);
    res.setHeader("Vary", "Origin");
    if (req.method === "OPTIONS") {
      res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
      return response(res, 204, {});
    }
    try {
      const url = new URL(req.url, "http://127.0.0.1");
      if (req.method === "GET" && url.pathname === "/api/git/log") {
        let log = "";
        try {
          log = await git("log", "-100", "--format=%H%x09%s%x09%cI");
        } catch {
          /* No history yet. */
        }
        return response(res, 200, {
          commits: log
            .trim()
            .split("\n")
            .filter(Boolean)
            .map((line) => {
              const [hash, message, timestamp] = line.split("\t");
              return { hash, message, timestamp };
            }),
        });
      }
      if (
        req.method === "POST" &&
        url.pathname.startsWith("/api/git/checkout/")
      ) {
        const hash = url.pathname.slice("/api/git/checkout/".length);
        if (hash !== "latest" && !/^[a-f0-9]{7,40}$/i.test(hash))
          return response(res, 400, { error: "Invalid commit hash" });
        const operation = checkoutQueue.then(async () => {
          const revision = hash === "latest" ? "HEAD" : hash;
          await git("rev-parse", "--verify", `${revision}^{commit}`);
          const current = await git("ls-files", "--", "build/");
          const historical = await git(
            "ls-tree",
            "-r",
            "--name-only",
            revision,
            "--",
            "build/",
          );
          if (current.trim() || historical.trim())
            await git(
              "restore",
              `--source=${revision}`,
              "--worktree",
              "--",
              "build/",
            );
        });
        checkoutQueue = operation.catch(() => {});
        await operation;
        return response(res, 200, { ok: true });
      }
      if (req.method === "GET" && url.pathname.startsWith("/artifacts/")) {
        const target = await artifactPath(
          workspace,
          decodeURIComponent(url.pathname.slice("/artifacts/".length)),
        );
        if (!target)
          return response(res, 404, { error: "Artifact unavailable" });
        res.writeHead(200, {
          "Content-Type":
            contentTypes[path.extname(target)] || "application/octet-stream",
          "Cache-Control": "no-store",
          "X-Content-Type-Options": "nosniff",
        });
        const stream = createReadStream(target);
        stream.on("error", () => res.destroy());
        return stream.pipe(res);
      }
      return response(res, 404, { error: "Not found" });
    } catch (error) {
      return response(res, 400, { error: error.message });
    }
  });
  const terminalSockets = new WebSocketServer({
    noServer: true,
    maxPayload: 1024 * 1024,
  });
  const telemetrySockets = new WebSocketServer({
    noServer: true,
    maxPayload: 4096,
  });
  server.on("upgrade", (req, socket, head) => {
    if (!allowedOrigin(req.headers.origin)) return socket.destroy();
    const endpoint = req.url?.split("?")[0];
    const handler =
      endpoint === "/ws/terminal"
        ? terminalSockets
        : endpoint === "/ws/telemetry"
          ? telemetrySockets
          : null;
    if (!handler) return socket.destroy();
    handler.handleUpgrade(req, socket, head, (connection) =>
      handler.emit("connection", connection, req),
    );
  });
  terminalSockets.on("connection", (socket) => {
    if (!pty) {
      send(socket, {
        type: "status",
        status: "unavailable",
        message:
          "node-pty is unavailable. Use an external interactive terminal.",
      });
      return;
    }
    let shell;
    try {
      shell = pty.spawn(process.env.SHELL || "/bin/bash", [], {
        cwd: workspace,
        name: "xterm-256color",
        cols: 100,
        rows: 30,
        env: { ...process.env, TERM: "xterm-256color" },
      });
    } catch (error) {
      send(socket, {
        type: "status",
        status: "unavailable",
        message: error.message,
      });
      return;
    }
    send(socket, { type: "status", status: "connected", message: workspace });
    shell.onData((data) => send(socket, { type: "output", data }));
    shell.onExit(({ exitCode }) => {
      send(socket, { type: "exit", exitCode });
      socket.close();
    });
    socket.on("message", (raw) => {
      try {
        const value = JSON.parse(raw.toString());
        if (value.type === "input" && typeof value.data === "string")
          shell.write(value.data);
        if (
          value.type === "resize" &&
          Number.isInteger(value.cols) &&
          Number.isInteger(value.rows)
        )
          shell.resize(
            Math.max(2, Math.min(500, value.cols)),
            Math.max(2, Math.min(200, value.rows)),
          );
      } catch {
        send(socket, {
          type: "error",
          message: "Expected input or resize JSON",
        });
      }
    });
    socket.on("close", () => {
      try {
        shell.kill();
      } catch {}
    });
  });
  async function update(file, socket) {
    try {
      const absolute = path.join(workspace, file);
      const stat = await fs.stat(absolute);
      const payload = jsonFiles.has(file)
        ? JSON.parse(await fs.readFile(absolute, "utf8"))
        : `/artifacts/${file}?t=${stat.mtimeMs}`;
      const value = { type: "FILE_UPDATED", file, payload };
      if (socket) send(socket, value);
      else telemetrySockets.clients.forEach((client) => send(client, value));
    } catch {
      /* Atomic updates and files not yet created are expected. */
    }
  }
  telemetrySockets.on("connection", (socket) => {
    trackedFiles.forEach((file) => {
      void update(file, socket);
    });
  });
  const watcher = chokidar.watch(
    trackedFiles.map((file) => path.join(workspace, file)),
    {
      ignoreInitial: true,
      awaitWriteFinish: { stabilityThreshold: 100, pollInterval: 50 },
    },
  );
  watcher.on("add", (absolute) => {
    void update(path.relative(workspace, absolute));
  });
  watcher.on("change", (absolute) => {
    void update(path.relative(workspace, absolute));
  });
  watcher.on("unlink", (absolute) =>
    telemetrySockets.clients.forEach((client) =>
      send(client, {
        type: "FILE_UPDATED",
        file: path.relative(workspace, absolute),
        payload: null,
      }),
    ),
  );
  await new Promise((resolve, reject) => {
    watcher.once("ready", resolve);
    watcher.once("error", reject);
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(bridgePort, "127.0.0.1", resolve);
  });
  console.log(`Aether-Forge bridge: http://127.0.0.1:${server.address().port}`);
  const close = async () => {
    terminalSockets.clients.forEach((client) => client.terminate());
    telemetrySockets.clients.forEach((client) => client.terminate());
    await watcher.close();
    terminalSockets.close();
    telemetrySockets.close();
    await new Promise((resolve) => server.close(resolve));
  };
  return { server, close };
}

if (
  process.argv[1] &&
  path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  const bridge = await startBridge();
  for (const signal of ["SIGINT", "SIGTERM"])
    process.once(signal, () => {
      void bridge.close().then(() => process.exit(0));
    });
}
