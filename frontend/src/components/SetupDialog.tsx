import { memo, useEffect, useState } from "react";

import type { SetupStatus } from "../types";
import { cn } from "../lib/utils";
import { Button } from "./ui/Button";
import { ArrowUpRightIcon } from "./ui/Icons";
import { Modal } from "./ui/Modal";

interface SetupDialogProps {
  open: boolean;
  required: boolean;
  setup: SetupStatus;
  platform: string;
  onClose: () => void;
  onSave: (apiKey: string, osSystem?: string) => Promise<void>;
}

const OS_OPTIONS = [
  { id: "windows", label: "WINDOWS" },
  { id: "mac", label: "MACOS" },
  { id: "linux", label: "LINUX" },
] as const;

function detectedOs(platform: string): string {
  const value = platform.toLowerCase();
  if (value.includes("win")) return "windows";
  if (value.includes("darwin") || value.includes("mac")) return "mac";
  return "linux";
}

export const SetupDialog = memo(function SetupDialog({
  open,
  required,
  setup,
  platform,
  onClose,
  onSave,
}: SetupDialogProps) {
  const [key, setKey] = useState("");
  const [os, setOs] = useState(() => detectedOs(platform));
  const [visible, setVisible] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) {
      setKey("");
      setError("");
      setVisible(false);
      return;
    }
    setKey("");
    setError("");
    setVisible(false);
    setOs(detectedOs(platform));
  }, [open, platform]);

  const submit = async () => {
    const value = key.trim();
    if (!value) {
      setError("A Gemini API key is required.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      await onSave(value, os);
      if (!required) onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The key could not be saved.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={required ? "Initialisation required" : "Update Gemini key"}
      eyebrow="GEMINI LINK // CREDENTIALS"
      dismissible={!required}
      footer={
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="max-w-md text-[11px] leading-5 text-[var(--muted)]">
            The key is stored in <span className="text-[var(--text)]">config/api_keys.json</span>{" "}
            and is never returned to the browser after saving.
          </p>
          <Button
            variant="primary"
            size="md"
            onClick={() => void submit()}
            disabled={saving}
            className="w-full sm:w-auto"
          >
            {saving ? "INITIALISING…" : required ? "INITIALISE SYSTEMS" : "SAVE KEY"}
          </Button>
        </div>
      }
    >
      <div className="space-y-5">
        <div className="border border-[var(--accent)]/20 bg-[var(--accent)]/[0.035] px-4 py-3">
          <p className="font-mono text-[9px] uppercase tracking-[0.18em] text-[var(--accent)]">
            {required ? "FIRST BOOT" : "LIVE LINK"}
          </p>
          <p className="mt-1.5 text-xs leading-5 text-[var(--muted)]">
            Gemini Live is the only conversation engine in this build. The
            microphone and speakers are handled by Jarvis's local Python audio
            pipeline, not by the browser.
          </p>
        </div>

        <div>
          <div className="flex items-center justify-between">
            <label
              htmlFor="gemini-key"
              className="font-mono text-[9px] font-semibold uppercase tracking-[0.16em] text-[var(--muted)]"
            >
              Gemini API key
            </label>
            {setup.key_masked ? (
              <span className="font-mono text-[8px] uppercase tracking-[0.12em] text-[var(--dim)]">
                CURRENT {setup.key_masked}
              </span>
            ) : null}
          </div>
          <div className="mt-2 flex gap-1.5">
            <input
              id="gemini-key"
              type={visible ? "text" : "password"}
              value={key}
              onChange={(event) => setKey(event.target.value)}
              placeholder="AIza…"
              autoComplete="off"
              spellCheck={false}
              maxLength={4096}
              autoFocus
              className="h-10 min-w-0 flex-1 rounded-[3px] border border-[var(--line-strong)] bg-[var(--void)] px-3 font-mono text-sm text-[var(--text)] outline-none transition-colors placeholder:text-[var(--dim)] focus:border-[var(--accent)]"
            />
            <Button
              variant="quiet"
              size="md"
              onClick={() => setVisible((current) => !current)}
              aria-pressed={visible}
              className="border border-[var(--line)]"
            >
              {visible ? "HIDE" : "SHOW"}
            </Button>
          </div>
          <a
            href="https://aistudio.google.com/apikey"
            target="_blank"
            rel="noreferrer"
            className="mt-2 inline-flex items-center gap-1 font-mono text-[8px] uppercase tracking-[0.12em] text-[var(--accent)] hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]"
          >
            Get a free key from Google AI Studio
            <ArrowUpRightIcon className="h-3 w-3" />
          </a>
        </div>

        <div>
          <p className="font-mono text-[9px] font-semibold uppercase tracking-[0.16em] text-[var(--muted)]">
            Operating system
          </p>
          <p className="mt-1 text-[11px] text-[var(--dim)]">
            Auto-detected: {platform || "unknown"}. This is saved with the existing
            configuration for compatibility.
          </p>
          <div className="mt-2 grid grid-cols-3 gap-1.5">
            {OS_OPTIONS.map((option) => (
              <button
                key={option.id}
                type="button"
                onClick={() => setOs(option.id)}
                aria-pressed={os === option.id}
                className={cn(
                  "h-9 rounded-[3px] border font-mono text-[9px] font-semibold uppercase tracking-[0.12em] transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]",
                  os === option.id
                    ? "border-[var(--accent)] bg-[var(--accent)] text-[var(--accent-contrast)]"
                    : "border-[var(--line-strong)] bg-transparent text-[var(--muted)] hover:border-[var(--accent)] hover:text-[var(--accent)]",
                )}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>

        {error ? (
          <p
            className="border border-[var(--danger)]/40 bg-[var(--danger)]/[0.06] px-3 py-2 font-mono text-[10px] leading-5 text-[var(--danger)]"
            role="alert"
          >
            {error}
          </p>
        ) : null}
      </div>
    </Modal>
  );
});
