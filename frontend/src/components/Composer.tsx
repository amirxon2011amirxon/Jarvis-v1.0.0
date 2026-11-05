import { memo, useEffect, useRef, useState } from "react";

import type { AzeState } from "../types";
import { cn } from "../lib/utils";
import { MicIcon, MicOffIcon, SendIcon, StopIcon } from "./ui/Icons";

interface ComposerProps {
  state: AzeState;
  muted: boolean;
  pttEnabled: boolean;
  inert?: boolean;
  onSend: (text: string) => Promise<boolean>;
  onInterrupt: () => Promise<void>;
  onToggleMute: () => Promise<void>;
  onPttHold: (held: boolean) => Promise<void>;
}

export const Composer = memo(function Composer({
  state,
  muted,
  pttEnabled,
  inert,
  onSend,
  onInterrupt,
  onToggleMute,
  onPttHold,
}: ComposerProps) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const sendingRef = useRef(false);
  const [pttHeld, setPttHeld] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(112, input.scrollHeight)}px`;
  }, [text]);

  const submit = async () => {
    const body = text.trim();
    if (!body || sendingRef.current) return;
    sendingRef.current = true;
    setSending(true);
    const submitted = text;
    try {
      const sent = await onSend(body);
      if (sent) {
        setText((current) => (current === submitted ? "" : current));
        inputRef.current?.focus();
      }
    } finally {
      sendingRef.current = false;
      setSending(false);
    }
  };

  const releasePtt = () => {
    if (!pttHeld) return;
    setPttHeld(false);
    void onPttHold(false);
  };

  useEffect(() => {
    if (!pttHeld) return;
    const release = () => {
      setPttHeld(false);
      void onPttHold(false);
    };
    const releaseOnUnload = () => {
      if (typeof navigator.sendBeacon !== "function") return;
      const payload = new Blob([JSON.stringify({ held: false })], {
        type: "application/json",
      });
      navigator.sendBeacon("/api/voice/ptt-hold", payload);
    };
    window.addEventListener("blur", release);
    window.addEventListener("pagehide", releaseOnUnload);
    return () => {
      window.removeEventListener("blur", release);
      window.removeEventListener("pagehide", releaseOnUnload);
    };
  }, [pttHeld, onPttHold]);

  const holdPtt = () => {
    if (!pttEnabled || pttHeld) return;
    setPttHeld(true);
    void onPttHold(true);
  };

  const stopDisabled = state === "OFFLINE" || state === "INITIALISING";

  return (
    <footer inert={inert ? true : undefined} className="aze-composer">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <div className="aze-composer-shell">
          <label htmlFor="aze-command" className="sr-only">
            Message Jarvis
          </label>
          <textarea
            id="aze-command"
            ref={inputRef}
            rows={1}
            value={text}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                void submit();
              }
            }}
            placeholder="Message Jarvis…"
            spellCheck={false}
            autoComplete="off"
            maxLength={8000}
            className="aze-composer-input"
          />

          <div className="aze-composer-actions">
            <button
              type="button"
              onClick={() => void onToggleMute()}
              aria-pressed={muted}
              aria-label={muted ? "Unmute microphone" : "Mute microphone"}
              title={muted ? "Open the microphone" : "Mute the microphone"}
              className={cn(
                "aze-round-button",
                muted ? "aze-round-button--danger" : "aze-round-button--live",
              )}
            >
              {muted ? <MicOffIcon className="h-4 w-4" /> : <MicIcon className="h-4 w-4" />}
            </button>

            <button
              type="button"
              disabled={!pttEnabled}
              aria-pressed={pttHeld}
              aria-label={pttHeld ? "Talking — release to stop" : "Hold to talk"}
              title={pttEnabled ? "Hold to talk (Ctrl+Space also works)" : "Enable push-to-talk in settings"}
              onPointerDown={(event) => {
                event.preventDefault();
                event.currentTarget.setPointerCapture(event.pointerId);
                holdPtt();
              }}
              onPointerUp={releasePtt}
              onPointerCancel={releasePtt}
              onPointerLeave={releasePtt}
              onLostPointerCapture={releasePtt}
              onKeyDown={(event) => {
                if ((event.key === " " || event.key === "Enter") && !event.repeat) {
                  event.preventDefault();
                  holdPtt();
                }
              }}
              onKeyUp={(event) => {
                if (event.key === " " || event.key === "Enter") {
                  event.preventDefault();
                  releasePtt();
                }
              }}
              onBlur={releasePtt}
              className={cn(
                "aze-ptt-chip",
                pttHeld && "aze-ptt-chip--held",
              )}
            >
              {pttHeld ? "TALK" : "PTT"}
            </button>

            <button
              type="button"
              onClick={() => void onInterrupt()}
              disabled={stopDisabled}
              aria-label="Stop speaking"
              title="Stop speaking / interrupt"
              className="aze-round-button aze-round-button--quiet"
            >
              <StopIcon className="h-3.5 w-3.5" />
            </button>

            <button
              type="submit"
              disabled={!text.trim() || sending}
              aria-label="Send message"
              title="Send message"
              className="aze-send-button"
            >
              {sending ? (
                <span className="aze-send-spinner" />
              ) : (
                <SendIcon className="h-4 w-4" />
              )}
            </button>
          </div>
        </div>
      </form>
    </footer>
  );
});
