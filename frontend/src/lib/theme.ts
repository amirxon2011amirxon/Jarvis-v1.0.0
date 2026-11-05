import {
  accentContrast,
  hexToRgb,
  isValidAccent,
  mixToBlack,
  mixToWhite,
  spaceRgb,
} from "./utils";

export const DEFAULT_ACCENT = "#d8a63a";
const DEFAULT_ACCENT_RGB = hexToRgb(DEFAULT_ACCENT);

/** Apply one validated accent colour to every token the interface reads. */
export function applyAccent(color: string): void {
  const safe = isValidAccent(color) ? color : DEFAULT_ACCENT;
  const rgb = hexToRgb(safe) ?? DEFAULT_ACCENT_RGB;
  if (!rgb) return;
  const root = document.documentElement;
  root.style.setProperty("--accent", safe);
  root.style.setProperty("--accent-rgb", spaceRgb(rgb));
  root.style.setProperty("--accent-contrast", accentContrast(safe));
  root.style.setProperty(
    "--accent-bright-rgb",
    spaceRgb(mixToWhite(rgb, 0.34)),
  );
  root.style.setProperty("--accent-ink", `rgb(${spaceRgb(mixToBlack(rgb, 0.42))})`);
}
