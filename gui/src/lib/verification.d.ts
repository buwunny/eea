import type { DrcReport, Simulation } from "../types";
export function drcFindings(report: DrcReport | null): { title: string; severity: string; details: string; position: { x: number; y: number } | null }[];
export function reportMatchesBoard(report: DrcReport | null, digest: string | null): boolean;
export function simulationResult(report: Simulation | null, passed: boolean | null): string;
