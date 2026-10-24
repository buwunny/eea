import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { WebSocket } from "ws";
import { allowedOrigin, artifactPath, startBridge } from "./bridge.js";

test("reject remote origins while accepting the local workbench", () => {
  assert.equal(allowedOrigin("http://localhost:5173"), true);
  assert.equal(allowedOrigin("tauri://localhost"), true);
  assert.equal(allowedOrigin("http://localhost.evil.example"), false);
  assert.equal(allowedOrigin("https://example.com"), false);
  assert.equal(allowedOrigin("null"), false);
});

test("live bridge streams filesystem state and restores build history without moving HEAD", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "eea-bridge-"));
  const exec = promisify(execFile);
  const git = async (...args) =>
    (
      await exec(
        "git",
        ["-c", "user.name=Test", "-c", "user.email=test@localhost", ...args],
        { cwd: root },
      )
    ).stdout.trim();
  let bridge;
  let socket;
  let terminal;
  try {
    await fs.mkdir(path.join(root, "build"));
    await fs.mkdir(path.join(root, "manufacturing"));
    await fs.writeFile(
      path.join(root, "telemetry.json"),
      JSON.stringify({ project_name: "Live fixture" }),
    );
    await git("init", "-q");
    await git("add", "-A");
    await git("commit", "-qm", "Empty circuit");
    const initial = await git("rev-parse", "HEAD");
    await fs.writeFile(path.join(root, "build/board.kicad_pcb"), "version one");
    await git("add", "-A");
    await git("commit", "-qm", "First board");
    const first = await git("rev-parse", "HEAD");
    await fs.writeFile(path.join(root, "build/board.kicad_pcb"), "version two");
    await git("add", "-A");
    await git("commit", "-qm", "Second board");
    const latest = await git("rev-parse", "HEAD");
    bridge = await startBridge({ workspace: root, port: 0 });
    const base = `http://127.0.0.1:${bridge.server.address().port}`;
    const history = await (await fetch(`${base}/api/git/log`)).json();
    assert.equal(history.commits.length, 3);
    assert.equal(
      (
        await fetch(`${base}/api/git/log`, {
          headers: { Origin: "https://evil.example" },
        })
      ).status,
      403,
    );
    assert.equal(
      (await fetch(`${base}/api/git/checkout/invalid`, { method: "POST" }))
        .status,
      400,
    );
    socket = new WebSocket(`${base.replace("http:", "ws:")}/ws/telemetry`, {
      origin: "http://localhost:5173",
    });
    const snapshot = await new Promise((resolve, reject) => {
      const timeout = setTimeout(
        () => reject(new Error("Telemetry snapshot timed out")),
        5000,
      );
      socket.on("message", (data) => {
        const value = JSON.parse(data);
        if (value.file === "telemetry.json") {
          clearTimeout(timeout);
          resolve(value);
        }
      });
      socket.once("error", reject);
    });
    assert.equal(snapshot.type, "FILE_UPDATED");
    assert.equal(snapshot.payload.project_name, "Live fixture");
    const changed = new Promise((resolve, reject) => {
      const timeout = setTimeout(
        () => reject(new Error("Filesystem update timed out")),
        5000,
      );
      socket.on("message", (data) => {
        const value = JSON.parse(data);
        if (value.payload?.project_name === "Updated fixture") {
          clearTimeout(timeout);
          resolve(value);
        }
      });
    });
    await fs.writeFile(
      path.join(root, "telemetry.json"),
      JSON.stringify({ project_name: "Updated fixture" }),
    );
    await changed;
    terminal = new WebSocket(`${base.replace("http:", "ws:")}/ws/terminal`, {
      origin: "http://localhost:5173",
    });
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(
        () => reject(new Error("PTY roundtrip timed out")),
        5000,
      );
      let output = "";
      terminal.on("message", (data) => {
        const value = JSON.parse(data);
        if (value.type === "status" && value.status === "unavailable") {
          clearTimeout(timeout);
          assert.ok(value.message);
          resolve();
        }
        if (value.type === "status" && value.status === "connected") {
          terminal.send(JSON.stringify({ type: "resize", cols: 80, rows: 24 }));
          terminal.send(
            JSON.stringify({
              type: "input",
              data: "printf 'EEA_PTY_%s\\n' READY\r",
            }),
          );
        }
        if (value.type === "output") {
          output += value.data;
          if (output.includes("EEA_PTY_READY")) {
            clearTimeout(timeout);
            resolve();
          }
        }
      });
      terminal.once("error", reject);
    });
    assert.equal(
      (await fetch(`${base}/api/git/checkout/${first}`, { method: "POST" }))
        .status,
      200,
    );
    assert.equal(
      await (await fetch(`${base}/artifacts/board.kicad_pcb`)).text(),
      "version one",
    );
    assert.equal(
      (await fetch(`${base}/api/git/checkout/${initial}`, { method: "POST" }))
        .status,
      200,
    );
    assert.equal(
      (await fetch(`${base}/artifacts/board.kicad_pcb`)).status,
      404,
    );
    assert.equal(
      (await fetch(`${base}/api/git/checkout/latest`, { method: "POST" }))
        .status,
      200,
    );
    assert.equal(
      await (await fetch(`${base}/artifacts/board.kicad_pcb`)).text(),
      "version two",
    );
    assert.equal(await git("rev-parse", "HEAD"), latest);
    // WebViews can reset TCP on reload instead of completing a WS close handshake.
    await new Promise((resolve) => {
      terminal.once("close", resolve);
      terminal._socket.resetAndDestroy();
    });
    assert.equal((await fetch(`${base}/api/git/log`)).status, 200);
  } finally {
    socket?.terminate();
    terminal?.terminate();
    if (bridge) await bridge.close();
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("artifact lookup rejects traversal, hidden files and escaped symlinks", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "eea-artifacts-"));
  try {
    await fs.mkdir(path.join(root, "build"));
    await fs.mkdir(path.join(root, "manufacturing"));
    await fs.writeFile(path.join(root, "build/board.kicad_pcb"), "board");
    await fs.writeFile(path.join(root, "secret"), "private");
    await fs.symlink(
      path.join(root, "secret"),
      path.join(root, "build/escape"),
    );
    assert.equal(
      await artifactPath(root, "board.kicad_pcb"),
      path.join(root, "build/board.kicad_pcb"),
    );
    assert.equal(await artifactPath(root, "../secret"), null);
    assert.equal(await artifactPath(root, "build/escape"), null);
    assert.equal(await artifactPath(root, ".git/config"), null);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});
