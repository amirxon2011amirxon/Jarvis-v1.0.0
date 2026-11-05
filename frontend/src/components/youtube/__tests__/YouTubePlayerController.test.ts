import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { YouTubePlayerController } from "../YouTubePlayerController";
import type {
  YTNamespace,
  YTPlayer,
  YTPlayerEvent,
  YTPlayerOptions,
  YouTubePlayerState,
} from "../youtubeTypes";

class FakeYTPlayer {
  readonly options: YTPlayerOptions;
  state = -1;
  current = 0;
  duration = 600;
  volume = 100;
  muted = false;
  videoId = "";
  destroyed = false;
  loadCalls: string[] = [];
  loadError: number | null = null;
  blockPlayback = false;
  ignoreVolume = false;

  constructor(options: YTPlayerOptions) {
    this.options = options;
    queueMicrotask(() => options.events?.onReady?.({}));
  }

  private emitState(next: number): void {
    this.state = next;
    this.options.events?.onStateChange?.({ data: next });
  }

  private emitError(code: number): void {
    this.options.events?.onError?.({ data: code } as YTPlayerEvent);
  }

  private setVideo(video: string | { videoId: string; startSeconds?: number }): void {
    const id = typeof video === "string" ? video : video.videoId;
    const start = typeof video === "string" ? undefined : video.startSeconds;
    this.videoId = id;
    this.current = start ?? 0;
    this.loadCalls.push(id);
  }

  playVideo(): void {
    if (this.blockPlayback) {
      this.emitState(2);
      return;
    }
    this.emitState(1);
  }

  pauseVideo(): void {
    this.emitState(2);
  }

  stopVideo(): void {
    this.emitState(5);
  }

  seekTo(seconds: number): void {
    this.current = Math.max(0, Math.min(seconds, this.duration));
  }

  loadVideoById(video: string | { videoId: string; startSeconds?: number }): void {
    this.setVideo(video);
    if (this.loadError !== null) {
      this.emitError(this.loadError);
      return;
    }
    if (this.blockPlayback) {
      this.emitState(2);
      return;
    }
    this.emitState(3);
    this.emitState(1);
  }

  cueVideoById(video: string | { videoId: string; startSeconds?: number }): void {
    this.setVideo(video);
    this.emitState(5);
  }

  getVolume(): number {
    return this.volume;
  }

  setVolume(volume: number): void {
    if (this.ignoreVolume) return;
    this.volume = Math.max(0, Math.min(100, Math.round(volume)));
  }

  mute(): void {
    this.muted = true;
  }

  unMute(): void {
    this.muted = false;
  }

  isMuted(): boolean {
    return this.muted;
  }

  getPlayerState(): number {
    return this.state;
  }

  getCurrentTime(): number {
    return this.current;
  }

  getDuration(): number {
    return this.duration;
  }

  getVideoData(): { video_id: string; title: string; author: string } {
    return {
      video_id: this.videoId,
      title: `Video ${this.videoId}`,
      author: `Channel ${this.videoId}`,
    };
  }

  addEventListener(): void {}
  removeEventListener(): void {}

  destroy(): void {
    this.destroyed = true;
  }
}

function setup(options: { blockPlayback?: boolean; loadError?: number } = {}) {
  let player: FakeYTPlayer | null = null;
  const createPlayer = vi.fn((_element: HTMLElement, playerOptions: YTPlayerOptions) => {
    player = new FakeYTPlayer(playerOptions);
    player.blockPlayback = Boolean(options.blockPlayback);
    player.loadError = options.loadError ?? null;
    return player as unknown as YTPlayer;
  });
  const loadApi = vi.fn(async () => ({ Player: FakeYTPlayer } as unknown as YTNamespace));
  const snapshots: YouTubePlayerState[] = [];
  const ready = vi.fn();
  const errors = vi.fn();
  const host = document.createElement("div");
  document.body.appendChild(host);
  const controller = new YouTubePlayerController({
    container: host,
    createPlayer,
    loadApi,
    onSnapshot: (state) => snapshots.push(state),
    onReady: ready,
    onError: errors,
    pollIntervalMs: 1_000,
  });
  createdControllers.push(controller);
  return {
    controller,
    createPlayer,
    loadApi,
    snapshots,
    ready,
    errors,
    get player() {
      return player;
    },
    host,
  };
}

const VIDEO = {
  video_id: "aaaaaaaaaaa",
  title: "Transformer architecture",
  channel: "AI Explained",
  duration_seconds: 600,
};

const createdControllers: YouTubePlayerController[] = [];

