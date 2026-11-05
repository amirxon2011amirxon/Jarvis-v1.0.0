import { memo } from "react";

import type { AzeState } from "../types";
import type { SocketStatus } from "../lib/ws";
import { useLocalClock } from "../hooks/useLocalClock";
import { cn } from "../lib/utils";
import { HexMark, MenuIcon, SettingsIcon } from "./ui/Icons";

interface HeaderProps {
  assistantName: string;
  state: AzeState;
  socket: SocketStatus;
  sidebarCollapsed: boolean;
  drawerOpen: boolean;
  inert?: boolean;
  onToggleSidebar: () => void;
  onOpenSettings: () => void;
}

export const Header = memo(function Header({
  assistantName,
  state,
  socket,
  sidebarCollapsed,
  drawerOpen,
  inert,
  onToggleSidebar,
  onOpenSettings,
}: HeaderProps) {
  const clock = useLocalClock();
  const linkOnline = socket === "online";

  return (
    <header inert={inert ? true : undefined} className="aze-topbar">
      <div className="aze-brand">
        <HexMark className="aze-brand-mark" />
        <h1 className="aze-brand-name">{assistantName}</h1>
        <button
          type="button"
          onClick={onToggleSidebar}
          aria-expanded={!sidebarCollapsed}
          aria-controls="module-rail"
          aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          className="aze-icon-button aze-sidebar-toggle"
        >
          <MenuIcon className="h-4 w-4" />
        </button>
      </div>

      <div className="aze-top-actions">
        <div
          className="aze-clock"
          aria-label={`Local time ${clock.time}:${clock.seconds}`}
        >
          <div className="aze-clock-time">
            <span key={clock.time} className="aze-clock-mins">{clock.time}</span>
            <span className="aze-clock-seconds">
              :<span key={clock.seconds} className="aze-clock-flip">{clock.seconds}</span>
            </span>
          </div>
        </div>

        <span
          className={cn(
            "aze-link-pill",
            linkOnline ? "aze-link-pill--online" : "aze-link-pill--warn",
          )}
          role="status"
          aria-live="polite"
          title={linkOnline ? "Local event link connected" : "Local event link reconnecting"}
        >
          <span className="aze-link-dot" />
          <span className="aze-link-text">
            {linkOnline ? "LINKED" : socket === "offline" ? "LINK LOST" : "SYNCING"}
          </span>
        </span>

        <button
          type="button"
          onClick={onToggleSidebar}
          aria-expanded={drawerOpen}
          aria-controls="module-rail"
          aria-label="Open modules"
          title="Modules"
          className="aze-icon-button xl:hidden"
        >
          <MenuIcon className="h-4 w-4" />
        </button>

        <button
          type="button"
          onClick={onOpenSettings}
          aria-label="Open settings"
          title="Open settings"
          className="aze-icon-button aze-icon-button--accent"
        >
          <SettingsIcon className="h-4 w-4" />
        </button>
      </div>

      <span className="sr-only">Jarvis state: {state}</span>
    </header>
  );
});
