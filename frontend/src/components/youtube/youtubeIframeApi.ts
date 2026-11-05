import type { YTNamespace } from "./youtubeTypes";

export class YouTubeIframeError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "YouTubeIframeError";
    this.code = code;
  }
}

export const IFRAME_API_TIMEOUT_MS = 15_000;
const SCRIPT_ATTR = "data-jarvis-youtube-api";

let apiPromise: Promise<YTNamespace> | null = null;

/**
 * Load https://www.youtube.com/iframe_api at most once.
 *
 * The script is only injected when the player panel actually mounts. Every
 * caller (including React StrictMode's double effect) shares one promise, and
 * the global ready callback is chained rather than replaced.
 */
export function loadYouTubeIframeApi(): Promise<YTNamespace> {
  if (typeof window === "undefined" || typeof document === "undefined") {
    return Promise.reject(
      new YouTubeIframeError(
        "player_init_failed",
        "The YouTube player requires a browser document.",
      ),
    );
  }
  if (window.YT?.Player) return Promise.resolve(window.YT);
  if (apiPromise) return apiPromise;

  apiPromise = new Promise<YTNamespace>((resolve, reject) => {
    let settled = false;
    let timer: number | null = null;

    const finish = (api?: YTNamespace, error?: YouTubeIframeError) => {
      if (settled) return;
      settled = true;
      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
      if (error) {
        // A failed load must be retryable: drop the rejected promise and the
        // script so a later attempt can start cleanly.
        apiPromise = null;
        const script = document.querySelector<HTMLScriptElement>(
          `script[${SCRIPT_ATTR}]`,
        );
        script?.remove();
        reject(error);
        return;
      }
      if (!api?.Player) {
        apiPromise = null;
        reject(
          new YouTubeIframeError(
            "player_init_failed",
            "YouTube's IFrame API loaded without a player constructor.",
          ),
        );
        return;
      }
      resolve(api);
    };

    const previous = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = () => {
      try {
        previous?.();
      } finally {
        finish(window.YT);
      }
    };

    timer = window.setTimeout(() => {
      finish(
        undefined,
        new YouTubeIframeError(
          "iframe_api_timeout",
          "YouTube's player did not load in time. Check the internet connection and try again.",
        ),
      );
    }, IFRAME_API_TIMEOUT_MS);

    let script = document.querySelector<HTMLScriptElement>(
      `script[${SCRIPT_ATTR}]`,
    );
    if (!script) {
      script = document.createElement("script");
      script.src = "https://www.youtube.com/iframe_api";
      script.async = true;
      script.defer = true;
      script.setAttribute(SCRIPT_ATTR, "1");
      script.onerror = () =>
        finish(
          undefined,
          new YouTubeIframeError(
            "iframe_api_unreachable",
            "YouTube's player script could not be loaded. Check the internet connection and try again.",
          ),
        );
      document.head.appendChild(script);
    }
  });

  return apiPromise;
}

/** Test-only reset; production code never needs to unload the API. */
export function resetYouTubeIframeApiForTests(): void {
  apiPromise = null;
  if (typeof window !== "undefined") {
    window.onYouTubeIframeAPIReady = undefined;
  }
}
