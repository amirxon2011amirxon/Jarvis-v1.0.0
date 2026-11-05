import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
} from "react";

import { YouTubePlayerController } from "./YouTubePlayerController";
import type {
  PlayerFactory,
  YTNamespace,
  YouTubeCommandOutcome,
  YouTubePlayerError,
  YouTubePlayerState,
  YouTubeVideo,
} from "./youtubeTypes";

export interface YouTubePlayerHandle {
  load(
    video: YouTubeVideo,
    autoplay: boolean,
    startSeconds?: number,
  ): Promise<YouTubeCommandOutcome>;
  execute(
    command: string,
    args?: Record<string, unknown>,
  ): Promise<YouTubeCommandOutcome>;
  setFullscreen(fullscreen: boolean): YouTubePlayerState;
  getSnapshot(): YouTubePlayerState;
  isReady(): boolean;
}

interface YouTubePlayerProps {
  onSnapshot: (state: YouTubePlayerState) => void;
  onReady: (state: YouTubePlayerState) => void;
  onError: (error: YouTubePlayerError, state: YouTubePlayerState) => void;
  /** Test seams only. */
  createPlayer?: PlayerFactory;
  loadApi?: () => Promise<YTNamespace>;
  pollIntervalMs?: number;
}

const FALLBACK_STATE: YouTubePlayerState = {
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

/**
 * Owns exactly one IFrame player instance for the lifetime of the panel.
 *
 * The controller is created after the host element exists and destroyed on
 * unmount. Callbacks are held in a ref so a parent re-render can never replace
 * the live player (which would create a second iframe).
 */
export const YouTubePlayer = forwardRef<YouTubePlayerHandle, YouTubePlayerProps>(
  function YouTubePlayer(
    {
      onSnapshot,
      onReady,
      onError,
      createPlayer,
      loadApi,
      pollIntervalMs,
    },
    ref,
  ) {
    const hostRef = useRef<HTMLDivElement | null>(null);
    const controllerRef = useRef<YouTubePlayerController | null>(null);
    const callbacks = useRef({ onSnapshot, onReady, onError });
    callbacks.current = { onSnapshot, onReady, onError };

    useEffect(() => {
      const host = hostRef.current;
      if (!host) return undefined;
      const controller = new YouTubePlayerController({
        container: host,
        onSnapshot: (state) => callbacks.current.onSnapshot(state),
        onReady: (state) => callbacks.current.onReady(state),
        onError: (error, state) => callbacks.current.onError(error, state),
        createPlayer,
        loadApi,
        pollIntervalMs,
      });
      controllerRef.current = controller;
      return () => {
        controllerRef.current = null;
        controller.destroy();
      };
      // The host is created once for this component instance. Test seams are
      // fixed by the harness; production passes none.
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    useImperativeHandle(
      ref,
      () => ({
        load(video, autoplay, startSeconds = 0) {
          const controller = controllerRef.current;
          if (!controller) {
            return Promise.resolve({
              ok: false,
              state: FALLBACK_STATE,
              error: {
                code: "player_not_ready",
                message: "The YouTube player is not available.",
              },
            } satisfies YouTubeCommandOutcome);
          }
          return controller.load(video, autoplay, startSeconds);
        },
        execute(command, args = {}) {
          const controller = controllerRef.current;
          if (!controller) {
            return Promise.resolve({
              ok: false,
              state: FALLBACK_STATE,
              error: {
                code: "player_not_ready",
                message: "The YouTube player is not available.",
              },
            } satisfies YouTubeCommandOutcome);
          }
          return controller.execute(command, args);
        },
        setFullscreen(fullscreen) {
          return controllerRef.current?.setFullscreen(fullscreen) ??
            { ...FALLBACK_STATE, fullscreen };
        },
        getSnapshot() {
          return controllerRef.current?.getSnapshot() ?? FALLBACK_STATE;
        },
        isReady() {
          return controllerRef.current?.isReady ?? false;
        },
      }),
      [],
    );

    return (
      <div
        ref={hostRef}
        className="aze-youtube-player"
        data-youtube-player=""
      />
    );
  },
);
