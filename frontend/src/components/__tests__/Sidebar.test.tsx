import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SIDEBAR_NAV, Sidebar } from "../Sidebar";
import type { ModuleInfo } from "../../types";

function moduleInfo(id: string, available = true): ModuleInfo {
  return {
    id,
    index: "02",
    name: id.toUpperCase(),
    description: `${id} module`,
    glyph: "?",
    available,
    count: 0,
    status: "0 LINKED",
  };
}

describe("Sidebar Agents and Agent Run navigation", () => {
  it("keeps Agents as a placeholder and adds a separate Agent Run entry", () => {
    const agents = SIDEBAR_NAV.find((entry) => entry.id === "agents");
    const agentRun = SIDEBAR_NAV.find((entry) => entry.id === "agent_run");

    expect(agents).toBeTruthy();
    expect(agents?.label).toBe("Agents");
    expect(agents?.description.toLowerCase()).toContain("placeholder");

    expect(agentRun).toBeTruthy();
    expect(agentRun?.label).toBe("Agent Run");
    expect(agentRun?.description.toLowerCase()).toContain("objective");
  });

  it("opens the placeholder bay from Agents", () => {
    const onOpenBay = vi.fn();
    render(
      <Sidebar
        modules={[moduleInfo("agents", false)]}
        collapsed={false}
        onToggleCollapsed={() => undefined}
        drawer={false}
        activeBay={null}
        onOpenBay={onOpenBay}
        onOpenSettings={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Agents" }));
    expect(onOpenBay).toHaveBeenCalledWith("agents");
  });

  it("opens Agent Run from its own sidebar entry", () => {
    const onOpenBay = vi.fn();
    render(
      <Sidebar
        modules={[]}
        collapsed={false}
        onToggleCollapsed={() => undefined}
        drawer={false}
        activeBay="agent_run"
        onOpenBay={onOpenBay}
        onOpenSettings={() => undefined}
      />,
    );

    const button = screen.getByRole("button", { name: "Agent Run" });
    expect(button.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(button);
    expect(onOpenBay).toHaveBeenCalledWith("agent_run");
  });

  it("keeps both entries visible without module telemetry", () => {
    render(
      <Sidebar
        modules={[]}
        collapsed={false}
        onToggleCollapsed={() => undefined}
        drawer={false}
        activeBay={null}
        onOpenBay={() => undefined}
        onOpenSettings={() => undefined}
      />,
    );

    expect(screen.getByRole("button", { name: "Agents" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Agent Run" })).toBeTruthy();
  });
});
