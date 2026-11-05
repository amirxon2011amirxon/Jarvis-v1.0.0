import { memo, useEffect, useMemo, useRef, useState } from "react";

import type { ToolEvent, ToolStatus } from "../types";
import { cn, formatClock, humanizeTool } from "../lib/utils";
import { ChevronRightIcon, PauseIcon, PlayIcon, TerminalIcon } from "./ui/Icons";

interface ToolBusProps {
  events: ToolEvent[];
  inert?: boolean;
  onClear: () => void;
}

type ActivityFilter = "all" | "running" | "success" | "failed";

const FILTERS: Array<{ id: ActivityFilter; label: string }> = [
  { id: "all", label: "ALL" },
  { id: "running", label: "RUNNING" },
  { id: "success", label: "SUCCESS" },
  { id: "failed", label: "FAILED" },
];

const STATUS_LABEL: Record<ToolStatus, string> = {
  queued: "QUEUED",
  pending: "PENDING",
  running: "RUNNING",
  success: "SUCCESS",
  failed: "FAILED",
  cancelled: "CANCELLED",
  unknown: "UNKNOWN",
};

function eventStatus(event: ToolEvent): ToolStatus {
  if (event.status) return event.status;
  if (event.phase === "start") return "running";
  if (event.pending) return "pending";
  return event.ok ? "success" : "failed";
}

function statusClass(status: ToolStatus): string {
  switch (status) {
    case "success":
      return "aze-tool-status--success";
    case "failed":
      return "aze-tool-status--failed";
    case "running":
      return "aze-tool-status--running";
    case "cancelled":
      return "aze-tool-status--muted";
    case "queued":
    case "pending":
      return "aze-tool-status--pending";
    default:
      return "aze-tool-status--muted";
  }
}

function duration(event: ToolEvent): string {
  if (event.phase !== "end") return "—";
  if (event.ms >= 1000) return `${(event.ms / 1000).toFixed(1)}s`;
  return `${event.ms}ms`;
}

export const ToolBus = memo(function ToolBus({ events, inert, onClear }: ToolBusProps) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState<ActivityFilter>("all");
  const [paused, setPaused] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const lockedOffset = useRef(0);

  const statusOf = (event: ToolEvent) => eventStatus(event);
  const visible = useMemo(() => {
    const rows = events.filter((event) =>
      filter === "all" ? true : statusOf(event) === filter);
    return rows
      .sort((a, b) => b.started_at.localeCompare(a.started_at))
      .slice(0, 120);
  }, [events, filter]);

  const active = events.filter((event) => statusOf(event) === "running").length;

  /* Pause = lock the viewport: after React commits new rows the stored
   * offset is re-applied, so the list never jumps while being inspected.
   * Events keep flowing; nothing is hidden or dropped. */
  useEffect(() => {
    const element = scrollRef.current;
    if (!element || !paused) return;
    element.scrollTop = lockedOffset.current;
  }, [visible, paused]);

  const toggle = (id: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <section inert={inert ? true : undefined} className="aze-toolbus" aria-label="Tool Bus">
      <header className="aze-toolbus-header">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="aze-toolbus-mark">
            <TerminalIcon className="h-3.5 w-3.5" />
          </span>
          <div className="min-w-0">
            <div className="flex items-baseline gap-2">
              <h2 className="font-mono text-[10px] font-semibold uppercase tracking-[0.22em] text-[var(--text)]">
                ACTIVITY MONITOR
              </h2>
              <span className="aze-eyebrow hidden sm:inline">REAL-TIME TOOL EXECUTION LOG</span>
            </div>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-end gap-2.5">
          <div className="aze-toolbus-filters" role="group" aria-label="Filter activity">
            {FILTERS.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => setFilter(item.id)}
                aria-pressed={filter === item.id}
                className={cn(
                  "aze-toolbus-filter",
                  filter === item.id && "aze-toolbus-filter--active",
                )}
              >
                {item.label}
              </button>
            ))}
          </div>
          <span className="aze-toolbus-count">
            <span className={cn("aze-status-dot", active ? "bg-[var(--accent)]" : "bg-[var(--dim)]")} />
            {active ? `${active} ACTIVE` : "IDLE"}
          </span>
          <button
            type="button"
            onClick={() => {
              if (paused) {
                lockedOffset.current = 0;
                setPaused(false);
              } else {
                lockedOffset.current = scrollRef.current?.scrollTop ?? 0;
                setPaused(true);
              }
            }}
            aria-pressed={paused}
            aria-label={paused ? "Resume auto scroll" : "Pause auto scroll"}
            title={paused ? "Resume auto scroll" : "Pause auto scroll"}
            className={cn("aze-toolbus-pause", paused && "aze-toolbus-pause--held")}
          >
            {paused ? <PlayIcon className="h-3 w-3" /> : <PauseIcon className="h-3 w-3" />}
          </button>
          <button
            type="button"
            onClick={onClear}
            disabled={events.length === 0}
            className="aze-toolbus-clear"
          >
            CLEAR
          </button>
        </div>
      </header>

      <div className="aze-toolbus-columns" aria-hidden="true">
        <span>TIME</span>
        <span>TOOL</span>
        <span>ACTION</span>
        <span>STATUS</span>
        <span>DESCRIPTION</span>
        <span className="text-right">DURATION</span>
      </div>

      <div
        ref={scrollRef}
        className={cn("aze-toolbus-scroll", paused && "aze-toolbus-scroll--paused")}
        role="list"
        aria-live={paused ? "off" : "polite"}
      >
        {visible.length === 0 ? (
          <div className="aze-toolbus-empty">
            <TerminalIcon className="h-4 w-4 text-[var(--dim)]" />
            <span>
              {filter === "all"
                ? "No tool executions in this session."
                : `No ${filter} executions in this session.`}
            </span>
          </div>
        ) : (
          visible.map((event) => {
            const status = statusOf(event);
            const hasDetails = Boolean(event.summary || event.target);
            const isExpanded = expanded.has(event.id);
            return (
              <div key={event.id} className="aze-toolbus-row" role="listitem">
                <span className="aze-toolbus-time">{formatClock(event.started_at)}</span>
                <span className="aze-toolbus-tool" title={event.tool}>
                  {humanizeTool(event.tool)}
                </span>
                <span className="aze-toolbus-action">
                  {event.target || "—"}
                </span>
                <span className={cn("aze-tool-status", statusClass(status))}>
                  <span className="aze-status-dot bg-current" />
                  {STATUS_LABEL[status]}
                </span>
                <button
                  type="button"
                  onClick={() => hasDetails && toggle(event.id)}
                  className={cn(
                    "aze-toolbus-details",
                    hasDetails && "aze-toolbus-details--button",
                    isExpanded && "aze-toolbus-details--open",
                  )}
                  title={event.summary || event.target || ""}
                  aria-expanded={hasDetails ? isExpanded : undefined}
                >
                  <span className="truncate">{event.summary || event.target || "—"}</span>
                  {hasDetails ? <ChevronRightIcon className="aze-toolbus-chevron" /> : null}
                </button>
                <span className="aze-toolbus-duration">{duration(event)}</span>
                {isExpanded ? (
                  <p className="aze-toolbus-detail-full">
                    {event.summary || event.target}
                  </p>
                ) : null}
              </div>
            );
          })
        )}
      </div>
    </section>
  );
});
