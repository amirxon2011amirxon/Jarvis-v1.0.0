import type {
  YouTubePlayerState,
  YouTubeVideo,
} from "./components/youtube/youtubeTypes";

export type AzeState =
  | "INITIALISING"
  | "LISTENING"
  | "THINKING"
  | "SPEAKING"
  | "PROCESSING"
  | "OFFLINE"
  | "MUTED";

export type ChatRole = "user" | "assistant";

export type AudioSource = "input" | "output";
export type AudioLevelListener = (level: number, source: AudioSource) => void;
export type SubscribeAudioLevel = (listener: AudioLevelListener) => () => void;

export interface ChatMessage {
  id: string;
  role: ChatRole;
  text: string;
  ts: string;
  /** Monotonic order assigned by the backend; absent on optimistic rows. */
  seq?: number;
  source?: "text" | "voice";
  streaming?: boolean;
  /** Client-side delivery state for an optimistic message. */
  pending?: boolean;
  failed?: boolean;
  cancelled?: boolean;
}

export type LogLevel = "info" | "system" | "error" | "tool" | "chat";

export interface LogEntry {
  id: string;
  ts: string;
  text: string;
  level: LogLevel;
}

export type ToolStatus =
  | "queued"
  | "pending"
  | "running"
  | "success"
  | "failed"
  | "cancelled"
  | "unknown";
export interface ToolEvent {
  id: string;
  tool: string;
  target: string;
  phase: "start" | "end";
  started_at: string;
  ended_at?: string;
  ms: number;
  ok?: boolean;
  summary?: string;
  pending?: boolean;
  status?: ToolStatus;
}

export interface ApprovalRequest {
  approval_id: string;
  tool: string;
  operation: string;
  target: string;
  summary: string;
  parameters: Record<string, unknown>;
  created_at: string;
  expires_at: string;
  status:
    | "pending"
    | "approved"
    | "rejected"
    | "expired"
    | "consumed"
    | "revoked";
  decided_at?: string;
  consumed_at?: string;
  reason?: string;
}

/** Snapshot of the current objective-scoped run, if Gemini started one. */
export interface AgentRunSnapshot {
  run_id: string;
  conversation_id: string;
  objective: string;
  status:
    | "in_progress"
    | "needs_user"
    | "completed"
    | "blocked"
    | "failed"
    | "cancelled";
  started_at: string;
  updated_at: string;
  finished_at?: string;
  step_count: number;
  tool_call_count: number;
  successful_tool_calls?: number;
  failed_tool_calls?: number;
  consecutive_failures: number;
  cancel_requested: boolean;
  last_tool_summary: string;
  current_step: string;
  artifacts: Array<{
    name: string;
    value?: string | number | boolean | null;
    created_at: string;
  }>;
  steps?: Array<{ step: string; at: string }>;
  tool_outcomes?: Array<{
    tool: string;
    ok: boolean;
    summary: string;
    category: string;
    at: string;
  }>;
  failures?: Array<{
    tool: string;
    category: string;
    recoverable: boolean;
    requires_user: boolean;
    error: string;
    hint: string;
    at: string;
  }>;
  blockers?: string[];
  completed_operations?: Array<{ tool: string; count: number }>;
  failed_operations?: Array<{
    tool: string;
    category: string;
    recoverable: boolean;
    retry_same_call: boolean;
    count: number;
    last_error: string;
  }>;
  remaining_work?: string;
  verification?: {
    required: boolean;
    reason: string;
    evidence: Array<{
      summary: string;
      source: string;
      tool: string;
      kind: string;
      at: string;
    }>;
    attempts: number;
    exhausted: boolean;
    missing: string[];
  };
  result_summary: string;
  last_error: string;
  elapsed_seconds: number;
  limits: {
    max_steps: number;
    max_tool_calls: number;
    max_consecutive_failures: number;
    max_duration_seconds: number;
  };
  is_terminal: boolean;
}

export interface ToolInfo {
  /** Canonical category/name id; absent only on older backends. */
  id?: string;
  name: string;
  description: string;
  tag: string;
  category?: string;
  category_label?: string;
  /** Registered actions this tool module exposes to the model. */
  actions?: string[];
  enabled?: boolean;
}

