import { useCallback, useEffect, useReducer, useRef } from "react";

import { ApiError, api } from "../lib/api";
import { AzeSocket, type SocketStatus } from "../lib/ws";
import { newId } from "../lib/utils";
import type {
  AudioLevelListener,
  AudioTestResult,
  AgentRunSnapshot,
  ApprovalRequest,
  BasicSettings,
  Bootstrap,
  ChatMessage,
  Diagnostics,
  FullSettings,
  LogEntry,
  Metrics,
  ModuleInfo,
  NotificationItem,
  ObservationSnapshot,
  ScanResponse,
  ServerEvent,
  SettingsPatch,
  SetupStatus,
  SkillAssetResponse,
  SkillDetail,
  SkillsSnapshot,
  ResearchState,
  SubscribeAudioLevel,
  Toast,
  ToolCategory,
  ToolEvent,
  ToolInfo,
  WeatherData,
  AzeState,
} from "../types";
import type {
  YouTubeCommand,
  YouTubePanelModel,
  YouTubePlayerError,
  YouTubePlayerState,
} from "../components/youtube/youtubeTypes";
import {
  EMPTY_YOUTUBE_MODEL,
  closedPlayerState,
  youtubeCommandCompleted,
  youtubeLoadCompleted,
  youtubeModelClosed,
  youtubeModelClosing,
  youtubeModelFromBootstrap,
  youtubeModelFromCommand,
  youtubeModelFromLoad,
  youtubeModelFromOpen,
} from "../components/youtube/youtubeState";

interface State {
  boot: Bootstrap | null;
  loading: boolean;
  fatal: string | null;
  socket: SocketStatus;
  state: AzeState;
  muted: boolean;
  assistantName: string;
  messages: ChatMessage[];
  logs: LogEntry[];
  toolEvents: ToolEvent[];
  agentRun: AgentRunSnapshot | null;
  observation: ObservationSnapshot | null;
  notifications: NotificationItem[];
  approvals: ApprovalRequest[];
  tools: ToolInfo[];
  toolCategories: ToolCategory[];
  modules: ModuleInfo[];
  skills: SkillsSnapshot;
  research: ResearchState;
  setup: SetupStatus;
  setupRequired: boolean;
  settings: BasicSettings | null;
  fullSettings: FullSettings | null;
  settingsLoading: boolean;
  conversationId: string;
  metrics: Metrics | null;
  diagnostics: Diagnostics | null;
  weather: WeatherData | null;
  weatherLoading: boolean;
  toasts: Toast[];
  audioTestLevel: number;
  audioTestKind: "input" | "output" | null;
  youtube: YouTubePanelModel;
  shuttingDown: boolean;
  shutdownReason: string;
}

const EMPTY_SETUP: SetupStatus = {
  configured: false,
  setup_done: false,
  key_masked: "",
};

export const EMPTY_SKILLS: SkillsSnapshot = {
  engine_version: "1.0.0",
  initialized: false,
  initialized_at: "",
  active_scope: "",
  skills: [],
  active: [],
  counts: {
    total: 0,
    enabled: 0,
    active: 0,
    builtin: 0,
    installed: 0,
    disabled: 0,
    issues: 0,
  },
  errors: [],
  state_file: "",
};

export const EMPTY_RESEARCH: ResearchState = {
  open: false,
  job_id: "",
  status: "idle",
  topic: "",
  depth: "deep",
  phase: "",
  message: "",
  progress: 0,
  sources_found: 0,
  sources_read: 0,
  started_at: "",
  updated_at: "",
  report: null,
  events: [],
  error: "",
  warnings: [],
  cancelled: false,
};

export const EMPTY_OBSERVATION: ObservationSnapshot = {
  state: "INACTIVE",
  observation_active: false,
  started_at: null,
  frames_streamed: 0,
  frames_dropped: 0,
  frame_interval_seconds: 0,
  last_error: "",
  voice_session_connected: false,
};

const initialState: State = {
  boot: null,
  loading: true,
  fatal: null,
  socket: "connecting",
  state: "INITIALISING",
  muted: false,
  assistantName: "Jarvis",
  messages: [],
  logs: [],
  toolEvents: [],
  agentRun: null,
  observation: null,
  notifications: [],
  approvals: [],
  tools: [],
  toolCategories: [],
  modules: [],
  skills: EMPTY_SKILLS,
  research: EMPTY_RESEARCH,
  setup: EMPTY_SETUP,
  setupRequired: false,
  settings: null,
  fullSettings: null,
  settingsLoading: false,
  conversationId: "",
  metrics: null,
  diagnostics: null,
  weather: null,
  weatherLoading: true,
  toasts: [],
  audioTestLevel: 0,
  audioTestKind: null,
  youtube: { ...EMPTY_YOUTUBE_MODEL },
  shuttingDown: false,
  shutdownReason: "",
};

