import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  YouTubeCommand,
  YouTubePlayerState,
  YouTubeVideo,
} from "../youtubeTypes";

const mocks = vi.hoisted(() => ({
  handle: {
    load: vi.fn(),
    execute: vi.fn(),
    setFullscreen: vi.fn(),
    getSnapshot: vi.fn(),
    isReady: vi.fn(),
  },
}));

vi.mock("../YouTubePlayer", async () => {
  const React = await import("react");
  const MockYouTubePlayer = React.forwardRef<unknown, Record<string, unknown>>(
    function MockYouTubePlayer(_props, ref) {
      React.useImperativeHandle(ref, () => mocks.handle);
      return React.createElement("div", { "data-testid": "fake-youtube-player" });
    },
  );
  return { YouTubePlayer: MockYouTubePlayer };
});

import { YouTubePlayerPanel, type YouTubePlayerPanelProps } from "../YouTubePlayerPanel";

const VIDEO: YouTubeVideo = {
  video_id: "aaaaaaaaaaa",
  title: "Transformer architecture",
  channel: "AI Explained",
  duration_seconds: 600,
};

const SECOND: YouTubeVideo = {
  video_id: "bbbbbbbbbbb",
  title: "Local LLMs",
  channel: "Fireship",
  duration_seconds: 300,
};

function playerState(overrides: Partial<YouTubePlayerState> = {}): YouTubePlayerState {
  return {
    panel_open: true,
    player_ready: true,
    video_id: VIDEO.video_id,
    title: VIDEO.title,
    channel: VIDEO.channel,
    playback_state: "playing",
    current_time: 20,
    duration: 600,
    volume: 75,
    muted: false,
    fullscreen: false,
    autoplay_blocked: false,
    error: null,
    ...overrides,
  };
}

function makeProps(
  overrides: Partial<YouTubePlayerPanelProps> = {},
): YouTubePlayerPanelProps {
  return {
    open: true,
    closing: false,
    video: VIDEO,
    autoplay: true,
    startSeconds: 0,
    loadKey: 1,
    loadRequestId: "load-1",
    closeRequestId: "",
    command: null,
    socketOnline: true,
    onState: vi.fn(),
    onReady: vi.fn(),
    onError: vi.fn(),
    onResult: vi.fn(),
    onCommandSettled: vi.fn(),
    onLoadSettled: vi.fn(),
    onRequestClose: vi.fn(),
    onCloseSettled: vi.fn(),
    ...overrides,
  };
}

function loadOutcome(video: YouTubeVideo, autoplay: boolean, start: number) {
  return {
    ok: true,
    state: playerState({
      video_id: video.video_id,
      title: video.title,
      channel: video.channel,
      duration: video.duration_seconds ?? 600,
      current_time: start,
      playback_state: autoplay ? "playing" : "cued",
    }),
  };
}

