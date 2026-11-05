import { memo, useCallback, useEffect, useRef, useState } from "react";

import { cn } from "../../lib/utils";
import {
  AlertIcon,
  ForwardIcon,
  FullscreenExitIcon,
  MaximizeIcon,
  PauseIcon,
  PlayIcon,
  RewindIcon,
  VolumeIcon,
  VolumeOffIcon,
  XIcon,
  YouTubeMarkIcon,
} from "../ui/Icons";
import { YouTubePlayer, type YouTubePlayerHandle } from "./YouTubePlayer";
import type {
  YouTubeCommand,
  YouTubeCommandOutcome,
  YouTubePlayerError,
  YouTubePlayerState,
  YouTubeVideo,
} from "./youtubeTypes";

export interface YouTubePlayerPanelProps {
  open: boolean;
  closing: boolean;
  video: YouTubeVideo | null;
  autoplay: boolean;
  startSeconds: number;
  loadKey: number;
  loadRequestId: string;
  closeRequestId: string;
  command: YouTubeCommand | null;
  socketOnline: boolean;
  onState: (state: YouTubePlayerState) => void;
  onReady: (state: YouTubePlayerState) => void;
  onError: (error: YouTubePlayerError, state: YouTubePlayerState) => void;
  onResult: (
    requestId: string,
    ok: boolean,
    state: YouTubePlayerState,
    error?: YouTubePlayerError,
  ) => void;
  onCommandSettled: (requestId: string) => void;
  onLoadSettled: (requestId: string) => void;
  onRequestClose: () => void;
  onCloseSettled: (requestId: string) => void;
}

const PLAYBACK_LABEL: Record<string, string> = {
  unstarted: "READY",
  cued: "CUED",
  playing: "PLAYING",
  paused: "PAUSED",
  buffering: "BUFFERING",
  ended: "ENDED",
  stopped: "STOPPED",
  loading: "LINKING",
  error: "ERROR",
  closed: "CLOSED",
};