type Action =
  | { type: "bootstrap.start" }
  | { type: "bootstrap.success"; data: Bootstrap }
  | { type: "bootstrap.superseded" }
  | { type: "bootstrap.error"; message: string }
  | { type: "snapshot"; data: Bootstrap }
  | { type: "server.event"; event: ServerEvent }
  | { type: "socket"; status: SocketStatus }
  | { type: "voice.state"; state: AzeState; muted: boolean }
  | { type: "optimistic-user"; message: ChatMessage }
  | { type: "message-delivered"; id: string }
  | { type: "message-failed"; id: string }
  | { type: "chat.message"; message: ChatMessage }
  | { type: "chat.delta"; id: string; text: string; ts: string }
  | { type: "chat.cancelled"; id: string }
  | { type: "chat.discarded"; id: string }
  | { type: "cancel-streaming" }
  | { type: "tool.updated"; event: ToolEvent }
  | { type: "tools.clear" }
  | { type: "tools.loaded"; tools: ToolInfo[]; categories?: ToolCategory[] }
  | { type: "settings.loading" }
  | { type: "settings.loaded"; settings: FullSettings }
  | { type: "settings.failed" }
  | { type: "settings.updated"; settings: BasicSettings; setup?: SetupStatus }
  | { type: "setup.required" }
  | { type: "setup.updated"; setup: SetupStatus }
  | { type: "weather.loading" }
  | { type: "weather.loaded"; weather: WeatherData }
  | { type: "metrics.loaded"; metrics: Metrics }
  | { type: "diagnostics.loaded"; diagnostics: Diagnostics }
  | { type: "module.scanned"; moduleId: string; result: ScanResponse }
  | { type: "skills.updated"; data: SkillsSnapshot }
  | { type: "toast.add"; toast: Toast }
  | { type: "toast.dismiss"; id: string }
  | { type: "audio.test.level"; kind: "input" | "output"; level: number }
  | { type: "audio.test.clear" }
  | { type: "youtube.close.request" }
  | { type: "youtube.close.settled" }
  | { type: "youtube.command.completed"; requestId: string }
  | { type: "youtube.load.completed"; requestId: string };

function upsertMessage(
  messages: ChatMessage[],
  incoming: ChatMessage,
): ChatMessage[] {
  const index = messages.findIndex((message) => message.id === incoming.id);
  let next: ChatMessage[];
  if (index === -1) {
    next = [...messages, incoming].slice(-300);
  } else {
    next = messages.slice();
    next[index] = {
      ...next[index],
      ...incoming,
      streaming: false,
      cancelled: false,
    };
  }
  if (incoming.seq === undefined) return next;
  // A finalized assistant message can arrive after a user message typed
  // while it was streaming. The backend sequence places it before that turn.
  return next
    .map((message, position) => ({ message, position }))
    .sort((a, b) => {
      const left = a.message.seq ?? Number.POSITIVE_INFINITY;
      const right = b.message.seq ?? Number.POSITIVE_INFINITY;
      return left === right ? a.position - b.position : left - right;
    })
    .map(({ message }) => message);
}

function updateToolEvent(
  events: ToolEvent[],
  incoming: ToolEvent,
): ToolEvent[] {
  const index = events.findIndex((event) => event.id === incoming.id);
  if (index === -1) return [...events, incoming].slice(-80);
  const next = events.slice();
  next[index] = { ...next[index], ...incoming };
  return next;
}

function updateApproval(
  approvals: ApprovalRequest[],
  incoming: ApprovalRequest,
): ApprovalRequest[] {
  if (incoming.status !== "pending" && incoming.status !== "approved") {
    return approvals.filter((item) => item.approval_id !== incoming.approval_id);
  }
  const index = approvals.findIndex(
    (item) => item.approval_id === incoming.approval_id,
  );
  if (index === -1) return [...approvals, incoming];
  const next = approvals.slice();
  next[index] = incoming;
  return next;
}

/** Rebuild folder groups when an older backend snapshot has no categories. */
function groupToolCategories(tools: ToolInfo[]): ToolCategory[] {
  const groups = new Map<string, ToolCategory>();
  for (const tool of tools) {
    const id = tool.category ?? "";
    let group = groups.get(id);
    if (!group) {
      const label =
        tool.category_label ??
        (id ? id.replace(/_/g, " ").toUpperCase() : "UNCATEGORISED");
      group = {
        id,
        label,
        description: "",
        tools: [],
        count: 0,
        enabled_count: 0,
        enabled: false,
      };
      groups.set(id, group);
    }
    group.tools.push(tool);
  }
  return [...groups.values()].map((group) => {
    const enabledCount = group.tools.filter(
      (tool) => tool.enabled !== false,
    ).length;
    return {
      ...group,
      count: group.tools.length,
      enabled_count: enabledCount,
      enabled: enabledCount === group.tools.length && group.tools.length > 0,
    };
  });
}

function mergeBasic(
  current: FullSettings | null,
  settings: BasicSettings,
  setup?: SetupStatus,
): FullSettings | null {
  if (!current) return current;
  return {
    ...current,
    ...settings,
    ...(setup ? { gemini: setup } : {}),
  };
}

function addToast(state: State, kind: Toast["kind"], message: string): State {
  const toast: Toast = { id: newId(), kind, message };
  return { ...state, toasts: [...state.toasts.slice(-3), toast] };
}

function notificationToast(item: NotificationItem): Toast {
  const status = String(item.metadata?.status || "").toLowerCase();
  const kind: Toast["kind"] =
    item.kind === "timer"
      ? "success"
      : item.kind === "agent" && (status === "failed" || status === "blocked")
        ? "error"
        : "info";
  const snooze = item.actions.find((action) => action.action === "snooze");
  return {
    id: `notification-${item.id}`,
    kind,
    title: item.title,
    message: item.body || item.title,
    notificationId: item.id,
    autoDismiss: false,
    snoozeMinutes: snooze?.minutes ?? 10,
  };
}

function mergeNotification(
  notifications: NotificationItem[],
  incoming: NotificationItem,
): NotificationItem[] {
  const index = notifications.findIndex((item) => item.id === incoming.id);
  if (index === -1) return [incoming, ...notifications].slice(0, 50);
  const next = notifications.slice();
  next[index] = incoming;
  return next;
}

