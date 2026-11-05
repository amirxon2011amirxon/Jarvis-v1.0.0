import { useMemo, useState, type ReactNode } from "react";

import type { AzeApi } from "../hooks/useAze";
import type {
  SkillAsset,
  SkillDetail,
  SkillInfo,
  SkillSource,
  SkillsSnapshot,
} from "../types";
import { cn } from "../lib/utils";
import { RefreshIcon, XIcon } from "./ui/Icons";

type Filter = "all" | "active" | "installed";

function formatTime(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function sourceLabel(source: SkillSource): string {
  return source === "builtin" ? "BUILT-IN" : "INSTALLED";
}

function matchesQuery(skill: SkillInfo, query: string): boolean {
  const text = query.trim().toLowerCase();
  if (!text) return true;
  const haystack = [
    skill.id,
    skill.name,
    skill.description,
    skill.category,
    ...(skill.tags ?? []),
  ]
    .join(" ")
    .toLowerCase();
  return text.split(/\s+/).every((part) => haystack.includes(part));
}

function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "accent" | "good" | "warn";
}) {
  return (
    <span className={cn("aze-skills-badge", `aze-skills-badge--${tone}`)}>
      {children}
    </span>
  );
}

function AssetList({
  title,
  assets,
  onOpen,
  activePath,
}: {
  title: string;
  assets: SkillAsset[];
  onOpen: (asset: SkillAsset) => void;
  activePath: string;
}) {
  if (assets.length === 0) return null;
  return (
    <section className="aze-skills-section">
      <p className="aze-eyebrow">{title}</p>
      <div className="aze-skills-asset-list">
        {assets.map((asset) => (
          <button
            key={asset.path}
            type="button"
            className={cn(
              "aze-skills-asset",
              asset.path === activePath && "aze-skills-asset--active",
            )}
            onClick={() => asset.readable && onOpen(asset)}
            disabled={!asset.readable}
            title={asset.readable ? asset.path : "Binary resource (not previewable)"}
          >
            <span className="aze-skills-asset-name">{asset.path}</span>
            <span className="aze-skills-asset-size">{formatSize(asset.size)}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

interface SkillsViewProps {
  aze: AzeApi;
  onClose: () => void;
}

export function SkillsView({ aze, onClose }: SkillsViewProps) {
  const skills = aze.state.skills;
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [assetPreview, setAssetPreview] = useState<{
    path: string;
    content: string;
  } | null>(null);
  const [assetLoading, setAssetLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [notice, setNotice] = useState("");

  const activeById = useMemo(
    () => new Map(skills.active.map((item) => [item.id, item])),
    [skills.active],
  );

  const selectedSkill = useMemo(
    () => skills.skills.find((item) => item.id === selectedId) ?? null,
    [skills.skills, selectedId],
  );

  const visible = useMemo(
    () =>
      skills.skills.filter((skill) => {
        if (!matchesQuery(skill, query)) return false;
        if (filter === "active") return activeById.has(skill.id);
        if (filter === "installed") return skill.source === "installed";
        return true;
      }),
    [activeById, filter, query, skills.skills],
  );

  const loadDetail = async (skillId: string): Promise<SkillDetail | null> => {
    setSelectedId(skillId);
    setDetail(null);
    setDetailError("");
    setAssetPreview(null);
    setDetailLoading(true);
    try {
      const data = await aze.skillDetail(skillId);
      setDetail(data);
      return data;
    } catch (error) {
      setDetailError(errorText(error));
      return null;
    } finally {
      setDetailLoading(false);
    }
  };

  const openAsset = async (asset: SkillAsset) => {
    if (!selectedId || !asset.readable) return;
    setAssetLoading(true);
    setDetailError("");
    try {
      const result = await aze.skillAsset(selectedId, asset.path);
      setAssetPreview({ path: result.path, content: result.content });
    } catch (error) {
      setDetailError(errorText(error));
    } finally {
      setAssetLoading(false);
    }
  };

  const refresh = async (): Promise<SkillsSnapshot | null> => {
    try {
      const snapshot = await aze.refreshSkills();
      setNotice(
        `Discovery refreshed — ${snapshot.counts.enabled} enabled, `
        + `${snapshot.counts.installed} installed, `
        + `${snapshot.errors.length} issue(s).`,
      );
      return snapshot;
    } catch (error) {
      setNotice(errorText(error));
      return null;
    }
  };

  const setEnabled = async (
    skill: SkillInfo,
    enabled: boolean,
  ): Promise<boolean> => {
    if (busyId) return false;
    setBusyId(skill.id);
    setNotice("");
    try {
      await aze.setSkillEnabled(skill.id, enabled);
      setNotice(`${skill.name} ${enabled ? "enabled" : "disabled"}.`);
      if (selectedId === skill.id) {
        const data = await aze.skillDetail(skill.id);
        setDetail(data);
      }
      return true;
    } catch (error) {
      setNotice(errorText(error));
      return false;
    } finally {
      setBusyId(null);
    }
  };

  const uninstall = async (skill: SkillInfo): Promise<boolean> => {
    if (skill.source !== "installed" || busyId) return false;
    const confirmed = window.confirm(
      `Remove the installed Skill "${skill.name}"? The package files in `
      + "Skills/Installed will be deleted. Built-in Skills are never removed.",
    );
    if (!confirmed) return false;
    setBusyId(skill.id);
    setNotice("");
    try {
      await aze.uninstallSkill(skill.id);
      setSelectedId(null);
      setDetail(null);
      setAssetPreview(null);
      setNotice(`${skill.name} removed from Installed/.`);
      return true;
    } catch (error) {
      setNotice(errorText(error));
      return false;
    } finally {
      setBusyId(null);
    }
  };

  const activeCount = skills.counts.active;
  const issueCount = skills.errors.length;

  return (
    <div
      className="aze-bay-overlay"
      role="dialog"
      aria-modal="false"
      aria-label="Skills"
    >
      <section className="aze-bay-sheet aze-skills-sheet">
        <header className="aze-bay-head">
          <div className="min-w-0">
            <p className="aze-eyebrow">
              J.A.R.V.I.S. // SKILLS ENGINE · v{skills.engine_version}
            </p>
            <h2 className="aze-bay-title">Skills</h2>
          </div>
          <div className="flex shrink-0 items-center gap-2.5">
            <span className="aze-bay-pill aze-bay-pill--live">
              {activeCount > 0 ? `${activeCount} ACTIVE` : "STANDBY"}
            </span>
            <button
              type="button"
              onClick={() => void refresh()}
              aria-label="Refresh Skill discovery"
              title="Rescan Builtin/ and Installed/"
              className="aze-icon-button"
            >
              <RefreshIcon className="h-4 w-4" />
            </button>
            <button
              type="button"
              onClick={onClose}
              aria-label="Close Skills"
              title="Close Skills"
              className="aze-icon-button"
            >
              <XIcon className="h-4 w-4" />
            </button>
          </div>
        </header>

        <div className="aze-skills-body">
          <section className="aze-skills-statrow" aria-label="Skill statistics">
            <article className="aze-skills-stat">
              <span>TOTAL</span>
              <strong>{skills.counts.total}</strong>
            </article>
            <article className="aze-skills-stat">
              <span>ACTIVE</span>
              <strong>{activeCount}</strong>
            </article>
            <article className="aze-skills-stat">
              <span>INSTALLED</span>
              <strong>{skills.counts.installed}</strong>
            </article>
            <article className={cn("aze-skills-stat", issueCount > 0 && "aze-skills-stat--warn")}>
              <span>ISSUES</span>
              <strong>{issueCount}</strong>
            </article>
          </section>

          {skills.active.length > 0 ? (
            <section className="aze-skills-active" aria-label="Active Skills">
              <p className="aze-eyebrow">ACTIVE FOR THIS CONVERSATION</p>
              <div className="aze-skills-active-list">
                {skills.active.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className="aze-skills-active-chip"
                    onClick={() => void loadDetail(item.id)}
                  >
                    <span className="aze-skills-dot" aria-hidden="true" />
                    <span className="aze-skills-active-name">{item.name}</span>
                    <span className="aze-skills-active-time">
                      {formatTime(item.activated_at)}
                    </span>
                  </button>
                ))}
              </div>
            </section>
          ) : (
            <p className="aze-skills-note">
              No Skill is active. Jarvis activates one only when a task benefits
              from specialised guidance.
            </p>
          )}

          <div className="aze-skills-toolbar">
            <label className="aze-skills-search">
              <span className="sr-only">Filter skills</span>
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.currentTarget.value)}
                placeholder="Filter skills, tags, categories…"
              />
            </label>
            <div className="aze-skills-tabs" role="group" aria-label="Skill filter">
              {(["all", "active", "installed"] as Filter[]).map((value) => (
                <button
                  key={value}
                  type="button"
                  className={cn(
                    "aze-skills-tab",
                    filter === value && "aze-skills-tab--active",
                  )}
                  aria-pressed={filter === value}
                  onClick={() => setFilter(value)}
                >
                  {value.toUpperCase()}
                </button>
              ))}
            </div>
          </div>

          {notice ? (
            <p className="aze-skills-note" role="status">{notice}</p>
          ) : null}
          {detailError ? (
            <p className="aze-skills-note aze-skills-note--error" role="alert">
              {detailError}
            </p>
          ) : null}

          <div className="aze-skills-grid">
            <section className="aze-skills-list" aria-label="Skill packages">
              {visible.length === 0 ? (
                <p className="aze-skills-empty">
                  No Skills match this view. Refresh discovery or clear the filter.
                </p>
              ) : (
                visible.map((skill) => {
                  const active = activeById.get(skill.id);
                  return (
                    <div
                      key={skill.id}
                      className={cn(
                        "aze-skills-row",
                        selectedId === skill.id && "aze-skills-row--selected",
                      )}
                    >
                      <button
                        type="button"
                        className="aze-skills-main"
                        onClick={() => void loadDetail(skill.id)}
                        aria-pressed={selectedId === skill.id}
                        aria-label={`Inspect ${skill.name}`}
                      >
                        <span className="aze-skills-row-head">
                          <span className="aze-skills-name">{skill.name}</span>
                          <span className="aze-skills-version">v{skill.version}</span>
                        </span>
                        <span className="aze-skills-desc">{skill.description}</span>
                        <span className="aze-skills-meta">
                          <Badge tone={skill.source === "builtin" ? "accent" : "neutral"}>
                            {sourceLabel(skill.source)}
                          </Badge>
                          <span className="aze-skills-category">{skill.category}</span>
                          {active ? <Badge tone="good">ACTIVE</Badge> : null}
                          {!skill.enabled ? <Badge tone="warn">DISABLED</Badge> : null}
                        </span>
                      </button>
                      <button
                        type="button"
                        role="switch"
                        aria-checked={skill.enabled}
                        aria-label={`${skill.enabled ? "Disable" : "Enable"} ${skill.name}`}
                        className={cn(
                          "aze-skills-switch",
                          skill.enabled && "aze-skills-switch--on",
                        )}
                        disabled={busyId === skill.id}
                        onClick={() => void setEnabled(skill, !skill.enabled)}
                      >
                        <span className="aze-skills-switch-knob" aria-hidden="true" />
                      </button>
                    </div>
                  );
                })
              )}
            </section>

            <aside className="aze-skills-detail" aria-live="polite">
              {!selectedId ? (
                <p className="aze-skills-empty">
                  Select a Skill to view its manifest, instructions, references
                  and package location.
                </p>
              ) : detailLoading ? (
                <p className="aze-skills-note">Loading package details…</p>
              ) : detailError ? (
                <p className="aze-skills-note aze-skills-note--error">{detailError}</p>
              ) : detail && selectedSkill ? (
                <div className="aze-skills-detail-body">
                  <div className="aze-skills-detail-head">
                    <div>
                      <p className="aze-skills-detail-name">{selectedSkill.name}</p>
                      <p className="aze-skills-detail-id">
                        {selectedSkill.id} · v{selectedSkill.version}
                      </p>
                    </div>
                    <Badge tone={selectedSkill.source === "builtin" ? "accent" : "neutral"}>
                      {sourceLabel(selectedSkill.source)}
                    </Badge>
                  </div>

                  <div className="aze-skills-actions">
                    <button
                      type="button"
                      className="aze-skills-button"
                      onClick={() => void setEnabled(selectedSkill, !selectedSkill.enabled)}
                      disabled={busyId === selectedSkill.id}
                    >
                      {selectedSkill.enabled ? "DISABLE" : "ENABLE"}
                    </button>
                    {selectedSkill.source === "installed" ? (
                      <button
                        type="button"
                        className="aze-skills-button aze-skills-button--danger"
                        onClick={() => void uninstall(selectedSkill)}
                        disabled={busyId === selectedSkill.id}
                      >
                        UNINSTALL
                      </button>
                    ) : null}
                  </div>

                  <dl className="aze-skills-facts">
                    <div><dt>Category</dt><dd>{selectedSkill.category}</dd></div>
                    <div>
                      <dt>State</dt>
                      <dd>
                        {selectedSkill.active
                          ? "ACTIVE"
                          : selectedSkill.enabled
                            ? "READY"
                            : "DISABLED"}
                      </dd>
                    </div>
                    <div><dt>Activated</dt><dd>{formatTime(selectedSkill.activated_at)}</dd></div>
                    <div><dt>License</dt><dd>{selectedSkill.license || "—"}</dd></div>
                    <div><dt>Available</dt><dd>{selectedSkill.available ? "YES" : "NO"}</dd></div>
                  </dl>

                  {selectedSkill.missing_tools?.length ? (
                    <p className="aze-skills-note aze-skills-note--warn">
                      Declared tools not currently available:{" "}
                      {selectedSkill.missing_tools.join(", ")}. This does not grant
                      access; use available tools only.
                    </p>
                  ) : null}

                  <section className="aze-skills-section">
                    <p className="aze-eyebrow">MANIFEST</p>
                    <dl className="aze-skills-manifest">
                      <div><dt>Author</dt><dd>{selectedSkill.author || "—"}</dd></div>
                      <div><dt>Entrypoint</dt><dd>{selectedSkill.entrypoint}</dd></div>
                      <div><dt>Tags</dt><dd>{selectedSkill.tags.join(", ") || "—"}</dd></div>
                      <div><dt>Tools</dt><dd>{selectedSkill.requirements.tools.join(", ") || "—"}</dd></div>
                      <div><dt>Package</dt><dd className="aze-skills-path">{detail.package.path}</dd></div>
                    </dl>
                  </section>

                  <section className="aze-skills-section">
                    <p className="aze-eyebrow">SKILL.MD</p>
                    {detail.instructions_error ? (
                      <p className="aze-skills-note aze-skills-note--error">
                        {detail.instructions_error}
                      </p>
                    ) : (
                      <pre className="aze-skills-instructions">{detail.instructions}</pre>
                    )}
                  </section>

                  <AssetList
                    title="REFERENCES"
                    assets={detail.references}
                    onOpen={(asset) => void openAsset(asset)}
                    activePath={assetPreview?.path ?? ""}
                  />
                  <AssetList
                    title="RESOURCES"
                    assets={detail.resources}
                    onOpen={(asset) => void openAsset(asset)}
                    activePath={assetPreview?.path ?? ""}
                  />

                  {assetLoading ? (
                    <p className="aze-skills-note">Loading reference…</p>
                  ) : assetPreview ? (
                    <section className="aze-skills-section">
                      <p className="aze-eyebrow">{assetPreview.path}</p>
                      <pre className="aze-skills-instructions">{assetPreview.content}</pre>
                    </section>
                  ) : null}

                  <section className="aze-skills-section">
                    <p className="aze-eyebrow">VALIDATION</p>
                    <p className="aze-skills-note">
                      {selectedSkill.validation.valid ? "Valid package." : "Invalid package."}
                      {selectedSkill.validation.warnings.length
                        ? ` ${selectedSkill.validation.warnings.join(" ")}`
                        : ""}
                    </p>
                  </section>
                </div>
              ) : (
                <p className="aze-skills-note">The selected Skill is no longer installed.</p>
              )}
            </aside>
          </div>

          {skills.errors.length > 0 ? (
            <section className="aze-skills-issues" aria-label="Discovery issues">
              <p className="aze-eyebrow">DISCOVERY ISSUES</p>
              {skills.errors.map((issue) => (
                <p key={`${issue.code}:${issue.path}`} className="aze-skills-issue">
                  <span className="aze-skills-issue-code">{issue.code}</span>
                  {issue.message}
                  <span className="aze-skills-issue-path">{issue.path}</span>
                </p>
              ))}
            </section>
          ) : null}
        </div>
      </section>
    </div>
  );
}
