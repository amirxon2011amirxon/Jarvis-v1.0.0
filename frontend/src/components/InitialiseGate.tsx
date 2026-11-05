import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "../lib/api";
import type { Diagnostics, SetupStatus } from "../types";
import { cn } from "../lib/utils";
import { HexMark } from "./ui/Icons";
import { Button } from "./ui/Button";

type Phase = "loading" | "ready" | "checking" | "error";

interface InitialiseGateProps {
  setup: SetupStatus;
  onEnter: () => void;
  onOpenSetup: () => void;
}

const CHECK_TONE: Record<string, string> = {
  ok: "text-[var(--ok)]",
  warn: "text-[var(--warn)]",
  error: "text-[var(--danger)]",
};

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

export function InitialiseGate({ setup, onEnter, onOpenSetup }: InitialiseGateProps) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [diagnostics, setDiagnostics] = useState<Diagnostics | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    const run = async () => {
      try {
        const [result] = await Promise.all([
          api.diagnostics(),
          sleep(1250),
        ]);
        if (!mounted.current) return;
        setDiagnostics(result);
        // Do not stomp on an initialisation that is already polling: the
        // automatic start below may have moved the gate to "checking" while
        // this first diagnostics call was still in flight.
        setPhase((current) => (current === "checking" ? current : "ready"));
      } catch (error) {
        if (!mounted.current) return;
        setMessage(
          error instanceof Error ? error.message : "The local backend did not answer.",
        );
        setPhase("error");
      }
    };
    void run();
    return () => {
      mounted.current = false;
    };
  }, []);

  const initialise = useCallback(async () => {
    if (!setup.configured) {
      onOpenSetup();
      return;
    }
    setPhase("checking");
    setMessage(null);
    try {
      let result = await api.initialize();
      if (!mounted.current) return;
      setDiagnostics(result);
      // The session thread may still be opening; poll real state instead of
      // claiming success because the request was accepted. The backend keeps
      // retrying on its own (short backoff), so stay here long enough to ride
      // out a DNS/network blip instead of forcing a manual retry.
      for (let attempt = 0; attempt < 60 && !result.ok; attempt += 1) {
        await sleep(750);
        if (!mounted.current) return;
        result = await api.diagnostics();
        setDiagnostics(result);
        if (!result.ok && result.connection_error) {
          setMessage(result.connection_error);
        }
      }
      if (!mounted.current) return;
      if (result.ok) {
        setMessage(null);
        setPhase("ready");
        await sleep(650);
        if (mounted.current) onEnter();
        return;
      }
      setPhase("error");
      setMessage(
        result.connection_error
          ? `${result.connection_error} Retrying continues in the background — press RETRY to try again now.`
          : "The Live session did not become ready. Check the checks below, then retry.",
      );
    } catch (error) {
      if (!mounted.current) return;
      setMessage(
        error instanceof Error ? error.message : "Initialisation request failed.",
      );
      setPhase("error");
    }
  }, [onEnter, onOpenSetup, setup.configured]);

  // A configured install should enter the interface on its own: typing `jarvis`
  // and then having to click INITIALISE is friction, not a safety feature.
  // The button stays as a manual retry. The ref guards StrictMode's double
  // effect invocation in development.
  const autoStarted = useRef(false);
  useEffect(() => {
    if (!setup.configured || autoStarted.current) return;
    autoStarted.current = true;
    void initialise();
  }, [initialise, setup.configured]);

  const checks = useMemo(() => diagnostics?.checks ?? [], [diagnostics]);
  const ready = Boolean(diagnostics?.ok);
  const busy = phase === "loading" || phase === "checking";

  return (
    <main className="aze-gate" aria-label="Jarvis initialisation">
      <div className="aze-gate-ambient" aria-hidden="true" />

      <section className="aze-gate-card" aria-busy={busy}>
        <header className="aze-gate-head">
          <div className="aze-gate-orbit" aria-hidden="true">
            <span className="aze-gate-ring aze-gate-ring--a" />
            <span className="aze-gate-ring aze-gate-ring--b" />
            <span className="aze-gate-ring aze-gate-ring--c" />
            <HexMark className="aze-gate-logo" />
          </div>
          <div className="min-w-0">
            <p className="aze-eyebrow">J.A.R.V.I.S. // LOCAL CONTROL PLANE</p>
            <h1 className="aze-gate-title">
              {phase === "loading"
                ? "WAKING THE CORE"
                : ready
                  ? "CORE READY"
                  : "INITIALISATION"}
            </h1>
            <p className="aze-gate-copy">
              {phase === "loading"
                ? "Reading the local runtime, provider configuration, and device links."
                : ready
                  ? "Provider, Live voice link, microphone, weather, and the tool registry all answered."
                  : "Real readiness checks. Nothing here is simulated."}
            </p>
          </div>
        </header>

        <div className="aze-gate-scan" aria-hidden="true">
          <span />
        </div>

        {phase === "loading" ? (
          <div className="aze-gate-loading" role="status">
            <span className="aze-gate-spinner" aria-hidden="true" />
            <span>PROBING LOCAL SERVICES…</span>
          </div>
        ) : (
          <div className="aze-gate-checks" role="list">
            {checks.map((check) => (
              <div key={check.id} className="aze-gate-check" role="listitem">
                <span className={cn("aze-status-dot", check.status === "ok"
                  ? "bg-[var(--ok)]"
                  : check.status === "warn"
                    ? "bg-[var(--warn)]"
                    : "bg-[var(--danger)]")} />
                <span className="aze-gate-check-label">{check.label}</span>
                <span className="aze-gate-check-detail">{check.detail}</span>
                <span className={cn("aze-gate-check-state", CHECK_TONE[check.status])}>
                  {check.status.toUpperCase()}
                </span>
              </div>
            ))}
          </div>
        )}

        {message ? (
          <p className="aze-gate-message" role="alert">{message}</p>
        ) : null}

        <footer className="aze-gate-actions">
          {!setup.configured ? (
            <Button variant="quiet" size="sm" onClick={onOpenSetup}>
              OPEN SETUP
            </Button>
          ) : null}
          {phase === "error" ? (
            <Button variant="quiet" size="sm" onClick={() => void initialise()}>
              RETRY
            </Button>
          ) : null}
          <Button
            variant="primary"
            size="md"
            disabled={busy}
            onClick={() => void initialise()}
          >
            {busy ? "INITIALISING…" : "INITIALISE"}
          </Button>
        </footer>

        <p className="aze-gate-foot">
          {diagnostics?.session_running
            ? "LIVE SESSION THREAD RUNNING"
            : "LIVE SESSION THREAD IDLE"}
          {diagnostics?.model ? ` · ${diagnostics.model}` : ""}
        </p>
      </section>
    </main>
  );
}