function addUnreadNotificationToasts(
  toasts: Toast[],
  notifications: NotificationItem[],
): Toast[] {
  const known = new Set(
    toasts.map((toast) => toast.notificationId).filter(Boolean),
  );
  const additions = notifications
    .filter((item) => item.status === "unread" && !known.has(item.id))
    .slice(0, 4)
    .map(notificationToast);
  if (additions.length === 0) return toasts;
  return [...toasts, ...additions].slice(-8);
}

function handleServerEvent(state: State, event: ServerEvent): State {
  switch (event.type) {
    case "snapshot":
      return applyBootstrap(state, event.data);
    case "state":
      return { ...state, state: event.state, muted: event.muted };
    case "log": {
      if (state.logs.some((entry) => entry.id === event.entry.id)) return state;
      const logs = [...state.logs, event.entry].slice(-600);
      return { ...state, logs };
    }
    case "chat.message": {
      const messages = upsertMessage(state.messages, {
        ...event.message,
        pending: false,
        failed: false,
      });
      return { ...state, messages };
    }
    case "chat.delta": {
      const index = state.messages.findIndex((m) => m.id === event.id);
      if (index === -1) {
        const message: ChatMessage = {
          id: event.id,
          role: "assistant",
          text: event.text,
          ts: event.ts,
          streaming: true,
        };
        return {
          ...state,
          messages: [...state.messages, message].slice(-300),
        };
      }
      const existing = state.messages[index];
      if (!existing.streaming && !existing.cancelled) return state;
      const next = state.messages.slice();
      next[index] = {
        ...existing,
        text: existing.text + event.text,
        streaming: true,
        cancelled: false,
      };
      return { ...state, messages: next };
    }
    case "chat.cancelled": {
      const messages = state.messages.map((message) =>
        message.id === event.id
          ? { ...message, streaming: false, cancelled: true }
          : message,
      );
      return { ...state, messages };
    }
    case "chat.discarded":
      return {
        ...state,
        messages: state.messages.filter((message) => message.id !== event.id),
      };
    case "chat.cleared":
      // The backend started a new conversation (for example a voice change
      // that cannot resume). Keeping old rows would claim context Jarvis lost.
      return {
        ...state,
        messages: [],
        agentRun: null,
        observation: null,
        approvals: [],
        conversationId: event.conversation_id ?? state.conversationId,
      };
    case "tool.updated":
      return {
        ...state,
        toolEvents: updateToolEvent(state.toolEvents, event.event),
      };
    case "approval.updated": {
      const approval = event.approval;
      if (!approval) return state;
      const next = updateApproval(state.approvals, approval);
      const status = event.event || approval.status;
      if (status === "expired") {
        return {
          ...addToast(state, "info", "An action approval expired."),
          approvals: next,
        };
      }
      return { ...state, approvals: next };
    }
    case "agent.updated":
      return {
        ...state,
        agentRun: event.run,
      };
    case "observation.updated":
      return {
        ...state,
        observation: event.observation,
      };
    case "notification.created":
    case "notification.updated": {
      const item = event.notification;
      if (!item) return state;
      const notifications = mergeNotification(state.notifications, item);
      if (item.status === "dismissed") {
        return {
          ...state,
          notifications,
          toasts: state.toasts.filter(
            (toast) => toast.notificationId !== item.id,
          ),
        };
      }
      if (event.type === "notification.updated") {
        return { ...state, notifications };
      }
      return {
        ...state,
        notifications,
        toasts: addUnreadNotificationToasts(state.toasts, [item]),
      };
    }
    case "tool.cleared":
      return { ...state, toolEvents: [] };
    case "youtube_open_panel":
      return {
        ...state,
        youtube: youtubeModelFromOpen(
          state.youtube,
          event.video,
          event.autoplay,
        ),
      };
    case "youtube_load_video":
      return {
        ...state,
        youtube: youtubeModelFromLoad(
          state.youtube,
          event.request_id,
          event.video,
          event.autoplay,
          event.start_seconds,
        ),
      };
    case "youtube_player_command": {
      const command: YouTubeCommand = {
        requestId: event.request_id,
        command: event.command,
        args: event.args ?? {},
      };
      return {
        ...state,
        youtube: youtubeModelFromCommand(state.youtube, command),
      };
    }
    case "youtube_close_panel":
      return {
        ...state,
        youtube: youtubeModelClosing(state.youtube, event.request_id),
      };
    case "youtube_state":
      return state;
    case "settings.updated": {
      const settings = event.settings;
      return {
        ...state,
        settings,
        assistantName: settings.assistant_name,
        fullSettings: mergeBasic(state.fullSettings, settings, event.setup),
        setup: event.setup ?? state.setup,
        setupRequired: event.setup
          ? !event.setup.configured
          : state.setupRequired,
      };
    }
    case "skills.updated":
      return { ...state, skills: event.data };
    case "research_state":
      return { ...state, research: event.state };
    case "setup.required":
      return {
        ...state,
        setupRequired: true,
        setup: { ...state.setup, configured: false },
      };
    case "setup.updated":
      return {
        ...state,
        setup: event.setup,
        setupRequired: !event.setup.configured,
      };
    case "shutdown":
      return {
        ...state,
        shuttingDown: true,
        shutdownReason: event.reason ?? "",
        observation: null,
      };
    case "audio.test":
      return {
        ...state,
        audioTestKind: event.kind,
        audioTestLevel: event.level,
      };
    case "audio":
      // High-rate level frames are delivered through subscribeAudio so the
      // whole application tree does not re-render 20 times a second.
      return state;
    case "error":
      return addToast(state, "error", event.message);
    case "ping":
    case "pong":
      return state;
    default:
      return state;
  }
}