export interface ToolCategory {
  id: string;
  label: string;
  description: string;
  tools: ToolInfo[];
  count: number;
  enabled_count: number;
  enabled: boolean;
}

export interface ModuleInfo {
  id: string;
  index: string;
  name: string;
  description: string;
  glyph: string;
  available: boolean;
  count: number;
  status: string;
}

/* ── Skills Engine ────────────────────────────────────────────────────────── */

export type SkillSource = "builtin" | "installed";

export interface SkillValidation {
  valid: boolean;
  errors: string[];
  warnings: string[];
}

export interface SkillInfo {
  id: string;
  name: string;
  description: string;
  version: string;
  author: string;
  license: string;
  category: string;
  tags: string[];
  source: SkillSource;
  enabled: boolean;
  available: boolean;
  active: boolean;
  activated_at: string | null;
  entrypoint: string;
  requirements: { tools: string[] };
  missing_tools?: string[];
  references: string[];
  resources: string[];
  validation: SkillValidation;
  registered_at: string;
}

export interface ActiveSkillInfo {
  id: string;
  name: string;
  version: string;
  source: SkillSource;
  scope_id: string;
  activated_at: string;
  state: "active";
  instruction_chars: number;
  references: string[];
  resources: string[];
}

export interface SkillIssue {
  path: string;
  source: string;
  message: string;
  code: string;
}

export interface SkillsCounts {
  total: number;
  enabled: number;
  active: number;
  builtin: number;
  installed: number;
  disabled: number;
  issues: number;
}

export interface SkillsSnapshot {
  engine_version: string;
  initialized: boolean;
  initialized_at: string;
  active_scope: string;
  skills: SkillInfo[];
  active: ActiveSkillInfo[];
  counts: SkillsCounts;
  errors: SkillIssue[];
  state_file: string;
}

export interface SkillAsset {
  path: string;
  kind: "references" | "resources";
  size: number;
  readable: boolean;
}

export type SkillDetail = Omit<SkillInfo, "references" | "resources"> & {
  manifest: Record<string, unknown>;
  package: {
    path: string;
    manifest_path: string;
    entrypoint_path: string;
    source: SkillSource;
    registered_at: string;
  };
  instructions: string;
  instructions_error: string;
  references: SkillAsset[];
  resources: SkillAsset[];
};

export interface SkillsSnapshotResponse {
  ok: boolean;
  snapshot: SkillsSnapshot;
}

export interface SkillAssetResponse {
  skill_id: string;
  path: string;
  kind: string;
  size: number;
  content: string;
}

export interface SkillUninstallResponse {
  ok: boolean;
  message: string;
  snapshot: SkillsSnapshot;
}

/* ── Deep research ────────────────────────────────────────────────────────── */

export type ResearchStatus =
  | "idle"
  | "running"
  | "complete"
  | "error"
  | "cancelled";
export type ResearchConfidence = "high" | "medium" | "low";

export interface ResearchSource {
  id: number;
  title: string;
  url: string;
  domain: string;
  published: string;
  retrieved_at: string;
  snippet: string;
  credibility: {
    score: number;
    label: ResearchConfidence;
    reasons: string[];
  };
}

export interface ResearchItem {
  id: string;
  text: string;
  kind: string;
  theme: string;
  source_ids: number[];
  support_count: number;
  confidence: string;
  numbers?: string[];
  note?: string;
  contrasting_text?: string;
  contrast_source_ids?: number[];
}

export interface ResearchSection {
  id: string;
  heading: string;
  items: ResearchItem[];
}

export interface ResearchReport {
  topic: string;
  focus: string;
  depth: string;
  generated_at: string;
  confidence: {
    level: ResearchConfidence;
    reason: string;
  };
  summary: string;
  key_findings: ResearchItem[];
  sections: ResearchSection[];
  statistics: ResearchItem[];
  contradictions: ResearchItem[];
  uncertainties: ResearchItem[];
  conclusions: ResearchItem[];
  limitations: string[];
  sources: ResearchSource[];
  methodology: Record<string, unknown>;
  report_markdown: string;
  prose: string;
}

export interface ResearchEvent {
  ts: string;
  phase: string;
  message: string;
}

