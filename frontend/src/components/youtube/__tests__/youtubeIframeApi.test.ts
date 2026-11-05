import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  IFRAME_API_TIMEOUT_MS,
  loadYouTubeIframeApi,
  resetYouTubeIframeApiForTests,
} from "../youtubeIframeApi";
import type { YTNamespace } from "../youtubeTypes";

const SCRIPT_SELECTOR = "script[data-jarvis-youtube-api]";
const FakePlayer = class {};

function fakeNamespace(): YTNamespace {
  return { Player: FakePlayer } as unknown as YTNamespace;
}

function removeApiScripts(): void {
  document.querySelectorAll(SCRIPT_SELECTOR).forEach((script) => script.remove());
}

describe("YouTube IFrame API loader", () => {
  beforeEach(() => {
    removeApiScripts();
    resetYouTubeIframeApiForTests();
    delete window.YT;
    delete window.onYouTubeIframeAPIReady;
  });

  it("resolves immediately when the API is already present", async () => {
    const api = fakeNamespace();
    window.YT = api;
    await expect(loadYouTubeIframeApi()).resolves.toBe(api);
    expect(document.querySelector(SCRIPT_SELECTOR)).toBeNull();
  });

  it("injects the official script exactly once for concurrent callers", async () => {
    const first = loadYouTubeIframeApi();
    const second = loadYouTubeIframeApi();
    expect(first).toBe(second);
    expect(document.querySelectorAll(SCRIPT_SELECTOR)).toHaveLength(1);

    const api = fakeNamespace();
    window.YT = api;
    window.onYouTubeIframeAPIReady?.();
    await expect(first).resolves.toBe(api);
  });

  it("reuses a script another caller already injected", async () => {
    const existing = document.createElement("script");
    existing.setAttribute("data-jarvis-youtube-api", "1");
    document.head.appendChild(existing);

    const promise = loadYouTubeIframeApi();
    expect(document.querySelectorAll(SCRIPT_SELECTOR)).toHaveLength(1);
    window.YT = fakeNamespace();
    window.onYouTubeIframeAPIReady?.();
    await expect(promise).resolves.toBe(window.YT);
    expect(document.querySelectorAll(SCRIPT_SELECTOR)).toHaveLength(1);
  });

  it("chains an existing ready callback instead of replacing it", async () => {
    const previous = vi.fn();
    window.onYouTubeIframeAPIReady = previous;
    const promise = loadYouTubeIframeApi();
    window.YT = fakeNamespace();
    window.onYouTubeIframeAPIReady?.();
    await promise;
    expect(previous).toHaveBeenCalledTimes(1);
  });

  it("times out, removes the failed script and can retry", async () => {
    vi.useFakeTimers();
    const promise = loadYouTubeIframeApi();
    const assertion = expect(promise).rejects.toMatchObject({
      code: "iframe_api_timeout",
    });
    await vi.advanceTimersByTimeAsync(IFRAME_API_TIMEOUT_MS);
    await assertion;
    expect(document.querySelector(SCRIPT_SELECTOR)).toBeNull();

    const retry = loadYouTubeIframeApi();
    expect(document.querySelectorAll(SCRIPT_SELECTOR)).toHaveLength(1);
    window.YT = fakeNamespace();
    window.onYouTubeIframeAPIReady?.();
    await expect(retry).resolves.toBe(window.YT);
  });

  it("reports a script network failure and resets for a later attempt", async () => {
    const promise = loadYouTubeIframeApi();
    const assertion = expect(promise).rejects.toMatchObject({
      code: "iframe_api_unreachable",
    });
    const script = document.querySelector<HTMLScriptElement>(SCRIPT_SELECTOR);
    expect(script).not.toBeNull();
    script?.dispatchEvent(new Event("error"));
    await assertion;
    expect(document.querySelector(SCRIPT_SELECTOR)).toBeNull();
  });
});
