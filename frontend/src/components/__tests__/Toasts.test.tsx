import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Toasts } from "../Toasts";
import type { Toast } from "../../types";

function notification(overrides: Partial<Toast> = {}): Toast {
  return {
    id: "notification-ntf-1",
    kind: "info",
    title: "Reminder: Study English",
    message: "Time to review your vocabulary.",
    notificationId: "ntf-1",
    autoDismiss: false,
    snoozeMinutes: 10,
    ...overrides,
  };
}

describe("Toasts notification behavior", () => {
  it("renders a reminder with its title, body and snooze action", () => {
    const onSnooze = vi.fn();
    render(
      <Toasts
        toasts={[notification()]}
        onDismiss={() => undefined}
        onSnooze={onSnooze}
      />,
    );

    expect(screen.getByText("Reminder: Study English")).toBeTruthy();
    expect(screen.getByText("Time to review your vocabulary.")).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: /SNOOZE 10M/i, hidden: true }),
    );
    expect(onSnooze).toHaveBeenCalledWith("ntf-1", 10);
  });

  it("dismisses through the existing notification close control", () => {
    const onDismiss = vi.fn();
    render(
      <Toasts toasts={[notification()]} onDismiss={onDismiss} />,
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: "Dismiss notification",
        hidden: true,
      }),
    );
    expect(onDismiss).toHaveBeenCalledWith("notification-ntf-1");
  });

  it("keeps ordinary feedback rows unchanged", () => {
    const onSnooze = vi.fn();
    render(
      <Toasts
        toasts={[{ id: "toast-1", kind: "success", message: "Saved." }]}
        onDismiss={() => undefined}
        onSnooze={onSnooze}
      />,
    );
    expect(screen.getByText("Saved.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /SNOOZE/i })).toBeNull();
  });
});
