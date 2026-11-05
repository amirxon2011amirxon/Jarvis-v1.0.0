import { memo } from "react";

import type { ModuleInfo } from "../types";
import { cn } from "../lib/utils";
import {
  BoltIcon,
  BotIcon,
  ChevronRightIcon,
  DatabaseIcon,
  GitBranchIcon,
  LinkIcon,
  MemoryIcon,
  PuzzleIcon,
  SettingsIcon,
  SparkleIcon,
  UsersIcon,
  XIcon,
} from "./ui/Icons";

interface SidebarProps {
  modules: ModuleInfo[];
  collapsed: boolean;
  onToggleCollapsed: () => void;
  drawer: boolean;
  activeBay: string | null;
  onOpenBay: (bayId: string) => void;
  onOpenSettings: () => void;
  onClose?: () => void;
}

export interface BayNavItem {
  id: string;
  label: string;
  icon: typeof BoltIcon;
  description: string;
}

/* The brief's navigation order; every bay opens as its own tab view next to
 * the sidebar (see BayView). Unavailable bays stay honest placeholders. */
export const SIDEBAR_NAV: BayNavItem[] = [
  { id: "memory", label: "Memory", icon: MemoryIcon, description: "Durable user, episode, knowledge and project memory." },
  { id: "actions", label: "Actions", icon: BoltIcon, description: "Live desktop and local-file actions." },
  { id: "agents", label: "Agents", icon: UsersIcon, description: "Placeholder bay for installed agent packs. Use Agent Run for the live objective run." },
  { id: "agent_run", label: "Agent Run", icon: BotIcon, description: "The live objective-scoped task run, tool activity and verification." },
  { id: "skills", label: "Skills", icon: SparkleIcon, description: "Installed instruction packages, live activation state and package inspection." },
  { id: "plugins", label: "Plugins", icon: PuzzleIcon, description: "Runtime extensions and device bridges dock in this bay." },
  { id: "connectors", label: "Connectors", icon: LinkIcon, description: "External service links will be provisioned here." },
  { id: "mcps", label: "MCPs", icon: DatabaseIcon, description: "Model Context Protocol servers will appear in this bay." },
  { id: "github", label: "GitHub Repos", icon: GitBranchIcon, description: "No GitHub integration is installed in this build." },
];

export const Sidebar = memo(function Sidebar({
  modules,
  collapsed,
  onToggleCollapsed,
  drawer,
  activeBay,
  onOpenBay,
  onOpenSettings,
  onClose,
}: SidebarProps) {
  const byId = new Map(modules.map((item) => [item.id, item]));

  return (
    <aside
      id={drawer ? "module-rail" : undefined}
      className={cn(
        "aze-sidebar",
        collapsed && !drawer && "aze-sidebar--collapsed",
      )}
      aria-label="Modules"
    >
      {drawer ? (
        <div className="aze-drawer-head">
          <div>
            <p className="aze-eyebrow">MODULE ARRAY</p>
            <p className="aze-sidebar-code">LOCAL // 07 BAYS</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            autoFocus
            className="aze-icon-button"
            aria-label="Close module rail"
          >
            <XIcon className="h-4 w-4" />
          </button>
        </div>
      ) : (
        <div className="aze-sidebar-head">
          <div className="min-w-0">
            <p className="aze-eyebrow">MODULE ARRAY</p>
            <p className="aze-sidebar-code">LOCAL // 07 BAYS</p>
          </div>
          <button
            type="button"
            onClick={onToggleCollapsed}
            aria-expanded={!collapsed}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            className="aze-sidebar-collapse"
          >
            <ChevronRightIcon
              className={cn(
                "h-3.5 w-3.5 transition-transform duration-200",
                !collapsed && "rotate-180",
              )}
            />
          </button>
        </div>
      )}

      <nav className="aze-nav" aria-label="Modules">
        {SIDEBAR_NAV.map((item) => {
          const module = byId.get(item.id);
          const active = activeBay === item.id;
          const Icon = item.icon;
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => onOpenBay(item.id)}
              aria-pressed={active}
              title={collapsed && !drawer ? item.label : undefined}
              aria-label={item.label}
              className={cn("aze-nav-row", active && "aze-nav-row--active")}
            >
              <span className="aze-nav-icon"><Icon className="h-4 w-4" /></span>
              <span className="aze-nav-label">{item.label}</span>
              {module ? (
                <span className={cn("aze-nav-count", module.count > 0 && "text-[var(--accent)]")}>
                  {module.count > 0 ? module.count : ""}
                </span>
              ) : null}
              {module?.available === false ? (
                <span className="aze-nav-dot aze-nav-dot--placeholder" aria-hidden="true" />
              ) : null}
            </button>
          );
        })}

        <div className="aze-nav-divider" aria-hidden="true" />

        <button
          type="button"
          onClick={onOpenSettings}
          className="aze-nav-row aze-nav-row--settings"
          aria-label="Settings"
          title={collapsed && !drawer ? "Settings" : undefined}
        >
          <span className="aze-nav-icon"><SettingsIcon className="h-4 w-4" /></span>
          <span className="aze-nav-label">Settings</span>
        </button>
      </nav>
    </aside>
  );
});
