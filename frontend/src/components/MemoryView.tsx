import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { ApiError, api } from "../lib/api";
import type {
  MemoryDetail,
  MemoryRecord,
  MemorySettings,
  MemoryStats,
  MemoryType,
} from "../types";
import { cn } from "../lib/utils";
import {
  AlertIcon,
  CheckIcon,
  DatabaseIcon,
  RefreshIcon,
  XIcon,
} from "./ui/Icons";

const TYPE_TABS: { id: MemoryType | "all"; label: string }[] = [
  { id: "all", label: "ALL" },
  { id: "user", label: "USER" },
  { id: "episodic", label: "EPISODES" },
  { id: "knowledge", label: "KNOWLEDGE" },
  { id: "project", label: "PROJECTS" },
];

const PAGE = 40;

function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "The memory operation failed.";
}

export interface MemoryProps {
  onClose?: () => void;
}

export function MemoryView({ onClose }: MemoryProps) {
  const alive = useRef(true);
  const listAbort = useRef<AbortController | null>(null);
  const detailAbort = useRef<AbortController | null>(null);

  const [stats, setStats] = useState<MemoryStats | null>(null);
  const [settings, setSettings] = useState<MemorySettings | null>(null);
  const [items, setItems] = useState<MemoryRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState<MemoryType | "all">("all");
  const [projectFilter, setProjectFilter] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<MemoryRecord | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({
    content: "",
    title: "",
    type: "user" as MemoryType,
    tags: "",
    project_id: "",
    importance: 0.5,
    confidence: 0.7,
  });

  const refreshStats = useCallback(async () => {
    try {
      const response = await api.memoryStats();
      if (!alive.current) return;
      setStats(response.data);
    } catch (err) {
      if (alive.current) setError(errorMessage(err));
    }
  }, []);

  const loadSettings = useCallback(async () => {
    try {
      const response = await api.memorySettings();
      if (alive.current) setSettings(response);
    } catch (err) {
      if (alive.current) setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void refreshStats();
    void loadSettings();
    return () => {
      alive.current = false;
      listAbort.current?.abort();
      detailAbort.current?.abort();
    };
  }, [refreshStats, loadSettings]);

  /* Debounced search. A newer request aborts the older one, so a slow result
   * can never replace the list for a query the user has already changed. */
  useEffect(() => {
    const handle = window.setTimeout(() => {
      listAbort.current?.abort();
      const controller = new AbortController();
      listAbort.current = controller;
      setLoading(true);
      setError(null);
      api
        .memoryList(
          {
            type: typeFilter === "all" ? undefined : typeFilter,
            project_id: projectFilter || undefined,
            query: query.trim() || undefined,
            limit: PAGE,
            offset: 0,
          },
          controller.signal,
        )
        .then((response) => {
          if (!alive.current || controller.signal.aborted) return;
          const rows = response.data?.memories ?? [];
          setItems(rows);
          setTotal(response.data?.total ?? rows.length);
          setSelectedId((current) =>
            rows.some((row) => row.id === current) ? current : rows[0]?.id ?? null,
          );
        })
        .catch((err) => {
          if (controller.signal.aborted || !alive.current) return;
          setError(errorMessage(err));
          setItems([]);
          setTotal(0);
        })
        .finally(() => {
          if (alive.current && !controller.signal.aborted) setLoading(false);
        });
    }, 260);
    return () => window.clearTimeout(handle);
  }, [query, typeFilter, projectFilter]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    detailAbort.current?.abort();
    const controller = new AbortController();
    detailAbort.current = controller;
    setDetailLoading(true);
    api
      .memoryGet(selectedId, controller.signal)
      .then((response: MemoryDetail) => {
        if (!alive.current || controller.signal.aborted) return;
        setDetail(response.memory ?? null);
      })
      .catch((err) => {
        if (controller.signal.aborted || !alive.current) return;
        setError(errorMessage(err));
      })
      .finally(() => {
        if (alive.current && !controller.signal.aborted) setDetailLoading(false);
      });
  }, [selectedId]);

  useEffect(() => {
    if (!detail) return;
    setDraft({
      content: detail.content,
      title: detail.title ?? "",
      type: detail.type,
      tags: (detail.tags ?? []).join(", "),
      project_id: detail.project_id ?? "",
      importance: detail.importance,
      confidence: detail.confidence,
    });
    setEditing(false);
  }, [detail]);

  const refreshAll = useCallback(async () => {
    await Promise.all([refreshStats(), loadSettings()]);
    setQuery((value) => value);
    listAbort.current?.abort();
    const controller = new AbortController();
    listAbort.current = controller;
    try {
      const response = await api.memoryList(
        {
          type: typeFilter === "all" ? undefined : typeFilter,
          project_id: projectFilter || undefined,
          query: query.trim() || undefined,
          limit: PAGE,
          offset: 0,
        },
        controller.signal,
      );
      if (!alive.current) return;
      const rows = response.data?.memories ?? [];
      setItems(rows);
      setTotal(response.data?.total ?? rows.length);
      setSelectedId((current) =>
        rows.some((row) => row.id === current) ? current : rows[0]?.id ?? null,
      );
    } catch (err) {
      if (!controller.signal.aborted && alive.current) setError(errorMessage(err));
    }
  }, [refreshStats, loadSettings, typeFilter, projectFilter, query]);

  const loadMore = async () => {
    if (busy || items.length >= total) return;
    setBusy(true);
    try {
      const response = await api.memoryList({
        type: typeFilter === "all" ? undefined : typeFilter,
        project_id: projectFilter || undefined,
        query: query.trim() || undefined,
        limit: PAGE,
        offset: items.length,
      });
      if (!alive.current) return;
      const rows = response.data?.memories ?? [];
      setItems((current) => {
        const seen = new Set(current.map((row) => row.id));
        return [...current, ...rows.filter((row) => !seen.has(row.id))];
      });
      setTotal(response.data?.total ?? items.length + rows.length);
    } catch (err) {
      if (alive.current) setError(errorMessage(err));
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const save = async () => {
    if (!detail || busy) return;
    if (!draft.content.trim()) {
      setError("Memory content cannot be empty.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const response = await api.memoryUpdate(detail.id, {
        content: draft.content.trim(),
        title: draft.title.trim(),
        type: draft.type,
        tags: draft.tags
          .split(",")
          .map((tag) => tag.trim())
          .filter(Boolean),
        project_id: draft.project_id.trim(),
        importance: draft.importance,
        confidence: draft.confidence,
        expected_version: detail.version,
      });
      if (response.memory) {
        setDetail(response.memory);
        setNotice("Memory updated.");
      }
      await refreshAll();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const remove = async () => {
    if (!detail || busy) return;
    if (!window.confirm(
      "Delete this memory permanently? Future conversations will not see it.",
    )) return;
    setBusy(true);
    setError(null);
    try {
      await api.memoryDelete(detail.id);
      setNotice("Memory deleted.");
      setSelectedId(null);
      setDetail(null);
      await refreshAll();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const runAction = async (
    action: () => Promise<{ ok: boolean; message?: string }>,
    success: string,
  ) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const response = await action();
      if (!response.ok) throw new Error(response.message || "The operation failed.");
      setNotice(response.message || success);
      await refreshAll();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const maintenance = () =>
    runAction(
      () => api.memoryMaintenance(true),
      "Maintenance complete: index rebuilt, relationships checked.",
    );

  const backup = () => runAction(() => api.memoryBackup(), "Backup created.");

  const exportAll = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const payload = await api.memoryExport();
      const blob = new Blob([JSON.stringify(payload, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `jarvis-memory-${new Date().toISOString().slice(0, 10)}.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setNotice("Export downloaded.");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const importFile = async (file: File | undefined) => {
    if (!file) return;
    if (file.size > 900_000) {
      setError("That export is larger than the interface's import limit.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const text = await file.text();
      const parsed = JSON.parse(text);
      const preview = await api.memoryImport(parsed, true);
      const count = Number((preview.data as { would_import?: number })?.would_import ?? 0);
      if (!window.confirm(
        `Import validation found ${count} record(s). Existing memories are not replaced. Continue?`,
      )) {
        setBusy(false);
        return;
      }
      const response = await api.memoryImport(parsed, false);
      setNotice(response.message || "Import complete.");
      await refreshAll();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const forgetAll = async () => {
    if (!window.confirm(
      "Delete every long-term memory? This cannot be undone unless you have a backup.",
    )) return;
    await runAction(
      () => api.memoryForget({ confirm: true }),
      "All long-term memories were deleted.",
    );
  };

  const toggleSetting = async (key: keyof MemorySettings, value: boolean) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const response = await api.updateMemorySettings({ [key]: value });
      setSettings(response.settings);
      setNotice("Memory settings updated.");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const typeCounts = stats?.by_type ?? {
    user: 0,
    episodic: 0,
    knowledge: 0,
    project: 0,
  };

  const projects = useMemo(
    () => (stats?.projects ?? []).filter(Boolean),
    [stats?.projects],
  );

  const sheet = (
      <section className="aze-bay-sheet aze-memory-sheet">
        <header className="aze-bay-head">
          <div className="min-w-0">
            <p className="aze-eyebrow">J.A.R.V.I.S. // CORE MEMORY</p>
            <h2 className="aze-bay-title">Memory</h2>
          </div>
          <div className="flex shrink-0 items-center gap-2.5">
            <span className={cn("aze-bay-pill", "aze-bay-pill--live")}>
              {stats?.fts ? "FTS INDEX ONLINE" : "KEYWORD FALLBACK"}
            </span>
            <button
              type="button"
              onClick={onClose}
              aria-label="Close memory control"
              title="Close memory"
              className="aze-icon-button"
              hidden={!onClose}
            >
              <XIcon className="h-4 w-4" />
            </button>
          </div>
        </header>

        <div className="aze-memory-body">
          <section className="aze-memory-statrow" aria-label="Memory statistics">
            <article className="aze-memory-stat">
              <span>ACTIVE</span>
              <strong>{stats?.total ?? 0}</strong>
            </article>
            {TYPE_TABS.filter((tab) => tab.id !== "all").map((tab) => (
              <article key={tab.id} className="aze-memory-stat">
                <span>{tab.label}</span>
                <strong>{typeCounts[tab.id as MemoryType] ?? 0}</strong>
              </article>
            ))}
            <article className="aze-memory-stat">
              <span>SUPERSEDED</span>
              <strong>{stats?.superseded ?? 0}</strong>
            </article>
            <button
              type="button"
              className="aze-memory-iconaction"
              onClick={() => void refreshAll()}
              aria-label="Refresh memory"
              title="Refresh"
            >
              <RefreshIcon className="h-4 w-4" />
            </button>
          </section>

          {stats?.recent?.length ? (
            <div className="aze-memory-recent" aria-label="Recent memory updates">
              <span className="aze-memory-recent-label">RECENT</span>
              {stats.recent.slice(0, 4).map((entry) => (
                <button
                  key={entry.id}
                  type="button"
                  className="aze-memory-recent-item"
                  onClick={() => setSelectedId(entry.id)}
                  title={entry.title}
                >
                  {entry.title}
                </button>
              ))}
            </div>
          ) : null}

          {error ? (
            <p className="aze-memory-banner aze-memory-banner--error" role="alert">
              <AlertIcon className="h-4 w-4" /> {error}
            </p>
          ) : null}
          {notice ? (
            <p className="aze-memory-banner aze-memory-banner--ok" role="status">
              <CheckIcon className="h-4 w-4" /> {notice}
            </p>
          ) : null}

          <div className="aze-memory-toolbar">
            <label className="aze-memory-search">
              <span className="sr-only">Search memories</span>
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search content, tags, projects…"
                maxLength={500}
              />
            </label>
            <select
              className="aze-memory-select"
              value={projectFilter}
              onChange={(event) => setProjectFilter(event.target.value)}
              aria-label="Filter by project"
            >
              <option value="">ALL PROJECTS</option>
              {projects.map((project) => (
                <option key={project} value={project}>{project}</option>
              ))}
            </select>
          </div>

          <div className="aze-memory-tabs" role="tablist" aria-label="Memory categories">
            {TYPE_TABS.map((tab) => (
              <button
                key={tab.id}
                type="button"
                role="tab"
                aria-selected={typeFilter === tab.id}
                className={cn("aze-memory-tab", typeFilter === tab.id && "aze-memory-tab--active")}
                onClick={() => setTypeFilter(tab.id)}
              >
                {tab.label}
              </button>
            ))}
          </div>

          <div className="aze-memory-grid">
            <div className="aze-memory-list" aria-busy={loading}>
              {loading && items.length === 0 ? (
                <p className="aze-memory-empty">Loading memories…</p>
              ) : items.length === 0 ? (
                <div className="aze-memory-empty">
                  <DatabaseIcon className="mx-auto h-8 w-8 opacity-50" />
                  <p>No memories match this view.</p>
                  <p className="aze-memory-empty-note">
                    Jarvis saves durable facts automatically, and you can ask
                    it to remember something at any time.
                  </p>
                </div>
              ) : (
                <>
                  {items.map((memory) => (
                    <button
                      key={memory.id}
                      type="button"
                      className={cn(
                        "aze-memory-row",
                        memory.id === selectedId && "aze-memory-row--active",
                      )}
                      onClick={() => setSelectedId(memory.id)}
                    >
                      <span className="aze-memory-row-head">
                        <span className="aze-memory-chip">{memory.type}</span>
                        {memory.project_id ? (
                          <span className="aze-memory-project">{memory.project_id}</span>
                        ) : null}
                        <span className="aze-memory-row-time">
                          {formatDate(memory.updated_at)}
                        </span>
                      </span>
                      <span className="aze-memory-row-title">
                        {memory.title || memory.content}
                      </span>
                      {memory.title ? (
                        <span className="aze-memory-row-content">{memory.content}</span>
                      ) : null}
                      <span className="aze-memory-row-meta">
                        importance {Math.round(memory.importance * 100)}% ·
                        confidence {Math.round(memory.confidence * 100)}%
                      </span>
                    </button>
                  ))}
                  {items.length < total ? (
                    <button
                      type="button"
                      className="aze-memory-more"
                      onClick={() => void loadMore()}
                      disabled={busy}
                    >
                      {busy ? "LOADING…" : `LOAD MORE (${items.length}/${total})`}
                    </button>
                  ) : null}
                </>
              )}
            </div>

            <aside className="aze-memory-detail" aria-live="polite">
              {!selectedId || (!detail && !detailLoading) ? (
                <p className="aze-memory-empty">Select a memory to inspect it.</p>
              ) : detailLoading && !detail ? (
                <p className="aze-memory-empty">Loading…</p>
              ) : detail ? (
                <>
                  <div className="aze-memory-detail-head">
                    <div>
                      <span className="aze-memory-chip">{detail.type}</span>
                      <p className="aze-memory-detail-date">
                        Created {formatDate(detail.created_at)} · Updated{" "}
                        {formatDate(detail.updated_at)}
                      </p>
                    </div>
                    <div className="flex gap-2">
                      {!editing ? (
                        <button
                          type="button"
                          className="aze-memory-small"
                          onClick={() => setEditing(true)}
                        >
                          EDIT
                        </button>
                      ) : (
                        <>
                          <button
                            type="button"
                            className="aze-memory-small"
                            onClick={() => {
                              setEditing(false);
                              if (detail) {
                                setDraft({
                                  content: detail.content,
                                  title: detail.title ?? "",
                                  type: detail.type,
                                  tags: (detail.tags ?? []).join(", "),
                                  project_id: detail.project_id ?? "",
                                  importance: detail.importance,
                                  confidence: detail.confidence,
                                });
                              }
                            }}
                            disabled={busy}
                          >
                            CANCEL
                          </button>
                          <button
                            type="button"
                            className="aze-memory-small aze-memory-small--primary"
                            onClick={() => void save()}
                            disabled={busy}
                          >
                            {busy ? "SAVING…" : "SAVE"}
                          </button>
                        </>
                      )}
                    </div>
                  </div>

                  {editing ? (
                    <div className="aze-memory-form">
                      <label>
                        CONTENT
                        <textarea
                          value={draft.content}
                          maxLength={8000}
                          rows={6}
                          onChange={(event) => setDraft({ ...draft, content: event.target.value })}
                        />
                      </label>
                      <label>
                        TITLE
                        <input
                          value={draft.title}
                          maxLength={200}
                          onChange={(event) => setDraft({ ...draft, title: event.target.value })}
                        />
                      </label>
                      <div className="aze-memory-form-grid">
                        <label>
                          CATEGORY
                          <select
                            value={draft.type}
                            onChange={(event) =>
                              setDraft({ ...draft, type: event.target.value as MemoryType })}
                          >
                            <option value="user">user</option>
                            <option value="episodic">episodic</option>
                            <option value="knowledge">knowledge</option>
                            <option value="project">project</option>
                          </select>
                        </label>
                        <label>
                          PROJECT
                          <input
                            value={draft.project_id}
                            maxLength={120}
                            placeholder="optional"
                            onChange={(event) =>
                              setDraft({ ...draft, project_id: event.target.value })}
                          />
                        </label>
                        <label>
                          IMPORTANCE {Math.round(draft.importance * 100)}%
                          <input
                            type="range"
                            min={0}
                            max={1}
                            step={0.05}
                            value={draft.importance}
                            onChange={(event) =>
                              setDraft({ ...draft, importance: Number(event.target.value) })}
                          />
                        </label>
                        <label>
                          CONFIDENCE {Math.round(draft.confidence * 100)}%
                          <input
                            type="range"
                            min={0}
                            max={1}
                            step={0.05}
                            value={draft.confidence}
                            onChange={(event) =>
                              setDraft({ ...draft, confidence: Number(event.target.value) })}
                          />
                        </label>
                      </div>
                      <label>
                        TAGS (comma separated)
                        <input
                          value={draft.tags}
                          onChange={(event) => setDraft({ ...draft, tags: event.target.value })}
                        />
                      </label>
                    </div>
                  ) : (
                    <>
                      {detail.title ? (
                        <h3 className="aze-memory-detail-title">{detail.title}</h3>
                      ) : null}
                      <p className="aze-memory-content">{detail.content}</p>
                      {detail.tags.length ? (
                        <div className="aze-memory-tags">
                          {detail.tags.map((tag) => (
                            <span key={tag} className="aze-memory-tag">{tag}</span>
                          ))}
                        </div>
                      ) : null}
                      <dl className="aze-memory-facts">
                        <div><dt>ID</dt><dd className="font-mono">{detail.id}</dd></div>
                        <div><dt>Status</dt><dd>{detail.status}</dd></div>
                        <div><dt>Source</dt><dd>{detail.source_type}</dd></div>
                        <div><dt>Project</dt><dd>{detail.project_id || "—"}</dd></div>
                        <div><dt>Importance</dt><dd>{Math.round(detail.importance * 100)}%</dd></div>
                        <div><dt>Confidence</dt><dd>{Math.round(detail.confidence * 100)}%</dd></div>
                        <div><dt>Last accessed</dt><dd>{formatDate(detail.last_accessed_at)}</dd></div>
                        <div><dt>Version</dt><dd>{detail.version}</dd></div>
                        {detail.superseded_by ? (
                          <div><dt>Superseded by</dt><dd className="font-mono">{detail.superseded_by}</dd></div>
                        ) : null}
                        {detail.expires_at ? (
                          <div><dt>Expires</dt><dd>{formatDate(detail.expires_at)}</dd></div>
                        ) : null}
                      </dl>
                      {detail.relations.length ? (
                        <div className="aze-memory-relations">
                          <p className="aze-eyebrow">RELATIONSHIPS</p>
                          {detail.relations.map((relation) => (
                            <button
                              key={`${relation.memory_id}-${relation.relation}-${relation.direction}`}
                              type="button"
                              className="aze-memory-relation"
                              onClick={() => setSelectedId(relation.memory_id)}
                            >
                              {relation.direction === "outgoing" ? "→" : "←"}{" "}
                              {relation.relation} · {relation.memory_id.slice(0, 8)}
                            </button>
                          ))}
                        </div>
                      ) : null}
                      <button
                        type="button"
                        className="aze-memory-delete"
                        onClick={() => void remove()}
                        disabled={busy}
                      >
                        DELETE MEMORY
                      </button>
                    </>
                  )}
                </>
              ) : (
                <p className="aze-memory-empty">Memory not found.</p>
              )}
            </aside>
          </div>

          <footer className="aze-memory-footer">
            <div className="aze-memory-settings">
              <label>
                <input
                  type="checkbox"
                  checked={settings?.enabled ?? true}
                  onChange={(event) => void toggleSetting("enabled", event.target.checked)}
                />
                MEMORY ENABLED
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={settings?.auto_extract ?? true}
                  onChange={(event) => void toggleSetting("auto_extract", event.target.checked)}
                />
                AUTO-EXTRACT
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={settings?.retrieval_enabled ?? true}
                  onChange={(event) => void toggleSetting("retrieval_enabled", event.target.checked)}
                />
                RETRIEVAL
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={settings?.voice_injection ?? true}
                  onChange={(event) => void toggleSetting("voice_injection", event.target.checked)}
                />
                VOICE CONTEXT
              </label>
            </div>
            <div className="aze-memory-actions">
              <button type="button" onClick={() => void maintenance()} disabled={busy}>
                REBUILD / CHECK
              </button>
              <button type="button" onClick={() => void backup()} disabled={busy}>
                BACKUP
              </button>
              <button type="button" onClick={() => void exportAll()} disabled={busy}>
                EXPORT
              </button>
              <label className="aze-memory-import">
                IMPORT
                <input
                  type="file"
                  accept="application/json,.json"
                  onChange={(event) => {
                    void importFile(event.target.files?.[0]);
                    event.target.value = "";
                  }}
                />
              </label>
              <button
                type="button"
                className="aze-memory-danger"
                onClick={() => void forgetAll()}
                disabled={busy}
              >
                FORGET ALL
              </button>
            </div>
            <p className="aze-memory-path" title={settings?.db_path}>
              {settings?.db_path ? `DB · ${settings.db_path}` : "DB · resolving…"}
              {stats ? ` · ${stats.tombstones} forgotten` : ""}
              {stats && stats.extraction?.failed
                ? ` · ${stats.extraction.failed} extraction error(s)`
                : ""}
              {settings?.error ? ` · ${settings.error}` : ""}
            </p>
          </footer>
        </div>
      </section>
  );

  return (
    <div
      className="aze-bay-overlay aze-memory-overlay"
      role="dialog"
      aria-modal="false"
      aria-label="Memory control"
    >
      {sheet}
    </div>
  );
}
