/**
 * Jarvis motion system — shared foundation.
 *
 * Single source of truth for animation timing/easing tokens and for the
 * interface-motion preferences (quality tier, reduced motion, particles,
 * ambience). Preferences persist client-side (they are presentation-only,
 * per-device) the same way panel sizes already persist in localStorage.
 *
 * The store is framework-agnostic: components subscribe through
 * `useMotionPrefs()`, and the three.js core subscribes directly, so one
 * state drives WebGL, CSS and React without prop drilling.
 */

import { useCallback, useSyncExternalStore } from "react";

export type MotionQuality = "low" | "balanced" | "high";

export interface MotionPrefs {
  /** Animation budget: fewer/slower decals at low, everything at high. */
  quality: MotionQuality;
  /** Explicit user override for nonessential movement. */
  reduced: boolean;
  /** Decorative particle families (halo dust, orbit motes, tracers). */
  particles: boolean;
  /** Background ambience (page glow, gate ambience, float loops). */
  ambience: boolean;
}

export interface EffectiveMotionPrefs extends MotionPrefs {
  /** OS-level reduced-motion preference ORed with the user override. */
  reducedActive: boolean;
}

export const MOTION_STORE_KEY = "aze.motion.v1";
export const OS_REDUCE_MEDIA = "(prefers-reduced-motion: reduce)";

export const DEFAULT_MOTION_PREFS: MotionPrefs = {
  quality: "balanced",
  reduced: false,
  particles: true,
  ambience: true,
};

/* Duration tokens (ms). Mirrored as CSS custom properties in index.css. */
export const DUR = {
  fast: 120,
  base: 190,
  slow: 280,
  reveal: 460,
  stage: 700,
} as const;

/* Easing tokens. Mirrored as CSS custom properties in index.css. */
export const EASE = {
  /** Default perceptive ease-out for entrances and color shifts. */
  out: "cubic-bezier(.22, .93, .34, 1)",
  /** Symmetric ease for crossfades and continuous loops. */
  inOut: "cubic-bezier(.6, .08, .22, 1)",
  /** Subtle overshoot for select-to-active transitions. */
  spring: "cubic-bezier(.34, 1.32, .44, 1)",
} as const;

function sanitize(raw: unknown): MotionPrefs {
  const source = (raw ?? {}) as Partial<Record<keyof MotionPrefs, unknown>>;
  const quality = source.quality;
  return {
    quality: quality === "low" || quality === "high" ? quality : "balanced",
    reduced: Boolean(source.reduced),
    particles: source.particles === undefined ? true : Boolean(source.particles),
    ambience: source.ambience === undefined ? true : Boolean(source.ambience),
  };
}

function readStoredPrefs(): MotionPrefs {
  if (typeof window === "undefined") return { ...DEFAULT_MOTION_PREFS };
  try {
    const raw = window.localStorage.getItem(MOTION_STORE_KEY);
    return sanitize(raw ? JSON.parse(raw) : undefined);
  } catch {
    return { ...DEFAULT_MOTION_PREFS };
  }
}

const osReduce =
  typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia(OS_REDUCE_MEDIA)
    : null;

let currentPrefs: MotionPrefs = readStoredPrefs();
let osReduceActive = Boolean(osReduce?.matches);

const listeners = new Set<(prefs: EffectiveMotionPrefs) => void>();

/* The effective snapshot MUST be cached: useSyncExternalStore compares
 * getSnapshot results by reference, so returning a fresh object every call
 * would trigger an infinite re-render loop. The cache is rebuilt only when
 * an input changes. */
function buildEffective(): EffectiveMotionPrefs {
  return {
    ...currentPrefs,
    reducedActive: currentPrefs.reduced || osReduceActive,
  };
}

function refreshEffective(): void {
  effective = buildEffective();
}

let effective: EffectiveMotionPrefs = buildEffective();

function snapshot(): EffectiveMotionPrefs {
  return effective;
}

function emit(): void {
  const prefs = effective;
  for (const listener of listeners) listener(prefs);
}

if (osReduce) {
  // Keep the store live with the OS accessibility preference. Users who
  // set an explicit value still always win through `reducedActive`.
  try {
    osReduce.addEventListener("change", (event) => {
      osReduceActive = event.matches;
      refreshEffective();
      emit();
    });
  } catch {
    // Older browsers: the snapshot is refreshed on the next subscribe.
  }
}

export function getMotionPrefs(): EffectiveMotionPrefs {
  return snapshot();
}

export function setMotionPrefs(patch: Partial<MotionPrefs>): void {
  const next = sanitize({ ...currentPrefs, ...patch });
  const changed =
    next.quality !== currentPrefs.quality ||
    next.reduced !== currentPrefs.reduced ||
    next.particles !== currentPrefs.particles ||
    next.ambience !== currentPrefs.ambience;
  if (!changed) return;
  currentPrefs = next;
  refreshEffective();
  try {
    window.localStorage.setItem(MOTION_STORE_KEY, JSON.stringify(currentPrefs));
  } catch {
    // Storage unavailable (private mode): the preference still applies here.
  }
  emit();
}

export function subscribeMotionPrefs(
  listener: (prefs: EffectiveMotionPrefs) => void,
): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** React binding for the motion preference store. */
export function useMotionPrefs(): EffectiveMotionPrefs {
  return useSyncExternalStore(subscribeMotionPrefs, snapshot, snapshot);
}

/** Partial setter with a stable identity for callbacks. */
export function useSetMotionPrefs(): (patch: Partial<MotionPrefs>) => void {
  return useCallback((patch: Partial<MotionPrefs>) => setMotionPrefs(patch), []);
}

/**
 * Mirrors the effective preferences onto `documentElement` datasets so CSS
 * can respond without React re-renders:
 *   `data-motion`     = low | balanced | high
 *   `data-motion-reduced`  = "1" when movement should be minimised
 *   `data-particles`  = "0" when decorative particles are off
 *   `data-ambience`   = "0" when background ambience is off
 */
export function applyMotionDocument(): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  const prefs = snapshot();
  root.dataset.motion = prefs.quality;
  if (prefs.reducedActive) root.dataset.motionReduced = "1";
  else delete root.dataset.motionReduced;
  root.dataset.particles = prefs.particles ? "1" : "0";
  root.dataset.ambience = prefs.ambience ? "1" : "0";
}

/** Convenience multipliers for the three.js core. */
export function qualityScale(quality: MotionQuality): number {
  return quality === "low" ? 0.62 : quality === "high" ? 1.08 : 1;
}
