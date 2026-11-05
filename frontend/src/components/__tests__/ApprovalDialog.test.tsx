import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { ApprovalDialog } from "../ApprovalDialog";
import type { ApprovalRequest } from "../../types";

function makeApproval(
  overrides: Partial<ApprovalRequest> = {},
): ApprovalRequest {
  return {
    approval_id: "approval-1",
    tool: "file_control",
    operation: "delete",
    target: "C:\\Temp\\report.txt",
    summary: "Delete C:\\Temp\\report.txt to the Recycle Bin",
    parameters: { permanent: false, recursive: false },
    created_at: new Date(Date.now() - 5_000).toISOString(),
    expires_at: new Date(Date.now() + 60_000).toISOString(),
    status: "pending",
    ...overrides,
  };
}

beforeAll(() => {
  // jsdom does not implement the native dialog methods used by the shared
  // Modal component.
  const proto = HTMLDialogElement.prototype as HTMLDialogElement & {
    showModal?: () => void;
    close?: () => void;
  };
  if (!proto.showModal) {
    proto.showModal = function showModal(this: HTMLDialogElement) {
      this.setAttribute("open", "");
    };
  }
  if (!proto.close) {
    proto.close = function close(this: HTMLDialogElement) {
      this.removeAttribute("open");
    };
  }
});

describe("ApprovalDialog", () => {
  it("shows the exact operation, target and parameters", () => {
    render(
      <ApprovalDialog
        approvals={[makeApproval()]}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    );

    expect(screen.getByText("Action approval")).toBeTruthy();
    expect(screen.getByText(/Delete C:\\Temp\\report.txt/)).toBeTruthy();
    expect(screen.getAllByText("C:\\Temp\\report.txt").length).toBeGreaterThan(0);
    expect(screen.getByText("permanent")).toBeTruthy();
    expect(screen.getAllByText("false").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "APPROVE" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "REJECT" })).toBeTruthy();
  });

  it("calls the explicit approval action", async () => {
    const onApprove = vi.fn().mockResolvedValue(undefined);
    render(
      <ApprovalDialog
        approvals={[makeApproval()]}
        onApprove={onApprove}
        onReject={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "APPROVE" }));
    await waitFor(() =>
      expect(onApprove).toHaveBeenCalledWith("approval-1"));
  });

  it("calls the explicit rejection action", async () => {
    const onReject = vi.fn().mockResolvedValue(undefined);
    render(
      <ApprovalDialog
        approvals={[makeApproval()]}
        onApprove={vi.fn()}
        onReject={onReject}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "REJECT" }));
    await waitFor(() =>
      expect(onReject).toHaveBeenCalledWith("approval-1"));
  });

  it("closes itself when every request has already expired locally", () => {
    render(
      <ApprovalDialog
        approvals={[
          makeApproval({
            expires_at: new Date(Date.now() - 1_000).toISOString(),
          }),
        ]}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    );

    expect(screen.getByText("EXPIRED")).toBeTruthy();
    expect(
      (screen.getByText("Action approval").closest("dialog") as HTMLDialogElement)
        .open,
    ).toBe(false);
  });

  it("stays closed when nothing is pending", () => {
    const { container } = render(
      <ApprovalDialog
        approvals={[makeApproval({ status: "approved" })]}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expect(container.querySelector("dialog")?.open).toBe(false);
  });

  it("does not submit twice while a decision is in flight", async () => {
    let resolve: (value: unknown) => void = () => undefined;
    const onApprove = vi.fn(
      () => new Promise((done) => {
        resolve = done;
      }),
    );
    render(
      <ApprovalDialog
        approvals={[makeApproval()]}
        onApprove={onApprove}
        onReject={vi.fn()}
      />,
    );

    const approve = screen.getByRole("button", { name: "APPROVE" });
    fireEvent.click(approve);
    fireEvent.click(approve);
    expect(onApprove).toHaveBeenCalledTimes(1);
    resolve(undefined);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "APPROVE" })).toBeTruthy());
  });

  it("keeps the dialog open and re-enables decisions after a backend error", async () => {
    const onApprove = vi.fn().mockRejectedValue(new Error("backend failed"));
    const { container } = render(
      <ApprovalDialog
        approvals={[makeApproval()]}
        onApprove={onApprove}
        onReject={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "APPROVE" }));
    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(
        (screen.getByRole("button", { name: "APPROVE" }) as HTMLButtonElement)
          .disabled,
      ).toBe(false),
    );
    expect(container.querySelector("dialog")?.open).toBe(true);
  });

  it("closes immediately when the backend marks the request approved", () => {
    const { container, rerender } = render(
      <ApprovalDialog
        approvals={[makeApproval()]}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expect(container.querySelector("dialog")?.open).toBe(true);
    rerender(
      <ApprovalDialog
        approvals={[makeApproval({ status: "approved" })]}
        onApprove={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expect(container.querySelector("dialog")?.open).toBe(false);
  });
});
