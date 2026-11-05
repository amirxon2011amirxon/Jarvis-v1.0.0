import type {
  AudioDevicesResponse,
  AudioTestResponse,
  ApprovalRequest,
  Bootstrap,
  Diagnostics,
  FullSettings,
  Health,
  MemoryAction,
  MemoryDetail,
  MemoryList,
  MemorySettings,
  MemorySettingsPatch,
  MemoryStatsResponse,
  MemoryUpdatePatch,
  MessageResponse,
  Metrics,
  MuteResponse,
  NotificationItem,
  ScanResponse,
  SettingsPatch,
  SettingsResponse,
  SetupKeyResponse,
  SkillAssetResponse,
  SkillDetail,
  SkillsSnapshot,
  SkillsSnapshotResponse,
  SkillUninstallResponse,
  ToolsResponse,
  VolumeResponse,
  WeatherData,
} from "../types";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function detailMessage(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === "object" && "msg" in item) {
          return String((item as { msg: unknown }).msg);
        }
        return String(item);
      })
      .join("; ");
  }
  return "";
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (method !== "GET" && method !== "HEAD") {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, {
    ...init,
    method,
    headers,
    credentials: "same-origin",
    cache: "no-store",
  });
  const raw = await response.text();
  let payload: unknown = null;
  if (raw) {
    try {
      payload = JSON.parse(raw) as unknown;
    } catch {
      payload = raw;
    }
  }
  if (!response.ok) {
    const detail =
      payload && typeof payload === "object" && "detail" in payload
        ? detailMessage((payload as { detail: unknown }).detail)
        : "";
    throw new ApiError(
      response.status,
      detail || `Request failed (${response.status}).`,
    );
  }
  return payload as T;
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export const api = {
  bootstrap: () => request<Bootstrap>("/api/bootstrap"),

  health: () => request<Health>("/api/health"),

  settings: (refreshAudio = false) =>
    request<FullSettings>(
      `/api/settings?refresh_audio=${refreshAudio ? "1" : "0"}`,
    ),

  updateSettings: (patch: SettingsPatch) =>
    request<SettingsResponse>("/api/settings", {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  audioDevices: (refresh = false) =>
    request<AudioDevicesResponse>(
      `/api/audio/devices?refresh=${refresh ? "1" : "0"}`,
    ),

  weather: (force = false, signal?: AbortSignal) =>
    request<WeatherData>(`/api/weather?force=${force ? "1" : "0"}`, { signal }),

  tools: () => request<ToolsResponse>("/api/tools"),

  selectTools: (enabled: string[]) =>
    post<ToolsResponse>("/api/tools/selection", { enabled }),

  clearTools: () => post<{ ok: boolean }>("/api/tools/clear"),

  diagnostics: () => request<Diagnostics>("/api/diagnostics"),

  initialize: () => post<Diagnostics>("/api/initialize"),

  metrics: () => request<Metrics>("/api/metrics"),

  sendMessage: (text: string, clientMessageId: string) =>
    post<MessageResponse>("/api/message", {
      text,
      client_message_id: clientMessageId,
    }),

  interrupt: () => post<{ ok: boolean }>("/api/voice/interrupt"),

  approvals: () => request<{ approvals: ApprovalRequest[] }>("/api/approvals"),

  approveAction: (approvalId: string) =>
    post<{ ok: boolean; approval: ApprovalRequest }>(
      `/api/approvals/${encodeURIComponent(approvalId)}/approve`,
      {},
    ),

  rejectAction: (approvalId: string) =>
    post<{ ok: boolean; approval: ApprovalRequest }>(
      `/api/approvals/${encodeURIComponent(approvalId)}/reject`,
      {},
    ),

  notifications: (status = "", limit = 50) =>
    request<{ notifications: NotificationItem[]; count: number }>(
      `/api/notifications?status=${encodeURIComponent(status)}&limit=${limit}`,
    ),

  dismissNotification: (notificationId: string) =>
    post<{ ok: boolean; notification: NotificationItem }>(
      `/api/notifications/${encodeURIComponent(notificationId)}/dismiss`,
      {},
    ),

  snoozeNotification: (notificationId: string, minutes = 10) =>
    post<{ ok: boolean; notification: NotificationItem }>(
      `/api/notifications/${encodeURIComponent(notificationId)}/snooze`,
      { minutes },
    ),

  markNotificationRead: (notificationId: string) =>
    post<{ ok: boolean; notification: NotificationItem }>(
      `/api/notifications/${encodeURIComponent(notificationId)}/read`,
      {},
    ),

  setMuted: (muted: boolean) =>
    post<MuteResponse>("/api/voice/mute", { muted }),

  pttHold: (held: boolean) =>
    post<{ ok: boolean; held: boolean }>("/api/voice/ptt-hold", { held }),

  reconnect: () => post<{ requested: boolean }>("/api/session/reconnect"),

  saveApiKey: (apiKey: string, osSystem?: string) =>
    post<SetupKeyResponse>("/api/setup/key", {
      api_key: apiKey,
      os_system: osSystem,
    }),

  testAudio: (kind: "input" | "output", device: string) =>
    post<AudioTestResponse>("/api/audio/test", { kind, device }),

  setVolume: (volume: number, device: string) =>
    post<VolumeResponse>("/api/audio/volume", { volume, device }),

  getVolume: (device: string) =>
    request<{ volume: number | null }>(
      `/api/audio/volume?device=${encodeURIComponent(device)}`,
    ),

  scanModule: (moduleId: string) =>
    post<ScanResponse>(`/api/modules/${encodeURIComponent(moduleId)}/scan`),

  /* ── skills ───────────────────────────────────────────────────────────── */

  skills: () => request<SkillsSnapshot>("/api/skills"),

  refreshSkills: () =>
    post<SkillsSnapshotResponse>("/api/skills/refresh"),

  skillDetail: (skillId: string, signal?: AbortSignal) =>
    request<SkillDetail>(
      `/api/skills/${encodeURIComponent(skillId)}`,
      { signal },
    ),

  skillAsset: (skillId: string, path: string, signal?: AbortSignal) =>
    request<SkillAssetResponse>(
      `/api/skills/${encodeURIComponent(skillId)}/asset?path=${encodeURIComponent(path)}`,
      { signal },
    ),

  setSkillEnabled: (skillId: string, enabled: boolean) =>
    request<SkillsSnapshotResponse>(
      `/api/skills/${encodeURIComponent(skillId)}`,
      { method: "PATCH", body: JSON.stringify({ enabled }) },
    ),

  uninstallSkill: (skillId: string, confirm = true) =>
    post<SkillUninstallResponse>(
      `/api/skills/${encodeURIComponent(skillId)}/uninstall`,
      { confirm },
    ),

  /* ── memory ───────────────────────────────────────────────────────────── */
  memoryStats: () => request<MemoryStatsResponse>("/api/memory/stats"),

  memoryList: (
    params: {
      type?: string;
      project_id?: string;
      query?: string;
      status?: string;
      limit?: number;
      offset?: number;
    },
    signal?: AbortSignal,
  ) => {
    const search = new URLSearchParams();
    if (params.type) search.set("type", params.type);
    if (params.project_id) search.set("project_id", params.project_id);
    if (params.query) search.set("query", params.query);
    if (params.status) search.set("status", params.status);
    if (params.limit) search.set("limit", String(params.limit));
    if (params.offset) search.set("offset", String(params.offset));
    return request<MemoryList>(`/api/memory?${search.toString()}`, { signal });
  },

  memoryGet: (memoryId: string, signal?: AbortSignal) =>
    request<MemoryDetail>(
      `/api/memory/${encodeURIComponent(memoryId)}`,
      { signal },
    ),

  memoryUpdate: (memoryId: string, patch: MemoryUpdatePatch) =>
    request<MemoryDetail>(
      `/api/memory/${encodeURIComponent(memoryId)}`,
      { method: "PATCH", body: JSON.stringify(patch) },
    ),

  memoryDelete: (memoryId: string) =>
    request<MemoryAction>(
      `/api/memory/${encodeURIComponent(memoryId)}`,
      { method: "DELETE", body: "{}" },
    ),

  memoryForget: (body: {
    memory_id?: string;
    query?: string;
    project_id?: string;
    type?: string;
    confirm?: boolean;
  }) => post<MemoryAction>("/api/memory/forget", body),

  memoryMaintenance: (integrity = false) =>
    post<MemoryAction>(`/api/memory/maintenance?integrity=${integrity ? "1" : "0"}`),

  memoryBackup: () => post<MemoryAction>("/api/memory/backup"),

  memoryExport: () => request<unknown>("/api/memory/export"),

  memoryImport: (data: unknown, dryRun = false) =>
    post<MemoryAction>("/api/memory/import", { data, dry_run: dryRun }),

  memorySettings: () => request<MemorySettings>("/api/memory/settings"),

  updateMemorySettings: (patch: MemorySettingsPatch) =>
    request<{ ok: boolean; settings: MemorySettings }>("/api/memory/settings", {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
};
