import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ScreenObservationIndicator } from "../ScreenObservationIndicator";
import type { ObservationSnapshot } from "../../types";

function makeSnapshot(
  overrides: Partial<ObservationSnapshot> = {},
): ObservationSnapshot {
  return {
    state: "INACTIVE",
    observation_active: false,
    started_at: null,
    frames_streamed: 0,
    frames_dropped: 0,
    frame_interval_seconds: 1,
    last_error: "",
    voice_session_connected: true,
    ...overrides,
  };
}

describe("ScreenObservationIndicator", () => {
  it("renders nothing when no authoritative state exists", () => {
    const { container } = render(
      <ScreenObservationIndicator observation={null} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("shows STARTING for a requested start, never WATCHING", () => {
    render(
      <ScreenObservationIndicator
        observation={makeSnapshot({ state: "STARTING" })}
      />,
    );
    expect(screen.getByText("STARTING")).toBeTruthy();
    expect(screen.queryByText("WATCHING")).toBeNull();
  });

  it("shows the active state and reflects a dropped voice link", () => {
    render(
      <ScreenObservationIndicator
        observation={makeSnapshot({
          state: "OBSERVING",
          observation_active: true,
          frames_streamed: 42,
          voice_session_connected: false,
        })}
      />,
    );
    expect(screen.getByText("WATCHING")).toBeTruthy();
    expect(screen.getByText("LINK DOWN")).toBeTruthy();
    expect(
      document.querySelector('[data-state="OBSERVING"]'),
    ).not.toBeNull();
  });

  it("updates to STOPPING and disappears when inactive", () => {
    const { container, rerender } = render(
      <ScreenObservationIndicator
        observation={makeSnapshot({
          state: "OBSERVING",
          observation_active: true,
        })}
      />,
    );
    expect(screen.getByText("WATCHING")).toBeTruthy();

    rerender(
      <ScreenObservationIndicator
        observation={makeSnapshot({ state: "STOPPING" })}
      />,
    );
    expect(screen.getByText("STOPPING")).toBeTruthy();

    rerender(
      <ScreenObservationIndicator observation={makeSnapshot()} />,
    );
    expect(container.firstChild).toBeNull();
    expect(screen.queryByText("OFF")).toBeNull();
  });

  it("surfaces an error state instead of a stale active one", () => {
    render(
      <ScreenObservationIndicator
        observation={makeSnapshot({
          state: "ERROR",
          last_error: "screen capture keeps failing",
        })}
      />,
    );
    expect(screen.getByText("ERROR")).toBeTruthy();
    expect(screen.getByText("DETAIL")).toBeTruthy();
    expect(screen.queryByText("WATCHING")).toBeNull();
  });
});
