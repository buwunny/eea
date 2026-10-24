import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { createHash } from "node:crypto";
import { WebSocket } from "ws";
import { startBridge } from "./bridge.js";
import { initializeProject } from "./projects.js";

async function preparedExport(root) {
  const digest = createHash("sha256")
    .update(await fs.readFile(path.join(root, "build/board.kicad_pcb")))
    .digest("hex");
  const report = {
    status: "success",
    verified: true,
    manufacturing_ready: true,
    board_sha256: digest,
    summary: "Export fixture complete",
  };
  await fs.writeFile(
    path.join(root, "build/drc_report.json"),
    JSON.stringify({
      verified: true,
      board_sha256: digest,
      drc_errors: 0,
      unconnected_items: 0,
    }),
  );
  for (const name of ["gerbers.zip", "bom.csv", "cpl.csv"])
    await fs.writeFile(
      path.join(root, "manufacturing", name),
      "fixture-" + name,
    );
  await fs.writeFile(
    path.join(root, "manufacturing/export_report.json"),
    JSON.stringify(report),
  );
  return report;
}
test("new/open projects isolate files, terminal cwd, MCP configs, history, and export downloads", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "eea-projects-"));
  const stateDirectory = path.join(root, "state");
  let bridge, socket, terminal;
  try {
    const first = path.join(root, "First project");
    await initializeProject(first, "First project");
    await fs.writeFile(
      path.join(first, "build/board.kicad_pcb"),
      "first board",
    );
    await preparedExport(first);
    const exported = [];
    bridge = await startBridge({
      workspace: first,
      port: 0,
      stateDirectory,
      exporter: async (directory) => {
        exported.push(directory);
        return preparedExport(directory);
      },
    });
    const base = `http://127.0.0.1:${bridge.server.address().port}`;
    const post = async (route, data) => {
      const response = await fetch(base + route, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
      return { status: response.status, data: await response.json() };
    };
    const initial = await (await fetch(base + "/api/projects")).json();
    assert.equal(initial.active.path, first);
    const oldExport = await (await fetch(base + "/api/exports")).json();
    assert.equal(oldExport.ready, true);
    const events = [];
    socket = new WebSocket(base.replace("http:", "ws:") + "/ws/telemetry");
    socket.on("message", (raw) => events.push(JSON.parse(raw)));
    await new Promise((resolve, reject) => {
      socket.once("open", resolve);
      socket.once("error", reject);
    });
    let result = await post("/api/projects/new", {
      parentPath: path.join(root, "New projects"),
      name: "Second",
      projectId: initial.active.id,
    });
    assert.equal(result.status, 200);
    const second = result.data.project;
    assert.equal(second.name, "Second");
    assert.equal(
      await fs.readFile(path.join(first, "build/board.kicad_pcb"), "utf8"),
      "first board",
    );
    for (const file of [
      ".mcp.json",
      ".gemini/settings.json",
      ".agents/mcp_config.json",
    ]) {
      const config = JSON.parse(
        await fs.readFile(path.join(second.path, file), "utf8"),
      );
      assert.equal(
        config.mcpServers["ee-anything"].env.EEA_WORKSPACE,
        second.path,
      );
    }
    assert.equal((await fetch(base + oldExport.files[0].url)).status, 409);
    assert.equal(
      (await fetch(base + "/artifacts/board.kicad_pcb")).status,
      404,
    );
    await new Promise((resolve) => setTimeout(resolve, 100));
    assert.ok(
      events.some(
        (event) =>
          event.type === "PROJECT_CHANGED" && event.project.id === second.id,
      ),
    );
    assert.ok(
      events.some(
        (event) =>
          event.file === "build/board.kicad_pcb" &&
          event.projectId === second.id &&
          event.payload === null,
      ),
    );
    terminal = new WebSocket(base.replace("http:", "ws:") + "/ws/terminal");
    await new Promise((resolve, reject) => {
      let output = "";
      const timeout = setTimeout(
        () =>
          reject(new Error("New terminal did not use project cwd/environment")),
        5000,
      );
      terminal.on("message", (raw) => {
        const value = JSON.parse(raw);
        if (value.type === "status" && value.status === "unavailable") {
          clearTimeout(timeout);
          resolve();
        }
        if (value.type === "status" && value.status === "connected") {
          assert.equal(value.message, second.path);
          terminal.send(
            JSON.stringify({
              type: "input",
              data: 'printf "%s\\n" "$PWD" "$EEA_WORKSPACE"\r',
            }),
          );
        }
        if (value.type === "output") {
          output += value.data;
          if (output.split(second.path).length >= 3) {
            clearTimeout(timeout);
            resolve();
          }
        }
      });
      terminal.once("error", reject);
    });
    terminal.terminate();
    terminal = null;
    await fs.writeFile(
      path.join(second.path, "telemetry.json"),
      JSON.stringify({
        project_name: "Second",
        activity: { status: "running", updated_at: new Date().toISOString() },
      }),
    );
    result = await post("/api/projects/open", {
      path: first,
      projectId: second.id,
    });
    assert.equal(result.status, 409);
    await fs.writeFile(
      path.join(second.path, "telemetry.json"),
      JSON.stringify({ project_name: "Second" }),
    );
    result = await post("/api/projects/new", {
      parentPath: path.dirname(second.path),
      name: "Second",
      projectId: second.id,
    });
    assert.equal(result.status, 400);
    result = await post("/api/projects/open", {
      path: root,
      projectId: second.id,
    });
    assert.equal(result.status, 400);
    result = await post("/api/projects/open", {
      path: first,
      projectId: second.id,
    });
    assert.equal(result.status, 200);
    const reopened = result.data.project;
    assert.equal(reopened.path, first);
    assert.notEqual(reopened.id, initial.active.id);
    const recent = await (await fetch(base + "/api/projects")).json();
    assert.ok(recent.recent.some((item) => item.path === second.path));
    result = await post("/api/exports", { projectId: reopened.id });
    assert.equal(result.status, 200);
    assert.equal(result.data.ready, true);
    assert.deepEqual(exported, [first]);
    const download = await fetch(
      base + result.data.files.find((file) => file.name === "gerbers.zip").url,
    );
    assert.equal(download.status, 200);
    assert.match(download.headers.get("content-disposition"), /attachment/);
    await fs.writeFile(
      path.join(first, "build/board.kicad_pcb"),
      "manually changed board",
    );
    assert.equal(
      (await (await fetch(base + "/api/exports")).json()).ready,
      false,
    );
    assert.equal((await fetch(base + result.data.files[0].url)).status, 409);
    const directories = await (
      await fetch(
        base + `/api/projects/directories?path=${encodeURIComponent(root)}`,
      )
    ).json();
    assert.ok(
      directories.folders.some((item) => item.name === "First project"),
    );
    assert.equal(
      JSON.parse(
        await fs.readFile(path.join(stateDirectory, "projects.json"), "utf8"),
      ).active,
      first,
    );
  } finally {
    socket?.terminate();
    terminal?.terminate();
    if (bridge) await bridge.close();
    await fs.rm(root, { recursive: true, force: true });
  }
});

test(
  "last opened project is restored without an environment override",
  { skip: !!process.env.EEA_WORKSPACE },
  async () => {
    const root = await fs.mkdtemp(
      path.join(os.tmpdir(), "eea-project-restore-"),
    );
    let bridge;
    try {
      const project = path.join(root, "remembered");
      await initializeProject(project, "Remembered");
      const stateDirectory = path.join(root, "state");
      await fs.mkdir(stateDirectory);
      await fs.writeFile(
        path.join(stateDirectory, "projects.json"),
        JSON.stringify({
          active: project,
          recent: [{ name: "Remembered", path: project }],
        }),
      );
      bridge = await startBridge({ stateDirectory, port: 0 });
      const result = await (
        await fetch(
          `http://127.0.0.1:${bridge.server.address().port}/api/projects`,
        )
      ).json();
      assert.equal(result.active.path, project);
    } finally {
      if (bridge) await bridge.close();
      await fs.rm(root, { recursive: true, force: true });
    }
  },
);
