import { memo, useEffect, useRef, useState, type ReactNode } from "react";

import type { ChatMessage, LogEntry } from "../types";
import { cn, formatClock } from "../lib/utils";
import { BotIcon } from "./ui/Icons";

interface ConversationPanelProps {
  messages: ChatMessage[];
  logs: LogEntry[];
  assistantName: string;
  /** True while the backend genuinely has our message but no reply yet. */
  awaitingReply?: boolean;
  footer?: ReactNode;
  /** Transcript dock: an expand/collapse control when hosted inside the
   * holographic shell. Absent = classic standalone panel behaviour. */
  dockOpen?: boolean;
  onToggleDock?: () => void;
}

type Tab = "conversation" | "log";

function MessageRow({ message, assistantName }: { message: ChatMessage; assistantName: string }) {
  const user = message.role === "user";
  return (
    <article
      aria-label={`${user ? "You" : assistantName}: ${message.text}`}
      className={cn("aze-message", user ? "aze-message--user" : "aze-message--assistant")}
    >
      <div className={cn("aze-avatar", user ? "aze-avatar--user" : "aze-avatar--assistant")} aria-hidden="true">
        {user ? <span className="aze-avatar-letter">U</span> : <BotIcon className="h-4 w-4" />}
      </div>
      <div className="min-w-0 flex-1">
        <div className="aze-message-meta">
          <span className={cn("aze-message-author", user ? "text-[var(--muted)]" : "text-[var(--accent)]")}>
            {user ? "You" : assistantName}
          </span>
          <span>{formatClock(message.ts)}</span>
          {message.pending ? <span className="text-[var(--warn)]">TRANSMITTING</span> : null}
          {message.failed ? <span className="text-[var(--danger)]">NOT DELIVERED</span> : null}
          {message.cancelled ? <span className="text-[var(--dim)]">INTERRUPTED</span> : null}
        </div>
        <div
          className={cn(
            "aze-message-bubble",
            user ? "aze-message-bubble--user" : "aze-message-bubble--assistant",
            message.failed && "aze-message-bubble--failed",
            message.pending && "opacity-70",
          )}
        >
          {message.text || (message.streaming ? "…" : "")}
          {message.streaming ? <span className="aze-stream-caret" aria-hidden="true" /> : null}
        </div>
      </div>
    </article>
  );
}

export const ConversationPanel = memo(function ConversationPanel({
  messages,
  logs,
  assistantName,
  awaitingReply = false,
  footer,
  dockOpen,
  onToggleDock,
}: ConversationPanelProps) {
  const [tab, setTab] = useState<Tab>("conversation");
  const scrollRef = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  useEffect(() => {
    const element = scrollRef.current;
    if (!element || !pinned.current) return;
    element.scrollTop = element.scrollHeight;
  }, [messages, logs, tab, awaitingReply, dockOpen]);

  const onScroll = () => {
    const element = scrollRef.current;
    if (!element) return;
    pinned.current = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
  };

  return (
    <section className="aze-chat-panel" aria-label="Conversation">
      <header className="aze-chat-header">
        <h2 className="truncate font-mono text-[12px] font-semibold uppercase tracking-[0.16em] text-[var(--muted)]">
          {assistantName}
        </h2>
        <div className="flex items-center gap-2">
          {onToggleDock ? (
            <button
              type="button"
              aria-pressed={Boolean(dockOpen)}
              onClick={onToggleDock}
              className={cn("aze-tab", dockOpen && "aze-tab--active")}
              title={dockOpen ? "Collapse the transcript" : "Expand the transcript"}
            >
              TRANSCRIPT
            </button>
          ) : null}
          <div className="flex items-center gap-1" role="tablist" aria-label="Conversation views">
            <button
              type="button"
              role="tab"
              aria-selected={tab === "conversation"}
              onClick={() => setTab("conversation")}
              className={cn("aze-tab", tab === "conversation" && "aze-tab--active")}
            >
              CHAT
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={tab === "log"}
              onClick={() => setTab("log")}
              className={cn("aze-tab", tab === "log" && "aze-tab--active")}
            >
              LOG
            </button>
          </div>
        </div>
      </header>

      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="aze-chat-scroll"
        role="tabpanel"
        aria-label={tab === "conversation" ? "Conversation messages" : "System log"}
      >
        {tab === "conversation" ? (
          messages.length === 0 ? (
            <div className="aze-empty-chat">
              <div className="aze-empty-mark">
                <BotIcon className="h-6 w-6" />
              </div>
              <p className="mt-3 text-xs leading-5 text-[var(--muted)]">
                Type a message or speak.
              </p>
            </div>
          ) : (
            <div className="space-y-5">
              {messages.map((message) => (
                <MessageRow key={message.id} message={message} assistantName={assistantName} />
              ))}
              {/* Typing indicator only while the adapter is truly waiting on
               * the model — once text streams in, the caret takes over. */}
              {awaitingReply && !messages.some((message) => message.streaming) ? (
                <div className="aze-typing" aria-live="polite">
                  <div className="aze-typing-avatar" aria-hidden="true">
                    <BotIcon className="h-4 w-4" />
                  </div>
                  <div className="aze-typing-body">
                    <span className="aze-typing-dots" aria-hidden="true">
                      <span className="aze-typing-dot" />
                      <span className="aze-typing-dot" />
                      <span className="aze-typing-dot" />
                    </span>
                    <span className="aze-typing-label">
                      {assistantName.toUpperCase()} IS THINKING
                    </span>
                  </div>
                </div>
              ) : null}
            </div>
          )
        ) : logs.length === 0 ? (
          <p className="font-mono text-[9px] uppercase tracking-[0.12em] text-[var(--dim)]">
            No system events yet.
          </p>
        ) : (
          <ol className="space-y-2.5">
            {logs.map((entry) => (
              <li key={entry.id} className="flex gap-2 font-mono text-[10px] leading-5">
                <span className="shrink-0 text-[var(--dim)]">{formatClock(entry.ts)}</span>
                <span
                  className={cn(
                    "min-w-0 break-words",
                    entry.level === "error"
                      ? "text-[var(--danger)]"
                      : entry.level === "system"
                        ? "text-[var(--muted)]"
                        : entry.level === "tool"
                          ? "text-[var(--warn)]"
                          : "text-[var(--text)]/85",
                  )}
                >
                  {entry.text}
                </span>
              </li>
            ))}
          </ol>
        )}
      </div>

      {footer}
    </section>
  );
});
