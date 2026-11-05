import { useMemo } from "react";

import type { AzeState, SubscribeAudioLevel } from "../types";
import { cn } from "../lib/utils";
import { ArcReactor } from "./ArcReactor";

interface HaloStageProps {
  state: AzeState;
  subscribeAudio: SubscribeAudioLevel;
  activeTools: number;
  muted: boolean;
  configured: boolean;
  setupRequired: boolean;
  awaitingReply: boolean;
  streamingReply: boolean;
  error: string | null;
  accent: string;
  /** true = glow constantly; false = glow while speaking. */
  glowAlways: boolean;
  /** Stop stage animation while the YouTube panel is open. */
  suspended?: boolean;
}

function displayStatus(
  state: AzeState,
  activeTools: number,
  configured: boolean,
  awaitingReply: boolean,
  streamingReply: boolean,
  error: string | null,
): { label: string; tone: string } {
  if (error) return { label: "ATTENTION REQUIRED", tone: "text-[var(--danger)]" };
  if (!configured) return { label: "AWAITING CREDENTIALS", tone: "text-[var(--muted)]" };
  if (state === "OFFLINE") return { label: "LINK OFFLINE", tone: "text-[var(--muted)]" };
  if (activeTools > 0) {
    return {
      label: activeTools === 1 ? "EXECUTING 1 ACTION" : `EXECUTING ${activeTools} ACTIONS`,
      tone: "text-[var(--accent)]",
    };
  }
  if (state === "SPEAKING") return { label: "SPEAKING", tone: "text-[var(--accent)]" };
  if (streamingReply) return { label: "RESPONDING", tone: "text-[var(--accent)]" };
  if (awaitingReply) return { label: "THINKING", tone: "text-[var(--warn)]" };
  switch (state) {
    case "LISTENING":
      return { label: "LISTENING", tone: "text-[var(--ok)]" };
    case "THINKING":
    case "PROCESSING":
      return { label: "THINKING", tone: "text-[var(--warn)]" };
    case "MUTED":
      return { label: "MICROPHONE MUTED", tone: "text-[var(--danger)]" };
    case "INITIALISING":
      return { label: "AT YOUR SERVICE.", tone: "text-[var(--accent-bright)]" };
    default:
      return { label: "AT YOUR SERVICE.", tone: "text-[var(--accent-bright)]" };
  }
}

export function HaloStage({
  state,
  subscribeAudio,
  activeTools,
  muted,
  configured,
  setupRequired,
  awaitingReply,
  streamingReply,
  error,
  accent,
  glowAlways,
  suspended = false,
}: HaloStageProps) {
  const reactorProps = {
    state,
    subscribeAudio,
    activeTools,
    muted,
    error: Boolean(error),
    accent,
    glowAlways,
    suspended,
  };

  const status = useMemo(
    () => displayStatus(state, activeTools, configured && !setupRequired, awaitingReply, streamingReply, error),
    [state, activeTools, configured, setupRequired, awaitingReply, streamingReply, error],
  );

  return (
    <section className="aze-halo-stage" aria-label="Jarvis core">
      <div className="aze-halo-canvas-wrap">
        <ArcReactor {...reactorProps} />
      </div>

      <div className="aze-halo-footer">
        <div className="aze-halo-status-line">
          <span
            className={cn(
              "aze-status-dot",
              error
                ? "bg-[var(--danger)]"
                : state === "SPEAKING" || activeTools > 0
                  ? "bg-[var(--accent)]"
                  : state === "LISTENING"
                    ? "bg-[var(--ok)]"
                    : "bg-[var(--muted)]",
            )}
          />
          <p className={cn("aze-halo-status", status.tone)} aria-live="polite">
            {status.label}
          </p>
        </div>
        {error ? <p className="aze-halo-error" title={error}>{error}</p> : null}
      </div>
    </section>
  );
}
