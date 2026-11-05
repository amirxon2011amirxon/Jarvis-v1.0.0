import { useEffect } from "react";

import type { SocketStatus } from "../lib/ws";
import { HexMark } from "./ui/Icons";

interface ShutdownScreenProps {
  socket: SocketStatus;
  reason?: string;
}

/**
 * Last screen before the local process exits.
 *
 * The backend closes the exact Jarvis tab through Brave's accessibility tree
 * when it can. Browsers often refuse `window.close()` for tabs they did not
 * script-open, so this overlay stays readable and offers a direct fallback.
 */
export function ShutdownScreen({ socket, reason }: ShutdownScreenProps) {
  useEffect(() => {
    // Best effort only; a blocked close must never stop the backend shutdown.
    const timer = window.setTimeout(() => {
      try {
        window.close();
      } catch {
        // The overlay remains with the manual close button.
      }
    }, 1_600);
    return () => window.clearTimeout(timer);
  }, []);

  const closeTab = () => {
    try {
      window.close();
    } catch {
      // Manual fallback: the tab can be closed with the browser's close button.
    }
  };

  return (
    <div
      className="aze-shutdown"
      role="alertdialog"
      aria-modal="true"
      aria-label="Jarvis is shutting down"
    >
      <section className="aze-shutdown-card">
        <HexMark className="aze-shutdown-mark" />
        <p className="aze-shutdown-eyebrow">J.A.R.V.I.S. // LOCAL CONTROL PLANE</p>
        <h1 className="aze-shutdown-title">POWERING DOWN</h1>
        <p className="aze-shutdown-copy">
          {socket === "offline"
            ? "The local server has stopped. This tab can be closed."
            : "The local session is closing and the backend server is stopping."}
        </p>
        <div className="aze-shutdown-progress" aria-hidden="true">
          <span />
        </div>
        <p className="aze-shutdown-foot">
          {reason ? `Requested: ${reason}` : "Session ended by voice command."}
        </p>
        <button
          type="button"
          className="aze-shutdown-close"
          onClick={closeTab}
        >
          CLOSE TAB
        </button>
      </section>
    </div>
  );
}
