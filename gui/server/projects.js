import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const exec = promisify(execFile);
const repo = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
export const defaultProjectsDirectory =
  process.env.EEA_PROJECTS_DIR || path.join(os.homedir(), "eea-projects");
export const defaultStateDirectory =
  process.env.EEA_STATE_DIR ||
  path.join(
    process.env.XDG_DATA_HOME || path.join(os.homedir(), ".local/share"),
    "eea",
  );

export async function readJson(file, fallback = null) {
  try {
    return JSON.parse(await fs.readFile(file, "utf8"));
  } catch (error) {
    if (error.code === "ENOENT") return fallback;
    throw error;
  }
}
export async function describeProject(directory) {
  const metadata = await readJson(path.join(directory, ".eea-project.json"));
  const telemetry = await readJson(path.join(directory, "telemetry.json"), {});
  let name = metadata?.name || telemetry.project_name;
  if (!name || ["EEA", "The Aether-Forge"].includes(name))
    name = path.basename(directory);
  return { path: directory, name };
}
export async function validateProject(directory) {
  if (
    typeof directory !== "string" ||
    !path.isAbsolute(directory) ||
    directory.includes("\0")
  )
    throw new Error("Choose an absolute project folder path");
  const real = await fs.realpath(directory);
  if (!(await fs.stat(real)).isDirectory())
    throw new Error("Choose a project folder");
  const telemetry = await readJson(path.join(real, "telemetry.json"));
  if (!telemetry || typeof telemetry !== "object" || Array.isArray(telemetry))
    throw new Error(
      "This folder is not an EEA project (telemetry.json is missing)",
    );
  const markers = await Promise.all(
    [".eea-project.json", "project_spec.md", "build/design.json"].map((file) =>
      fs
        .stat(path.join(real, file))
        .then((s) => s.isFile())
        .catch(() => false),
    ),
  );
  if (!markers.some(Boolean))
    throw new Error("This folder is not an EEA project");
  return real;
}
export async function initializeProject(directory, name) {
  const args = [
    path.join(repo, "scripts/init_workspace.py"),
    "--workspace",
    directory,
    "--refresh-mcp",
  ];
  if (name) args.push("--name", name);
  await exec(process.env.EEA_PYTHON || "python3", args, {
    timeout: 30000,
    maxBuffer: 1024 * 1024,
  });
  return describeProject(directory);
}
export async function createProject(parent, name) {
  if (
    typeof name !== "string" ||
    !name.trim() ||
    name.length > 80 ||
    /[/\\\x00-\x1f]/.test(name) ||
    [".", ".."].includes(name.trim())
  )
    throw new Error("Enter a project name without path separators");
  if (typeof parent !== "string" || !path.isAbsolute(parent))
    throw new Error("Choose an absolute parent folder path");
  await fs.mkdir(parent, { recursive: true });
  const base = await fs.realpath(parent);
  const directory = path.join(base, name.trim());
  try {
    await fs.mkdir(directory);
  } catch (error) {
    if (error.code === "EEXIST")
      throw new Error(
        "That folder already exists. Open it or choose another project name.",
      );
    throw error;
  }
  return initializeProject(directory, name.trim());
}
export async function projectIsBusy(directory) {
  const data = await readJson(path.join(directory, "telemetry.json"), {});
  return (
    data.activity?.status === "running" &&
    Date.now() - Date.parse(data.activity.updated_at) < 15000
  );
}
export async function directoryListing(requested = defaultProjectsDirectory) {
  if (typeof requested !== "string" || !path.isAbsolute(requested))
    throw new Error("Enter an absolute folder path");
  let directory = requested;
  while (!(await fs.stat(directory).catch(() => null))?.isDirectory()) {
    const parent = path.dirname(directory);
    if (parent === directory) throw new Error("Folder unavailable");
    directory = parent;
  }
  directory = await fs.realpath(directory);
  const entries = await fs.readdir(directory, { withFileTypes: true });
  const folders = entries
    .filter((entry) => !entry.name.startsWith(".") && entry.isDirectory())
    .map((entry) => ({
      name: entry.name,
      path: path.join(directory, entry.name),
    }))
    .sort((a, b) => a.name.localeCompare(b.name));
  return {
    path: directory,
    parent:
      path.dirname(directory) === directory ? null : path.dirname(directory),
    folders,
  };
}
export async function readRegistry(stateDirectory) {
  return readJson(path.join(stateDirectory, "projects.json"), { recent: [] });
}
export async function rememberProject(stateDirectory, project) {
  const old = await readRegistry(stateDirectory);
  const recent = [
    project,
    ...(old.recent || []).filter((item) => item.path !== project.path),
  ].slice(0, 12);
  await fs.mkdir(stateDirectory, { recursive: true });
  const temporary = path.join(stateDirectory, "projects.json.tmp");
  await fs.writeFile(
    temporary,
    JSON.stringify({ active: project.path, recent }, null, 2) + "\n",
  );
  await fs.rename(temporary, path.join(stateDirectory, "projects.json"));
  return recent;
}