export interface ResearchState {
  open: boolean;
  job_id: string;
  status: ResearchStatus;
  topic: string;
  depth: string;
  phase: string;
  message: string;
  progress: number;
  sources_found: number;
  sources_read: number;
  started_at: string;
  updated_at: string;
  report: ResearchReport | null;
  events: ResearchEvent[];
  error: string;
  warnings: string[];
  cancelled?: boolean;
}

/* ── Continuous screen observation ────────────────────────────────────────── */

export type ObservationState =
  | "INACTIVE"
  | "STARTING"
  | "OBSERVING"
  | "STOPPING"
  | "ERROR";

export interface ObservationSnapshot {
  state: ObservationState;
  observation_active: boolean;
  started_at: string | null;
  frames_streamed: number;
  frames_dropped: number;
  frame_interval_seconds: number;
  last_error: string;
  last_frame_age_seconds?: number;
  voice_session_connected: boolean;
}

export interface SetupStatus {
  configured: boolean;
  setup_done: boolean;
  key_masked: string;
}

export interface TurnTuning {
  enabled: boolean;
  silence_ms: number;
  prefix_ms: number;
  end_sensitivity: "low" | "default" | "high";
  start_sensitivity: "low" | "default" | "high";
}

export interface BasicSettings {
  assistant_name: string;
  user_name: string;
  voice_name: string;
  voices: string[];
  ui_color: string;
  push_to_talk_enabled: boolean;
  proactive_audio_enabled: boolean;
  thinking_enabled: boolean;
  turn_tuning: TurnTuning;
  weather_location?: string;
}

export interface FullSettings extends BasicSettings {
  input_device: string;
  output_device: string;
  input_devices: string[];
  output_devices: string[];
  output_volume: number | null;
  gemini: SetupStatus;
  platform: string;
}

export interface AppInfo {
  name: string;
  version: string;
  platform: string;
}

export interface Bootstrap {
  protocol_version: number;
  app: AppInfo;
  state: AzeState;
  muted: boolean;
  assistant_name: string;
  ready: boolean;
  conversation_id?: string;
  conversation_title?: string;
  messages: ChatMessage[];
  logs: LogEntry[];
  tool_events: ToolEvent[];
  agent_run?: AgentRunSnapshot | null;
  observation?: ObservationSnapshot | null;
  notifications?: NotificationItem[];
  approvals?: ApprovalRequest[];
  tools: ToolInfo[];
  tool_categories: ToolCategory[];
  modules: ModuleInfo[];
  skills?: SkillsSnapshot;
  research?: ResearchState;
  setup: SetupStatus;
  settings: BasicSettings;
  youtube?: YouTubePlayerState;
  server_time: string;
}

export interface WeatherData {
  available: boolean;
  location: string;
  timezone?: string;
  temperature_c?: number;
  apparent_c?: number;
  humidity?: number;
  wind_kmh?: number;
  weather_code?: number;
  is_day?: boolean;
  observed_at?: string;
  fetched_at?: string;
  error?: string;
}

export interface DriveInfo {
  letter: string;
  used_gb: number;
  total_gb: number;
  percent: number;
}

export interface Metrics {
  cpu: number;
  mem: number;
  net: number;
  gpu: number;
  tmp: number;
  model?: string | null;
  provider?: string | null;
  response_latency_ms?: number | null;
  microphone?: "active" | "muted" | "offline" | string;
  voice_engine?: "online" | "connecting" | "offline" | string;
  active_agents?: number;
  agent_runtime?: boolean;
  link_state?: AzeState;
  /* Hardware extras for the Hardware Monitor. The OS decides what actually
   * exists: every field is optional and must degrade to "not reported". */
  freq_ghz?: number;
  cpu_temp?: number;
  mem_used_gb?: number;
  mem_total_gb?: number;
  drives?: DriveInfo[];
  os_name?: string;
  os_version?: string;
  uptime_s?: number;
}

export interface Health {
  ok: boolean;
  app: string;
  version: string;
  configured: boolean;
  state: AzeState;
  session_running: boolean;
  session_connected: boolean;
  ws_clients: number;
  time: string;
}