function applyBootstrap(state: State, data: Bootstrap): State {
  const known = new Set(data.messages.map((message) => message.id));
  const pending = state.messages.filter(
    (message) => message.pending && !message.failed && !known.has(message.id),
  );
  return {
    ...state,
    boot: data,
    loading: false,
    fatal: null,
    state: data.state,
    muted: data.muted,
    assistantName: data.assistant_name,
    messages: [...data.messages, ...pending],
    logs: data.logs,
    toolEvents: data.tool_events,
    agentRun:
      data.agent_run === undefined ? state.agentRun : data.agent_run,
    observation:
      data.observation === undefined ? state.observation : data.observation,
    notifications:
      data.notifications === undefined ? state.notifications : data.notifications,
    toasts: addUnreadNotificationToasts(
      state.toasts,
      data.notifications === undefined ? state.notifications : data.notifications,
    ),
    approvals: data.approvals ?? [],
    tools: data.tools,
    toolCategories:
      data.tool_categories?.length
        ? data.tool_categories
        : groupToolCategories(data.tools),
    modules: data.modules,
    skills: data.skills ?? state.skills,
    research: data.research ?? state.research,
    setup: data.setup,
    setupRequired: !data.setup.configured,
    settings: data.settings,
    fullSettings: mergeBasic(state.fullSettings, data.settings, data.setup),
    weatherLoading: state.weatherLoading,
    conversationId: data.conversation_id ?? state.conversationId,
    youtube: youtubeModelFromBootstrap(state.youtube, data.youtube),
  };
}

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "bootstrap.start":
      return { ...state, loading: true, fatal: null };
    case "bootstrap.success":
      return applyBootstrap(state, action.data);
    case "bootstrap.superseded":
      // A WebSocket snapshot arrived while the HTTP request was in flight.
      // That snapshot is authoritative; applying the older HTTP body would
      // rewind messages, logs and tool rows.
      return { ...state, loading: false, fatal: null };
    case "bootstrap.error":
      return {
        ...state,
        loading: false,
        // If the socket already supplied a snapshot, a failed HTTP duplicate
        // is not a fatal startup failure.
        fatal: state.boot ? state.fatal : action.message,
      };
    case "snapshot":
      return applyBootstrap(state, action.data);
    case "server.event":
      return handleServerEvent(state, action.event);
    case "socket":
      // A dropped local event link means the last observation state is no
      // longer authoritative. Hide it until the next snapshot arrives.
      return {
        ...state,
        socket: action.status,
        observation:
          action.status === "online" ? state.observation : null,
      };
    case "voice.state":
      return { ...state, state: action.state, muted: action.muted };
    case "optimistic-user": {
      if (state.messages.some((message) => message.id === action.message.id)) {
        return state;
      }
      return {
        ...state,
        messages: [...state.messages, action.message].slice(-300),
      };
    }
    case "message-delivered":
      return {
        ...state,
        messages: state.messages.map((message) =>
          message.id === action.id
            ? { ...message, pending: false, failed: false }
            : message,
        ),
      };
    case "message-failed":
      return {
        ...state,
        messages: state.messages.map((message) =>
          message.id === action.id
            ? { ...message, pending: false, failed: true }
            : message,
        ),
      };
    case "chat.message":
      return {
        ...state,
        messages: upsertMessage(state.messages, {
          ...action.message,
          pending: false,
          failed: false,
        }),
      };
    case "chat.delta": {
      const index = state.messages.findIndex((m) => m.id === action.id);
      if (index === -1) {
        const message: ChatMessage = {
          id: action.id,
          role: "assistant",
          text: action.text,
          ts: action.ts,
          streaming: true,
        };
        return {
          ...state,
          messages: [...state.messages, message].slice(-300),
        };
      }
      const existing = state.messages[index];
      if (!existing.streaming && !existing.cancelled) return state;
      const next = state.messages.slice();
      next[index] = {
        ...existing,
        text: existing.text + action.text,
        streaming: true,
        cancelled: false,
      };
      return { ...state, messages: next };
    }
    case "chat.cancelled":
      return {
        ...state,
        messages: state.messages.map((message) =>
          message.id === action.id
            ? { ...message, streaming: false, cancelled: true }
            : message,
        ),
      };
    case "chat.discarded":
      return {
        ...state,
        messages: state.messages.filter((message) => message.id !== action.id),
      };
    case "cancel-streaming":
      return {
        ...state,
        messages: state.messages.map((message) =>
          message.streaming
            ? { ...message, streaming: false, cancelled: true }
            : message,
        ),
      };
    case "tool.updated":
      return {
        ...state,
        toolEvents: updateToolEvent(state.toolEvents, action.event),
      };
    case "tools.clear":
      return { ...state, toolEvents: [] };
    case "tools.loaded": {
      const count = action.tools.length;
      const noun = count === 1 ? "TOOL" : "TOOLS";
      return {
        ...state,
        tools: action.tools,
        toolCategories:
          action.categories && action.categories.length
            ? action.categories
            : groupToolCategories(action.tools),
        modules: state.modules.map((module) =>
          module.id === "actions"
            ? {
                ...module,
                count,
                status: `${count} CORE ${noun} LINKED`,
              }
            : module,
        ),
      };
    }
    case "settings.loading":
      return { ...state, settingsLoading: true };
    case "settings.loaded":
      return {
        ...state,
        settingsLoading: false,
        fullSettings: action.settings,
        settings: action.settings,
        setup: action.settings.gemini,
        setupRequired: !action.settings.gemini.configured,
        assistantName: action.settings.assistant_name,
      };
    case "settings.failed":
      return { ...state, settingsLoading: false };
    case "settings.updated":
      return {
        ...state,
        settings: action.settings,
        assistantName: action.settings.assistant_name,
        fullSettings: mergeBasic(state.fullSettings, action.settings, action.setup),
        setup: action.setup ?? state.setup,
        setupRequired: action.setup
          ? !action.setup.configured
          : state.setupRequired,
      };
    case "setup.required":
      return {
        ...state,
        setupRequired: true,
        setup: { ...state.setup, configured: false },
      };
    case "setup.updated":
      return {
        ...state,
        setup: action.setup,
        setupRequired: !action.setup.configured,
      };
    case "weather.loading":
      return { ...state, weatherLoading: true };
    case "weather.loaded":
      return { ...state, weather: action.weather, weatherLoading: false };
    case "metrics.loaded":
      return { ...state, metrics: action.metrics };
    case "diagnostics.loaded":
      return { ...state, diagnostics: action.diagnostics };
    case "module.scanned":
      return {
        ...state,
        modules: state.modules.map((module) =>
          module.id === action.moduleId
            ? {
                ...module,
                status: action.result.status,
                count: action.result.count,
              }
            : module,
        ),
      };
    case "skills.updated":
      return { ...state, skills: action.data };
    case "toast.add":
      return {
        ...state,
        toasts: [...state.toasts.slice(-3), action.toast],
      };
    case "toast.dismiss":
      return {
        ...state,
        toasts: state.toasts.filter((toast) => toast.id !== action.id),
      };
    case "audio.test.level":
      return {
        ...state,
        audioTestKind: action.kind,
        audioTestLevel: action.level,
      };
    case "audio.test.clear":
      return { ...state, audioTestKind: null, audioTestLevel: 0 };
    case "youtube.close.request":
      return {
        ...state,
        youtube: youtubeModelClosing(state.youtube, ""),
      };
    case "youtube.close.settled":
      return {
        ...state,
        youtube: youtubeModelClosed(state.youtube),
      };
    case "youtube.command.completed":
      return {
        ...state,
        youtube: youtubeCommandCompleted(state.youtube, action.requestId),
      };
    case "youtube.load.completed":
      return {
        ...state,
        youtube: youtubeLoadCompleted(state.youtube, action.requestId),
      };
    default:
      return state;
  }
}

