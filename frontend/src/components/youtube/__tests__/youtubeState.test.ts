import { describe, expect, it } from "vitest";

import {
  EMPTY_YOUTUBE_MODEL,
  youtubeCommandCompleted,
  youtubeLoadCompleted,
  youtubeModelClosed,
  youtubeModelClosing,
  youtubeModelFromBootstrap,
  youtubeModelFromCommand,
  youtubeModelFromLoad,
  youtubeModelFromOpen,
} from "../youtubeState";
import type { YouTubePlayerState, YouTubeVideo } from "../youtubeTypes";

const VIDEO: YouTubeVideo = {
  video_id: "aaaaaaaaaaa",
  title: "Transformers",
  channel: "AI Explained",
  duration_seconds: 600,
};

const SECOND: YouTubeVideo = {
  video_id: "bbbbbbbbbbb",
  title: "Local LLMs",
  channel: "Fireship",
  duration_seconds: 300,
};

function state(overrides: Partial<YouTubePlayerState> = {}): YouTubePlayerState {
  return {
    panel_open: true,
    player_ready: true,
    video_id: VIDEO.video_id,
    title: VIDEO.title,
    channel: VIDEO.channel,
    playback_state: "playing",
    current_time: 12,
    duration: 600,
    volume: 80,
    muted: false,
    fullscreen: false,
    autoplay_blocked: false,
    error: null,
    ...overrides,
  };
}

describe("YouTube panel state", () => {
  it("opens without loading until a load event arrives", () => {
    const opened = youtubeModelFromOpen(
      EMPTY_YOUTUBE_MODEL,
      VIDEO,
      true,
    );
    expect(opened.open).toBe(true);
    expect(opened.loadKey).toBe(0);
    expect(opened.video).toEqual(VIDEO);
  });

  it("increments the load key for every video switch", () => {
    const first = youtubeModelFromLoad(
      EMPTY_YOUTUBE_MODEL,
      "request-1",
      VIDEO,
      true,
      0,
    );
    const second = youtubeModelFromLoad(
      first,
      "request-2",
      SECOND,
      false,
      42,
    );
    expect(first.loadKey).toBe(1);
    expect(second.loadKey).toBe(2);
    expect(second.video).toEqual(SECOND);
    expect(second.loadRequestId).toBe("request-2");
    expect(second.autoplay).toBe(false);
    expect(second.startSeconds).toBe(42);
  });

  it("deduplicates a command with the same request id", () => {
    const model = { ...EMPTY_YOUTUBE_MODEL, open: true };
    const command = {
      requestId: "request-1",
      command: "pause",
      args: {},
    };
    const once = youtubeModelFromCommand(model, command);
    const twice = youtubeModelFromCommand(once, command);
    expect(twice).toBe(once);
    expect(youtubeCommandCompleted(once, "request-1").command).toBeNull();
    expect(youtubeLoadCompleted(
      { ...model, loadRequestId: "load-1" },
      "load-1",
    ).loadRequestId).toBe("");
  });

  it("keeps one panel while closing and closes cleanly", () => {
    const open = youtubeModelFromLoad(
      EMPTY_YOUTUBE_MODEL,
      "load-1",
      VIDEO,
      true,
      0,
    );
    const closing = youtubeModelClosing(open, "close-1");
    expect(closing.open).toBe(true);
    expect(closing.closing).toBe(true);
    expect(closing.closeRequestId).toBe("close-1");
    const closed = youtubeModelClosed(closing);
    expect(closed.open).toBe(false);
    expect(closed.video).toBeNull();
    expect(closed.closing).toBe(false);
  });

  it("does not restart a player that survived a websocket reconnect", () => {
    const open = youtubeModelFromLoad(
      EMPTY_YOUTUBE_MODEL,
      "load-1",
      VIDEO,
      true,
      0,
    );
    const same = youtubeModelFromBootstrap(open, state());
    expect(same).toBe(open);
    expect(same.loadKey).toBe(1);
  });

  it("restores a video from a fresh page snapshot", () => {
    const restored = youtubeModelFromBootstrap(
      EMPTY_YOUTUBE_MODEL,
      state({ playback_state: "paused", current_time: 120 }),
    );
    expect(restored.open).toBe(true);
    expect(restored.video?.video_id).toBe(VIDEO.video_id);
    expect(restored.loadKey).toBe(1);
    expect(restored.startSeconds).toBe(120);
    expect(restored.autoplay).toBe(false);
  });

  it("loads a different video found in a snapshot", () => {
    const open = youtubeModelFromLoad(
      EMPTY_YOUTUBE_MODEL,
      "load-1",
      VIDEO,
      true,
      0,
    );
    const switched = youtubeModelFromBootstrap(
      open,
      state({ video_id: SECOND.video_id, title: SECOND.title }),
    );
    expect(switched.video?.video_id).toBe(SECOND.video_id);
    expect(switched.loadKey).toBe(2);
  });

  it("closes when the backend says the panel is closed", () => {
    const open = youtubeModelFromLoad(
      EMPTY_YOUTUBE_MODEL,
      "load-1",
      VIDEO,
      true,
      0,
    );
    const closed = youtubeModelFromBootstrap(
      open,
      state({ panel_open: false, playback_state: "closed" }),
    );
    expect(closed.open).toBe(false);
    expect(closed.video).toBeNull();
  });
});
