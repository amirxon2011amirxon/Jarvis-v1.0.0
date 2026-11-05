import { memo, useEffect, useRef, useState } from "react";

import type { Toast } from "../types";
import { cn } from "../lib/utils";
import { AlertIcon, CheckIcon, ClockIcon, RadioIcon, XIcon } from "./ui/Icons";

function ToastRow({
  toast,
  onDismiss,
  onSnooze,
}: {
  toast: Toast;
  onDismiss: (id: string) => void;
  onSnooze?: (notificationId: string, minutes: number) => void;
}) {
  const [leaving, setLeaving] = useState(false);

  useEffect(() => {
    /* Ordinary feedback auto-dismisses; a reminder/timer/agent notification
     * stays until the user dismisses or snoozes it. */
    if (toast.autoDismiss === false) return;
    const leaveTimer = window.setTimeout(() => setLeaving(true), 5250);
    const dismissTimer = window.setTimeout(() => onDismiss(toast.id), 5560);
    return () => {
      window.clearTimeout(leaveTimer);
      window.clearTimeout(dismissTimer);
    };
  }, [toast.id, toast.autoDismiss, onDismiss]);

  const tone =
    toast.kind === "error"
      ? "border-[var(--danger)]/55 text-[var(--danger)]"
      : toast.kind === "success"
        ? "border-[var(--ok)]/45 text-[var(--ok)]"
        : "border-[var(--accent)]/45 text-[var(--accent)]";

  const Icon =
    toast.kind === "error" ? AlertIcon : toast.kind === "success" ? CheckIcon : RadioIcon;

  return (
    <div
      className={cn(
        "aze-toast-in flex w-[min(360px,calc(100vw-2rem))] items-start gap-2.5 border bg-[var(--panel)]/95 px-3 py-2.5 shadow-[0_10px_40px_rgba(0,0,0,0.45)] backdrop-blur",
        leaving && "aze-toast-leave",
        tone,
      )}
      role={toast.kind === "error" ? "alert" : "status"}
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0" />
      <div className="min-w-0 flex-1">
        {toast.title ? (
          <p className="mb-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-[var(--accent)]">
            {toast.title}
          </p>
        ) : null}
        <p className="text-xs leading-5 text-[var(--text)]">{toast.message}</p>
        {toast.notificationId && onSnooze && toast.snoozeMinutes ? (
          <button
            type="button"
            onClick={() =>
              onSnooze(toast.notificationId as string, toast.snoozeMinutes as number)
            }
            className="mt-1.5 inline-flex items-center gap-1 rounded-[2px] border border-[var(--accent)]/35 px-1.5 py-0.5 text-[9px] uppercase tracking-[0.1em] text-[var(--accent)] transition-colors hover:border-[var(--accent)] hover:text-[var(--text)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]"
          >
            <ClockIcon className="h-3 w-3" />
            SNOOZE {toast.snoozeMinutes}M
          </button>
        ) : null}
      </div>
      <button
        type="button"
        onClick={() => onDismiss(toast.id)}
        className="rounded-[2px] p-0.5 text-[var(--muted)] transition-colors hover:text-[var(--text)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]"
        aria-label="Dismiss notification"
      >
        <XIcon className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

export const Toasts = memo(function Toasts({
  toasts,
  onDismiss,
  onSnooze,
}: {
  toasts: Toast[];
  onDismiss: (id: string) => void;
  onSnooze?: (notificationId: string, minutes: number) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = containerRef.current;
    if (!element || typeof element.showPopover !== "function") return;
    try {
      const open = element.matches(":popover-open");
      if (toasts.length > 0) {
        // Re-showing brings the popover above a native <dialog> in the top
        // layer, so feedback remains visible while Settings is open.
        if (open) element.hidePopover();
        element.showPopover();
      } else if (open) {
        element.hidePopover();
      }
    } catch {
      // Older browsers without the Popover API simply use normal stacking.
    }
  }, [toasts]);

  return (
    <div
      ref={containerRef}
      popover="manual"
      className="aze-toaster pointer-events-none flex flex-col items-end gap-2"
    >
      {toasts.map((toast) => (
        <div key={toast.id} className="pointer-events-auto">
          <ToastRow toast={toast} onDismiss={onDismiss} onSnooze={onSnooze} />
        </div>
      ))}
    </div>
  );
});
