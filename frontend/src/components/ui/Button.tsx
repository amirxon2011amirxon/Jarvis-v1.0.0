import type { ButtonHTMLAttributes, ReactNode } from "react";

import { cn } from "../../lib/utils";

type Variant = "primary" | "ghost" | "quiet" | "danger";
type Size = "sm" | "md" | "lg";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  leading?: ReactNode;
  trailing?: ReactNode;
}

const sizes: Record<Size, string> = {
  sm: "h-8 px-3 text-[10px] tracking-[0.14em]",
  md: "h-10 px-4 text-[11px] tracking-[0.12em]",
  lg: "h-12 px-5 text-xs tracking-[0.14em]",
};

const variants: Record<Variant, string> = {
  primary:
    "border border-[var(--accent)] bg-[var(--accent)] text-[var(--accent-contrast)] hover:bg-[var(--accent-soft)] hover:border-[var(--accent-soft)]",
  ghost:
    "border border-[var(--line-strong)] bg-transparent text-[var(--text)] hover:border-[var(--accent)] hover:text-[var(--accent)]",
  quiet:
    "border border-transparent bg-transparent text-[var(--muted)] hover:border-[var(--line)] hover:text-[var(--text)]",
  danger:
    "border border-[var(--danger)]/60 bg-transparent text-[var(--danger)] hover:bg-[var(--danger)]/10",
};

export function Button({
  variant = "ghost",
  size = "md",
  leading,
  trailing,
  className,
  children,
  type = "button",
  ...props
}: ButtonProps) {
  return (
    <button
      type={type}
      className={cn(
        "inline-flex shrink-0 items-center justify-center gap-2 rounded-[3px] font-mono font-semibold uppercase transition-colors duration-150",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]",
        "disabled:cursor-not-allowed disabled:opacity-40",
        sizes[size],
        variants[variant],
        className,
      )}
      {...props}
    >
      {leading}
      {children}
      {trailing}
    </button>
  );
}
