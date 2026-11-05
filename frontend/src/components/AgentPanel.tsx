import { useEffect, useMemo, useState, type ReactNode } from "react";

import type { AgentRunSnapshot, ToolEvent, ToolStatus } from "../types";
import { cn, humanizeTool } from "../lib/utils";
import {
  BotIcon,
  XIcon,
} from "./ui/Icons";

type TabId = "overview" | "activity" | "tools" | "verification" | "statistics";

const TABS: Array<{ id: TabId; label: string }> = [
  { id: "overview", label: "OVERVIEW" },
  { id: "activity", label: "ACTIVITY" },
  { id: "tools", label: "TOOLS" },
  { id: "verification", label: "VERIFICATION" },
  { id: "statistics", label: "STATISTICS" },
];

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);

function isTerminal(run: AgentRunSnapshot): boolean {
  return Boolean(run.is_terminal) || TERMINAL_STATUSES.has(run.status);
}

const STATUS_LABEL: Record<AgentRunSnapshot["status"], string> = {
  in_progress: "IN PROGRESS",
  needs_user: "AWAITING USER",
  completed: "COMPLETED",
  blocked: "BLOCKED",
  failed: "FAILED",
  cancelled: "CANCELLED",
};

const TOOL_STATUS_LABEL: Record<ToolStatus, string> = {
  queued: "QUEUED",
  pending: "PENDING",
  running: "RUNNING",
  success: "SUCCESS",
  failed: "FAILED",
  cancelled: "CANCELLED",
  unknown: "UNKNOWN",
};

type Tone = "accent" | "ok" | "warn" | "bad" | "idle";

interface AgentPanelProps {
  run: AgentRunSnapshot | null;
  toolEvents: ToolEvent[];
  onClose: () => void;
}

interface ToolRow {
  id: string;
  tool: string;
  status: ToolStatus;
  at: string;
  durationMs: number | null;
  target: string;
  summary: string;
  pending: boolean;
}

interface TimelineEntry {
  id: string;
  at: string;
  kind: string;
  title: string;
  detail: string;
  meta: string;
  tone: Tone;
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "--:--:--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return "Not reported";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function formatElapsed(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  if (hours) return `${hours}h ${minutes}m ${rest}s`;
  if (minutes) return `${minutes}m ${rest}s`;
  return `${rest}s`;
}

