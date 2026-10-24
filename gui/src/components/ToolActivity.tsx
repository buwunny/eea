import { useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  LoaderCircle,
  Wrench,
} from "lucide-react";
import { Telemetry } from "../types";

export default function ToolActivity({
  telemetry,
  connected,
  demo,
}: {
  telemetry: Telemetry;
  connected: boolean;
  demo: boolean;
}) {
  const [now, setNow] = useState(Date.now());
  const [details, setDetails] = useState(false);
  const [warningPage, setWarningPage] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const activity = telemetry.activity;
  const warnings = telemetry.warnings ?? [];
  const warningPages = Math.max(1, Math.ceil(warnings.length / 3));
  const currentWarningPage = Math.min(warningPage, warningPages - 1);
  const running = activity?.status === "running";
  const stale =
    running && (!connected || now - Date.parse(activity.updated_at) > 8000);
  const failed =
    activity?.status === "failed" ||
    (!activity &&
      ((telemetry.metrics.unconnected_items ?? 0) > 0 ||
        (telemetry.metrics.drc_errors ?? 0) > 0));
  const warning =
    activity?.status === "warning" ||
    (!activity && !!telemetry.warnings?.length);
  const elapsed = running
    ? Math.max(0, Math.floor((now - Date.parse(activity.started_at)) / 1000))
    : activity?.elapsed_seconds;
  const status = demo
    ? "Demo"
    : stale
      ? "Updates stopped"
      : running
        ? "Running"
        : failed
          ? "Needs attention"
          : warning
            ? "Completed with warnings"
            : activity
              ? "Finished"
              : "Waiting for MCP";
  const message = demo
    ? "Sample data"
    : stale
      ? `${activity.tool} · check the CLI or reconnect the MCP server`
      : (activity?.message ??
        ((telemetry.metrics.unconnected_items ?? 0) > 0
          ? `${telemetry.metrics.unconnected_items} unrouted connections`
          : telemetry.warnings?.[0]) ??
        "No tool call yet");
  const Icon =
    stale || failed || warning
      ? AlertTriangle
      : running
        ? LoaderCircle
        : activity
          ? CheckCircle2
          : Wrench;
  return (
    <div
      className={`activity-strip ${stale || failed ? "error" : warning ? "caution" : running ? "running" : ""}`}
      role="status"
    >
      <Icon size={17} className={running && !stale ? "activity-spinner" : ""} />
      <strong>{status}</strong>
      <span className="activity-message" title={message}>
        {message}
      </span>
      {elapsed !== undefined && !demo && (
        <span className="activity-time">
          <Clock3 size={13} />
          {Math.round(elapsed)}s
        </span>
      )}
      {!!telemetry.warnings?.length && !demo && (
        <button onClick={() => setDetails(!details)} aria-expanded={details}>
          Details
        </button>
      )}
      {details && !!warnings.length && !demo && (
        <div className="activity-details">
          <button
            className="details-close"
            onClick={() => setDetails(false)}
            aria-label="Close tool details"
          >
            ×
          </button>
          <b>{activity?.tool ?? "Last tool result"}</b>
          <ul>
            {warnings
              .slice(currentWarningPage * 3, currentWarningPage * 3 + 3)
              .map((message, i) => (
                <li key={i} title={message}>
                  {message.length > 160 ? message.slice(0, 160) + "…" : message}
                </li>
              ))}
          </ul>
          {warningPages > 1 && (
            <div className="warning-pages">
              <button
                aria-label="Previous warnings"
                disabled={currentWarningPage === 0}
                onClick={() => setWarningPage(currentWarningPage - 1)}
              >
                Previous
              </button>
              <span>
                {currentWarningPage + 1} / {warningPages}
              </span>
              <button
                aria-label="Next warnings"
                disabled={currentWarningPage === warningPages - 1}
                onClick={() => setWarningPage(currentWarningPage + 1)}
              >
                Next
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