describe("YouTubePlayerController", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  afterEach(() => {
    while (createdControllers.length) {
      createdControllers.pop()?.destroy();
    }
  });

  it("initializes one player asynchronously and reports readiness", async () => {
    const harness = setup();
    await vi.waitFor(() => expect(harness.controller.isReady).toBe(true));
    expect(harness.loadApi).toHaveBeenCalledTimes(1);
    expect(harness.createPlayer).toHaveBeenCalledTimes(1);
    expect(harness.ready).toHaveBeenCalledTimes(1);
  });

  it("does not create a player if it is destroyed while the API loads", async () => {
    let release: (api: YTNamespace) => void = () => undefined;
    const loadApi = vi.fn(
      () => new Promise<YTNamespace>((resolve) => { release = resolve; }),
    );
    const createPlayer = vi.fn();
    const controller = new YouTubePlayerController({
      container: document.createElement("div"),
      loadApi,
      createPlayer,
      onSnapshot: () => undefined,
    });
    createdControllers.push(controller);
    controller.destroy();
    release({ Player: FakeYTPlayer } as unknown as YTNamespace);
    await Promise.resolve();
    await Promise.resolve();
    expect(createPlayer).not.toHaveBeenCalled();
  });

  it("loads and switches video in the same player instance", async () => {
    const harness = setup();
    await vi.waitFor(() => expect(harness.controller.isReady).toBe(true));
    const first = await harness.controller.load(VIDEO, true);
    expect(first.ok).toBe(true);
    expect(first.state.playback_state).toBe("playing");
    const second = await harness.controller.load(
      { ...VIDEO, video_id: "bbbbbbbbbbb", title: "Neural networks" },
      true,
    );
    expect(second.ok).toBe(true);
    expect(second.state.video_id).toBe("bbbbbbbbbbb");
    expect(harness.createPlayer).toHaveBeenCalledTimes(1);
    expect(harness.player?.loadCalls).toEqual(["aaaaaaaaaaa", "bbbbbbbbbbb"]);
  });

  it("cues a video without autoplay and reports it as cued", async () => {
    const harness = setup();
    await vi.waitFor(() => expect(harness.controller.isReady).toBe(true));
    const outcome = await harness.controller.load(VIDEO, false, 45);
    expect(outcome.ok).toBe(true);
    expect(outcome.state.playback_state).toBe("cued");
    expect(outcome.state.current_time).toBe(45);
  });

  it("marks a browser-blocked autoplay without faking playback", async () => {
    const harness = setup({ blockPlayback: true });
    await vi.waitFor(() => expect(harness.controller.isReady).toBe(true));
    const outcome = await harness.controller.load(VIDEO, true);
    expect(outcome.ok).toBe(true);
    expect(outcome.state.autoplay_blocked).toBe(true);
    expect(outcome.state.playback_state).toBe("paused");
  });

  it("pauses, plays and toggles through the real API", async () => {
    const harness = setup();
    await vi.waitFor(() => expect(harness.controller.isReady).toBe(true));
    await harness.controller.load(VIDEO, true);
    const paused = await harness.controller.execute("pause");
    expect(paused.ok).toBe(true);
    expect(paused.state.playback_state).toBe("paused");
    const played = await harness.controller.execute("play");
    expect(played.ok).toBe(true);
    expect(played.state.playback_state).toBe("playing");
    const toggled = await harness.controller.execute("toggle_playback");
    expect(toggled.ok).toBe(true);
    expect(toggled.state.playback_state).toBe("paused");
  });

  it("seeks, sets volume and mutes with verified state", async () => {
    const harness = setup();
    await vi.waitFor(() => expect(harness.controller.isReady).toBe(true));
    await harness.controller.load(VIDEO, true);
    const seek = await harness.controller.execute("seek_forward", { seconds: 30 });
    expect(seek.ok).toBe(true);
    expect(seek.state.current_time).toBe(30);
    const volume = await harness.controller.execute("set_volume", { volume: 60 });
    expect(volume.ok).toBe(true);
    expect(volume.state.volume).toBe(60);
    const mute = await harness.controller.execute("mute");
    expect(mute.state.muted).toBe(true);
    const unmute = await harness.controller.execute("unmute");
    expect(unmute.state.muted).toBe(false);
  });

  it("surfaces an embedding error from the real player", async () => {
    const harness = setup({ loadError: 101 });
    await vi.waitFor(() => expect(harness.controller.isReady).toBe(true));
    const outcome = await harness.controller.load(VIDEO, true);
    expect(outcome.ok).toBe(false);
    expect(outcome.error?.code).toBe("embedding_disabled");
    expect(harness.errors).toHaveBeenCalled();
  });

  it("reports a failed control without marking playback unavailable", async () => {
    const harness = setup();
    await vi.waitFor(() => expect(harness.controller.isReady).toBe(true));
    await harness.controller.load(VIDEO, true);
    harness.player!.ignoreVolume = true;
    const outcome = await harness.controller.execute("set_volume", {
      volume: 20,
    });
    expect(outcome.ok).toBe(false);
    expect(outcome.error?.code).toBe("command_failed");
    expect(outcome.state.error).toBeNull();
    expect(harness.controller.getSnapshot().error).toBeNull();
  });

  it("returns a synchronized snapshot for get_status", async () => {
    const harness = setup();
    await vi.waitFor(() => expect(harness.controller.isReady).toBe(true));
    await harness.controller.load(VIDEO, true);
    harness.player!.current = 88;
    const outcome = await harness.controller.execute("get_status");
    expect(outcome.ok).toBe(true);
    expect(outcome.state.current_time).toBe(88);
  });

  it("destroys the player and its timers on cleanup", async () => {
    const harness = setup();
    await vi.waitFor(() => expect(harness.controller.isReady).toBe(true));
    await harness.controller.load(VIDEO, true);
    harness.controller.destroy();
    expect(harness.player?.destroyed).toBe(true);
    expect(harness.controller.getSnapshot().panel_open).toBe(false);
    const after = await harness.controller.execute("pause");
    expect(after.ok).toBe(false);
  });
});