function formatDuration(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms)) return "—";
  if (ms >= 60000) return `${(ms / 60000).toFixed(1)}m`;
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.max(0, Math.round(ms))}ms`;
}

function textOrUnavailable(value: string | null | undefined): string {
  const text = String(value ?? "").trim();
  return text || "Not reported";
}

function statusLabel(run: AgentRunSnapshot): string {
  return STATUS_LABEL[run.status] ?? run.status.toUpperCase();
}

function statusTone(run: AgentRunSnapshot): Tone {
  switch (run.status) {
    case "completed":
      return "ok";
    case "failed":
      return "bad";
    case "needs_user":
    case "blocked":
      return "warn";
    case "cancelled":
      return "idle";
    default:
      return "accent";
  }
}

function toolStatusOf(event: ToolEvent): ToolStatus {
  if (event.status) return event.status;
  if (event.phase === "start") return "running";
  if (event.pending) return "pending";
  return event.ok ? "success" : "failed";
}

function toolTone(status: ToolStatus): Tone {
  switch (status) {
    case "success":
      return "ok";
    case "failed":
      return "bad";
    case "running":
      return "accent";
    case "pending":
    case "queued":
      return "warn";
    default:
      return "idle";
  }
}

function isTimeout(row: ToolRow): boolean {
  return row.status === "failed" && /timed?\s*out|timeout/i.test(row.summary);
}

function toolStatusLabel(row: ToolRow): string {
  if (isTimeout(row)) return "TIMEOUT";
  return TOOL_STATUS_LABEL[row.status] ?? "UNKNOWN";
}

function timeValue(value: string): number {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? Number.POSITIVE_INFINITY : parsed;
}

/**
 * Tool events are session-wide. A run only owns the calls inside its own
 * window, with a small allowance for the `task_status start` call that
 * precedes the run record by a few milliseconds.
 */
function scopedToolEvents(
  run: AgentRunSnapshot,
  events: ToolEvent[],
): ToolEvent[] {
  const start = Date.parse(run.started_at);
  const finish = run.finished_at ? Date.parse(run.finished_at) : Number.NaN;
  const lower = Number.isFinite(start) ? start - 2_000 : Number.NEGATIVE_INFINITY;
  const upper = Number.isFinite(finish) ? finish + 2_000 : Number.POSITIVE_INFINITY;
  return events.filter((event) => {
    const at = Date.parse(event.ended_at ?? event.started_at);
    if (Number.isNaN(at)) return true;
    return at >= lower && at <= upper;
  });
}

function buildToolRows(
  run: AgentRunSnapshot,
  events: ToolEvent[],
): { rows: ToolRow[]; source: "events" | "records" } {
  const scoped = scopedToolEvents(run, events);
  if (scoped.length) {
    const rows = scoped.map((event) => {
      const status = toolStatusOf(event);
      return {
        id: event.id,
        tool: event.tool,
        status,
        at: event.ended_at ?? event.started_at,
        durationMs: event.phase === "end" && Number.isFinite(event.ms)
          ? event.ms
          : null,
        target: event.target || "",
        summary: event.summary || "",
        pending: Boolean(event.pending),
      };
    });
    rows.sort((a, b) => timeValue(b.at) - timeValue(a.at));
    return { rows, source: "events" };
  }

  const failures = run.failures ?? [];
  const rows = (run.tool_outcomes ?? []).map((outcome, index) => {
    const failure =
      failures.find(
        (item) => item.tool === outcome.tool && item.error === outcome.summary,
      ) ?? failures.find((item) => item.tool === outcome.tool);
    return {
      id: `record-${index}-${outcome.at}`,
      tool: outcome.tool,
      status: (outcome.ok ? "success" : "failed") as ToolStatus,
      at: outcome.at,
      durationMs: null,
      target: "",
      summary: outcome.summary || failure?.error || "",
      pending: false,
    };
  });
  rows.sort((a, b) => timeValue(b.at) - timeValue(a.at));
  return { rows, source: "records" };
}

function buildTimeline(run: AgentRunSnapshot): TimelineEntry[] {
  const entries: TimelineEntry[] = [];

  (run.steps ?? []).forEach((step, index) => {
    entries.push({
      id: `step-${index}-${step.at}`,
      at: step.at,
      kind: "STEP",
      title: `Step ${index + 1}`,
      detail: textOrUnavailable(step.step),
      meta: "",
      tone: "accent",
    });
  });

  (run.tool_outcomes ?? []).forEach((outcome, index) => {
    entries.push({
      id: `tool-${index}-${outcome.at}`,
      at: outcome.at,
      kind: outcome.ok ? "TOOL" : "FAILED",
      title: humanizeTool(outcome.tool || "TOOL"),
      detail: textOrUnavailable(outcome.summary),
      meta: outcome.category ? `CATEGORY · ${outcome.category}` : "",
      tone: outcome.ok ? "ok" : "bad",
    });
  });

  (run.failures ?? []).forEach((failure, index) => {
    const flags = [
      failure.recoverable ? "RECOVERABLE" : "NOT RECOVERABLE",
      failure.requires_user ? "USER ACTION REQUIRED" : "",
    ].filter(Boolean);
    entries.push({
      id: `recovery-${index}-${failure.at}`,
      at: failure.at,
      kind: "RECOVERY",
      title: humanizeTool(failure.tool || "TOOL"),
      detail: textOrUnavailable(failure.hint || failure.error),
      meta: [failure.category, ...flags].filter(Boolean).join(" · "),
      tone: failure.requires_user ? "warn" : "accent",
    });
  });

  (run.verification?.evidence ?? []).forEach((evidence, index) => {
    entries.push({
      id: `evidence-${index}-${evidence.at}`,
      at: evidence.at,
      kind: "EVIDENCE",
      title: evidence.tool ? humanizeTool(evidence.tool) : "EVIDENCE",
      detail: textOrUnavailable(evidence.summary),
      meta: [evidence.kind, evidence.source].filter(Boolean).join(" · "),
      tone: "ok",
    });
  });

  if (isTerminal(run)) {
    entries.push({
      id: `terminal-${run.status}-${run.finished_at ?? run.updated_at}`,
      at: run.finished_at ?? run.updated_at,
      kind: statusLabel(run),
      title:
        textOrUnavailable(run.result_summary) === "Not reported"
          ? `${statusLabel(run)} RUN`
          : textOrUnavailable(run.result_summary),
      detail:
        run.status === "failed" || run.status === "cancelled"
          ? textOrUnavailable(run.last_error)
          : "",
      meta: run.finished_at ? `FINISHED · ${formatDateTime(run.finished_at)}` : "",
      tone: statusTone(run),
    });
  }

  return entries.sort((a, b) => timeValue(a.at) - timeValue(b.at)).slice(-250);
}

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

function useElapsed(run: AgentRunSnapshot | null, now: number): number | null {
  return useMemo(() => {
    if (!run) return null;
    if (isTerminal(run)) {
      return Number.isFinite(run.elapsed_seconds) ? run.elapsed_seconds : null;
    }
    const started = Date.parse(run.started_at);
    if (!Number.isNaN(started)) {
      return Math.max(0, (now - started) / 1000);
    }
    return Number.isFinite(run.elapsed_seconds) ? run.elapsed_seconds : null;
  }, [run, now]);
}

function listNote(
  values: unknown[] | undefined,
  empty: string,
  unavailable: string,
): string {
  if (values === undefined) return unavailable;
  return values.length === 0 ? empty : "";
}

function StatusBadge({
  label,
  tone,
  className,
}: {
  label: string;
  tone: Tone;
  className?: string;
}) {
  return (
    <span className={cn("aze-agent-badge", `aze-agent-tone--${tone}`, className)}>
      {label}
    </span>
  );
}

function Metric({
  label,
  value,
  note,
  tone,
}: {
  label: string;
  value: ReactNode;
  note?: string;
  tone?: Tone;
}) {
  return (
    <article className="aze-agent-metric" role="group" aria-label={label}>
      <span className="aze-agent-metric-label">{label}</span>
      <strong className={cn("aze-agent-metric-value", tone && `aze-agent-text--${tone}`)}>
        {value}
      </strong>
      {note ? <span className="aze-agent-metric-note">{note}</span> : null}
    </article>
  );
}

function UsageBar({
  used,
  limit,
  label,
}: {
  used: number;
  limit: number;
  label: string;
}) {
  const ratio = limit > 0 ? Math.min(1, Math.max(0, used / limit)) : 0;
  const tone = ratio >= 1 ? "bad" : ratio >= 0.8 ? "warn" : "accent";
  return (
    <div className="aze-agent-usage" aria-label={`${label}: ${used} of ${limit}`}>
      <span className="aze-agent-usage-track">
        <span
          className={cn("aze-agent-usage-fill", `aze-agent-bg--${tone}`)}
          style={{ width: `${Math.round(ratio * 100)}%` }}
        />
      </span>
    </div>
  );
}

function Overview({
  run,
  elapsed,
}: {
  run: AgentRunSnapshot;
  elapsed: number | null;
}) {
  const steps = run.steps;
  const toolLimit = Number(run.limits?.max_tool_calls);
  const toolUsage = Number.isFinite(toolLimit) && toolLimit > 0
    ? `${run.tool_call_count} / ${toolLimit}`
    : `${run.tool_call_count}`;

  return (
    <div className="aze-agent-stack">
      <section className="aze-agent-metric-grid" aria-label="Run overview">
        <Metric label="STATUS" value={statusLabel(run)} tone={statusTone(run)} />
        <Metric label="ELAPSED" value={formatElapsed(elapsed)} />
        <Metric
          label="CURRENT STEP"
          value={textOrUnavailable(run.current_step)}
          note={run.updated_at ? `Updated ${formatDateTime(run.updated_at)}` : undefined}
        />
        <Metric
          label="TOOL CALLS"
          value={toolUsage}
          note={Number.isFinite(toolLimit) && toolLimit > 0 ? "USED / LIMIT" : "LIMIT NOT REPORTED"}
        />
        <Metric
          label="REMAINING WORK"
          value={textOrUnavailable(run.remaining_work)}
          note={run.remaining_work ? undefined : "No remaining-work note reported"}
        />
      </section>

      {Number.isFinite(toolLimit) && toolLimit > 0 ? (
        <section className="aze-agent-section" aria-label="Tool call usage">
          <header className="aze-agent-section-head">
            <h3>TOOL-CALL USAGE</h3>
            <span>{run.tool_call_count} of {toolLimit} calls</span>
          </header>
          <UsageBar
            used={run.tool_call_count}
            limit={toolLimit}
            label="Tool-call usage"
          />
        </section>
      ) : null}

      {run.result_summary ? (
        <section className="aze-agent-section">
          <header className="aze-agent-section-head">
            <h3>RESULT</h3>
          </header>
          <p className="aze-agent-prose">{run.result_summary}</p>
        </section>
      ) : null}

      {run.last_error ? (
        <section className="aze-agent-section">
          <header className="aze-agent-section-head">
            <h3>LAST ERROR</h3>
          </header>
          <p className="aze-agent-prose aze-agent-text--bad">{run.last_error}</p>
        </section>
      ) : null}

      <section className="aze-agent-section">
        <header className="aze-agent-section-head">
          <h3>COMPLETED STEPS</h3>
          <span>
            {steps === undefined ? "NOT REPORTED" : `${steps.length} RECORDED`}
          </span>
        </header>
        {steps === undefined ? (
          <p className="aze-agent-note">
            Step history is not reported by this backend snapshot.
          </p>
        ) : steps.length === 0 ? (
          <p className="aze-agent-note">No steps have been recorded yet.</p>
        ) : (
          <ol className="aze-agent-steps">
            {steps.map((step, index) => (
              <li key={`${step.at}-${index}`} className="aze-agent-step">
                <span className="aze-agent-step-index">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <span className="aze-agent-step-main">
                  <span className="aze-agent-step-label">
                    {textOrUnavailable(step.step)}
                  </span>
                  <span className="aze-agent-step-time">{formatTime(step.at)}</span>
                </span>
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}

function Activity({ run }: { run: AgentRunSnapshot }) {
  const entries = useMemo(() => buildTimeline(run), [run]);
  return (
    <div className="aze-agent-stack">
      <header className="aze-agent-section-head">
        <h3>EXECUTION TIMELINE</h3>
        <span>{entries.length} EVENTS</span>
      </header>
      {entries.length === 0 ? (
        <p className="aze-agent-empty-inline">
          No activity has been reported for this run yet.
        </p>
      ) : (
        <ol className="aze-agent-events" aria-label="Run activity timeline">
          {entries.map((entry) => (
            <li key={entry.id} className="aze-agent-event">
              <span
                className={cn("aze-agent-event-dot", `aze-agent-bg--${entry.tone}`)}
                aria-hidden="true"
              />
              <div className="aze-agent-event-body">
                <div className="aze-agent-event-head">
                  <span className="aze-agent-event-time">{formatTime(entry.at)}</span>
                  <span className={cn("aze-agent-event-kind", `aze-agent-text--${entry.tone}`)}>
                    {entry.kind}
                  </span>
                  <span className="aze-agent-event-title">{entry.title}</span>
                </div>
                {entry.detail ? (
                  <p className="aze-agent-event-detail">{entry.detail}</p>
                ) : null}
                {entry.meta ? (
                  <span className="aze-agent-event-meta">{entry.meta}</span>
                ) : null}
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function Tools({
  run,
  toolEvents,
}: {
  run: AgentRunSnapshot;
  toolEvents: ToolEvent[];
}) {
  const { rows, source } = useMemo(
    () => buildToolRows(run, toolEvents),
    [run, toolEvents],
  );
  const recorded = run.tool_outcomes;

  return (
    <div className="aze-agent-stack">
      <header className="aze-agent-section-head">
        <h3>TOOL EXECUTIONS</h3>
        <span>{rows.length} CALL{rows.length === 1 ? "" : "S"}</span>
      </header>

      {source === "records" && recorded?.length ? (
        <p className="aze-agent-note">
          Detailed execution events are unavailable for this run. Showing the
          recorded tool outcomes and their bounded result summaries.
        </p>
      ) : null}

      {rows.length === 0 ? (
        <p className="aze-agent-empty-inline">
          {recorded === undefined
            ? "Tool execution history is not reported by this backend snapshot."
            : "No tool executions have been recorded for this run."}
        </p>
      ) : (
        <div className="aze-agent-tools" role="table" aria-label="Tool executions">
          <div className="aze-agent-tools-head" role="row">
            <span role="columnheader">TIME</span>
            <span role="columnheader">TOOL</span>
            <span role="columnheader">STATUS</span>
            <span role="columnheader">DURATION</span>
            <span role="columnheader">TARGET / ARGUMENT</span>
            <span role="columnheader">BOUNDED RESULT</span>
          </div>
          {rows.map((row) => (
            <div key={row.id} className="aze-agent-tool-row" role="row">
              <span className="aze-agent-tool-cell" role="cell" data-label="TIME">
                {formatTime(row.at)}
              </span>
              <span className="aze-agent-tool-cell aze-agent-tool-name" role="cell" data-label="TOOL">
                {humanizeTool(row.tool || "TOOL")}
              </span>
              <span className="aze-agent-tool-cell" role="cell" data-label="STATUS">
                <StatusBadge
                  label={toolStatusLabel(row)}
                  tone={toolTone(row.status)}
                />
              </span>
              <span className="aze-agent-tool-cell" role="cell" data-label="DURATION">
                {formatDuration(row.durationMs)}
              </span>
              <span
                className="aze-agent-tool-cell aze-agent-tool-target"
                role="cell"
                data-label="TARGET / ARGUMENT"
                title={row.target || undefined}
              >
                {row.target || "Not reported"}
              </span>
              <span
                className={cn(
                  "aze-agent-tool-cell aze-agent-tool-result",
                  row.status === "failed" && "aze-agent-text--bad",
                )}
                role="cell"
                data-label="BOUNDED RESULT"
                title={row.summary || undefined}
              >
                {row.summary || (row.status === "running"
                  ? "In progress…"
                  : row.pending
                    ? "Awaiting the user's decision."
                    : "Not reported")}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function verificationState(run: AgentRunSnapshot): {
  label: string;
  tone: Tone;
  detail: string;
} {
  const verification = run.verification;
  if (!verification) {
    return {
      label: "NOT REPORTED",
      tone: "idle",
      detail: "Verification state is not reported by this backend snapshot.",
    };
  }
  if (!verification.required) {
    return {
      label: "NOT REQUIRED",
      tone: "idle",
      detail: verification.reason || "No evidence was required for this objective.",
    };
  }
  if (verification.exhausted) {
    return {
      label: "EXHAUSTED",
      tone: "bad",
      detail: "Verification attempts were exhausted before the run finished.",
    };
  }
  if (verification.missing.length > 0) {
    return {
      label: "MISSING EVIDENCE",
      tone: "warn",
      detail: "The run still needs evidence before completion can be verified.",
    };
  }
  if (run.status === "completed") {
    return {
      label: "VERIFIED",
      tone: "ok",
      detail: verification.reason || "All required evidence was collected.",
    };
  }
  return {
    label: "EVIDENCE COMPLETE",
    tone: "ok",
    detail: verification.reason || "No required evidence is currently missing.",
  };
}

function Verification({ run }: { run: AgentRunSnapshot }) {
  const verification = run.verification;
  const state = verificationState(run);
  const evidence = verification?.evidence;
  const missing = verification?.missing;
  const blockers = run.blockers;
  const failed = run.failed_operations;
  const failures = run.failures;

  return (
    <div className="aze-agent-stack">
      <section className="aze-agent-verdict" aria-label="Verification status">
        <div>
          <span className="aze-agent-metric-label">VERIFICATION STATUS</span>
          <strong className={cn("aze-agent-verdict-label", `aze-agent-text--${state.tone}`)}>
            {state.label}
          </strong>
          <p className="aze-agent-verdict-detail">{state.detail}</p>
        </div>
        <div className="aze-agent-verdict-counts">
          <span>
            <strong>{evidence === undefined ? "—" : evidence.length}</strong>
            EVIDENCE
          </span>
          <span>
            <strong>{missing === undefined ? "—" : missing.length}</strong>
            MISSING
          </span>
          <span>
            <strong>{verification ? verification.attempts : "—"}</strong>
            ATTEMPTS
          </span>
          <span>
            <strong>
              {verification ? (verification.exhausted ? "YES" : "NO") : "—"}
            </strong>
            EXHAUSTED
          </span>
        </div>
      </section>

      <section className="aze-agent-section">
        <header className="aze-agent-section-head">
          <h3>EVIDENCE COLLECTED</h3>
          <span>{evidence === undefined ? "NOT REPORTED" : `${evidence.length} ITEMS`}</span>
        </header>
        {listNote(evidence, "No evidence has been collected.", "Evidence is not reported by this backend snapshot.") ? (
          <p className="aze-agent-note">
            {listNote(evidence, "No evidence has been collected.", "Evidence is not reported by this backend snapshot.")}
          </p>
        ) : (
          <ul className="aze-agent-list">
            {evidence?.map((item, index) => (
              <li key={`${item.at}-${index}`} className="aze-agent-list-item">
                <div className="aze-agent-list-head">
                  <span className="aze-agent-list-title">
                    {item.tool ? humanizeTool(item.tool) : item.kind || "Evidence"}
                  </span>
                  <span className="aze-agent-list-time">{formatTime(item.at)}</span>
                </div>
                <p className="aze-agent-list-detail">{textOrUnavailable(item.summary)}</p>
                <span className="aze-agent-list-meta">
                  {[item.kind, item.source].filter(Boolean).join(" · ")}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="aze-agent-section">
        <header className="aze-agent-section-head">
          <h3>MISSING EVIDENCE</h3>
          <span>{missing === undefined ? "NOT REPORTED" : `${missing.length} ITEMS`}</span>
        </header>
        {listNote(missing, "No required evidence is missing.", "Missing evidence is not reported by this backend snapshot.") ? (
          <p className="aze-agent-note">
            {listNote(missing, "No required evidence is missing.", "Missing evidence is not reported by this backend snapshot.")}
          </p>
        ) : (
          <ul className="aze-agent-list aze-agent-list--warn">
            {missing?.map((item) => (
              <li key={item} className="aze-agent-list-item">
                <span className="aze-agent-list-detail">{item}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="aze-agent-section">
        <header className="aze-agent-section-head">
          <h3>BLOCKERS</h3>
          <span>{blockers === undefined ? "NOT REPORTED" : `${blockers.length} ITEMS`}</span>
        </header>
        {listNote(blockers, "No blockers are recorded.", "Blockers are not reported by this backend snapshot.") ? (
          <p className="aze-agent-note">
            {listNote(blockers, "No blockers are recorded.", "Blockers are not reported by this backend snapshot.")}
          </p>
        ) : (
          <ul className="aze-agent-list aze-agent-list--bad">
            {blockers?.map((item) => (
              <li key={item} className="aze-agent-list-item">
                <span className="aze-agent-list-detail">{item}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="aze-agent-section">
        <header className="aze-agent-section-head">
          <h3>FAILED OPERATIONS</h3>
          <span>{failed === undefined ? "NOT REPORTED" : `${failed.length} ITEMS`}</span>
        </header>
        {listNote(failed, "No failed operations are recorded.", "Failed operations are not reported by this backend snapshot.") ? (
          <p className="aze-agent-note">
            {listNote(failed, "No failed operations are recorded.", "Failed operations are not reported by this backend snapshot.")}
          </p>
        ) : (
          <ul className="aze-agent-list">
            {failed?.map((item, index) => (
              <li key={`${item.tool}-${index}`} className="aze-agent-list-item">
                <div className="aze-agent-list-head">
                  <span className="aze-agent-list-title">{humanizeTool(item.tool)}</span>
                  <StatusBadge label={(item.category || "failed").toUpperCase()} tone="bad" />
                </div>
                <p className="aze-agent-list-detail">{textOrUnavailable(item.last_error)}</p>
                <span className="aze-agent-list-meta">
                  {item.count} ATTEMPT{item.count === 1 ? "" : "S"} ·{" "}
                  {item.retry_same_call ? "SAME CALL RETRY ALLOWED" : "SAME CALL RETRY BLOCKED"} ·{" "}
                  {item.recoverable ? "RECOVERABLE" : "NOT RECOVERABLE"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="aze-agent-section">
        <header className="aze-agent-section-head">
          <h3>RECOVERY ATTEMPTS</h3>
          <span>{failures === undefined ? "NOT REPORTED" : `${failures.length} FAILURES`}</span>
        </header>
        {listNote(failures, "No recovery guidance has been recorded.", "Recovery attempts are not reported by this backend snapshot.") ? (
          <p className="aze-agent-note">
            {listNote(failures, "No recovery guidance has been recorded.", "Recovery attempts are not reported by this backend snapshot.")}
          </p>
        ) : (
          <ul className="aze-agent-list">
            {failures?.map((item, index) => (
              <li key={`${item.tool}-${item.at}-${index}`} className="aze-agent-list-item">
                <div className="aze-agent-list-head">
                  <span className="aze-agent-list-title">{humanizeTool(item.tool)}</span>
                  <span className="aze-agent-list-time">{formatTime(item.at)}</span>
                </div>
                <p className="aze-agent-list-detail">{textOrUnavailable(item.error)}</p>
                <span className="aze-agent-list-meta">
                  {[item.category, item.recoverable ? "RECOVERABLE" : "NOT RECOVERABLE",
                    item.requires_user ? "USER ACTION" : ""].filter(Boolean).join(" · ")}
                </span>
                {item.hint ? (
                  <p className="aze-agent-recovery-hint">
                    <span>GUIDANCE</span> {item.hint}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function Statistics({
  run,
  elapsed,
}: {
  run: AgentRunSnapshot;
  elapsed: number | null;
}) {
  const successful = typeof run.successful_tool_calls === "number"
    ? run.successful_tool_calls
    : undefined;
  const failed = typeof run.failed_tool_calls === "number"
    ? run.failed_tool_calls
    : undefined;
  const limits = run.limits;

  return (
    <div className="aze-agent-stack">
      <section className="aze-agent-metric-grid aze-agent-metric-grid--stats" aria-label="Run statistics">
        <Metric label="TOTAL STEPS" value={run.step_count} />
        <Metric label="TOTAL TOOL CALLS" value={run.tool_call_count} />
        <Metric
          label="SUCCESSFUL CALLS"
          value={successful === undefined ? "—" : successful}
          note={successful === undefined ? "Not reported" : "BACKEND COUNTER"}
        />
        <Metric
          label="FAILED CALLS"
          value={failed === undefined ? "—" : failed}
          note={failed === undefined ? "Not reported" : "BACKEND COUNTER"}
        />
        <Metric
          label="VERIFICATION ATTEMPTS"
          value={run.verification ? run.verification.attempts : "—"}
          note={run.verification ? undefined : "Not reported"}
        />
        <Metric label="ELAPSED" value={formatElapsed(elapsed)} />
      </section>

      <section className="aze-agent-section">
        <header className="aze-agent-section-head">
          <h3>CONSECUTIVE FAILURES</h3>
          <span>
            {run.consecutive_failures} RECORDED
          </span>
        </header>
        <p className="aze-agent-note">
          Current streak of consecutive failed tool calls. The limit is enforced
          by the AgentRun controller.
        </p>
        {limits && Number.isFinite(limits.max_consecutive_failures)
          && limits.max_consecutive_failures > 0 ? (
          <UsageBar
            used={run.consecutive_failures}
            limit={limits.max_consecutive_failures}
            label="Consecutive failure usage"
          />
        ) : null}
      </section>

      <section className="aze-agent-section">
        <header className="aze-agent-section-head">
          <h3>CONFIGURED EXECUTION LIMITS</h3>
          <span>{limits ? "BACKEND REPORTED" : "NOT REPORTED"}</span>
        </header>
        {limits ? (
          <div className="aze-agent-limits">
            <LimitRow
              label="Steps"
              used={run.step_count}
              limit={limits.max_steps}
            />
            <LimitRow
              label="Tool calls"
              used={run.tool_call_count}
              limit={limits.max_tool_calls}
            />
            <LimitRow
              label="Consecutive failures"
              used={run.consecutive_failures}
              limit={limits.max_consecutive_failures}
            />
            <LimitRow
              label="Duration"
              used={elapsed}
              limit={limits.max_duration_seconds}
              format={(value) => formatElapsed(value)}
            />
          </div>
        ) : (
          <p className="aze-agent-note">
            Execution limits are not reported by this backend snapshot.
          </p>
        )}
      </section>

      <section className="aze-agent-section">
        <header className="aze-agent-section-head">
          <h3>RUN IDENTITY</h3>
        </header>
        <dl className="aze-agent-facts">
          <div><dt>Run</dt><dd>{run.run_id || "Not reported"}</dd></div>
          <div><dt>Conversation</dt><dd>{run.conversation_id || "Not reported"}</dd></div>
          <div><dt>Started</dt><dd>{formatDateTime(run.started_at)}</dd></div>
          <div><dt>Updated</dt><dd>{formatDateTime(run.updated_at)}</dd></div>
          <div><dt>Finished</dt><dd>{formatDateTime(run.finished_at)}</dd></div>
          <div><dt>Terminal</dt><dd>{isTerminal(run) ? "YES" : "NO"}</dd></div>
        </dl>
      </section>
    </div>
  );
}

function LimitRow({
  label,
  used,
  limit,
  format,
}: {
  label: string;
  used: number | null;
  limit: number;
  format?: (value: number) => string;
}) {
  const hasLimit = Number.isFinite(limit) && limit > 0;
  const rendered = format ? format : (value: number) => String(value);
  return (
    <div className="aze-agent-limit-row">
      <span className="aze-agent-limit-label">{label}</span>
      <span className="aze-agent-limit-value">
        {used === null || !Number.isFinite(used) ? "—" : rendered(used)}
        <span> / </span>
        {hasLimit ? rendered(limit) : "Not reported"}
      </span>
      {hasLimit && used !== null && Number.isFinite(used) ? (
        <UsageBar used={used} limit={limit} label={`${label} usage`} />
      ) : null}
    </div>
  );
}

export function AgentPanel({ run, toolEvents, onClose }: AgentPanelProps) {
  const [tab, setTab] = useState<TabId>("overview");
  const live = Boolean(run && !isTerminal(run));
  const now = useNow(live);
  const elapsed = useElapsed(run, now);

  return (
    <div
      className="aze-bay-overlay"
      role="dialog"
      aria-modal="false"
      aria-label="Agent run"
    >
      <section className="aze-bay-sheet aze-agent-sheet">
        <header className="aze-bay-head">
          <div className="min-w-0">
            <p className="aze-eyebrow">J.A.R.V.I.S. // OBJECTIVE RUN</p>
            <h2 className="aze-bay-title">Agent</h2>
          </div>
          <div className="flex shrink-0 items-center gap-2.5">
            <span
              className={cn(
                "aze-bay-pill",
                run ? `aze-agent-pill--${statusTone(run)}` : "aze-bay-pill--placeholder",
              )}
            >
              {run ? statusLabel(run) : "NO ACTIVE RUN"}
            </span>
            <button
              type="button"
              onClick={onClose}
              aria-label="Close Agent panel"
              title="Close Agent"
              className="aze-icon-button"
            >
              <XIcon className="h-4 w-4" />
            </button>
          </div>
        </header>

        {!run ? (
          <div className="aze-agent-empty">
            <BotIcon className="h-9 w-9 text-[var(--accent)] opacity-70" />
            <p className="aze-agent-empty-title">NO AGENT RUN</p>
            <p className="aze-agent-empty-copy">
              Jarvis starts an objective run only when it explicitly calls the
              task_status tool. Ordinary conversation continues without one.
            </p>
          </div>
        ) : (
          <>
            <div className="aze-agent-mission">
              <span className="aze-agent-mission-label">OBJECTIVE</span>
              <p className="aze-agent-mission-text">
                {textOrUnavailable(run.objective)}
              </p>
              <span className="aze-agent-mission-meta">
                RUN {run.run_id ? run.run_id.slice(0, 12) : "NOT REPORTED"}
                {" · "}
                CONVERSATION {run.conversation_id
                  ? run.conversation_id.slice(0, 10)
                  : "NOT REPORTED"}
                {" · "}
                STARTED {formatDateTime(run.started_at)}
              </span>
            </div>

            <div className="aze-agent-tabbar">
              <div className="aze-skills-tabs" role="tablist" aria-label="Agent panel sections">
                {TABS.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    role="tab"
                    aria-selected={tab === item.id}
                    className={cn(
                      "aze-skills-tab",
                      tab === item.id && "aze-skills-tab--active",
                    )}
                    onClick={() => setTab(item.id)}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
              <div className="aze-agent-live-state" aria-live="polite">
                <span
                  className={cn(
                    "aze-status-dot",
                    live ? "bg-[var(--accent)]" : "bg-[var(--dim)]",
                  )}
                />
                {live ? "LIVE" : "FINAL"}
                <span className="aze-agent-live-time">
                  UPDATED {formatTime(run.updated_at)}
                </span>
              </div>
            </div>

            <div className="aze-agent-body" role="tabpanel">
              {tab === "overview" ? (
                <Overview run={run} elapsed={elapsed} />
              ) : tab === "activity" ? (
                <Activity run={run} />
              ) : tab === "tools" ? (
                <Tools run={run} toolEvents={toolEvents} />
              ) : tab === "verification" ? (
                <Verification run={run} />
              ) : (
                <Statistics run={run} elapsed={elapsed} />
              )}
            </div>
          </>
        )}
      </section>
    </div>
  );
}
