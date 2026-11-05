import { fireEvent, render, screen } from "@testing-library/react";
import { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ShutdownScreen } from "../ShutdownScreen";

describe("ShutdownScreen", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("shows the power-down state and offers a manual close", () => {
    const close = vi.spyOn(window, "close").mockImplementation(() => undefined);
    render(<ShutdownScreen socket="online" reason="voice command" />);
    expect(screen.getByText("POWERING DOWN")).toBeTruthy();
    expect(screen.getByText("Requested: voice command")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "CLOSE TAB" }));
    expect(close).toHaveBeenCalled();
  });

  it("attempts a best-effort tab close after the notice", () => {
    vi.useFakeTimers();
    const close = vi.spyOn(window, "close").mockImplementation(() => undefined);
    render(<ShutdownScreen socket="online" />);
    act(() => {
      vi.advanceTimersByTime(1_600);
    });
    expect(close).toHaveBeenCalled();
  });

  it("reports the server as stopped once the socket is offline", () => {
    render(<ShutdownScreen socket="offline" />);
    expect(screen.getByText(/local server has stopped/i)).toBeTruthy();
  });
});
