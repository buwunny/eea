import http from "node:http";
import { randomUUID } from "node:crypto";
import {
  defaultProjectsDirectory,
  defaultStateDirectory,
  describeProject,
  validateProject,
  initializeProject,
  createProject,
  directoryListing,
  projectIsBusy,
  readRegistry,
  rememberProject,
} from "./projects.js";
import { exportInfo, generateExports } from "./exports.js";
import { environmentDoctor } from "./doctor.js";
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
  "build/design.json",
  "manufacturing/export_report.json",
  "manufacturing/gerbers.zip",
];
const jsonFiles = new Set([
  "telemetry.json",
  "build/sim_results.json",
  "build/drc_report.json",
  "build/design.json",
  "manufacturing/export_report.json",
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
  stateDirectory = defaultStateDirectory,
  exporter = generateExports,
  doctor = environmentDoctor,
} = {}) {
  let workspace = path.resolve(bridgeWorkspace);
  const registry = await readRegistry(stateDirectory);
  if (
    bridgeWorkspace ===
      path.resolve(
        process.env.EEA_WORKSPACE || path.join(here, "../../workspace"),
      ) &&
    !process.env.EEA_WORKSPACE &&
    registry.active
  ) {
    try {
      workspace = await validateProject(registry.active);
    } catch {
      /* Keep the default when a remembered project was moved. */
    }
  }
  let project = { ...(await describeProject(workspace)), id: randomUUID() };
  let watcher;
  let projectMutation = false;
  let exportRunning = false;
  async function body(req) {
    let text = "";
    for await (const chunk of req) {
      text += chunk;
      if (text.length > 65536) throw new Error("Request too large");
    }
    return text ? JSON.parse(text) : {};
  }
  async function canSwitch() {
    if (exportRunning || (await projectIsBusy(workspace))) {
      const error = new Error(
        "A tool is running. Stop the CLI or wait for it to finish before switching projects.",
      );
      error.status = 409;
      throw error;
    }
  }
  async function activate(target) {
    const next = { ...target, id: randomUUID() };
    const nextWatcher = await watchWorkspace(target.path, next.id);
    try {
      await rememberProject(stateDirectory, {
        path: project.path,
        name: project.name,
      });
      await rememberProject(stateDirectory, target);
    } catch (error) {
      await nextWatcher.close();
      throw error;
    }
    await watcher.close();
    workspace = target.path;
    project = next;
    watcher = nextWatcher;
    telemetrySockets.clients.forEach((client) =>
      send(client, { type: "PROJECT_CHANGED", project }),
    );
    terminalSockets.clients.forEach((client) =>
      client.close(1012, "Project changed"),
    );
    await Promise.all(trackedFiles.map((file) => update(file)));
    return project;
  }
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
      const requestProjectId = project.id;
      if (req.method === "GET" && url.pathname === "/api/environment")
        return response(res, 200, await doctor());
      if (
        url.searchParams.has("project") &&
        url.searchParams.get("project") !== project.id
      )
        return response(res, 409, {
          error: "The active project changed. Refresh this view.",
        });
      if (req.method === "GET" && url.pathname === "/api/projects")
        return response(res, 200, {
          active: project,
          recent: (await readRegistry(stateDirectory)).recent || [],
          defaultDirectory: defaultProjectsDirectory,
        });
      if (req.method === "GET" && url.pathname === "/api/projects/directories")
        return response(
          res,
          200,
          await directoryListing(url.searchParams.get("path") || undefined),
        );
      if (
        req.method === "POST" &&
        ["/api/projects/new", "/api/projects/open"].includes(url.pathname)
      ) {
        const input = await body(req);
        const operation = checkoutQueue.then(async () => {
          if (input.projectId && input.projectId !== project.id)
            throw new Error("The active project changed. Try again.");
          projectMutation = true;
          try {
            await canSwitch();
            const target = url.pathname.endsWith("/new")
              ? await createProject(
                  input.parentPath || defaultProjectsDirectory,
                  input.name,
                )
              : await initializeProject(await validateProject(input.path));
            return await activate(target);
          } finally {
            projectMutation = false;
          }
        });
        checkoutQueue = operation.catch(() => {});
        return response(res, 200, { project: await operation });
      }
      if (req.method === "GET" && url.pathname === "/api/exports")
        return response(res, 200, await exportInfo(workspace, project.id));
      if (req.method === "POST" && url.pathname === "/api/exports") {
        const input = await body(req);
        if (exportRunning || projectMutation)
          return response(res, 409, {
            error: "Another project operation is running",
          });
        exportRunning = true;
        const operation = checkoutQueue.then(async () => {
          if (input.projectId && input.projectId !== project.id)
            throw new Error("The active project changed. Try again.");
          if (await projectIsBusy(workspace))
            throw new Error(
              "An MCP tool is running. Wait for it to finish before exporting.",
            );
          const report = await exporter(workspace);
          return { ...(await exportInfo(workspace, project.id)), report };
        });
        checkoutQueue = operation.catch(() => {});
        try {
          const result = await operation;
          return response(res, result.ready ? 200 : 422, result);
        } finally {
          exportRunning = false;
        }
      }

      if (req.method === "GET" && url.pathname === "/api/git/log") {
        let log = "";
        try {
          log = await git("log", "-100", "--format=%H%x09%s%x09%cI");
        } catch {
          /* No history yet. */
        }
        return response(res, 200, {
          projectId: requestProjectId,
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
      // History is read-only: snapshots are served from git objects and never written
      // into workspace/, so browsing old revisions cannot disturb the live build files.
      if (
        req.method === "GET" &&
        url.pathname.startsWith("/api/git/snapshot/")
      ) {
        const hash = url.pathname.slice("/api/git/snapshot/".length);
        if (!/^[a-f0-9]{7,40}$/i.test(hash))
          return response(res, 400, { error: "Invalid commit hash" });
        let listing;
        try {
          listing = await git("ls-tree", "-r", "--name-only", `${hash}^{commit}`, "--", "build/");
        } catch {
          return response(res, 404, { error: "Revision not found" });
        }
        return response(res, 200, {
          projectId: requestProjectId,
          hash,
          files: listing.split("\n").filter(Boolean),
        });
      }
      if (
        req.method === "GET" &&
        url.pathname.startsWith("/artifacts/") &&
        url.searchParams.has("rev")
      ) {
        const rev = url.searchParams.get("rev");
        let relative = decodeURIComponent(url.pathname.slice("/artifacts/".length));
        if (!relative.startsWith("build/")) relative = `build/${relative}`;
        if (
          !/^[a-f0-9]{7,40}$/i.test(rev) ||
          relative.includes("\0") ||
          relative.includes("\\") ||
          relative.split("/").some((part) => part === ".." || part.startsWith("."))
        )
          return response(res, 400, { error: "Invalid revision artifact" });
        let content;
        try {
          content = (
            await exec("git", ["show", `${rev}:${relative}`], {
              cwd: workspace,
              timeout: 15000,
              encoding: "buffer",
              maxBuffer: 64 * 1024 * 1024,
            })
          ).stdout;
        } catch {
          return response(res, 404, { error: "Artifact not in this revision" });
        }
        res.writeHead(200, {
          "Content-Type":
            contentTypes[path.extname(relative)] || "application/octet-stream",
          // A full commit hash names immutable content, so scrubbing back and forth is instant.
          "Cache-Control":
            rev.length === 40 ? "private, max-age=31536000, immutable" : "no-store",
          "X-Content-Type-Options": "nosniff",
        });
        return res.end(content);
      }
      if (req.method === "GET" && url.pathname.startsWith("/artifacts/")) {
        const target = await artifactPath(
          workspace,
          decodeURIComponent(url.pathname.slice("/artifacts/".length)),
        );
        if (!target)
          return response(res, 404, { error: "Artifact unavailable" });
        if (
          path.basename(target) === "gerbers.zip" &&
          !(await exportInfo(workspace, project.id)).ready
        )
          return response(res, 409, {
            error:
              "Export is stale or incomplete. Generate manufacturing files again.",
          });
        res.writeHead(200, {
          "Content-Type":
            contentTypes[path.extname(target)] || "application/octet-stream",
          "Cache-Control": "no-store",
          "X-Content-Type-Options": "nosniff",
          ...([".zip", ".csv"].includes(path.extname(target))
            ? {
                "Content-Disposition": `attachment; filename="${path.basename(target)}"`,
              }
            : {}),
        });
        const stream = createReadStream(target);
        stream.on("error", () => res.destroy());
        return stream.pipe(res);
      }
      return response(res, 404, { error: "Not found" });
    } catch (error) {
      return response(res, error.status || 400, { error: error.message });
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
    handler.handleUpgrade(req, socket, head, (connection) => {
      // A closing WebView may reset its socket instead of sending a close frame.
      // Handle it on the connection so it cannot crash the bridge process.
      connection.on("error", (error) => {
        if (error.code !== "ECONNRESET")
          console.warn(`WebSocket ${endpoint}: ${error.message}`);
      });
      handler.emit("connection", connection, req);
    });
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
        env: {
          ...process.env,
          EEA_WORKSPACE: workspace,
          TERM: "xterm-256color",
        },
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
  // Latest broadcast read per file; an older read that finishes late (including one that
  // saw the file briefly missing) must not overwrite a newer one. Reads for one newly
  // connected socket are separate and must not cancel broadcasts to other windows.
  const readSequence = new Map();
  async function update(
    file,
    socket,
    root = workspace,
    projectId = project.id,
    attempt = 0,
  ) {
    const sequence = socket ? 0 : (readSequence.get(file) ?? 0) + 1;
    if (!socket) readSequence.set(file, sequence);
    const current = () => socket || readSequence.get(file) === sequence;
    try {
      const absolute = path.join(root, file);
      const stat = await fs.stat(absolute);
      const payload = jsonFiles.has(file)
        ? JSON.parse(await fs.readFile(absolute, "utf8"))
        : `/artifacts/${file}?project=${encodeURIComponent(projectId)}&t=${stat.mtimeMs}`;
      if (projectId !== project.id) return;
      if (!current()) return;
      const value = { type: "FILE_UPDATED", projectId, file, payload };
      if (socket) send(socket, value);
      else telemetrySockets.clients.forEach((client) => send(client, value));
    } catch (error) {
      // A JSON file caught mid-write: read it again shortly instead of dropping the update.
      if (error instanceof SyntaxError && attempt < 5) {
        setTimeout(() => {
          if (current()) void update(file, socket, root, projectId, attempt + 1);
        }, 100);
        return;
      }
      if (error.code === "ENOENT" && projectId === project.id && current()) {
        const value = { type: "FILE_UPDATED", projectId, file, payload: null };
        if (socket) send(socket, value);
        else telemetrySockets.clients.forEach((client) => send(client, value));
      }
    }
  }
  telemetrySockets.on("connection", (socket) => {
    send(socket, { type: "PROJECT_CHANGED", project });
    trackedFiles.forEach((file) => {
      void update(file, socket);
    });
  });
  async function watchWorkspace(root, projectId) {
    // Watch the folders, not the files: tools save by writing a temp file and renaming it
    // over the old one, and a watch on the file itself stops firing after that swap.
    const tracked = new Set(trackedFiles);
    const watchedDirs = new Set(["build", "manufacturing"]);
    const relative = (absolute) =>
      path.relative(root, absolute).split(path.sep).join("/");
    const nextWatcher = chokidar.watch(root, {
      ignoreInitial: true,
      depth: 1,
      ignored: (absolute) => {
        const file = relative(absolute);
        return !(file === "" || watchedDirs.has(file) || tracked.has(file));
      },
      awaitWriteFinish: { stabilityThreshold: 80, pollInterval: 20 },
    });
    nextWatcher.on("add", (absolute) => {
      void update(relative(absolute), undefined, root, projectId);
    });
    nextWatcher.on("change", (absolute) => {
      void update(relative(absolute), undefined, root, projectId);
    });
    nextWatcher.on("unlink", (absolute) => {
      const file = relative(absolute);
      if (!tracked.has(file)) return;
      // Savers often delete and recreate a file; re-check before announcing it is gone.
      // Going through update() also makes this the newest read, cancelling stale ones.
      setTimeout(() => void update(file, undefined, root, projectId), 150);
    });
    await new Promise((resolve, reject) => {
      nextWatcher.once("ready", resolve);
      nextWatcher.once("error", reject);
    });
    return nextWatcher;
  }
  watcher = await watchWorkspace(workspace, project.id);
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(bridgePort, "127.0.0.1", resolve);
  });
  console.log(`EEA bridge: http://127.0.0.1:${server.address().port}`);
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
