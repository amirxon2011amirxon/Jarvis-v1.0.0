import {
  loadYouTubeIframeApi,
} from "./youtubeIframeApi";
import type {
  PlayerFactory,
  YTNamespace,
  YTPlayer,
  YTPlayerOptions,
  YouTubeCommandOutcome,
  YouTubePlayerError,
  YouTubePlayerState,
  YouTubePlaybackState,
  YouTubeVideo,
} from "./youtubeTypes";

const PLAYER_STATES = {
  UNSTARTED: -1,
  ENDED: 0,
  PLAYING: 1,
  PAUSED: 2,
  BUFFERING: 3,
  CUED: 5,
} as const;

const READY_TIMEOUT_MS = 10_000;
const LOAD_EVENT_TIMEOUT_MS = 6_000;
const COMMAND_TIMEOUT_MS = 3_000;
const SEEK_TIMEOUT_MS = 2_000;
const SEEK_TOLERANCE_S = 2.5;
const POLL_INTERVAL_MS = 1_000;
const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;

type StateWaiter = {
  predicate: (state: YouTubePlaybackState) => boolean;
  resolve: (state: YouTubePlaybackState) => void;
};

export interface YouTubeControllerOptions {
  container: HTMLElement;
  onSnapshot: (state: YouTubePlayerState) => void;
  onReady?: (state: YouTubePlayerState) => void;
  onError?: (error: YouTubePlayerError, state: YouTubePlayerState) => void;
  /** Test seams; production uses the official API. */
  createPlayer?: PlayerFactory;
  loadApi?: () => Promise<YTNamespace>;
  pollIntervalMs?: number;
}

interface Deferred {
  promise: Promise<void>;
  resolve: () => void;
  reject: (error: Error) => void;
}

