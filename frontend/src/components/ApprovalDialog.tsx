import { useEffect, useMemo, useState } from "react";

import type { ApprovalRequest } from "../types";
import { cn } from "../lib/utils";
import { AlertIcon } from "./ui/Icons";
import { Button } from "./ui/Button";
import { Modal } from "./ui/Modal";

interface ApprovalDialogProps {
  approvals: ApprovalRequest[];
  onApprove: (approvalId: string) => Promise<unknown>;
  onReject: (approvalId: string) => Promise<unknown>;
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function formatRemaining(seconds: number | null): string {
  if (seconds === null) return "Expiry not reported";
  if (seconds <= 0) return "Expired";
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}:${String(rest).padStart(2, "0")} remaining`;
}

function formatValue(value: unknown): string {
  if (Array.isArray(value)) return value.map(String).join(", ");
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

export function ApprovalDialog({
  approvals,
  onApprove,
  onReject,
}: ApprovalDialogProps) {
  const pending = useMemo(
    () => approvals.filter((item) => item.status === "pending"),
    [approvals],
  );
  const [busyId, setBusyId] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (pending.length === 0) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [pending.length]);

  const decide = async (
    approvalId: string,
    action: (id: string) => Promise<unknown>,
  ) => {
    if (busyId) return;
    setBusyId(approvalId);
    try {
      await action(approvalId);
    } catch {
      // The hook has already surfaced the API error; keep the dialog open.
    } finally {
      setBusyId(null);
    }
  };

  // `now` is used for the countdown; keep the dependency explicit.
  const remainingFor = (value: string): number | null => {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return null;
    return Math.max(0, Math.ceil((date.getTime() - now) / 1000));
  };

  // A request whose expiry event was missed must not leave a non-dismissible
  // modal with no usable button. Hide it locally once its own deadline passed;
  // the backend still expires and revokes it independently.
  const actionable = pending.filter((approval) => {
    const seconds = remainingFor(approval.expires_at);
    return seconds === null || seconds > 0;
  });

  return (
    <Modal
      open={actionable.length > 0}
      onClose={() => undefined}
      title="Action approval"
      eyebrow="J.A.R.V.I.S. // USER AUTHORIZATION"
      dismissible={false}
      wide
      footer={
        <p className="font-mono text-[9px] leading-5 text-[var(--dim)]">
          The assistant cannot approve its own requests. Approval is
          single-use, tied to the exact target below, and expires automatically.
        </p>
      }
    >
      <div className="flex flex-col gap-4">
        {pending.map((approval) => {
          const seconds = remainingFor(approval.expires_at);
          const expired = seconds !== null && seconds <= 0;
          const working = busyId === approval.approval_id;
          return (
            <section
              key={approval.approval_id}
              className="rounded-[7px] border border-[var(--line-strong)] bg-[var(--accent-soft)] p-4"
              aria-label={`Approval request: ${approval.summary}`}
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="font-mono text-[8px] uppercase tracking-[0.18em] text-[var(--dim)]">
                    {approval.tool} // {approval.operation}
                  </p>
                  <h3 className="mt-1 text-sm font-semibold leading-6 text-[var(--text)]">
                    {approval.summary || approval.target || "Protected action"}
                  </h3>
                </div>
                <span
                  className={cn(
                    "shrink-0 rounded-[4px] border px-2 py-1 font-mono text-[8px] font-semibold uppercase tracking-[0.12em]",
                    expired
                      ? "border-[var(--danger)]/40 text-[var(--danger)]"
                      : "border-[var(--warn)]/40 text-[var(--warn)]",
                  )}
                >
                  {expired ? "EXPIRED" : "PENDING"}
                </span>
              </div>

              <div className="mt-3 grid gap-2 text-[11px] sm:grid-cols-[110px_1fr]">
                <span className="font-mono text-[8px] uppercase tracking-[0.14em] text-[var(--dim)]">
                  TARGET
                </span>
                <span className="break-words font-mono text-[10px] text-[var(--text)]">
                  {approval.target || "—"}
                </span>
                <span className="font-mono text-[8px] uppercase tracking-[0.14em] text-[var(--dim)]">
                  REQUESTED
                </span>
                <span className="text-[var(--muted)]">
                  {formatTime(approval.created_at)}
                </span>
                <span className="font-mono text-[8px] uppercase tracking-[0.14em] text-[var(--dim)]">
                  EXPIRES
                </span>
                <span
                  className={cn(
                    "font-mono text-[10px]",
                    expired ? "text-[var(--danger)]" : "text-[var(--warn)]",
                  )}
                >
                  {formatRemaining(seconds)} · {formatTime(approval.expires_at)}
                </span>
              </div>

              {Object.keys(approval.parameters ?? {}).length > 0 ? (
                <div className="mt-3 border-t border-[var(--line)] pt-2">
                  <p className="font-mono text-[8px] uppercase tracking-[0.14em] text-[var(--dim)]">
                    PARAMETERS
                  </p>
                  <dl className="mt-1 grid gap-1 sm:grid-cols-[110px_1fr]">
                    {Object.entries(approval.parameters).map(([key, value]) => (
                      <div key={key} className="contents">
                        <dt className="font-mono text-[8px] uppercase tracking-[0.1em] text-[var(--dim)]">
                          {key}
                        </dt>
                        <dd className="break-words font-mono text-[9px] text-[var(--muted)]">
                          {formatValue(value)}
                        </dd>
                      </div>
                    ))}
                  </dl>
                </div>
              ) : null}

              <p className="mt-3 flex items-start gap-2 text-[10px] leading-5 text-[var(--muted)]">
                <AlertIcon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[var(--warn)]" />
                Approve only if you requested this exact operation. Rejecting
                leaves the target unchanged.
              </p>

              <div className="mt-3 flex flex-wrap justify-end gap-2">
                <Button
                  variant="danger"
                  size="sm"
                  disabled={working || expired}
                  onClick={() => {
                    void decide(approval.approval_id, onReject);
                  }}
                >
                  REJECT
                </Button>
                <Button
                  variant="primary"
                  size="sm"
                  disabled={working || expired}
                  onClick={() => {
                    void decide(approval.approval_id, onApprove);
                  }}
                >
                  {working ? "WORKING…" : "APPROVE"}
                </Button>
              </div>
            </section>
          );
        })}
      </div>
    </Modal>
  );
}
