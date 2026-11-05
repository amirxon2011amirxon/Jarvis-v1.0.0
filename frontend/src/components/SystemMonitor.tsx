import { memo, useMemo } from "react";

import type { AzeState, Diagnostics, Metrics, SetupStatus } from "../types";
import { cn } from "../lib/utils";
import { CheckIcon } from "./ui/Icons";

interface SystemMonitorProps {
  metrics: Metrics | null;
  diagnostics: Diagnostics | null;
  state: AzeState;
  muted: boolean;
  setup: SetupStatus;
}

type ServiceStatus = "ok" | "active" | "warn" | "error" | "disabled" | "unknown";

interface ServiceRow {
  id: string;
  name: string;
  subtitle: string;
  status: ServiceStatus;
  statusLabel: string;
  detail: string;
}

const STATUS_CLASS: Record<ServiceStatus, string> = {
  ok: "text-[var(--ok)]",
  active: "text-[var(--accent)]",
  warn: "text-[var(--warn)]",
  error: "text-[var(--danger)]",
  disabled: "text-[var(--dim)]",
  unknown: "text-[var(--muted)]",
};

const DOT_CLASS: Record<ServiceStatus, string> = {
  ok: "bg-[var(--ok)]",
  active: "bg-[var(--accent)]",
  warn: "bg-[var(--warn)]",
  error: "bg-[var(--danger)]",
  disabled: "bg-[var(--dim)]",
  unknown: "bg-[var(--muted)]",
};

/* Service-row logic. Every verdict comes from real state:
 *  - diagnostics checks (real readiness sweep) for voice, model and settings;
 *  - the live voice state for listening/speaking activity;
 *  - "Unknown" until the first diagnostics poll answers — never invented. */
function buildRows(
  metrics: Metrics | null,
  diagnostics: Diagnostics | null,
  state: AzeState,
  muted: boolean,
): ServiceRow[] {
  const rows: ServiceRow[] = [];
  const check = (id: string) =>
    diagnostics?.checks.find((row) => row.id === id) ?? null;

  if (!diagnostics) {
    for (const [index, name] of ["VOICE", "GEMINI", "SETTINGS"].entries()) {
      rows.push({
        id: `unknown-${index}`,
        name,
        subtitle: "",
        status: "unknown",
        statusLabel: "UNKNOWN",
        detail: "Waiting for the first readiness sweep",
      });
    }
    return rows;
  }

  /* ══ Voice ═══════════════════════════════════════════════════════════ */
  const voiceCheck = check("voice");
  const micCheck = check("microphone");
  const outputCheck = check("audio");
  const liveActivity =
    state === "LISTENING" ? "listening"
      : state === "SPEAKING" ? "speaking"
        : state === "MUTED" && muted ? "muted"
          : null;
  const voiceStatus: ServiceStatus =
    voiceCheck?.status === "ok" ? (muted ? "warn" : "ok")
      : voiceCheck?.status === "warn" ? "warn"
        : voiceCheck?.status === "error" ? "error"
          : "unknown";
  rows.push({
    id: "voice",
    name: "VOICE",
    subtitle: "Voice engine",
    status: muted && voiceStatus !== "error" ? "active" : voiceStatus,
    statusLabel:
      liveActivity ? liveActivity.toUpperCase()
        : voiceStatus === "ok" ? "ACTIVE"
          : voiceStatus === "warn" ? "CONNECTING"
            : voiceStatus === "error" ? "OFFLINE"
              : "UNKNOWN",
    detail: [
      micCheck ? `Mic ${micCheck.detail.toLowerCase() || "state unknown"}` : null,
      outputCheck ? `${outputCheck.detail} outputs` : null,
    ].filter(Boolean).join(" · ") || voiceCheck?.detail || "STT/TTS state not reported yet",
  });

  /* ══ Gemini ══════════════════════════════════════════════════════════ */
  const providerCheck = check("provider");
  const sessionCheck = check("session");
  const model = diagnostics.model ?? metrics?.model ?? null;
  const geminiStatus: ServiceStatus =
    sessionCheck?.status === "ok" ? "ok"
      : sessionCheck?.status === "warn" ? "warn"
        : sessionCheck?.status === "error" ? "error"
          : "unknown";
  rows.push({
    id: "gemini",
    name: "GEMINI",
    subtitle: "Google Gemini",
    status: geminiStatus,
    statusLabel:
      geminiStatus === "ok" ? "OPERATIONAL"
        : geminiStatus === "warn" ? "INITIALIZING"
          : geminiStatus === "error" ? "OFFLINE"
            : "UNKNOWN",
    detail: model
      ? `${model}${sessionCheck ? ` · ${sessionCheck.detail.toLowerCase()}` : ""}`
      : providerCheck?.detail ?? "No Live model available",
  });

  /* ══ Settings ════════════════════════════════════════════════════════ */
  const settingsCheck = check("settings");
  const settingsStatus: ServiceStatus =
    settingsCheck?.status === "ok" ? "ok"
      : settingsCheck?.status === "error" ? "error"
        : "unknown";
  rows.push({
    id: "settings",
    name: "SETTINGS",
    subtitle: "Configuration",
    status: settingsStatus,
    statusLabel:
      settingsStatus === "ok" ? "READY"
        : settingsStatus === "error" ? "MISSING CONFIG"
          : "UNKNOWN",
    detail: settingsCheck?.detail ?? "Configuration state not reported yet",
  });

  return rows;
}

export const SystemMonitor = memo(function SystemMonitor({
  metrics,
  diagnostics,
  state,
  muted,
  setup,
}: SystemMonitorProps) {
  void setup; // configured-ness already surfaces through the settings row
  const rows = useMemo(
    () => buildRows(metrics, diagnostics, state, muted),
    [metrics, diagnostics, state, muted],
  );

  return (
    <section className="aze-monitor" aria-label="System Monitor">
      <header className="aze-monitor-head">
        <h2>
          <CheckIcon className="h-3 w-3" />
          SYSTEM MONITOR
        </h2>
        <span className="aze-monitor-code">SRV/01</span>
      </header>
      <div className="aze-monitor-body">
        {rows.map((row) => (
          <div className="aze-system-row" key={row.id} title={row.detail}>
            <span
              className={cn("aze-status-dot aze-system-dot", DOT_CLASS[row.status])}
              aria-hidden="true"
            />
            <span className="aze-system-copy">
              <span className="aze-system-name">
                {row.name}
                {row.subtitle ? <small> · {row.subtitle}</small> : null}
              </span>
              <span className="aze-system-detail">{row.detail}</span>
            </span>
            <span className={cn("aze-system-state", STATUS_CLASS[row.status])}>
              {row.statusLabel}
            </span>
          </div>
        ))}
      </div>
    </section>
  );
});
