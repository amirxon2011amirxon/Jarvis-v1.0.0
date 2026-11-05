/** Shared YouTube player protocol types.
 *
 * The backend sends snake_case player state because it is also the wire shape
 * for the existing WebSocket. UI-only panel data uses camelCase.
 */

export type YouTubePlaybackState =
  | "unstarted"
  | "cued"
  | "playing"
  | "paused"
  | "buffering"
  | "ended"
  | "stopped"
  | "loading"
  | "error"
  | "closed";

export interface YouTubeVideo {
  video_id: string;
  title: string;
  channel: string;
  duration_seconds?: number;
  url?: string;
  thumbnail?: string;
}

export interface YouTubePlayerError {
  code: string;
  message: string;
}

export interface YouTubePlayerState {
  panel_open: boolean;
  player_ready: boolean;
  video_id: string;
  title: string;
  channel: string;
  playback_state: YouTubePlaybackState;
  current_time: number;
  duration: number;
  volume: number;
  muted: boolean;
  fullscreen: boolean;
  autoplay_blocked: boolean;
  error: YouTubePlayerError | null;
  remaining_seconds?: number;
  video_url?: string;
  thumbnail?: string;
  status_text?: string;
  updated_at?: string;
}

export interface YouTubeCommand {
  requestId: string;
  command: string;
  args: Record<string, unknown>;
}

export interface YouTubePanelModel {
  open: boolean;
  closing: boolean;
  video: YouTubeVideo | null;
  autoplay: boolean;
  startSeconds: number;
  /** Increments on every load so the same video can be restarted. */
  loadKey: number;
  /** Correlates this load with a backend tool call, empty for a snapshot. */
  loadRequestId: string;
  /** Correlates a backend-initiated close. */
  closeRequestId: string;
  command: YouTubeCommand | null;
}

export interface YouTubeCommandOutcome {
  ok: boolean;
  state: YouTubePlayerState;
  error?: YouTubePlayerError;
}

/* ── Minimal official IFrame API surface ─────────────────────────────────── */

export interface YTPlayerData {
  video_id?: string;
  title?: string;
  author?: string;
}

export interface YTPlayerEvent {
  data?: number;
  target?: YTPlayer;
}

export interface YTPlayer {
  playVideo(): void;
  pauseVideo(): void;
  stopVideo(): void;
  seekTo(seconds: number, allowSeekAhead: boolean): void;
  loadVideoById(
    video: string | { videoId: string; startSeconds?: number },
  ): void;
  cueVideoById(
    video: string | { videoId: string; startSeconds?: number },
  ): void;
  getVolume(): number;
  setVolume(volume: number): void;
  mute(): void;
  unMute(): void;
  isMuted(): boolean;
  getPlayerState(): number;
  getCurrentTime(): number;
  getDuration(): number;
  getVideoData(): YTPlayerData;
  addEventListener(event: string, listener: (event: YTPlayerEvent) => void): void;
  removeEventListener(
    event: string,
    listener: (event: YTPlayerEvent) => void,
  ): void;
  destroy(): void;
}

export interface YTPlayerOptions {
  width?: string | number;
  height?: string | number;
  videoId?: string;
  playerVars?: Record<string, string | number>;
  events?: {
    onReady?: (event: YTPlayerEvent) => void;
    onStateChange?: (event: YTPlayerEvent) => void;
    onError?: (event: YTPlayerEvent) => void;
  };
}

export interface YTNamespace {
  Player: new (
    element: HTMLElement | string,
    options: YTPlayerOptions,
  ) => YTPlayer;
  PlayerState?: {
    UNSTARTED: number;
    ENDED: number;
    PLAYING: number;
    PAUSED: number;
    BUFFERING: number;
    CUED: number;
  };
}

export type PlayerFactory = (
  element: HTMLElement,
  options: YTPlayerOptions,
) => YTPlayer;

declare global {
  interface Window {
    YT?: YTNamespace;
    onYouTubeIframeAPIReady?: () => void;
  }
}
