import { Component, StrictMode, type ErrorInfo, type ReactNode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import "./index.css";

class RootErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Jarvis interface error", error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="flex min-h-[100dvh] items-center justify-center bg-[var(--void)] px-6">
          <div className="max-w-md border border-[var(--danger)]/40 bg-[var(--danger)]/[0.05] p-6 text-center">
            <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.2em] text-[var(--danger)]">
              INTERFACE FAULT
            </p>
            <p className="mt-3 text-xs leading-5 text-[var(--muted)]">
              The interface stopped rendering. The Python backend is unaffected.
            </p>
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="mt-5 h-10 border border-[var(--accent)] bg-[var(--accent)] px-4 font-mono text-[10px] font-semibold uppercase tracking-[0.14em] text-[var(--accent-contrast)]"
            >
              RELOAD INTERFACE
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

// A rebuild replaces hashed chunks. An already-open tab can still hold the
// previous index.html and request a chunk that no longer exists, which makes a
// lazy panel show "could not render". Reload once against the new index.
const CHUNK_RELOAD_KEY = "jarvis.chunk-reload.v1";
window.addEventListener("vite:preloadError", (event) => {
  event.preventDefault();
  try {
    if (!sessionStorage.getItem(CHUNK_RELOAD_KEY)) {
      sessionStorage.setItem(CHUNK_RELOAD_KEY, "1");
      window.location.reload();
    }
  } catch {
    window.location.reload();
  }
});
window.setTimeout(() => {
  try {
    sessionStorage.removeItem(CHUNK_RELOAD_KEY);
  } catch {
    // Storage can be unavailable in a locked-down browser.
  }
}, 8000);

const container = document.getElementById("root");
if (!container) throw new Error("Root element is missing.");

createRoot(container).render(
  <StrictMode>
    <RootErrorBoundary>
      <App />
    </RootErrorBoundary>
  </StrictMode>,
);