describe("YouTubePlayerPanel", () => {
  beforeEach(() => {
    mocks.handle.load.mockImplementation(
      async (video: YouTubeVideo, autoplay: boolean, start: number) =>
        loadOutcome(video, autoplay, start),
    );
    mocks.handle.execute.mockImplementation(async (command: string) =>
      ({ ok: true, state: playerState({
        playback_state: command === "pause" ? "paused" : "playing",
      }) }),
    );
    mocks.handle.setFullscreen.mockImplementation(
      (fullscreen: boolean) => playerState({ fullscreen }),
    );
    mocks.handle.getSnapshot.mockReturnValue(playerState());
    mocks.handle.isReady.mockReturnValue(true);
  });

  afterEach(() => {
    delete (HTMLElement.prototype as { requestFullscreen?: unknown })
      .requestFullscreen;
    delete (document as { fullscreenEnabled?: unknown }).fullscreenEnabled;
    delete (document as { exitFullscreen?: unknown }).exitFullscreen;
  });

  it("renders nothing while closed and opens as a single region", () => {
    const props = makeProps();
    const { rerender } = render(<YouTubePlayerPanel {...props} open={false} />);
    expect(screen.queryByRole("region", { name: "YouTube player" })).toBeNull();
    rerender(<YouTubePlayerPanel {...props} />);
    expect(screen.getByRole("region", { name: "YouTube player" })).toBeTruthy();
    expect(screen.getAllByTestId("fake-youtube-player")).toHaveLength(1);
  });

  it("shows a loading state until the real load resolves", async () => {
    let resolveLoad: (value: unknown) => void = () => undefined;
    mocks.handle.load.mockImplementation(
      () => new Promise((resolve) => { resolveLoad = resolve; }),
    );
    render(<YouTubePlayerPanel {...makeProps()} />);
    expect(screen.getByText("LINKING PLAYER")).toBeTruthy();
    resolveLoad(loadOutcome(VIDEO, true, 0));
    await waitFor(() => expect(screen.getByText("PLAYING")).toBeTruthy());
  });

  it("loads the selected video, reports the real result and switches in place", async () => {
    const props = makeProps();
    const { rerender } = render(<YouTubePlayerPanel {...props} />);
    await waitFor(() =>
      expect(mocks.handle.load).toHaveBeenCalledWith(VIDEO, true, 0));
    expect(screen.getByText("Transformer architecture")).toBeTruthy();
    expect(screen.getByText("AI Explained")).toBeTruthy();
    expect(screen.getByText("PLAYING")).toBeTruthy();
    await waitFor(() =>
      expect(props.onResult).toHaveBeenCalledWith(
        "load-1",
        true,
        expect.objectContaining({ video_id: VIDEO.video_id }),
        undefined,
      ));
    expect(mocks.handle.load).toHaveBeenCalledTimes(1);

    rerender(
      <YouTubePlayerPanel
        {...props}
        video={SECOND}
        loadKey={2}
        loadRequestId="load-2"
      />,
    );
    await waitFor(() =>
      expect(mocks.handle.load).toHaveBeenCalledWith(SECOND, true, 0));
    expect(screen.getByText("Local LLMs")).toBeTruthy();
    expect(screen.getAllByTestId("fake-youtube-player")).toHaveLength(1);
    expect(props.onResult).toHaveBeenLastCalledWith(
      "load-2",
      true,
      expect.objectContaining({ video_id: SECOND.video_id }),
      undefined,
    );
  });

  it("dispatches a backend command once and returns its actual outcome", async () => {
    const command: YouTubeCommand = {
      requestId: "command-1",
      command: "pause",
      args: {},
    };
    const props = makeProps();
    const { rerender } = render(<YouTubePlayerPanel {...props} />);
    await waitFor(() => expect(mocks.handle.load).toHaveBeenCalled());

    rerender(<YouTubePlayerPanel {...props} command={command} />);
    await waitFor(() =>
      expect(mocks.handle.execute).toHaveBeenCalledWith("pause", {}));
    await waitFor(() =>
      expect(props.onResult).toHaveBeenCalledWith(
        "command-1",
        true,
        expect.objectContaining({ playback_state: "paused" }),
        undefined,
      ));
    expect(props.onCommandSettled).toHaveBeenCalledWith("command-1");

    rerender(<YouTubePlayerPanel {...props} command={command} />);
    await Promise.resolve();
    expect(mocks.handle.execute).toHaveBeenCalledTimes(1);
  });

  it("handles pause and seek controls through the official player methods", async () => {
    const props = makeProps();
    render(<YouTubePlayerPanel {...props} />);
    await waitFor(() => expect(mocks.handle.load).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText("PLAYING")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "Pause video" }));
    await waitFor(() =>
      expect(mocks.handle.execute).toHaveBeenCalledWith("toggle_playback", {}));

    fireEvent.click(screen.getByRole("button", { name: "Go back 10 seconds" }));
    await waitFor(() =>
      expect(mocks.handle.execute).toHaveBeenCalledWith(
        "seek_backward", { seconds: 10 }));
  });

  it("shows a command failure as a notice, not a playback-unavailable error", async () => {
    mocks.handle.execute.mockResolvedValue({
      ok: false,
      state: playerState({ playback_state: "paused" }),
      error: {
        code: "command_failed",
        message: "The player did not reach the requested seek position.",
      },
    });
    const props = makeProps();
    const { rerender } = render(<YouTubePlayerPanel {...props} />);
    await waitFor(() => expect(mocks.handle.load).toHaveBeenCalled());
    rerender(
      <YouTubePlayerPanel
        {...props}
        command={{
          requestId: "seek-1",
          command: "seek_backward",
          args: { seconds: 10 },
        }}
      />,
    );
    await waitFor(() =>
      expect(props.onResult).toHaveBeenCalledWith(
        "seek-1",
        false,
        expect.anything(),
        expect.objectContaining({ code: "command_failed" }),
      ));
    expect(screen.queryByText("PLAYBACK UNAVAILABLE")).toBeNull();
    expect(
      screen.getByText(/did not reach the requested seek position/),
    ).toBeTruthy();
  });

  it("keeps the playable fallback visible when autoplay is blocked", async () => {
    mocks.handle.load.mockResolvedValue({
      ok: true,
      state: playerState({ playback_state: "paused", autoplay_blocked: true }),
    });
    const props = makeProps();
    render(<YouTubePlayerPanel {...props} />);
    await waitFor(() => expect(screen.getByText("PLAY NOW")).toBeTruthy());
    fireEvent.click(screen.getByText("PLAY NOW"));
    await waitFor(() =>
      expect(mocks.handle.execute).toHaveBeenCalledWith("play", {}));
  });

  it("shows a player error and retries without creating a second player", async () => {
    mocks.handle.load.mockResolvedValueOnce({
      ok: false,
      state: playerState({
        playback_state: "error",
        error: {
          code: "player_error",
          message: "The player stopped unexpectedly.",
        },
      }),
      error: {
        code: "player_error",
        message: "The player stopped unexpectedly.",
      },
    });
    const props = makeProps();
    render(<YouTubePlayerPanel {...props} />);
    await waitFor(() =>
      expect(screen.getByText("PLAYBACK UNAVAILABLE")).toBeTruthy());
    expect(screen.getByText("The player stopped unexpectedly.")).toBeTruthy();
    expect(props.onResult).toHaveBeenCalledWith(
      "load-1",
      false,
      expect.anything(),
      expect.objectContaining({ code: "player_error" }),
    );
    fireEvent.click(screen.getByText("RETRY VIDEO"));
    await waitFor(() => expect(mocks.handle.load).toHaveBeenCalledTimes(2));
    expect(screen.getAllByTestId("fake-youtube-player")).toHaveLength(1);
  });

  it("falls back to in-app fullscreen when native fullscreen is blocked", async () => {
    Object.defineProperty(document, "fullscreenEnabled", {
      configurable: true,
      value: true,
    });
    Object.defineProperty(HTMLElement.prototype, "requestFullscreen", {
      configurable: true,
      value: vi.fn().mockRejectedValue(
        new DOMException("blocked", "NotAllowedError"),
      ),
    });
    const props = makeProps();
    const { rerender } = render(<YouTubePlayerPanel {...props} />);
    await waitFor(() => expect(mocks.handle.load).toHaveBeenCalled());
    rerender(
      <YouTubePlayerPanel
        {...props}
        command={{ requestId: "fs-1", command: "fullscreen", args: {} }}
      />,
    );
    await waitFor(() =>
      expect(props.onResult).toHaveBeenCalledWith(
        "fs-1",
        true,
        expect.objectContaining({ fullscreen: true }),
        undefined,
      ));
    const region = screen.getByRole("region", { name: "YouTube player" });
    expect(region.className).toContain("aze-youtube-panel--theater");
    expect(
      screen.getByRole("button", { name: "Exit fullscreen" }),
    ).toBeTruthy();
  });

  it("stops the player and settles a close request", async () => {
    const props = makeProps();
    const { rerender } = render(<YouTubePlayerPanel {...props} />);
    await waitFor(() => expect(mocks.handle.load).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Close YouTube player" }));
    expect(props.onRequestClose).toHaveBeenCalledTimes(1);

    rerender(<YouTubePlayerPanel {...props} closing closeRequestId="close-1" />);
    await waitFor(() =>
      expect(mocks.handle.execute).toHaveBeenCalledWith("stop"));
    const region = screen.getByRole("region", { name: "YouTube player" });
    fireEvent(region, new Event("animationend", { bubbles: true }));
    await waitFor(() =>
      expect(props.onCloseSettled).toHaveBeenCalledWith("close-1"));
  });

  it("reports its latest state after a websocket reconnect", async () => {
    const props = makeProps({ socketOnline: false });
    const { rerender } = render(<YouTubePlayerPanel {...props} />);
    await waitFor(() => expect(mocks.handle.load).toHaveBeenCalled());
    rerender(<YouTubePlayerPanel {...props} socketOnline />);
    await waitFor(() => expect(props.onState).toHaveBeenCalled());
  });
});
