import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AgentPanel } from "../AgentPanel";
import type { AgentRunSnapshot, ToolEvent } from "../../types";

function makeRun(overrides: Partial<AgentRunSnapshot> = {}): AgentRunSnapshot {
  return {
    run_id: "run-1234567890",
    conversation_id: "conversation-1",
    objective: "Open the browser and summarise the page.",
    status: "in_progress",
    started_at: "2026-11-04T12:00:00+00:00",
    updated_at: "2026-11-04T12:00:20+00:00",
    step_count: 2,
    tool_call_count: 2,
    successful_tool_calls: 1,
    failed_tool_calls: 1,
    consecutive_failures: 1,
    cancel_requested: false,
    last_tool_summary: "web_fetch: Request timed out",
    current_step: "Reading the page",
    artifacts: [],
    steps: [
      { step: "Opened Chrome", at: "2026-11-04T12:00:02+00:00" },
      { step: "Read the page", at: "2026-11-04T12:00:10+00:00" },
    ],
    tool_outcomes: [
      {
        tool: "browser_open",
        ok: true,
        summary: "Opened Chrome",
        category: "",
        at: "2026-11-04T12:00:03+00:00",
      },
      {
        tool: "web_fetch",
        ok: false,
        summary: "Request timed out after 10 seconds.",
        category: "timeout",
        at: "2026-11-04T12:00:14+00:00",
      },
    ],
    failures: [
      {
        tool: "web_fetch",
        category: "timeout",
        recoverable: true,
        requires_user: false,
        error: "Request timed out after 10 seconds.",
        hint: "Retry with a longer timeout or another source.",
        at: "2026-11-04T12:00:15+00:00",
      },
    ],
    blockers: ["web_fetch: Request timed out"],
    completed_operations: [{ tool: "browser_open", count: 1 }],
    failed_operations: [
      {
        tool: "web_fetch",
        category: "timeout",
        recoverable: true,
        retry_same_call: true,
        count: 1,
        last_error: "Request timed out after 10 seconds.",
      },
    ],
    remaining_work: "Summarise the page.",
    verification: {
      required: true,
      reason: "The task claims a page summary.",
      evidence: [
        {
          summary: "Chrome opened",
          source: "observation",
          tool: "browser_open",
          kind: "observation",
          at: "2026-11-04T12:00:04+00:00",
        },
      ],
      attempts: 1,
      exhausted: false,
      missing: ["explicit evidence that the page was summarised"],
    },
    result_summary: "",
    last_error: "Request timed out after 10 seconds.",
    elapsed_seconds: 20,
    limits: {
      max_steps: 12,
      max_tool_calls: 40,
      max_consecutive_failures: 3,
      max_duration_seconds: 300,
    },
    is_terminal: false,
    ...overrides,
  };
}

function makeToolEvents(): ToolEvent[] {
  return [
    {
      id: "browser-open",
      tool: "browser_open",
      target: "Chrome",
      phase: "end",
      started_at: "2026-11-04T12:00:02+00:00",
      ended_at: "2026-11-04T12:00:03+00:00",
      ms: 1200,
      ok: true,
      summary: "Opened Chrome",
      status: "success",
    },
    {
      id: "web-fetch",
      tool: "web_fetch",
      target: "https://example.com",
      phase: "end",
      started_at: "2026-11-04T12:00:13+00:00",
      ended_at: "2026-11-04T12:00:14+00:00",
      ms: 10000,
      ok: false,
      summary: "Request timed out after 10 seconds.",
      status: "failed",
    },
  ];
}

