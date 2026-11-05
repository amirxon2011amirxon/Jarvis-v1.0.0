import { memo, useEffect, useMemo, useRef } from "react";

import type { Metrics } from "../types";
import { CpuIcon } from "./ui/Icons";

interface HardwareMonitorProps {
  metrics: Metrics | null;
}

const HISTORY_POINTS = 56;
const SPARK_W = 100;
const SPARK_H = 30;

function fmtUptime(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h ${minutes}m`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function fmtGigabytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "--";
  return `${value.toFixed(1)} GB`;
}

/* Thin luminous cyan line on a dark plot: an SVG polyline over a client-side
 * ring buffer. The buffer lives in a ref so new samples never re-create
 * history; the component re-renders at the cadence metrics arrive (~2.5s). */
function CpuSparkline({ history }: { history: number[] }) {
  const points = useMemo(() => {
    const blank = history.length < 2;
    const step = SPARK_W / Math.max(1, HISTORY_POINTS - 1);
    const baseline = blank ? [0.5] : history;
    const offset = blank ? HISTORY_POINTS - 1 : HISTORY_POINTS - history.length;
    return baseline
      .map((value, index) => {
        const x = (offset + index) * step;
        const y = SPARK_H - (Math.max(0, Math.min(100, value)) / 100) * (SPARK_H - 2) - 1;
        return `${x.toFixed(2)},${y.toFixed(2)}`;
      })
      .join(" ");
  }, [history]);

  return (
    <svg
      className="aze-hw-spark"
      viewBox={`0 0 ${SPARK_W} ${SPARK_H}`}
      preserveAspectRatio="none"
      role="img"
      aria-label="CPU utilisation history"
    >
      <polyline className="aze-hw-spark-area" points={`0,${SPARK_H} ${points} ${SPARK_W},${SPARK_H}`} />
      <polyline className="aze-hw-spark-line" points={points} />
    </svg>
  );
}

function Meter({ percent, tone }: { percent: number; tone: string }) {
  const safe = Math.max(0, Math.min(100, percent));
  return (
    <div className="aze-meter-track aze-hw-meter-track" aria-hidden="true">
      <span
        className={`aze-meter-fill ${tone}`}
        style={{ width: `${safe}%` }}
      />
    </div>
  );
}

export const HardwareMonitor = memo(function HardwareMonitor({ metrics }: HardwareMonitorProps) {
  const historyRef = useRef<number[]>([]);
  const lastStamp = useRef(0);

  /* Append real samples only — a metrics object without fresh data (or a
   * duplicate timestamp when the server switches) never distorts the graph. */
  const cpu = typeof metrics?.cpu === "number" ? metrics.cpu : null;
  const stamp = metrics?.uptime_s ?? null;
  useEffect(() => {
    if (cpu === null || stamp === null) return;
    if (lastStamp.current === stamp) return;
    lastStamp.current = stamp;
    const next = historyRef.current.length >= HISTORY_POINTS
      ? historyRef.current.slice(-HISTORY_POINTS + 1)
      : historyRef.current.slice();
    next.push(cpu);
    historyRef.current = next;
  }, [cpu, stamp]);

  const freq = metrics?.freq_ghz ?? -1;
  const temp = metrics?.cpu_temp ?? -1;
  const memPct = typeof metrics?.mem === "number" ? metrics.mem : null;
  const memUsed = metrics?.mem_used_gb;
  const memTotal = metrics?.mem_total_gb;
  const drives = Array.isArray(metrics?.drives) ? metrics.drives : [];
  const osName = metrics?.os_name;
  const osVersion = metrics?.os_version;
  const uptime = metrics?.uptime_s;

  return (
    <section className="aze-monitor" aria-label="Hardware Monitor">
      <header className="aze-monitor-head">
        <h2>
          <CpuIcon className="h-3 w-3" />
          HARDWARE MONITOR
        </h2>
        <span className="aze-monitor-code">HW/01</span>
      </header>
      <div className="aze-monitor-body aze-hw-body">
        {/* ── CPU ─────────────────────────────────────────────────────── */}
        <div className="aze-hw-cpu">
          <div className="aze-hw-cpu-head">
            <span className="aze-hw-label">CPU</span>
            <span className="aze-hw-value">
              {cpu === null ? "--" : `${Math.round(cpu)}%`}
              {freq > 0 ? <small> · {freq.toFixed(2)} GHz</small> : null}
              {temp > 0 ? (
                <small className="text-[var(--muted)]"> · {Math.round(temp)}°C</small>
              ) : null}
            </span>
          </div>
          <CpuSparkline history={[...historyRef.current]} />
        </div>

        {/* ── RAM ─────────────────────────────────────────────────────── */}
        <div className="aze-hw-block">
          <div className="aze-hw-row">
            <span className="aze-hw-label">RAM</span>
            <span className="aze-hw-value">
              {memPct === null
                ? "--"
                : typeof memUsed === "number" && typeof memTotal === "number"
                  ? `${Math.round(memPct)}% · ${fmtGigabytes(memUsed)}/${fmtGigabytes(memTotal)}`
                  : `${Math.round(memPct)}%`}
            </span>
          </div>
          <Meter percent={memPct ?? 0} tone="aze-hw-fill--accent" />
        </div>

        {/* ── Drives ──────────────────────────────────────────────────── */}
        <div className="aze-hw-block aze-hw-drives">
          {drives.length === 0 ? (
            <p className="aze-empty-note">No drive inventory reported yet.</p>
          ) : (
            drives.map((drive) => (
              <div key={drive.letter} className="aze-hw-drive" title={drive.letter}>
                <span className="aze-hw-drive-label">{drive.letter}</span>
                <Meter percent={drive.percent} tone="aze-hw-fill--quiet" />
                <span className="aze-hw-drive-value">
                  {fmtGigabytes(drive.used_gb)}/{fmtGigabytes(drive.total_gb)}
                </span>
              </div>
            ))
          )}
        </div>

        {/* ── OS ──────────────────────────────────────────────────────── */}
        <div className="aze-hw-os">
          <span className="aze-hw-os-name">
            {osName ? `${osName}${osVersion ? ` · ${osVersion}` : ""}` : "Operating system not reported"}
          </span>
          {typeof uptime === "number" ? (
            <span className="aze-hw-os-uptime">Uptime · {fmtUptime(uptime)}</span>
          ) : null}
        </div>
      </div>
    </section>
  );
});
