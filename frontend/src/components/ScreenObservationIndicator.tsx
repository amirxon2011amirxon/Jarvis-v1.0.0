import type { ObservationSnapshot } from "../types";
import { cn } from "../lib/utils";

const LABELS: Record<ObservationSnapshot["state"], string> = {
  INACTIVE: "OFF",
  STARTING: "STARTING",
  OBSERVING: "WATCHING",
  STOPPING: "STOPPING",
  ERROR: "ERROR",
};

const TONES: Record<ObservationSnapshot["state"], string> = {
  INACTIVE: "inactive",
  STARTING: "starting",
  OBSERVING: "active",
  STOPPING: "stopping",
  ERROR: "error",
};

/**
 * Always-visible dashboard chip for continuous screen observation.
 *
 * It renders from the backend's authoritative snapshot only: a requested
 * start shows STARTING, never WATCHING, and the chip disappears entirely
 * when no authoritative state is available.
 */
export function ScreenObservationIndicator({
  observation,
}: {
  observation: ObservationSnapshot | null;
}) {
  if (!observation) return null;
  // Inactive observation is the normal resting state. Keep the composer
  // clear instead of showing a permanent "SCREEN OFF" chip.
  if (observation.state === "INACTIVE" && !observation.last_error) return null;
  const tone = TONES[observation.state] ?? "inactive";
  const label = LABELS[observation.state] ?? observation.state;
  const linkDown =
    observation.state === "OBSERVING" && !observation.voice_session_connected;
  return (
    <div
      className={cn(
        "aze-observation-indicator",
        `aze-observation-indicator--${tone}`,
      )}
      role="status"
      aria-live="polite"
      data-state={observation.state}
      title={observation.last_error || undefined}
    >
      <span className="aze-observation-dot" aria-hidden="true" />
      <span className="aze-observation-label">SCREEN</span>
      <span className="aze-observation-state">{label}</span>
      {linkDown ? (
        <span className="aze-observation-note">LINK DOWN</span>
      ) : null}
      {observation.state === "ERROR" && observation.last_error ? (
        <span className="aze-observation-note">DETAIL</span>
      ) : null}
    </div>
  );
}
