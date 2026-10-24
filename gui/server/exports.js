import fs from "node:fs/promises";
import path from "node:path";
import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import { readJson } from "./projects.js";
const exec = promisify(execFile);
const repo = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
export async function exportInfo(workspace, projectId) {
  const board = await fs
    .readFile(path.join(workspace, "build/board.kicad_pcb"))
    .catch(() => null);
  const report = await readJson(
    path.join(workspace, "manufacturing/export_report.json"),
  );
  const drc = await readJson(path.join(workspace, "build/drc_report.json"));
  const readiness = await readJson(path.join(workspace, "build/readiness.json"));
  const digest = board
    ? createHash("sha256").update(board).digest("hex")
    : null;
  const design = await fs.readFile(path.join(workspace, "build/design.json")).catch(() => null);
  const designDigest = design ? createHash("sha256").update(design).digest("hex") : null;
  const files = [];
  for (const [name, label] of [
    ["gerbers.zip", "Gerbers (.zip)"],
    ["bom.csv", "BOM (.csv)"],
    ["cpl.csv", "Pick-and-place (.csv)"],
  ]) {
    const stat = await fs
      .stat(path.join(workspace, "manufacturing", name))
      .catch(() => null);
    if (stat?.isFile())
      files.push({
        name,
        label,
        size: stat.size,
        url: `/artifacts/manufacturing/${name}?project=${encodeURIComponent(projectId)}&t=${stat.mtimeMs}`,
      });
  }
  const ready =
    !!board &&
    report?.manufacturing_ready === true &&
    report.board_sha256 === digest &&
    (!Object.hasOwn(report, "source_design_sha256") || report.source_design_sha256 === designDigest) &&
    drc?.verified === true &&
    drc.board_sha256 === digest &&
    drc.drc_errors === 0 &&
    drc.unconnected_items === 0 &&
    files.length === 3;
  return {
    projectId,
    hasBoard: !!board,
    ready,
    files: ready ? files : [],
    report,
    readiness: readiness?.board_sha256 === digest && readiness?.design_sha256 === designDigest ? readiness : null,
  };
}
export async function generateExports(workspace) {
  const venv = path.join(
    repo,
    "mcp-server/.venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );
  const python =
    process.env.EEA_PYTHON ||
    (await fs
      .access(venv)
      .then(() => venv)
      .catch(() => "python3"));
  const { stdout } = await exec(
    python,
    [path.join(repo, "mcp-server/export_project.py"), "--workspace", workspace],
    {
      cwd: repo,
      env: { ...process.env, EEA_WORKSPACE: workspace },
      timeout: 360000,
      maxBuffer: 2 * 1024 * 1024,
    },
  );
  return JSON.parse(stdout.trim());
}