export type ServerEvent =
  | { type: "snapshot"; data: Bootstrap; ts: string }
  | { type: "state"; state: AzeState; muted: boolean; ts: string }
  | { type: "log"; entry: LogEntry }
  | { type: "chat.message"; message: ChatMessage }
  | {
      type: "chat.delta";
      id: string;
      role: "assistant";
      text: string;
      ts: string;
    }
  | { type: "chat.cancelled"; id: string; ts: string }
  | { type: "chat.discarded"; id: string; ts: string }
  | { type: "chat.cleared"; conversation_id?: string; ts: string }
  | { type: "tool.updated"; event: ToolEvent }
  | { type: "tool.cleared"; ts: string }
  | {
      type: "approval.updated";
      event: string;
      approval: ApprovalRequest | null;
      ts: string;
    }
  | {
      type: "agent.updated";
      event: string;
      run_id: string;
      run: AgentRunSnapshot | null;
      error?: string;
      limit?: string;
      ts: string;
    }
  | {
      type: "observation.updated";
      observation: ObservationSnapshot | null;
      state: string;
      active: boolean;
      ts: string;
    }
  | {
      type: "notification.created";
      event: string;
      notification: NotificationItem;
      ts: string;
    }
  | {
      type: "notification.updated";
      event: string;
      notification: NotificationItem;
      ts: string;
    }
  | { type: "audio"; source: "input" | "output"; level: number; ts: string }
  | {
      type: "audio.test";
      kind: "input" | "output";
      level: number;
      ts: string;
    }
  | {
      type: "settings.updated";
      settings: BasicSettings;
      setup?: SetupStatus;
      ts: string;
    }
  | { type: "skills.updated"; data: SkillsSnapshot; ts: string }
  | { type: "research_state"; state: ResearchState; ts: string }
  | {
      type: "youtube_open_panel";
      video: YouTubeVideo | null;
      autoplay: boolean;
      ts: string;
    }
  | {
      type: "youtube_load_video";
      request_id: string;
      video: YouTubeVideo;
      autoplay: boolean;
      start_seconds: number;
      ts: string;
    }
  | {
      type: "youtube_player_command";
      request_id: string;
      command: string;
      args: Record<string, unknown>;
      ts: string;
    }
  | { type: "youtube_close_panel"; request_id: string; ts: string }
  | { type: "youtube_state"; state: YouTubePlayerState; ts: string }
  | { type: "setup.required"; ts: string }
  | { type: "setup.updated"; setup: SetupStatus; ts: string }
  | { type: "shutdown"; reason?: string; ts: string }
  | { type: "ping"; ts: string }
  | { type: "pong"; ts: string }
  | { type: "error"; message: string };

export interface MessageResponse {
  accepted: boolean;
  message: ChatMessage;
}

export interface MuteResponse {
  ok: boolean;
  muted: boolean;
  state: AzeState;
}

export interface SettingsPatch {
  assistant_name?: string;
  user_name?: string;
  voice_name?: string;
  ui_color?: string;
  push_to_talk_enabled?: boolean;
  proactive_audio_enabled?: boolean;
  thinking_enabled?: boolean;
  turn_tuning?: Partial<TurnTuning>;
  input_device?: string;
  output_device?: string;
  weather_location?: string;
}

export interface SettingsResponse {
  ok: boolean;
  settings: FullSettings;
  setup: SetupStatus;
}

export interface SetupKeyResponse {
  ok: boolean;
  setup: SetupStatus;
}

export interface AudioDevicesResponse {
  input: string[];
  output: string[];
  input_device: string;
  output_device: string;
  output_volume: number | null;
}

export interface AudioTestResult {
  ok: boolean;
  peak?: number;
  level?: number;
  verdict: string;
}

export interface AudioTestResponse {
  ok: boolean;
  result: AudioTestResult;
}

export interface VolumeResponse {
  ok: boolean;
  volume: number;
}

export interface ScanResponse {
  ok: boolean;
  module_id: string;
  status: string;
  count: number;
  message: string;
}

export interface ToolsResponse {
  tools: ToolInfo[];
  categories?: ToolCategory[];
  count?: number;
  enabled_count?: number;
  all_enabled?: boolean;
  enabled?: string[];
}

export type DiagnosticStatus = "ok" | "warn" | "error";

export interface DiagnosticCheck {
  id: string;
  label: string;
  status: DiagnosticStatus;
  detail: string;
}