export interface AzeApi {
  state: State;
  sendText: (text: string) => Promise<boolean>;
  toggleMute: () => Promise<void>;
  interrupt: () => Promise<void>;
  approveAction: (approvalId: string) => Promise<ApprovalRequest>;
  rejectAction: (approvalId: string) => Promise<ApprovalRequest>;
  setPttHeld: (held: boolean) => Promise<void>;
  saveSettings: (patch: SettingsPatch) => Promise<FullSettings>;
  loadSettings: (refreshAudio?: boolean) => Promise<void>;
  refreshAudioDevices: () => Promise<void>;
  testAudio: (kind: "input" | "output", device: string) => Promise<AudioTestResult>;
  setVolume: (volume: number, device: string) => Promise<boolean>;
  getVolume: (device: string) => Promise<number | null>;
  saveApiKey: (apiKey: string, osSystem?: string) => Promise<void>;
  scanModule: (moduleId: string) => Promise<ScanResponse>;
  refreshSkills: () => Promise<SkillsSnapshot>;
  setSkillEnabled: (skillId: string, enabled: boolean) => Promise<SkillsSnapshot>;
  skillDetail: (skillId: string) => Promise<SkillDetail>;
  skillAsset: (skillId: string, path: string) => Promise<SkillAssetResponse>;
  uninstallSkill: (skillId: string) => Promise<SkillsSnapshot>;
  reconnect: () => Promise<void>;
  refreshWeather: (force?: boolean) => Promise<void>;
  clearToolEvents: () => Promise<void>;
  setToolSelection: (enabled: string[]) => Promise<void>;
  dismissToast: (id: string) => void;
  snoozeNotification: (notificationId: string, minutes?: number) => Promise<void>;
  markNotificationRead: (notificationId: string) => Promise<void>;
  retryBootstrap: () => Promise<void>;
  clearAudioTest: () => void;
  subscribeAudio: SubscribeAudioLevel;
  youtubeSendState: (state: YouTubePlayerState) => void;
  youtubeReady: (state: YouTubePlayerState) => void;
  youtubeError: (
    error: YouTubePlayerError,
    state: YouTubePlayerState,
    requestId?: string,
  ) => void;
  youtubeCommandResult: (
    requestId: string,
    ok: boolean,
    state: YouTubePlayerState,
    error?: YouTubePlayerError,
  ) => void;
  youtubeRequestClose: () => void;
  youtubeSettleClose: (requestId: string) => void;
  youtubeCommandSettled: (requestId: string) => void;
  youtubeLoadSettled: (requestId: string) => void;
}

function messageFromError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Unexpected error.";
}

