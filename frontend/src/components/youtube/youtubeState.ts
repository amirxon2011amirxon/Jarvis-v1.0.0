import type {
  YouTubeCommand,
  YouTubePanelModel,
  YouTubePlayerState,
  YouTubeVideo,
} from "./youtubeTypes";

export const EMPTY_YOUTUBE_MODEL: YouTubePanelModel = {
  open: false,
  closing: false,
  video: null,
  autoplay: true,
  startSeconds: 0,
  loadKey: 0,
  loadRequestId: "",
  closeRequestId: "",
  command: null,
};

export function closedPlayerState(): YouTubePlayerState {
  return {
    panel_open: false,
    player_ready: false,
    video_id: "",
    title: "",
    channel: "",
    playback_state: "closed",
    current_time: 0,
    duration: 0,
    volume: 100,
    muted: false,
    fullscreen: false,
    autoplay_blocked: false,
    error: null,
  };
}

export function videoFromPlayerState(
  state: YouTubePlayerState | null | undefined,
): YouTubeVideo | null {
  if (!state?.video_id) return null;
  return {
    video_id: state.video_id,
    title: state.title || "YouTube video",
    channel: state.channel || "",
    duration_seconds: state.duration || 0,
    url: state.video_url || `https://www.youtube.com/watch?v=${state.video_id}`,
    thumbnail: state.thumbnail,
  };
}

export function youtubeModelFromOpen(
  model: YouTubePanelModel,
  video: YouTubeVideo | null,
  autoplay: boolean,
): YouTubePanelModel {
  return {
    ...model,
    open: true,
    closing: false,
    video: video ?? model.video,
    autoplay: Boolean(autoplay),
    closeRequestId: "",
  };
}

export function youtubeModelFromLoad(
  model: YouTubePanelModel,
  requestId: string,
  video: YouTubeVideo,
  autoplay: boolean,
  startSeconds: number,
): YouTubePanelModel {
  return {
    ...model,
    open: true,
    closing: false,
    video,
    autoplay: Boolean(autoplay),
    startSeconds: Math.max(0, Number(startSeconds) || 0),
    loadKey: model.loadKey + 1,
    loadRequestId: requestId || "",
    closeRequestId: "",
    command: null,
  };
}

export function youtubeModelFromCommand(
  model: YouTubePanelModel,
  command: YouTubeCommand,
): YouTubePanelModel {
  if (!model.open) return model;
  if (model.command?.requestId === command.requestId) return model;
  return { ...model, command };
}

export function youtubeModelClosing(
  model: YouTubePanelModel,
  requestId: string,
): YouTubePanelModel {
  if (!model.open) return model;
  return {
    ...model,
    closing: true,
    closeRequestId: requestId || "",
    command: null,
  };
}

export function youtubeModelClosed(
  model: YouTubePanelModel,
): YouTubePanelModel {
  return {
    ...EMPTY_YOUTUBE_MODEL,
    loadKey: model.loadKey,
  };
}

export function youtubeModelFromBootstrap(
  model: YouTubePanelModel,
  state: YouTubePlayerState | null | undefined,
): YouTubePanelModel {
  if (!state?.panel_open) {
    return model.open ? youtubeModelClosed(model) : model;
  }
  const video = videoFromPlayerState(state);
  if (!video) {
    if (model.open) return model;
    return {
      ...EMPTY_YOUTUBE_MODEL,
      open: true,
      autoplay: false,
      loadKey: model.loadKey,
    };
  }
  // The same player instance survived a reconnect: keep it, do not restart.
  if (model.open && model.video?.video_id === video.video_id) {
    return model;
  }
  return {
    ...EMPTY_YOUTUBE_MODEL,
    open: true,
    video,
    autoplay:
      state.playback_state === "playing" ||
      state.playback_state === "buffering",
    startSeconds: Math.max(0, Number(state.current_time) || 0),
    loadKey: model.loadKey + 1,
  };
}

export function youtubeCommandCompleted(
  model: YouTubePanelModel,
  requestId: string,
): YouTubePanelModel {
  if (!model.command || model.command.requestId !== requestId) return model;
  return { ...model, command: null };
}

export function youtubeLoadCompleted(
  model: YouTubePanelModel,
  requestId: string,
): YouTubePanelModel {
  if (!model.loadRequestId || model.loadRequestId !== requestId) return model;
  return { ...model, loadRequestId: "" };
}