function formatTime(seconds: number): string {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

function blankSnapshot(video: YouTubeVideo | null): YouTubePlayerState {
  return {
    panel_open: true,
    player_ready: false,
    video_id: video?.video_id ?? "",
    title: video?.title ?? "",
    channel: video?.channel ?? "",
    playback_state: video ? "loading" : "unstarted",
    current_time: 0,
    duration: Math.max(0, Number(video?.duration_seconds) || 0),
    volume: 100,
    muted: false,
    fullscreen: false,
    autoplay_blocked: false,
    error: null,
    thumbnail: video?.thumbnail,
    video_url: video?.url,
  };
}

/**
 * The dynamic YouTube panel.
 *
 * The outer wrapper renders nothing while closed. The inner content owns one
 * player, all browser-side commands, fullscreen and the timestamp sync. It is
 * mounted once per open panel, so switching videos replaces the video in the
 * existing player instead of creating another one.
 */
export const YouTubePlayerPanel = memo(function YouTubePlayerPanel(
  props: YouTubePlayerPanelProps,
) {
  if (!props.open) return null;
  return <YouTubePanelContent {...props} />;
});

function YouTubePanelContent({
  closing,
  video,
  autoplay,
  startSeconds,
  loadKey,
  loadRequestId,
  closeRequestId,
  command,
  socketOnline,
  onState,
  onReady,
  onError,
  onResult,
  onCommandSettled,
  onLoadSettled,
  onRequestClose,
  onCloseSettled,
}: YouTubePlayerPanelProps) {
  const panelRef = useRef<HTMLElement | null>(null);
  const playerRef = useRef<YouTubePlayerHandle | null>(null);
  const [snapshot, setSnapshot] = useState<YouTubePlayerState>(() =>
    blankSnapshot(video),
  );
  const snapshotRef = useRef(snapshot);
  const [hint, setHint] = useState<string | null>(null);
  const [theater, setTheater] = useState(false);
  const theaterRef = useRef(false);
  const mountedRef = useRef(true);
  const closingRef = useRef(closing);
  const processedCommands = useRef(new Set<string>());
  const loadSequence = useRef(0);
  const lastSent = useRef({ at: 0, signature: "" });
  const videoRef = useRef(video);
  const loadMetaRef = useRef({ autoplay, startSeconds, loadRequestId });
  const userInteracted = useRef(false);

  videoRef.current = video;
  loadMetaRef.current = { autoplay, startSeconds, loadRequestId };
  closingRef.current = closing;

  const currentFullscreen = useCallback(
    () => Boolean(
      theaterRef.current ||
      (panelRef.current &&
        document.fullscreenElement &&
        panelRef.current.contains(document.fullscreenElement)),
    ),
    [],
  );

  const publishState = useCallback(
    (next: YouTubePlayerState, force = false) => {
      if (!mountedRef.current) return;
      const signature = [
        next.player_ready,
        next.video_id,
        next.playback_state,
        next.volume,
        next.muted,
        next.fullscreen,
        next.autoplay_blocked,
        next.title,
        next.channel,
        next.error?.code ?? "",
      ].join("|");
      const now = Date.now();
      const last = lastSent.current;
      if (!force && signature === last.signature && now - last.at < 2_000) {
        return;
      }
      lastSent.current = { at: now, signature };
      onState(next);
    },
    [onState],
  );

  const refresh = useCallback(
    (next: YouTubePlayerState, force = false) => {
      const merged = { ...next, fullscreen: currentFullscreen() };
      snapshotRef.current = merged;
      setSnapshot(merged);
      publishState(merged, force);
      return merged;
    },
    [currentFullscreen, publishState],
  );

  const withVideoMetadata = useCallback(
    (next: YouTubePlayerState) => {
      const target = videoRef.current;
      if (!target) return next;
      if (next.video_id) return next;
      return {
        ...next,
        video_id: target.video_id,
        title: next.title || target.title,
        channel: next.channel || target.channel,
        duration: next.duration || target.duration_seconds || 0,
        thumbnail: next.thumbnail || target.thumbnail,
        video_url: next.video_url || target.url,
      };
    },
    [],
  );

  const handleSnapshot = useCallback(
    (next: YouTubePlayerState) => {
      refresh(withVideoMetadata(next));
    },
    [refresh, withVideoMetadata],
  );

  const handleReady = useCallback(
    (next: YouTubePlayerState) => {
      const merged = refresh(withVideoMetadata(next), true);
      onReady(merged);
    },
    [onReady, refresh, withVideoMetadata],
  );

  const handlePlayerError = useCallback(
    (error: YouTubePlayerError, next: YouTubePlayerState) => {
      const merged = refresh(withVideoMetadata(next), true);
      onError(error, merged);
    },
    [onError, refresh, withVideoMetadata],
  );

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    snapshotRef.current = snapshot;
  }, [snapshot]);

  useEffect(() => {
    document.documentElement.dataset.youtubeTheater = theater ? "1" : "0";
    return () => {
      document.documentElement.dataset.youtubeTheater = "0";
    };
  }, [theater]);

  const runLoad = useCallback(
    async (
      target: YouTubeVideo,
      shouldAutoplay: boolean,
      start: number,
      requestId: string,
    ) => {
      const sequence = ++loadSequence.current;
      userInteracted.current = false;
      const base = snapshotRef.current;
      const initial: YouTubePlayerState = {
        ...base,
        panel_open: true,
        video_id: target.video_id,
        title: target.title || "YouTube video",
        channel: target.channel || "",
        duration: Math.max(0, Number(target.duration_seconds) || 0),
        current_time: Math.max(0, Number(start) || 0),
        playback_state: "loading",
        autoplay_blocked: false,
        error: null,
        thumbnail: target.thumbnail || base.thumbnail,
        video_url: target.url,
      };
      snapshotRef.current = initial;
      setSnapshot(initial);
      setHint(null);
      publishState(initial, true);

      const handle = playerRef.current;
      let outcome: YouTubeCommandOutcome;
      if (!handle) {
        outcome = {
          ok: false,
          state: initial,
          error: {
            code: "player_not_ready",
            message: "The YouTube player is still starting.",
          },
        };
      } else {
        try {
          outcome = await handle.load(target, shouldAutoplay, start);
        } catch (error) {
          outcome = {
            ok: false,
            state: initial,
            error: {
              code: "load_failed",
              message:
                error instanceof Error
                  ? error.message
                  : "The video could not be loaded.",
            },
          };
        }
      }

      if (sequence !== loadSequence.current || !mountedRef.current) {
        if (requestId) onLoadSettled(requestId);
        return;
      }
      const merged = refresh(outcome.state, true);
      if (outcome.error && !outcome.state.error) {
        setHint(outcome.error.message);
      }
      if (requestId) {
        onResult(requestId, outcome.ok, merged, outcome.error);
        onLoadSettled(requestId);
      }
    },
    [onLoadSettled, onResult, publishState, refresh],
  );

  useEffect(() => {
    if (!videoRef.current || loadKey <= 0) return;
    const meta = loadMetaRef.current;
    void runLoad(videoRef.current, meta.autoplay, meta.startSeconds,
      meta.loadRequestId);
  }, [loadKey, runLoad]);

  useEffect(() => {
    if (!socketOnline) return;
    publishState(snapshotRef.current, true);
  }, [socketOnline, publishState]);

  /* ── fullscreen ──────────────────────────────────────────────────────── */

  const requestFullscreen = useCallback(async (): Promise<YouTubePlayerError | null> => {
    const element = panelRef.current;
    if (!element) {
      return {
        code: "fullscreen_unavailable",
        message: "The YouTube panel is not ready for fullscreen.",
      };
    }
    if (document.fullscreenEnabled === false ||
        typeof element.requestFullscreen !== "function") {
      return {
        code: "fullscreen_unavailable",
        message: "This browser does not support fullscreen for the player.",
      };
    }
    try {
      await element.requestFullscreen();
      return null;
    } catch (error) {
      const name =
        error && typeof error === "object" && "name" in error
          ? String((error as { name?: unknown }).name)
          : "";
      if (name === "NotAllowedError" || name === "SecurityError") {
        return {
          code: "fullscreen_blocked",
          message:
            "The browser blocked fullscreen for that voice command. Use the FULLSCREEN button in the YouTube panel.",
        };
      }
      return {
        code: "fullscreen_error",
        message: "Fullscreen could not be entered.",
      };
    }
  }, []);

  const exitFullscreen = useCallback(async (): Promise<YouTubePlayerError | null> => {
    if (!document.fullscreenElement ||
        typeof document.exitFullscreen !== "function") {
      return null;
    }
    try {
      await document.exitFullscreen();
      return null;
    } catch {
      return {
        code: "fullscreen_error",
        message: "Fullscreen could not be exited.",
      };
    }
  }, []);

  const applyTheater = useCallback(
    (active: boolean) => {
      theaterRef.current = active;
      setTheater(active);
      const next = playerRef.current?.setFullscreen(active) ?? {
        ...snapshotRef.current,
        fullscreen: active,
      };
      refresh(next, true);
      return next;
    },
    [refresh],
  );

  // Escape leaves the in-app fullscreen fallback as well.
  useEffect(() => {
    if (!theater) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      applyTheater(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [applyTheater, theater]);

  const syncFullscreen = useCallback(() => {
    const active = currentFullscreen();
    const next = playerRef.current?.setFullscreen(active);
    if (!next) {
      refresh({ ...snapshotRef.current, fullscreen: active }, true);
    }
  }, [currentFullscreen, refresh]);

  useEffect(() => {
    document.addEventListener("fullscreenchange", syncFullscreen);
    return () => {
      document.removeEventListener("fullscreenchange", syncFullscreen);
    };
  }, [syncFullscreen]);

  const commitOutcome = useCallback(
    (outcome: YouTubeCommandOutcome) => {
      const merged = refresh(outcome.state, true);
      if (outcome.error && !outcome.state.error) {
        setHint(outcome.error.message);
      } else {
        setHint(null);
      }
      return merged;
    },
    [refresh],
  );

  const toggleFullscreen = useCallback(async () => {
    if (currentFullscreen()) {
      if (document.fullscreenElement) await exitFullscreen();
      applyTheater(false);
      if (hint) setHint(null);
      return;
    }
    const error = await requestFullscreen();
    if (error) {
      // Browsers require a direct user gesture for native fullscreen, and
      // voice commands do not provide one. The in-app fullscreen fallback is
      // still a real, usable fullscreen presentation of the player.
      applyTheater(true);
      setHint(null);
      return;
    }
    applyTheater(false);
    setHint(null);
  }, [applyTheater, currentFullscreen, exitFullscreen, hint, requestFullscreen]);

  /* ── backend commands ────────────────────────────────────────────────── */

  useEffect(() => {
    if (!command) return;
    if (processedCommands.current.has(command.requestId)) return;
    processedCommands.current.add(command.requestId);
    const requestId = command.requestId;
    void (async () => {
      let outcome: YouTubeCommandOutcome;
      if (command.command === "fullscreen") {
        if (currentFullscreen()) {
          const state = playerRef.current?.setFullscreen(true) ??
            { ...snapshotRef.current, fullscreen: true };
          outcome = { ok: true, state };
        } else {
          const error = await requestFullscreen();
          if (error) {
            // Native fullscreen needs a user gesture. Voice commands cannot
            // provide one, so use the in-app fullscreen mode instead of
            // reporting a failed command.
            applyTheater(true);
            outcome = {
              ok: true,
              state: { ...snapshotRef.current, fullscreen: true },
            };
          } else {
            applyTheater(false);
            const state = playerRef.current?.setFullscreen(true) ??
              { ...snapshotRef.current, fullscreen: true };
            outcome = { ok: true, state };
          }
        }
      } else if (command.command === "exit_fullscreen") {
        if (document.fullscreenElement) await exitFullscreen();
        applyTheater(false);
        const state = playerRef.current?.setFullscreen(false) ??
          { ...snapshotRef.current, fullscreen: false };
        outcome = { ok: true, state: { ...state, fullscreen: false } };
      } else {
        const handle = playerRef.current;
        if (!handle) {
          outcome = {
            ok: false,
            state: snapshotRef.current,
            error: {
              code: "player_not_ready",
              message: "The YouTube player is not available.",
            },
          };
        } else {
          try {
            outcome = await handle.execute(command.command, command.args);
          } catch (error) {
            outcome = {
              ok: false,
              state: snapshotRef.current,
              error: {
                code: "command_failed",
                message:
                  error instanceof Error
                    ? error.message
                    : "The YouTube command failed.",
              },
            };
          }
        }
      }
      if (!mountedRef.current) return;
      const merged = commitOutcome(outcome);
      onResult(requestId, outcome.ok, merged, outcome.error);
      onCommandSettled(requestId);
    })();
  }, [
    applyTheater,
    command,
    commitOutcome,
    currentFullscreen,
    exitFullscreen,
    onCommandSettled,
    onResult,
    requestFullscreen,
  ]);

  /* ── local controls ──────────────────────────────────────────────────── */

  const executeLocal = useCallback(
    async (name: string, args: Record<string, unknown> = {}) => {
      const handle = playerRef.current;
      if (!handle) return;
      try {
        commitOutcome(await handle.execute(name, args));
      } catch (error) {
        setHint(
          error instanceof Error
            ? error.message
            : "The YouTube command failed.",
        );
      }
    },
    [commitOutcome],
  );

  const togglePlayback = useCallback(() => {
    userInteracted.current = true;
    void executeLocal("toggle_playback");
  }, [executeLocal]);

  const seekBy = useCallback(
    (direction: "seek_backward" | "seek_forward") => {
      void executeLocal(direction, { seconds: 10 });
    },
    [executeLocal],
  );

  const changeVolume = useCallback(
    async (value: number) => {
      if (snapshotRef.current.muted) {
        await executeLocal("unmute");
      }
      await executeLocal("set_volume", { volume: value });
    },
    [executeLocal],
  );

  const toggleMute = useCallback(() => {
    void executeLocal(snapshotRef.current.muted ? "unmute" : "mute");
  }, [executeLocal]);

  const retry = useCallback(() => {
    const target = videoRef.current;
    if (!target) return;
    const meta = loadMetaRef.current;
    void runLoad(target, meta.autoplay, meta.startSeconds, "");
  }, [runLoad]);

  useEffect(() => {
    if (!closing) return;
    closingRef.current = true;
    void playerRef.current?.execute("stop");
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      onCloseSettled(closeRequestId);
    };
    const node = panelRef.current;
    const onAnimationEnd = (event: AnimationEvent) => {
      if (event.target === node) finish();
    };
    node?.addEventListener("animationend", onAnimationEnd);
    const fallback = window.setTimeout(finish, 360);
    return () => {
      node?.removeEventListener("animationend", onAnimationEnd);
      window.clearTimeout(fallback);
    };
  }, [closing, closeRequestId, onCloseSettled]);

  /* ── presentation ────────────────────────────────────────────────────── */

  const playback = snapshot.playback_state;
  const isPlaying = playback === "playing" || playback === "buffering";
  const isReady = snapshot.player_ready;
  const controlsDisabled =
    !isReady || playback === "loading" || Boolean(snapshot.error);
  const isLoading = !snapshot.error &&
    (!isReady || playback === "loading");
  const duration = Math.max(0, Number(snapshot.duration) || 0);
  const current = Math.max(0, Math.min(
    Number(snapshot.current_time) || 0,
    duration || Number.MAX_SAFE_INTEGER,
  ));
  const progress = duration > 0 ? Math.min(1, current / duration) : 0;
  const title = snapshot.title || video?.title || "YouTube video";
  const channel = snapshot.channel || video?.channel || "";
  const thumbnail = snapshot.thumbnail || video?.thumbnail || "";
  // A voice-launched video often starts paused because the browser requires a
  // user gesture for audible autoplay. Show the explicit PLAY NOW affordance
  // even if the API reported a transient buffering/unstarted state first.
  const autoplayBlocked =
    !snapshot.error &&
    !isPlaying &&
    Boolean(video) &&
    autoplay &&
    !userInteracted.current &&
    (snapshot.autoplay_blocked ||
      playback === "paused" ||
      playback === "cued" ||
      playback === "unstarted");
  const retryableError =
    Boolean(video) &&
    snapshot.player_ready &&
    ![
      "embedding_disabled",
      "video_not_found",
      "video_unavailable",
      "player_not_ready",
      "player_init_failed",
      "iframe_api_timeout",
      "iframe_api_unreachable",
      "load_failed",
    ].includes(snapshot.error?.code ?? "");

  return (
    <section
      ref={panelRef}
      className={cn(
        "aze-youtube-panel",
        theater && "aze-youtube-panel--theater",
        closing && "aze-youtube-panel--closing",
      )}
      role="region"
      aria-label="YouTube player"
      aria-busy={isLoading || closing}
    >
      <header className="aze-youtube-head">
        <div className="aze-youtube-identity">
          <YouTubeMarkIcon className="aze-youtube-mark" />
          <div className="min-w-0">
            <p className="aze-youtube-eyebrow">YOUTUBE // IN-APP PLAYER</p>
            <h2 className="aze-youtube-title" title={title}>{title}</h2>
            {channel ? (
              <p className="aze-youtube-channel">{channel}</p>
            ) : null}
          </div>
        </div>
        <div className="aze-youtube-head-actions">
          <span
            className="aze-youtube-state"
            data-state={playback}
            aria-live="polite"
          >
            <span className="aze-youtube-state-dot" aria-hidden="true" />
            {PLAYBACK_LABEL[playback] ?? playback.toUpperCase()}
          </span>
          <button
            type="button"
            className="aze-icon-button aze-youtube-close"
            aria-label="Close YouTube player"
            title="Close YouTube"
            onClick={onRequestClose}
          >
            <XIcon className="h-4 w-4" />
          </button>
        </div>
      </header>

      <div className="aze-youtube-viewport">
        <YouTubePlayer
          ref={playerRef}
          onSnapshot={handleSnapshot}
          onReady={handleReady}
          onError={handlePlayerError}
        />

        {isLoading ? (
          <div className="aze-youtube-overlay" role="status" aria-live="polite">
            {thumbnail ? (
              <img
                className="aze-youtube-poster"
                src={thumbnail}
                alt=""
                aria-hidden="true"
              />
            ) : null}
            <div className="aze-youtube-overlay-card">
              <span className="aze-youtube-spinner" aria-hidden="true" />
              <p className="aze-youtube-overlay-title">LINKING PLAYER</p>
              <p className="aze-youtube-overlay-note">
                {video?.title
                  ? "Preparing the selected video…"
                  : "Waiting for a video selection…"}
              </p>
            </div>
          </div>
        ) : null}

        {snapshot.error ? (
          <div className="aze-youtube-overlay aze-youtube-overlay--error">
            <div className="aze-youtube-overlay-card">
              <AlertIcon className="aze-youtube-alert-icon" />
              <p className="aze-youtube-overlay-title">PLAYBACK UNAVAILABLE</p>
              <p className="aze-youtube-overlay-note">
                {snapshot.error.message}
              </p>
              {retryableError ? (
                <button
                  type="button"
                  className="aze-youtube-overlay-button"
                  onClick={retry}
                >
                  RETRY VIDEO
                </button>
              ) : null}
            </div>
          </div>
        ) : null}

        {autoplayBlocked ? (
          <div className="aze-youtube-overlay">
            <div className="aze-youtube-overlay-card">
              <PlayIcon className="aze-youtube-alert-icon" />
              <p className="aze-youtube-overlay-title">PLAYBACK PAUSED</p>
              <p className="aze-youtube-overlay-note">
                The browser blocked automatic playback. Press play to start.
              </p>
              <button
                type="button"
                className="aze-youtube-overlay-button"
                onClick={() => void executeLocal("play")}
              >
                PLAY NOW
              </button>
            </div>
          </div>
        ) : null}
      </div>

      {hint ? (
        <div className="aze-youtube-notice" role="status">
          <AlertIcon className="h-3.5 w-3.5" />
          <span>{hint}</span>
          <button
            type="button"
            className="aze-youtube-notice-dismiss"
            onClick={() => setHint(null)}
            aria-label="Dismiss YouTube notice"
          >
            <XIcon className="h-3 w-3" />
          </button>
        </div>
      ) : null}

      <div className="aze-youtube-controls">
        <div
          className="aze-youtube-progress"
          role="progressbar"
          aria-label="Video progress"
          aria-valuemin={0}
          aria-valuemax={Math.round(duration)}
          aria-valuenow={Math.round(current)}
        >
          <span
            className="aze-youtube-progress-fill"
            style={{ width: `${Math.round(progress * 100)}%` }}
          />
        </div>
        <div className="aze-youtube-control-row">
          <div className="aze-youtube-control-group">
            <button
              type="button"
              className="aze-youtube-control"
              aria-label={isPlaying ? "Pause video" : "Play video"}
              title={isPlaying ? "Pause" : "Play"}
              onClick={togglePlayback}
              disabled={controlsDisabled}
            >
              {isPlaying ? (
                <PauseIcon className="h-4 w-4" />
              ) : (
                <PlayIcon className="h-4 w-4" />
              )}
            </button>
            <button
              type="button"
              className="aze-youtube-control"
              aria-label="Go back 10 seconds"
              title="Back 10 seconds"
              onClick={() => seekBy("seek_backward")}
              disabled={controlsDisabled}
            >
              <RewindIcon className="h-4 w-4" />
            </button>
            <button
              type="button"
              className="aze-youtube-control"
              aria-label="Go forward 10 seconds"
              title="Forward 10 seconds"
              onClick={() => seekBy("seek_forward")}
              disabled={controlsDisabled}
            >
              <ForwardIcon className="h-4 w-4" />
            </button>
            <span className="aze-youtube-time">
              {formatTime(current)} <span aria-hidden="true">/</span>{" "}
              {formatTime(duration)}
            </span>
          </div>

          <div className="aze-youtube-control-group aze-youtube-volume">
            <button
              type="button"
              className="aze-youtube-control"
              aria-label={snapshot.muted ? "Unmute video" : "Mute video"}
              title={snapshot.muted ? "Unmute" : "Mute"}
              onClick={toggleMute}
              disabled={controlsDisabled}
            >
              {snapshot.muted ? (
                <VolumeOffIcon className="h-4 w-4" />
              ) : (
                <VolumeIcon className="h-4 w-4" />
              )}
            </button>
            <input
              type="range"
              min={0}
              max={100}
              step={1}
              value={snapshot.muted ? 0 : snapshot.volume}
              aria-label="YouTube volume"
              className="aze-youtube-volume-slider"
              onChange={(event) => void changeVolume(Number(event.currentTarget.value))}
              disabled={controlsDisabled}
            />
            <span className="aze-youtube-volume-value" aria-hidden="true">
              {snapshot.muted ? "MUTE" : `${snapshot.volume}%`}
            </span>
          </div>

          <div className="aze-youtube-control-group">
            <button
              type="button"
              className="aze-youtube-control"
              aria-label={
                snapshot.fullscreen ? "Exit fullscreen" : "Enter fullscreen"
              }
              title={snapshot.fullscreen ? "Exit fullscreen" : "Fullscreen"}
              onClick={() => void toggleFullscreen()}
            >
              {snapshot.fullscreen ? (
                <FullscreenExitIcon className="h-4 w-4" />
              ) : (
                <MaximizeIcon className="h-4 w-4" />
              )}
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}