function deferred(): Deferred {
  let resolve!: () => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<void>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function asNumber(value: unknown, fallback = 0): number {
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function playerErrorMessage(code: number): YouTubePlayerError {
  switch (code) {
    case 2:
      return {
        code: "invalid_video_id",
        message: "YouTube rejected the video ID.",
      };
    case 5:
      return {
        code: "player_error",
        message: "The HTML5 YouTube player could not play this video.",
      };
    case 100:
      return {
        code: "video_not_found",
        message: "The video was removed or is not available.",
      };
    case 101:
    case 150:
      return {
        code: "embedding_disabled",
        message: "The video owner does not allow embedded playback.",
      };
    default:
      return {
        code: "player_error",
        message: `The YouTube player reported error ${code}.`,
      };
  }
}

export class YouTubePlayerController {
  private readonly container: HTMLElement;
  private readonly onSnapshot: (state: YouTubePlayerState) => void;
  private readonly onReadyCallback?: (state: YouTubePlayerState) => void;
  private readonly onErrorCallback?: (
    error: YouTubePlayerError,
    state: YouTubePlayerState,
  ) => void;
  private readonly createPlayerFactory?: PlayerFactory;
  private readonly loadApi: () => Promise<YTNamespace>;
  private readonly ready = deferred();
  private readonly pollIntervalMs: number;

  private player: YTPlayer | null = null;
  private destroyed = false;
  private readyTimer: number | null = null;
  private pollTimer: number | null = null;
  private readonly waiters = new Set<StateWaiter>();
  private requestedVideoId = "";

  private state: YouTubePlayerState = {
    panel_open: true,
    player_ready: false,
    video_id: "",
    title: "",
    channel: "",
    playback_state: "unstarted",
    current_time: 0,
    duration: 0,
    volume: 100,
    muted: false,
    fullscreen: false,
    autoplay_blocked: false,
    error: null,
  };

  constructor(options: YouTubeControllerOptions) {
    this.container = options.container;
    this.onSnapshot = options.onSnapshot;
    this.onReadyCallback = options.onReady;
    this.onErrorCallback = options.onError;
    this.createPlayerFactory = options.createPlayer;
    this.loadApi = options.loadApi ?? loadYouTubeIframeApi;
    this.pollIntervalMs = options.pollIntervalMs ?? POLL_INTERVAL_MS;

    // Kick off initialization immediately; load() awaits the same promise.
    void this.ready.promise.catch(() => undefined);
    void this.initialize().catch(() => undefined);
  }

  /* ── lifecycle ─────────────────────────────────────────────────────────── */

  private async initialize(): Promise<void> {
    try {
      const api = await this.loadApi();
      if (this.destroyed) return;
      const factory: PlayerFactory =
        this.createPlayerFactory ??
        ((element, playerOptions) => new api.Player(element, playerOptions));
      const options: YTPlayerOptions = {
        width: "100%",
        height: "100%",
        playerVars: {
          autoplay: 0,
          controls: 1,
          enablejsapi: 1,
          hl: "en",
          iv_load_policy: 3,
          modestbranding: 1,
          playsinline: 1,
          rel: 0,
          origin: window.location.origin,
        },
        events: {
          onReady: () => this.handleReady(),
          onStateChange: (event) => this.handleStateChange(asNumber(event.data, -1)),
          onError: (event) => this.handlePlayerError(asNumber(event.data, 5)),
        },
      };
      this.player = factory(this.container, options);
      this.readyTimer = window.setTimeout(() => {
        if (this.state.player_ready || this.destroyed) return;
        this.fail(
          "player_init_failed",
          "The YouTube player did not become ready in time.",
        );
      }, READY_TIMEOUT_MS);
    } catch (error) {
      if (this.destroyed) return;
      const code =
        error && typeof error === "object" && "code" in error
          ? String((error as { code?: unknown }).code)
          : "player_init_failed";
      const message =
        error instanceof Error
          ? error.message
          : "The YouTube player could not be initialized.";
      this.fail(code, message);
    }
  }

  private handleReady(): void {
    if (this.destroyed) {
      this.destroyPlayer();
      return;
    }
    if (this.readyTimer !== null) {
      window.clearTimeout(this.readyTimer);
      this.readyTimer = null;
    }
    this.state.player_ready = true;
    this.refreshFromPlayer();
    this.ready.resolve();
    this.emit(true);
    this.onReadyCallback?.(this.getSnapshot());
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    this.stopPolling();
    if (this.readyTimer !== null) {
      window.clearTimeout(this.readyTimer);
      this.readyTimer = null;
    }
    this.resolveWaiters("stopped");
    this.destroyPlayer();
    this.state = {
      ...this.state,
      panel_open: false,
      player_ready: false,
      playback_state: "closed",
      error: null,
    };
    // Do not emit after unmount; the panel sends its final state in the close
    // acknowledgement instead.
  }

  private destroyPlayer(): void {
    if (!this.player) return;
    try {
      this.player.destroy();
    } catch {
      // Destroying an already-gone iframe is harmless.
    }
    this.player = null;
  }

  private fail(code: string, message: string): void {
    const error = { code, message };
    this.state = {
      ...this.state,
      player_ready: false,
      error,
      playback_state: "error",
    };
    this.stopPolling();
    const readyError = new Error(message) as Error & { code?: string };
    readyError.code = code;
    this.ready.reject(readyError);
    this.resolveWaiters("error");
    this.emit(false);
    this.onErrorCallback?.(error, this.getSnapshot());
  }

  /* ── public commands ───────────────────────────────────────────────────── */

  async load(
    video: YouTubeVideo,
    autoplay: boolean,
    startSeconds = 0,
  ): Promise<YouTubeCommandOutcome> {
    if (!VIDEO_ID.test(video.video_id)) {
      return this.fatalErrorOutcome(
        "invalid_video_id",
        "The video ID is not a valid YouTube ID.",
      );
    }
    try {
      await this.ensureReady();
      if (!this.player) {
        return this.fatalErrorOutcome(
          "player_not_ready",
          "The YouTube player is not available.",
        );
      }
      const start = Math.max(0, asNumber(startSeconds));
      this.requestedVideoId = video.video_id;
      this.state = {
        ...this.state,
        panel_open: true,
        player_ready: true,
        video_id: video.video_id,
        title: video.title || "YouTube video",
        channel: video.channel || "",
        duration: Math.max(0, asNumber(video.duration_seconds)),
        current_time: start,
        playback_state: "loading",
        autoplay_blocked: false,
        error: null,
      };
      this.emit(true);

      if (autoplay) {
        this.player.loadVideoById({
          videoId: video.video_id,
          startSeconds: start,
        });
      } else {
        this.player.cueVideoById({
          videoId: video.video_id,
          startSeconds: start,
        });
      }

      const reached = await this.waitForState(
        (state) =>
          state === "playing" ||
          state === "buffering" ||
          state === "paused" ||
          state === "cued" ||
          state === "ended" ||
          state === "error",
        LOAD_EVENT_TIMEOUT_MS,
      );

      this.refreshFromPlayer();
      if (this.state.error) {
        return {
          ok: false,
          state: this.getSnapshot(),
          error: this.state.error,
        };
      }
      const playback = this.state.playback_state;
      if (autoplay && (playback === "paused" || playback === "cued")) {
        this.state = {
          ...this.state,
          autoplay_blocked: true,
        };
        this.emit(true);
      }
      if (!reached && playback === "loading") {
        this.state = {
          ...this.state,
          playback_state: "buffering",
        };
        this.emit(true);
      }
      return { ok: true, state: this.getSnapshot() };
    } catch (error) {
      const fallback =
        error && typeof error === "object" && "code" in error
          ? String((error as { code?: unknown }).code)
          : "load_failed";
      const message =
        error instanceof Error
          ? error.message
          : "The video could not be loaded.";
      return this.fatalErrorOutcome(fallback, message);
    }
  }

  async execute(
    command: string,
    args: Record<string, unknown> = {},
  ): Promise<YouTubeCommandOutcome> {
    try {
      await this.ensureReady();
      if (!this.player) {
        return this.commandErrorOutcome(
          "player_not_ready",
          "The YouTube player is not ready yet.",
        );
      }
      switch (command) {
        case "play":
          return await this.play();
        case "pause":
          return await this.pause();
        case "toggle_playback":
          return await this.togglePlayback();
        case "stop":
          return await this.stop();
        case "seek_forward":
          return await this.seekTo(
            this.currentTime() + Math.max(0, asNumber(args.seconds, 10)),
          );
        case "seek_backward":
          return await this.seekTo(
            this.currentTime() - Math.max(0, asNumber(args.seconds, 10)),
          );
        case "seek_to":
          return await this.seekTo(asNumber(args.seconds));
        case "volume_up":
          return await this.setVolume(
            this.currentVolume() + Math.max(1, asNumber(args.step, 10)),
          );
        case "volume_down":
          return await this.setVolume(
            this.currentVolume() - Math.max(1, asNumber(args.step, 10)),
          );
        case "set_volume":
          return await this.setVolume(asNumber(args.volume));
        case "mute":
          return await this.setMuted(true);
        case "unmute":
          return await this.setMuted(false);
        case "get_status":
          this.refreshFromPlayer();
          this.emit(false);
          return { ok: true, state: this.getSnapshot() };
        default:
          return this.commandErrorOutcome(
            "invalid_operation",
            `The YouTube player does not support '${command}'.`,
          );
      }
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : "The YouTube command failed.";
      return this.commandErrorOutcome("command_failed", message);
    }
  }

  private async play(): Promise<YouTubeCommandOutcome> {
    if (this.currentPlayerState() === PLAYER_STATES.PLAYING) {
      return this.successOutcome();
    }
    this.player?.playVideo();
    const reached = await this.waitForState(
      (state) => state === "playing" || state === "buffering",
      COMMAND_TIMEOUT_MS,
    );
    this.refreshFromPlayer();
    if (!reached) {
      if (this.currentPlayerState() === PLAYER_STATES.PLAYING) {
        return this.successOutcome();
      }
      this.state = { ...this.state, autoplay_blocked: true };
      this.emit(true);
      return {
        ok: false,
        state: this.getSnapshot(),
        error: {
          code: "autoplay_blocked",
          message:
            "The browser blocked playback. Press play in the YouTube panel to start it.",
        },
      };
    }
    return this.successOutcome();
  }

  private async pause(): Promise<YouTubeCommandOutcome> {
    if (this.currentPlayerState() === PLAYER_STATES.PAUSED) {
      return this.successOutcome();
    }
    this.player?.pauseVideo();
    const reached = await this.waitForState(
      (state) => state === "paused",
      COMMAND_TIMEOUT_MS,
    );
    this.refreshFromPlayer();
    const status = this.currentPlayerState();
    const alreadyStopped =
      status === PLAYER_STATES.PAUSED ||
      status === PLAYER_STATES.CUED ||
      status === PLAYER_STATES.UNSTARTED ||
      status === PLAYER_STATES.ENDED;
    if (!reached && !alreadyStopped) {
      return this.commandErrorOutcome(
        "command_failed",
        "The video did not pause.",
      );
    }
    return this.successOutcome();
  }

  private async togglePlayback(): Promise<YouTubeCommandOutcome> {
    const status = this.currentPlayerState();
    if (status === PLAYER_STATES.PLAYING || status === PLAYER_STATES.BUFFERING) {
      return this.pause();
    }
    return this.play();
  }

  private async stop(): Promise<YouTubeCommandOutcome> {
    this.player?.stopVideo();
    await this.waitForState(
      (state) => state !== "playing" && state !== "buffering",
      COMMAND_TIMEOUT_MS,
    );
    this.refreshFromPlayer();
    const status = this.currentPlayerState();
    if (status === PLAYER_STATES.PLAYING || status === PLAYER_STATES.BUFFERING) {
      return this.commandErrorOutcome("command_failed", "The video did not stop.");
    }
    this.state = { ...this.state, error: null, autoplay_blocked: false };
    this.emit(true);
    return this.successOutcome();
  }

  private async seekTo(targetSeconds: number): Promise<YouTubeCommandOutcome> {
    if (!Number.isFinite(targetSeconds)) {
      return this.commandErrorOutcome("invalid_parameter", "The seek time is invalid.");
    }
    const duration = this.currentDuration();
    const target = Math.max(
      0,
      duration > 0 ? Math.min(targetSeconds, duration) : targetSeconds,
    );
    const startedAt = this.currentTime();
    this.player?.seekTo(target, true);
    // The IFrame API has no seek-complete callback. Give the player a moment
    // to publish the new time, then report the real snapshot. A slow
    // timestamp read must not turn a working seek into a failed tool.
    await this.waitForSeek(target, startedAt);
    this.refreshFromPlayer();
    return this.successOutcome();
  }

  private async setVolume(target: number): Promise<YouTubeCommandOutcome> {
    if (!Number.isFinite(target)) {
      return this.commandErrorOutcome("invalid_parameter", "The volume is invalid.");
    }
    const volume = Math.max(0, Math.min(100, Math.round(target)));
    const before = this.currentVolume();
    if (this.currentMuted()) {
      this.player?.unMute();
    }
    this.player?.setVolume(volume);
    // YouTube returns the old volume for a tick or two after setVolume. Wait
    // for the real value instead of declaring the command failed and lighting
    // up the tool bus even though the volume visibly changed.
    await this.waitForValue(
      () => Math.abs(this.currentVolume() - volume) <= 1,
      800,
    );
    const actual = this.currentVolume();
    this.state = {
      ...this.state,
      volume: actual,
      muted: this.currentMuted(),
    };
    this.emit(true);
    if (Math.abs(actual - volume) > 1 && actual === before) {
      return this.commandErrorOutcome(
        "command_failed",
        "The player did not accept the volume change.",
      );
    }
    // The actual volume is in the returned state. A stale read is a timing
    // detail, not a failed user action.
    return this.successOutcome();
  }

  private async setMuted(muted: boolean): Promise<YouTubeCommandOutcome> {
    if (muted) this.player?.mute();
    else this.player?.unMute();
    await this.waitForValue(() => this.currentMuted() === muted, 800);
    const actual = this.currentMuted();
    this.state = {
      ...this.state,
      muted: actual,
      volume: this.currentVolume(),
    };
    this.emit(true);
    if (actual !== muted) {
      return this.commandErrorOutcome(
        "command_failed",
        muted ? "The player did not mute." : "The player did not unmute.",
      );
    }
    return this.successOutcome();
  }

  setFullscreen(fullscreen: boolean): YouTubePlayerState {
    if (this.state.fullscreen !== fullscreen) {
      this.state = { ...this.state, fullscreen: Boolean(fullscreen) };
      this.emit(true);
    }
    return this.getSnapshot();
  }

  getSnapshot(): YouTubePlayerState {
    return { ...this.state };
  }

  get isReady(): boolean {
    return this.state.player_ready;
  }

  private async ensureReady(): Promise<void> {
    if (this.destroyed) {
      throw new Error("The YouTube player has been closed.");
    }
    if (this.state.player_ready) return;
    let timer: number | null = null;
    try {
      await Promise.race([
        this.ready.promise,
        new Promise<never>((_resolve, reject) => {
          timer = window.setTimeout(
            () =>
              reject(
                Object.assign(
                  new Error("The YouTube player is still starting."),
                  { code: "player_not_ready" },
                ),
              ),
            READY_TIMEOUT_MS,
          );
        }),
      ]);
    } finally {
      if (timer !== null) window.clearTimeout(timer);
    }
  }

  private currentPlayerState(): number {
    try {
      return this.player?.getPlayerState() ?? PLAYER_STATES.UNSTARTED;
    } catch {
      return PLAYER_STATES.UNSTARTED;
    }
  }

  private currentTime(): number {
    try {
      return Math.max(0, asNumber(this.player?.getCurrentTime(), 0));
    } catch {
      return 0;
    }
  }

  private currentDuration(): number {
    try {
      return Math.max(0, asNumber(this.player?.getDuration(), 0));
    } catch {
      return 0;
    }
  }

  private currentVolume(): number {
    try {
      return Math.max(0, Math.min(100, Math.round(
        asNumber(this.player?.getVolume(), 100),
      )));
    } catch {
      return this.state.volume;
    }
  }

  private currentMuted(): boolean {
    try {
      return Boolean(this.player?.isMuted());
    } catch {
      return this.state.muted;
    }
  }

  /* ── state synchronization ─────────────────────────────────────────────── */

  private handleStateChange(code: number): void {
    if (this.destroyed) return;
    const playback = this.playbackFromCode(code);
    this.state = {
      ...this.state,
      playback_state: playback,
    };
    this.refreshFromPlayer();
    if (playback === "ended") {
      this.state = {
        ...this.state,
        current_time: this.state.duration,
      };
    }
    if (playback === "ended" || playback === "error") {
      this.stopPolling();
    } else {
      this.startPolling();
    }
    this.emit(true);
    this.resolveWaiters(playback);
  }

  private playbackFromCode(code: number): YouTubePlaybackState {
    switch (code) {
      case PLAYER_STATES.PLAYING:
        return "playing";
      case PLAYER_STATES.PAUSED:
        return "paused";
      case PLAYER_STATES.BUFFERING:
        return "buffering";
      case PLAYER_STATES.ENDED:
        return "ended";
      case PLAYER_STATES.CUED:
        return "cued";
      case PLAYER_STATES.UNSTARTED:
        return "unstarted";
      default:
        return this.state.playback_state;
    }
  }

  private refreshFromPlayer(): void {
    if (!this.player) return;
    let videoData: { video_id?: string; title?: string; author?: string } = {};
    try {
      videoData = this.player.getVideoData() ?? {};
    } catch {
      videoData = {};
    }
    const reportedId = String(videoData.video_id || "");
    const expectedId = this.requestedVideoId || this.state.video_id;
    const reportedIsCurrent =
      VIDEO_ID.test(reportedId) &&
      (!expectedId || reportedId === expectedId);
    const id = reportedIsCurrent
      ? reportedId
      : this.state.video_id || this.requestedVideoId;
    const title = (reportedIsCurrent
      ? String(videoData.title || "").trim()
      : "") || this.state.title;
    const channel =
      (reportedIsCurrent
        ? String(videoData.author || "").trim()
        : "") || this.state.channel;
    const duration = this.currentDuration() || this.state.duration;
    const current = this.currentTime();
    const volume = this.currentVolume();
    const muted = this.currentMuted();
    this.state = {
      ...this.state,
      video_id: id,
      title,
      channel,
      duration,
      current_time: duration > 0 ? Math.min(current, duration) : current,
      volume,
      muted,
      player_ready: true,
    };
  }

  private startPolling(): void {
    if (this.pollTimer !== null) return;
    this.pollTimer = window.setInterval(() => {
      if (this.destroyed || !this.player) return;
      const status = this.currentPlayerState();
      if (status === PLAYER_STATES.ENDED || this.state.error) {
        this.stopPolling();
        return;
      }
      // Keep a light timestamp poll while paused too: a native seek in the
      // YouTube controls fires no state event, so without this the custom
      // timeline drifts away from the real playhead.
      const beforeTime = this.state.current_time;
      const beforeDuration = this.state.duration;
      const beforePlayback = this.state.playback_state;
      this.refreshFromPlayer();
      const changed =
        Math.abs(this.state.current_time - beforeTime) > 0.05 ||
        Math.abs(this.state.duration - beforeDuration) > 0.05 ||
        this.state.playback_state !== beforePlayback ||
        status === PLAYER_STATES.PLAYING ||
        status === PLAYER_STATES.BUFFERING;
      if (changed) this.emit(false);
    }, this.pollIntervalMs);
  }

  private stopPolling(): void {
    if (this.pollTimer === null) return;
    window.clearInterval(this.pollTimer);
    this.pollTimer = null;
  }

  private handlePlayerError(code: number): void {
    if (this.destroyed) return;
    const error = playerErrorMessage(code);
    this.state = {
      ...this.state,
      error,
      playback_state: "error",
      player_ready: this.state.player_ready,
    };
    this.stopPolling();
    this.emit(true);
    this.resolveWaiters("error");
    this.onErrorCallback?.(error, this.getSnapshot());
  }

  private emit(immediate: boolean): void {
    if (this.destroyed) return;
    const snapshot = this.getSnapshot();
    try {
      this.onSnapshot(snapshot);
    } catch {
      // A broken UI callback must not take down the player.
    }
    void immediate;
  }

  private resolveWaiters(state: YouTubePlaybackState): void {
    for (const waiter of [...this.waiters]) {
      if (waiter.predicate(state) || state === "error") {
        this.waiters.delete(waiter);
        waiter.resolve(state);
      }
    }
  }

  private waitForState(
    predicate: (state: YouTubePlaybackState) => boolean,
    timeoutMs: number,
  ): Promise<YouTubePlaybackState | null> {
    const current = this.state.playback_state;
    if (predicate(current)) return Promise.resolve(current);
    return new Promise((resolve) => {
      let settled = false;
      const waiter: StateWaiter = {
        predicate,
        resolve: (state) => {
          if (settled) return;
          settled = true;
          window.clearTimeout(timer);
          this.waiters.delete(waiter);
          resolve(state);
        },
      };
      const timer = window.setTimeout(() => {
        if (settled) return;
        settled = true;
        this.waiters.delete(waiter);
        resolve(null);
      }, Math.max(1, timeoutMs));
      this.waiters.add(waiter);
    });
  }

  private waitForValue(
    predicate: () => boolean,
    timeoutMs: number,
  ): Promise<boolean> {
    if (predicate()) return Promise.resolve(true);
    return new Promise((resolve) => {
      let settled = false;
      const finish = (value: boolean) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        window.clearInterval(poll);
        resolve(value);
      };
      const timer = window.setTimeout(() => finish(predicate()), timeoutMs);
      const poll = window.setInterval(() => {
        if (this.destroyed) {
          finish(true);
          return;
        }
        if (predicate()) finish(true);
      }, 50);
    });
  }

  private waitForSeek(target: number, startedAt: number): Promise<boolean> {
    const nearTarget = (tolerance: number) =>
      Math.abs(this.currentTime() - target) <= tolerance;
    if (nearTarget(SEEK_TOLERANCE_S)) {
      return Promise.resolve(true);
    }
    return new Promise((resolve) => {
      let settled = false;
      const finish = (reached: boolean) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        window.clearInterval(poll);
        resolve(reached);
      };
      const timer = window.setTimeout(() => {
        // The IFrame API has no seek-complete event and may expose the new
        // time only after buffering. The seek call itself is the execution
        // result; never turn a slow timestamp refresh into a failed tool.
        finish(true);
      }, SEEK_TIMEOUT_MS);
      const poll = window.setInterval(() => {
        if (this.destroyed) {
          finish(true);
          return;
        }
        if (nearTarget(SEEK_TOLERANCE_S)) {
          finish(true);
          return;
        }
        // Some players only expose the new time after a buffering round-trip.
        // Detect measurable movement toward the target rather than requiring
        // an exact frame immediately.
        const targetDistance = Math.abs(target - startedAt);
        const moved = Math.abs(this.currentTime() - startedAt);
        if (targetDistance > 3 && moved >= Math.min(3, targetDistance * 0.25)) {
          finish(true);
        }
      }, 100);
    });
  }

  private successOutcome(): YouTubeCommandOutcome {
    this.refreshFromPlayer();
    this.state = { ...this.state, error: null };
    this.emit(false);
    return { ok: true, state: this.getSnapshot() };
  }

  private commandErrorOutcome(
    code: string,
    message: string,
  ): YouTubeCommandOutcome {
    // A failed pause/seek/volume command is not a broken video. It becomes a
    // dismissible notice in the panel, never the "playback unavailable"
    // overlay that is reserved for genuine load/player failures.
    return {
      ok: false,
      state: this.getSnapshot(),
      error: { code, message },
    };
  }

  private fatalErrorOutcome(
    code: string,
    message: string,
  ): YouTubeCommandOutcome {
    const error = { code, message };
    this.stopPolling();
    this.state = {
      ...this.state,
      error,
      playback_state: "error",
    };
    this.emit(true);
    return { ok: false, state: this.getSnapshot(), error };
  }
}
