import { cn } from "../../lib/utils";

interface ToggleProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  description?: string;
  disabled?: boolean;
}

export function Toggle({
  checked,
  onChange,
  label,
  description,
  disabled = false,
}: ToggleProps) {
  return (
    <div className="flex items-start justify-between gap-4 py-2">
      <div className="min-w-0">
        <p className="font-mono text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--text)]">
          {label}
        </p>
        {description ? (
          <p className="mt-1 text-xs leading-5 text-[var(--muted)]">
            {description}
          </p>
        ) : null}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={cn(
          "relative mt-0.5 h-6 w-11 shrink-0 rounded-full border transition-colors duration-200",
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]",
          "disabled:cursor-not-allowed disabled:opacity-40",
          checked
            ? "border-[var(--accent)] bg-[var(--accent)]/25"
            : "border-[var(--line-strong)] bg-[var(--panel-2)]",
        )}
      >
        <span
          className={cn(
            "absolute top-1/2 h-4 w-4 -translate-y-1/2 rounded-full transition-all duration-200 ease-out",
            checked
              ? "left-[22px] bg-[var(--accent)] shadow-[0_0_10px_rgb(var(--accent-rgb)/0.45)]"
              : "left-1 bg-[var(--muted)] shadow-none",
          )}
        />
      </button>
    </div>
  );
}
