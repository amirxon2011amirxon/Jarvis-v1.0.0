export function cn(
  ...values: Array<string | false | null | undefined>
): string {
  return values.filter(Boolean).join(" ");
}

export function newId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `m-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/* Local time helpers: the top-bar clock and every timestamp must follow the
 * operating system's zone, never a hardcoded one. The OS timezone only enters
 * as text in the local zone label. */

export function localZoneName(): string {
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    return zone.replace(/_/g, " ").toUpperCase();
  } catch {
    return "";
  }
}

export function localOffset(date: Date = new Date()): string {
  const minutes = -date.getTimezoneOffset();
  if (!Number.isFinite(minutes)) return "";
  const sign = minutes < 0 ? "-" : "+";
  const abs = Math.abs(minutes);
  const hours = Math.floor(abs / 60);
  const rest = abs % 60;
  return `UTC${sign}${hours}${rest ? `:${String(rest).padStart(2, "0")}` : ""}`;
}

export function formatClock(value: string | number | Date): string {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "--:--";
  return new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

export function formatClockSeconds(value: Date): string {
  return new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(value);
}

export function formatDate(value: Date): string {
  return new Intl.DateTimeFormat("en-GB", {
    weekday: "long",
    day: "2-digit",
    month: "long",
    year: "numeric",
  })
    .format(value)
    .toUpperCase();
}

export function humanizeTool(name: string): string {
  return name.replace(/_/g, " ").toUpperCase();
}

export function weatherLabel(code: number | undefined): string {
  if (code === undefined) return "CONDITION UNKNOWN";
  if (code === 0) return "CLEAR SKY";
  if (code === 1) return "MAINLY CLEAR";
  if (code === 2) return "PARTLY CLOUDY";
  if (code === 3) return "OVERCAST";
  if (code === 45 || code === 48) return "FOG";
  if (code >= 51 && code <= 57) return "DRIZZLE";
  if (code >= 61 && code <= 67) return "RAIN";
  if (code >= 71 && code <= 77) return "SNOW";
  if (code >= 80 && code <= 82) return "RAIN SHOWERS";
  if (code >= 85 && code <= 86) return "SNOW SHOWERS";
  if (code >= 95) return "THUNDERSTORM";
  return "CONDITION UNKNOWN";
}

export function accentContrast(hex: string): string {
  const match = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!match) return "#031418";
  const raw = match[1];
  const r = parseInt(raw.slice(0, 2), 16) / 255;
  const g = parseInt(raw.slice(2, 4), 16) / 255;
  const b = parseInt(raw.slice(4, 6), 16) / 255;
  const channel = (value: number) =>
    value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  const luminance =
    0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
  return luminance > 0.42 ? "#031418" : "#eaffff";
}

export function isValidAccent(value: string): boolean {
  return /^#[0-9a-fA-F]{6}$/.test(value.trim());
}

export interface AccentRgb {
  r: number;
  g: number;
  b: number;
}

export function hexToRgb(hex: string): AccentRgb | null {
  const match = /^#([0-9a-f]{6})$/i.exec(hex.trim());
  if (!match) return null;
  const raw = match[1].toLowerCase();
  return {
    r: parseInt(raw.slice(0, 2), 16),
    g: parseInt(raw.slice(2, 4), 16),
    b: parseInt(raw.slice(4, 6), 16),
  };
}

export function spaceRgb({ r, g, b }: AccentRgb): string {
  return `${r} ${g} ${b}`;
}

export function mixToWhite(color: AccentRgb, share: number): AccentRgb {
  const factor = Math.max(0, Math.min(1, share));
  const mix = (channel: number) =>
    Math.round(channel + (255 - channel) * factor);
  return { r: mix(color.r), g: mix(color.g), b: mix(color.b) };
}

export function mixToBlack(color: AccentRgb, share: number): AccentRgb {
  const factor = Math.max(0, Math.min(1, share));
  const mix = (channel: number) => Math.round(channel * (1 - factor));
  return { r: mix(color.r), g: mix(color.g), b: mix(color.b) };
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