export function useAze(): AzeApi {
  const [state, dispatch] = useReducer(reducer, initialState);
  const socketRef = useRef<AzeSocket | null>(null);
  const weatherAbort = useRef<AbortController | null>(null);
  const snapshotSeen = useRef(false);
  const audioListeners = useRef(new Set<AudioLevelListener>());

  const subscribeAudio = useCallback<SubscribeAudioLevel>((listener) => {
    audioListeners.current.add(listener);
    return () => {
      audioListeners.current.delete(listener);
    };
  }, []);

  const loadBootstrap = useCallback(async () => {
    dispatch({ type: "bootstrap.start" });
    try {
      const data = await api.bootstrap();
      // The WebSocket sends a newer snapshot on connect. If it landed first,
      // ignore this older response instead of overwriting live state.
      dispatch(
        snapshotSeen.current
          ? { type: "bootstrap.superseded" }
          : { type: "bootstrap.success", data },
      );
    } catch (error) {
      dispatch({ type: "bootstrap.error", message: messageFromError(error) });
    }
  }, []);

  const loadWeather = useCallback(async (force = false) => {
    weatherAbort.current?.abort();
    const controller = new AbortController();
    weatherAbort.current = controller;
    dispatch({ type: "weather.loading" });
    try {
      const weather = await api.weather(force, controller.signal);
      if (!controller.signal.aborted) {
        dispatch({ type: "weather.loaded", weather });
      }
    } catch {
      if (!controller.signal.aborted) {
        dispatch({
          type: "weather.loaded",
          weather: {
            available: false,
            location: "Tashkent",
            error: "Weather service unavailable",
          },
        });
      }
    }
  }, []);

  useEffect(() => {
    void loadBootstrap();
    void loadWeather(false);

    const socket = new AzeSocket(
      (event) => {
        if (event.type === "snapshot") snapshotSeen.current = true;
        if (event.type === "audio") {
          for (const listener of audioListeners.current) {
            listener(event.level, event.source);
          }
          return;
        }
        dispatch({ type: "server.event", event });
      },
      (status) => dispatch({ type: "socket", status }),
    );
    socketRef.current = socket;
    socket.connect();

    const metricsTimer = window.setInterval(() => {
      void api
        .metrics()
        .then((metrics) => dispatch({ type: "metrics.loaded", metrics }))
        .catch(() => undefined);
    }, 2500);
    void api
      .metrics()
      .then((metrics) => dispatch({ type: "metrics.loaded", metrics }))
      .catch(() => undefined);

    /* Real readiness checks for the System Monitor panel: maintenance
     * controller is polled on a slow cadence; live events keep voice state
     * fresh between polls. */
    const diagnosticsTimer = window.setInterval(() => {
      void api
        .diagnostics()
        .then((diagnostics) =>
          dispatch({ type: "diagnostics.loaded", diagnostics }))
        .catch(() => undefined);
    }, 15000);
    void api
      .diagnostics()
      .then((diagnostics) =>
        dispatch({ type: "diagnostics.loaded", diagnostics }))
      .catch(() => undefined);

    const weatherTimer = window.setInterval(() => {
      void loadWeather(false);
    }, 10 * 60 * 1000);

    const toolsTimer = window.setInterval(() => {
      void api
        .tools()
        .then((response) =>
          dispatch({
            type: "tools.loaded",
            tools: response.tools,
            categories: response.categories,
          }))
        .catch(() => undefined);
    }, 5000);

    return () => {
      window.clearInterval(metricsTimer);
      window.clearInterval(diagnosticsTimer);
      window.clearInterval(weatherTimer);
      window.clearInterval(toolsTimer);
      weatherAbort.current?.abort();
      socket.close();
      socketRef.current = null;
    };
  }, [loadBootstrap, loadWeather]);

  const sendText = useCallback(async (text: string): Promise<boolean> => {
    const body = text.trim();
    if (!body) return false;
    const id = newId();
    dispatch({
      type: "optimistic-user",
      message: {
        id,
        role: "user",
        text: body,
        ts: new Date().toISOString(),
        source: "text",
        pending: true,
      },
    });
    try {
      await api.sendMessage(body, id);
      dispatch({ type: "message-delivered", id });
      return true;
    } catch (error) {
      dispatch({ type: "message-failed", id });
      dispatch({
        type: "toast.add",
        toast: { id: newId(), kind: "error", message: messageFromError(error) },
      });
      return false;
    }
  }, []);

  const toggleMute = useCallback(async () => {
    try {
      const result = await api.setMuted(!state.muted);
      dispatch({
        type: "voice.state",
        state: result.state,
        muted: result.muted,
      });
    } catch (error) {
      dispatch({
        type: "toast.add",
        toast: { id: newId(), kind: "error", message: messageFromError(error) },
      });
    }
  }, [state.muted]);

  const interrupt = useCallback(async () => {
    try {
      await api.interrupt();
      dispatch({ type: "cancel-streaming" });
    } catch (error) {
      dispatch({
        type: "toast.add",
        toast: { id: newId(), kind: "error", message: messageFromError(error) },
      });
    }
  }, []);

  const approveAction = useCallback(async (approvalId: string) => {
    try {
      const response = await api.approveAction(approvalId);
      dispatch({
        type: "server.event",
        event: {
          type: "approval.updated",
          event: "approved",
          approval: response.approval,
          ts: new Date().toISOString(),
        },
      });
      return response.approval;
    } catch (error) {
      dispatch({
        type: "toast.add",
        toast: { id: newId(), kind: "error", message: messageFromError(error) },
      });
      throw error;
    }
  }, []);

  const rejectAction = useCallback(async (approvalId: string) => {
    try {
      const response = await api.rejectAction(approvalId);
      dispatch({
        type: "server.event",
        event: {
          type: "approval.updated",
          event: "rejected",
          approval: response.approval,
          ts: new Date().toISOString(),
        },
      });
      return response.approval;
    } catch (error) {
      dispatch({
        type: "toast.add",
        toast: { id: newId(), kind: "error", message: messageFromError(error) },
      });
      throw error;
    }
  }, []);

  const setPttHeld = useCallback(async (held: boolean) => {
    try {
      await api.pttHold(held);
    } catch (error) {
      dispatch({
        type: "toast.add",
        toast: { id: newId(), kind: "error", message: messageFromError(error) },
      });
    }
  }, []);

  const loadSettings = useCallback(async (refreshAudio = false) => {
    dispatch({ type: "settings.loading" });
    try {
      const settings = await api.settings(refreshAudio);
      dispatch({ type: "settings.loaded", settings });
    } catch (error) {
      dispatch({ type: "settings.failed" });
      dispatch({
        type: "toast.add",
        toast: { id: newId(), kind: "error", message: messageFromError(error) },
      });
    }
  }, []);

  const saveSettings = useCallback(
    async (patch: SettingsPatch): Promise<FullSettings> => {
      try {
        const response = await api.updateSettings(patch);
        dispatch({
          type: "settings.loaded",
          settings: response.settings,
        });
        dispatch({
          type: "toast.add",
          toast: { id: newId(), kind: "success", message: "Settings saved." },
        });
        return response.settings;
      } catch (error) {
        dispatch({
          type: "toast.add",
          toast: { id: newId(), kind: "error", message: messageFromError(error) },
        });
        throw error;
      }
    },
    [],
  );

  const refreshAudioDevices = useCallback(async () => {
    try {
      const devices = await api.audioDevices(true);
      if (state.fullSettings) {
        dispatch({
          type: "settings.loaded",
          settings: {
            ...state.fullSettings,
            input_devices: devices.input,
            output_devices: devices.output,
            input_device: devices.input_device,
            output_device: devices.output_device,
            output_volume: devices.output_volume,
          },
        });
      }
    } catch (error) {
      dispatch({
        type: "toast.add",
        toast: { id: newId(), kind: "error", message: messageFromError(error) },
      });
    }
  }, [state.fullSettings]);

  const testAudio = useCallback(
    async (kind: "input" | "output", device: string) => {
      dispatch({ type: "audio.test.clear" });
      try {
        const response = await api.testAudio(kind, device);
        return response.result;
      } finally {
        dispatch({ type: "audio.test.clear" });
      }
    },
    [],
  );

  const setVolume = useCallback(async (volume: number, device: string) => {
    try {
      const response = await api.setVolume(volume, device);
      if (!response.ok) {
        dispatch({
          type: "toast.add",
          toast: {
            id: newId(),
            kind: "error",
            message: "Output level could not be set.",
          },
        });
      }
      return response.ok;
    } catch (error) {
      dispatch({
        type: "toast.add",
        toast: { id: newId(), kind: "error", message: messageFromError(error) },
      });
      return false;
    }
  }, []);

  const getVolume = useCallback(async (device: string) => {
    try {
      const response = await api.getVolume(device);
      return response.volume;
    } catch {
      return null;
    }
  }, []);

  const saveApiKey = useCallback(async (apiKey: string, osSystem?: string) => {
    try {
      const response = await api.saveApiKey(apiKey, osSystem);
      dispatch({ type: "setup.updated", setup: response.setup });
      dispatch({
        type: "toast.add",
        toast: { id: newId(), kind: "success", message: "API key saved." },
      });
    } catch (error) {
      dispatch({
        type: "toast.add",
        toast: { id: newId(), kind: "error", message: messageFromError(error) },
      });
      throw error;
    }
  }, []);

  const scanModule = useCallback(async (moduleId: string) => {
    try {
      const result = await api.scanModule(moduleId);
      dispatch({ type: "module.scanned", moduleId, result });
      return result;
    } catch (error) {
      dispatch({
        type: "toast.add",
        toast: { id: newId(), kind: "error", message: messageFromError(error) },
      });
      throw error;
    }
  }, []);

  /* ── Skills Engine ───────────────────────────────────────────────────────
   * The panel reads the same snapshot the engine publishes on the WebSocket;
   * these actions also apply the authoritative HTTP response so a disconnected
   * socket cannot leave the panel showing stale enable/disable state. */
  const refreshSkills = useCallback(async () => {
    try {
      const response = await api.refreshSkills();
      dispatch({ type: "skills.updated", data: response.snapshot });
      return response.snapshot;
    } catch (error) {
      dispatch({
        type: "toast.add",
        toast: { id: newId(), kind: "error", message: messageFromError(error) },
      });
      throw error;
    }
  }, []);

  const setSkillEnabled = useCallback(
    async (skillId: string, enabled: boolean) => {
      try {
        const response = await api.setSkillEnabled(skillId, enabled);
        dispatch({ type: "skills.updated", data: response.snapshot });
        return response.snapshot;
      } catch (error) {
        dispatch({
          type: "toast.add",
          toast: { id: newId(), kind: "error", message: messageFromError(error) },
        });
        throw error;
      }
    },
    [],
  );

  const skillDetail = useCallback(
    (skillId: string) => api.skillDetail(skillId),
    [],
  );

  const skillAsset = useCallback(
    (skillId: string, path: string) => api.skillAsset(skillId, path),
    [],
  );

  const uninstallSkill = useCallback(async (skillId: string) => {
    try {
      const response = await api.uninstallSkill(skillId, true);
      dispatch({ type: "skills.updated", data: response.snapshot });
      dispatch({
        type: "toast.add",
        toast: {
          id: newId(),
          kind: "success",
          message: response.message || "Installed Skill removed.",
        },
      });
      return response.snapshot;
    } catch (error) {
      dispatch({
        type: "toast.add",
        toast: { id: newId(), kind: "error", message: messageFromError(error) },
      });
      throw error;
    }
  }, []);

  const reconnect = useCallback(async () => {
    try {
      await api.reconnect();
      dispatch({
        type: "toast.add",
        toast: { id: newId(), kind: "info", message: "Reconnect requested." },
      });
    } catch (error) {
      dispatch({
        type: "toast.add",
        toast: { id: newId(), kind: "error", message: messageFromError(error) },
      });
    }
  }, []);

  const dismissToast = useCallback(
    (id: string) => {
      const toast = state.toasts.find((item) => item.id === id);
      if (toast?.notificationId) {
        void api.dismissNotification(toast.notificationId).catch(() => undefined);
      }
      dispatch({ type: "toast.dismiss", id });
    },
    [state.toasts],
  );

  const snoozeNotification = useCallback(
    async (notificationId: string, minutes = 10) => {
      try {
        await api.snoozeNotification(notificationId, minutes);
        dispatch({
          type: "toast.add",
          toast: {
            id: newId(),
            kind: "info",
            message: `Reminder snoozed for ${minutes} minute(s).`,
          },
        });
      } catch (error) {
        dispatch({
          type: "toast.add",
          toast: {
            id: newId(),
            kind: "error",
            message: messageFromError(error),
          },
        });
      }
    },
    [],
  );

  const markNotificationRead = useCallback(async (notificationId: string) => {
    try {
      await api.markNotificationRead(notificationId);
    } catch {
      // Read state is best effort; the notification still works.
    }
  }, []);

  const clearToolEvents = useCallback(async () => {
    dispatch({ type: "tools.clear" });
    try {
      await api.clearTools();
    } catch (error) {
      dispatch({
        type: "toast.add",
        toast: { id: newId(), kind: "error", message: messageFromError(error) },
      });
    }
  }, []);

  const setToolSelection = useCallback(async (enabled: string[]) => {
    try {
      const response = await api.selectTools(enabled);
      dispatch({
        type: "tools.loaded",
        tools: response.tools,
        categories: response.categories,
      });
    } catch (error) {
      dispatch({
        type: "toast.add",
        toast: { id: newId(), kind: "error", message: messageFromError(error) },
      });
      throw error;
    }
  }, []);

  const retryBootstrap = useCallback(async () => {
    // A manual retry is allowed to apply an HTTP snapshot even though an older
    // WebSocket snapshot was seen; the following sync() supersedes it if live.
    snapshotSeen.current = false;
    await loadBootstrap();
    socketRef.current?.sync();
  }, [loadBootstrap]);

  const clearAudioTest = useCallback(() => {
    dispatch({ type: "audio.test.clear" });
  }, []);

  /* ── YouTube player bridge ───────────────────────────────────────────────
   * These all reuse the one existing WebSocket. The backend correlates each
   * request_id with a bounded wait; the panel only reports real controller
   * results, never an optimistic acknowledgement. */
  const youtubeSendState = useCallback((playerState: YouTubePlayerState) => {
    socketRef.current?.send({ type: "youtube_player_state", state: playerState });
  }, []);

  const youtubeReady = useCallback((playerState: YouTubePlayerState) => {
    socketRef.current?.send({ type: "youtube_player_ready", state: playerState });
  }, []);

  const youtubeError = useCallback(
    (
      error: YouTubePlayerError,
      playerState: YouTubePlayerState,
      requestId?: string,
    ) => {
      socketRef.current?.send({
        type: "youtube_player_error",
        request_id: requestId ?? "",
        error,
        state: playerState,
      });
    },
    [],
  );

  const youtubeCommandResult = useCallback(
    (
      requestId: string,
      ok: boolean,
      playerState: YouTubePlayerState,
      error?: YouTubePlayerError,
    ) => {
      socketRef.current?.send({
        type: "youtube_player_result",
        request_id: requestId,
        ok,
        state: playerState,
        ...(error ? { error } : {}),
      });
    },
    [],
  );

  const youtubeRequestClose = useCallback(() => {
    dispatch({ type: "youtube.close.request" });
  }, []);

  const youtubeSettleClose = useCallback((requestId: string) => {
    const closed = closedPlayerState();
    socketRef.current?.send({ type: "youtube_player_state", state: closed });
    if (requestId) {
      socketRef.current?.send({
        type: "youtube_player_result",
        request_id: requestId,
        ok: true,
        state: closed,
      });
    }
    dispatch({ type: "youtube.close.settled" });
  }, []);

  const youtubeCommandSettled = useCallback((requestId: string) => {
    dispatch({ type: "youtube.command.completed", requestId });
  }, []);

  const youtubeLoadSettled = useCallback((requestId: string) => {
    dispatch({ type: "youtube.load.completed", requestId });
  }, []);

  return {
    state,
    sendText,
    toggleMute,
    interrupt,
    approveAction,
    rejectAction,
    setPttHeld,
    saveSettings,
    loadSettings,
    refreshAudioDevices,
    testAudio,
    setVolume,
    getVolume,
    saveApiKey,
    scanModule,
    refreshSkills,
    setSkillEnabled,
    skillDetail,
    skillAsset,
    uninstallSkill,
    reconnect,
    refreshWeather: loadWeather,
    clearToolEvents,
    setToolSelection,
    dismissToast,
    snoozeNotification,
    markNotificationRead,
    retryBootstrap,
    clearAudioTest,
    subscribeAudio,
    youtubeSendState,
    youtubeReady,
    youtubeError,
    youtubeCommandResult,
    youtubeRequestClose,
    youtubeSettleClose,
    youtubeCommandSettled,
    youtubeLoadSettled,
  };
}