describe("AgentPanel", () => {
  it("shows a useful empty state and closes from the header", () => {
    const onClose = vi.fn();
    render(<AgentPanel run={null} toolEvents={[]} onClose={onClose} />);

    expect(screen.getByText("NO AGENT RUN")).toBeTruthy();
    expect(
      screen.getByText(/task_status tool/i),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close Agent panel" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("renders the live overview from the backend snapshot", () => {
    vi.useFakeTimers();
    render(
      <AgentPanel run={makeRun()} toolEvents={[]} onClose={() => undefined} />,
    );

    expect(screen.getAllByText("IN PROGRESS").length).toBeGreaterThan(0);
    expect(
      screen.getByText("Open the browser and summarise the page."),
    ).toBeTruthy();
    expect(screen.getByText("Reading the page")).toBeTruthy();
    expect(screen.getAllByText("2 / 40").length).toBeGreaterThan(0);
    expect(screen.getByText("Summarise the page.")).toBeTruthy();
    expect(screen.getByText("2 RECORDED")).toBeTruthy();
    expect(screen.getByText("Opened Chrome")).toBeTruthy();
    expect(screen.getByText("Read the page")).toBeTruthy();
    expect(screen.getByText("LIVE")).toBeTruthy();
  });

  it("shows tool execution details, verification and statistics", () => {
    const run = makeRun({
      status: "failed",
      is_terminal: true,
      finished_at: "2026-11-04T12:01:00+00:00",
      result_summary: "The page could not be summarised.",
    });
    render(
      <AgentPanel run={run} toolEvents={makeToolEvents()} onClose={() => undefined} />,
    );

    fireEvent.click(screen.getByRole("tab", { name: "TOOLS" }));
    expect(screen.getByText("2 CALLS")).toBeTruthy();
    expect(screen.getByText("BROWSER OPEN")).toBeTruthy();
    expect(screen.getByText("Chrome")).toBeTruthy();
    expect(screen.getByText("1.2s")).toBeTruthy();
    expect(screen.getByText("TIMEOUT")).toBeTruthy();
    expect(screen.getByText("https://example.com")).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: "VERIFICATION" }));
    expect(screen.getAllByText("MISSING EVIDENCE").length).toBeGreaterThan(0);
    expect(screen.getByText("Chrome opened")).toBeTruthy();
    expect(
      screen.getByText("explicit evidence that the page was summarised"),
    ).toBeTruthy();
    expect(screen.getByText("web_fetch: Request timed out")).toBeTruthy();
    expect(
      screen.getByText("Retry with a longer timeout or another source."),
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: "STATISTICS" }));
    expect(
      within(screen.getByRole("group", { name: "SUCCESSFUL CALLS" })).getByText("1"),
    ).toBeTruthy();
    expect(
      within(screen.getByRole("group", { name: "FAILED CALLS" })).getByText("1"),
    ).toBeTruthy();
    expect(screen.getByText("CONFIGURED EXECUTION LIMITS")).toBeTruthy();
    expect(screen.getByLabelText("Steps usage: 2 of 12")).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: "ACTIVITY" }));
    expect(screen.getByText("EXECUTION TIMELINE")).toBeTruthy();
    expect(screen.getByText("RECOVERY")).toBeTruthy();
    expect(screen.getByText("EVIDENCE")).toBeTruthy();
    expect(screen.getAllByText("FAILED").length).toBeGreaterThan(0);
  });

  it("distinguishes unavailable information from zero values", () => {
    const run = makeRun({
      status: "completed",
      is_terminal: true,
      finished_at: "2026-11-04T12:00:30+00:00",
      steps: undefined,
      tool_outcomes: undefined,
      successful_tool_calls: undefined,
      failed_tool_calls: undefined,
      failures: undefined,
      blockers: undefined,
      failed_operations: undefined,
      remaining_work: "",
      verification: undefined,
      elapsed_seconds: 30,
      result_summary: "Done.",
    });
    render(
      <AgentPanel run={run} toolEvents={[]} onClose={() => undefined} />,
    );

    expect(screen.getAllByText("Not reported").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("tab", { name: "STATISTICS" }));
    expect(
      within(screen.getByRole("group", { name: "SUCCESSFUL CALLS" })).getByText("—"),
    ).toBeTruthy();
    expect(
      within(screen.getByRole("group", { name: "TOTAL STEPS" })).getByText("2"),
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: "VERIFICATION" }));
    expect(screen.getAllByText("NOT REPORTED").length).toBeGreaterThan(0);
    expect(
      screen.getByText(/Verification state is not reported/i),
    ).toBeTruthy();
  });
});
