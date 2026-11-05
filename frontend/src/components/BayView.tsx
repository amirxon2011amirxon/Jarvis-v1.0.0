import { memo, useEffect, useMemo, useState } from "react";

import type { ModuleInfo, ScanResponse, ToolCategory, ToolInfo } from "../types";
import { cn, humanizeTool } from "../lib/utils";
import { XIcon } from "./ui/Icons";

export interface BayTabInfo {
  id: string;
  label: string;
}

interface BayViewProps {
  /** The bay whose content is displayed. */
  module: ModuleInfo;
  label: string;
  description: string;
  tools: ToolInfo[];
  /** Folder-scoped tool groups from the backend catalog. */
  categories: ToolCategory[];
  /** Persist the complete set of enabled tool module ids. */
  onSelectTools: (enabled: string[]) => Promise<void>;
  /** Every open bay tab, for the strip at the top of the sheet. */
  tabs: BayTabInfo[];
  activeTab: string;
  onSwitch: (bayId: string) => void;
  onCloseTab: (bayId: string) => void;
  onScan: (moduleId: string) => Promise<ScanResponse>;
}

/** A full-screen bay control sheet — the same treatment as the settings
 * dialog. Every sidebar item opens one tab here, and all bays are controlled
 * from the strip. Unavailable bays are honest placeholders; nothing is
 * invented. */
