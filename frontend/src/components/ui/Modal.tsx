import { useEffect, useId, useRef, type ReactNode } from "react";

import { cn } from "../../lib/utils";
import { XIcon } from "./Icons";

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  eyebrow?: string;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
  dismissible?: boolean;
  className?: string;
}

export function Modal({
  open,
  onClose,
  title,
  eyebrow,
  children,
  footer,
  wide = false,
  dismissible = true,
  className,
}: ModalProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const suppressClose = useRef(false);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
      document.body.style.overflow = "hidden";
    } else if (!open && dialog.open) {
      suppressClose.current = true;
      dialog.close();
      document.body.style.overflow = "";
    }
    return () => {
      document.body.style.overflow = "";
    };
  }, [open]);

  useEffect(() => {
    return () => {
      document.body.style.overflow = "";
    };
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className={cn(
        "aze-modal m-auto w-[calc(100vw-2rem)] rounded-[6px] border border-[var(--line-strong)] bg-[var(--panel)] p-0 text-[var(--text)]",
        wide ? "max-w-5xl" : "max-w-lg",
        className,
      )}
      aria-labelledby={titleId}
      onClose={() => {
        document.body.style.overflow = "";
        if (suppressClose.current) {
          suppressClose.current = false;
          return;
        }
        if (open) onClose();
      }}
      onCancel={(event) => {
        event.preventDefault();
        if (dismissible) onClose();
      }}
      onClick={(event) => {
        if (event.target === dialogRef.current && dismissible) onClose();
      }}
    >
      <div className="flex max-h-[88dvh] flex-col">
        <header className="flex items-start justify-between gap-6 border-b border-[var(--line)] px-5 py-4 sm:px-7">
          <div>
            {eyebrow ? (
              <p className="font-mono text-[9px] font-semibold uppercase tracking-[0.24em] text-[var(--accent)]">
                {eyebrow}
              </p>
            ) : null}
            <h2
              id={titleId}
              className="mt-1 font-mono text-sm font-semibold uppercase tracking-[0.18em] text-[var(--text)]"
            >
              {title}
            </h2>
          </div>
          {dismissible ? (
            <button
              type="button"
              onClick={onClose}
              className="rounded-[3px] border border-transparent p-1.5 text-[var(--muted)] transition-colors hover:border-[var(--line)] hover:text-[var(--text)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]"
              aria-label="Close dialog"
            >
              <XIcon className="h-4 w-4" />
            </button>
          ) : null}
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5 sm:px-7">
          {children}
        </div>
        {footer ? (
          <footer className="border-t border-[var(--line)] px-5 py-4 sm:px-7">
            {footer}
          </footer>
        ) : null}
      </div>
    </dialog>
  );
}