export interface Diagnostics {
  ok: boolean;
  checks: DiagnosticCheck[];
  model?: string | null;
  provider?: string | null;
  session_running: boolean;
  session_connected: boolean;
  /** Last Live connection failure, when the link is retrying. */
  connection_error?: string;
  weather_available: boolean;
  tools: number;
  input_devices: number;
  output_devices: number;
  state: AzeState;
}

export interface Toast {
  id: string;
  kind: "info" | "success" | "error";
  message: string;
  title?: string;
  notificationId?: string;
  autoDismiss?: boolean;
  snoozeMinutes?: number;
}

export type NotificationStatus = "unread" | "read" | "dismissed";
export type NotificationKind = "reminder" | "timer" | "agent" | "system";
export type NotificationPriority = "low" | "normal" | "high" | "urgent";

export interface NotificationItem {
  id: string;
  reminder_id: string;
  title: string;
  body: string;
  kind: NotificationKind;
  priority: NotificationPriority;
  status: NotificationStatus;
  created_at: string;
  read_at: string;
  dismissed_at: string;
  delivery: {
    dashboard?: string;
    desktop?: string;
    error?: string;
  };
  actions: Array<{
    action: string;
    minutes?: number;
    label?: string;
  }>;
  metadata: Record<string, unknown>;
  source: string;
  dedupe_key?: string;
}

/* ── Long-term memory ─────────────────────────────────────────────────────── */
export type MemoryType = "user" | "episodic" | "knowledge" | "project";
export type MemoryStatus = "active" | "superseded" | "deleted";

export interface MemoryRecord {
  id: string;
  type: MemoryType;
  content: string;
  title: string;
  created_at: string;
  updated_at: string;
  last_accessed_at: string | null;
  source_type: string;
  source_ref: string;
  confidence: number;
  importance: number;
  project_id: string | null;
  expires_at: string | null;
  status: MemoryStatus;
  superseded_by: string | null;
  metadata: Record<string, unknown>;
  tags: string[];
  version: number;
  score: number;
  relations: {
    memory_id: string;
    relation: string;
    direction: "incoming" | "outgoing";
  }[];
}

export interface MemoryStats {
  total: number;
  by_type: Record<MemoryType, number>;
  superseded: number;
  tombstones: number;
  extraction: Record<string, number>;
  recent: { id: string; type: MemoryType; title: string; updated_at: string }[];
  projects: string[];
  fts: boolean;
  schema_version: number;
  retrieval_enabled: boolean;
  auto_extract: boolean;
  pending_extractions: { turn_id: string; status: string; updated_at: string }[];
  db_path: string;
}

export interface MemoryStatsResponse {
  ok: boolean;
  status: string;
  message?: string;
  data: MemoryStats;
}

export interface MemoryList {
  ok: boolean;
  status: string;
  data: {
    memories: MemoryRecord[];
    total: number;
    limit?: number;
    offset?: number;
    query?: string;
  };
}

export interface MemoryDetail {
  ok: boolean;
  status: string;
  memory?: MemoryRecord;
  message?: string;
}

export interface MemoryAction {
  ok: boolean;
  status: string;
  message?: string;
  summary?: string;
  data?: Record<string, unknown>;
  memory?: MemoryRecord;
}

export interface MemorySettings {
  enabled: boolean;
  auto_extract: boolean;
  retrieval_enabled: boolean;
  voice_injection: boolean;
  max_retrieved: number;
  max_context_chars: number;
  search_limit: number;
  backup_retention: number;
  expiry_days: number;
  db_path: string;
  backup_dir: string;
  error?: string;
}

export interface MemorySettingsPatch {
  enabled?: boolean;
  auto_extract?: boolean;
  retrieval_enabled?: boolean;
  voice_injection?: boolean;
  max_retrieved?: number;
  max_context_chars?: number;
  search_limit?: number;
  backup_retention?: number;
  expiry_days?: number;
}

export interface MemoryUpdatePatch {
  content?: string;
  type?: MemoryType;
  title?: string;
  tags?: string[];
  project_id?: string | null;
  confidence?: number;
  importance?: number;
  expected_version?: number;
  supersede?: boolean;
}