export const BayView = memo(function BayView({
  module,
  label,
  description,
  tools,
  categories,
  onSelectTools,
  tabs,
  activeTab,
  onSwitch,
  onCloseTab,
  onScan,
}: BayViewProps) {
  const [scanning, setScanning] = useState(false);
  const [scanResult, setScanResult] = useState<string | null>(null);

  /* Folders come from the backend catalog. The fallback keeps an older
   * snapshot usable: it groups by the tool's category fields instead of
   * pretending no tools exist. */
  const folders = useMemo<ToolCategory[]>(() => {
    if (categories.length) return categories;
    const grouped = new Map<string, ToolCategory>();
    for (const tool of tools) {
      const id = tool.category ?? "";
      let folder = grouped.get(id);
      if (!folder) {
        folder = {
          id,
          label: tool.category_label ?? (id || "UNCATEGORISED"),
          description: "",
          tools: [],
          count: 0,
          enabled_count: 0,
          enabled: false,
        };
        grouped.set(id, folder);
      }
      folder.tools.push(tool);
    }
    return [...grouped.values()];
  }, [categories, tools]);

  const selectedIds = useMemo(() => {
    const ids = new Set<string>();
    for (const folder of folders) {
      for (const tool of folder.tools) {
        if (tool.enabled !== false) ids.add(tool.id ?? tool.name);
      }
    }
    return ids;
  }, [folders]);

  const applySelection = (ids: Set<string>) => {
    void onSelectTools([...ids]).catch(() => undefined);
  };

  const toggleTool = (tool: ToolInfo) => {
    const id = tool.id ?? tool.name;
    const next = new Set(selectedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    applySelection(next);
  };

  const toggleFolder = (folder: ToolCategory) => {
    const ids = folder.tools.map((tool) => tool.id ?? tool.name);
    const allOn = ids.length > 0 && ids.every((id) => selectedIds.has(id));
    const next = new Set(selectedIds);
    for (const id of ids) {
      if (allOn) next.delete(id);
      else next.add(id);
    }
    applySelection(next);
  };

  /* A freshly focused bay starts clean; its own scan result persists while
   * its tab stays open, exactly like a browser tab would. */
  useEffect(() => {
    setScanning(false);
    setScanResult(null);
  }, [module.id]);

  const scan = async () => {
    if (scanning) return;
    setScanning(true);
    setScanResult(null);
    try {
      const response = await onScan(module.id);
      setScanResult(response.message.replace(/^SYS:\s*/, ""));
    } catch {
      // The hook already surfaced the failure as a toast.
    } finally {
      setScanning(false);
    }
  };

  return (
    <div
      className="aze-bay-overlay"
      role="dialog"
      aria-modal="false"
      aria-label={`${label} bay control`}
      onMouseDown={(event) => {
        // Clicking the scrim collapses the sheet; keeping the open tabs.
        if (event.target === event.currentTarget) onCloseTab("__collapse__");
      }}
    >
      <section className="aze-bay-sheet">
        {tabs.length > 1 ? (
          <div className="aze-baytabs" role="tablist" aria-label="Open bays">
            {tabs.map((tab) => (
              <span
                key={tab.id}
                className={cn(
                  "aze-baytab",
                  tab.id === activeTab && "aze-baytab--active",
                )}
              >
                <button
                  type="button"
                  role="tab"
                  aria-selected={tab.id === activeTab}
                  onClick={() => onSwitch(tab.id)}
                  className="aze-baytab-label"
                >
                  {tab.label}
                </button>
                <button
                  type="button"
                  onClick={() => onCloseTab(tab.id)}
                  aria-label={`Close ${tab.label} bay`}
                  className="aze-baytab-close"
                >
                  <XIcon className="h-3 w-3" />
                </button>
              </span>
            ))}
          </div>
        ) : null}

        <header className="aze-bay-head">
          <div className="min-w-0">
            <p className="aze-eyebrow">J.A.R.V.I.S. // BAY {module.index} CONTROL</p>
            <h2 className="aze-bay-title">{label}</h2>
          </div>
          <div className="flex shrink-0 items-center gap-2.5">
            <span className={cn("aze-bay-pill", module.available ? "aze-bay-pill--live" : "aze-bay-pill--placeholder")}>
              {module.available ? "LIVE BAY" : "PLACEHOLDER BAY"}
            </span>
            <button
              type="button"
              onClick={() => onCloseTab(module.id)}
              aria-label={`Close ${label} bay`}
              title="Close bay"
              className="aze-icon-button"
            >
              <XIcon className="h-4 w-4" />
            </button>
          </div>
        </header>

        <div className="aze-bay-body">
          <p className="aze-bay-desc">{module.description || description}</p>
          <p className="aze-bay-status">{module.status}</p>

          {module.id === "actions" ? (
            tools.length === 0 ? (
              <p className="aze-bay-note">No tools registered.</p>
            ) : (
              <div className="aze-tool-folders" aria-label="Tool categories">
                {folders.map((folder) => {
                  const ids = folder.tools.map((tool) => tool.id ?? tool.name);
                  const active = ids.filter((id) => selectedIds.has(id)).length;
                  const allOn = ids.length > 0 && active === ids.length;
                  return (
                    <section
                      key={folder.id || "uncategorised"}
                      className="aze-tool-folder"
                      aria-label={`${folder.label} tools`}
                    >
                      <header className="aze-tool-folder-head">
                        <button
                          type="button"
                          className={cn(
                            "aze-tool-folder-toggle",
                            allOn && "aze-tool-folder-toggle--on",
                          )}
                          aria-pressed={allOn}
                          title={allOn
                            ? `Disable every tool in ${folder.label}`
                            : `Enable every tool in ${folder.label}`}
                          onClick={() => toggleFolder(folder)}
                        >
                          <span
                            className={cn(
                              "aze-status-dot",
                              allOn ? "bg-[var(--ok)]" : "bg-[var(--dim)]",
                            )}
                            aria-hidden="true"
                          />
                          <span className="aze-tool-folder-name">{folder.label}</span>
                          <span className="aze-tool-folder-count">
                            {active}/{ids.length} ACTIVE
                          </span>
                        </button>
                        {folder.description ? (
                          <p className="aze-tool-folder-desc">{folder.description}</p>
                        ) : null}
                      </header>
                      <div className="aze-bay-grid">
                        {folder.tools.map((tool) => {
                          const id = tool.id ?? tool.name;
                          const on = selectedIds.has(id);
                          const actionCount = tool.actions?.length ?? 0;
                          return (
                            <button
                              key={id}
                              type="button"
                              className={cn(
                                "aze-bay-card aze-tool-card",
                                !on && "aze-tool-card--off",
                              )}
                              aria-pressed={on}
                              title={tool.description}
                              onClick={() => toggleTool(tool)}
                            >
                              <span
                                className={cn(
                                  "aze-status-dot aze-bay-card-dot",
                                  on ? "bg-[var(--ok)]" : "bg-[var(--dim)]",
                                )}
                                aria-hidden="true"
                              />
                              <span className="min-w-0">
                                <span className="aze-bay-card-name">
                                  {humanizeTool(tool.name)}
                                </span>
                                <span className="aze-bay-card-tag">
                                  {on ? "ENABLED" : "DISABLED"} · {actionCount}{" "}
                                  {actionCount === 1 ? "ACTION" : "ACTIONS"}
                                </span>
                                {actionCount ? (
                                  <span className="aze-bay-card-actions">
                                    {tool.actions?.join(" · ")}
                                  </span>
                                ) : null}
                              </span>
                            </button>
                          );
                        })}
                      </div>
                    </section>
                  );
                })}
              </div>
            )
          ) : (
            <div className="aze-bay-grid" aria-hidden="true">
              {[1, 2, 3, 4, 5, 6].map((slot) => (
                <div key={slot} className="aze-bay-card aze-bay-card--slot">
                  <span className="aze-bay-slot-code">SLOT 0{slot}</span>
                  <p className="aze-bay-slot-note">
                    {module.available ? "Extensible bay slot." : "Awaiting a bay installer — nothing is linked yet."}
                  </p>
                </div>
              ))}
            </div>
          )}

          {scanResult ? (
            <p className="aze-bay-scan-result" role="status">{scanResult}</p>
          ) : null}

          <button type="button" onClick={() => void scan()} disabled={scanning} className="aze-bay-scan">
            {scanning ? "SCANNING…" : "SCAN MODULE BAY"}
          </button>
        </div>
      </section>
    </div>
  );
});
