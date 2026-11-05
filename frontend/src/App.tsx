import { useCallback, useEffect, useMemo, useState } from "react";

import { BayView } from "./components/BayView";
import { AgentPanel } from "./components/AgentPanel";
import { ApprovalDialog } from "./components/ApprovalDialog";
import { Composer } from "./components/Composer";
import { ConversationPanel } from "./components/ConversationPanel";
import { HaloStage } from "./components/HaloStage";
import { Header } from "./components/Header";
import { InitialiseGate } from "./components/InitialiseGate";
import { MemoryView } from "./components/MemoryView";
import { ResearchPanel } from "./components/ResearchPanel";
import { ScreenObservationIndicator } from "./components/ScreenObservationIndicator";
import { SettingsDialog } from "./components/SettingsDialog";
import { SetupDialog } from "./components/SetupDialog";
import { ShutdownScreen } from "./components/ShutdownScreen";
import { Sidebar, SIDEBAR_NAV } from "./components/Sidebar";
import { SkillsView } from "./components/SkillsView";
import { Toasts } from "./components/Toasts";
import { Button } from "./components/ui/Button";
import { HexMark } from "./components/ui/Icons";
import { YouTubePlayerPanel } from "./components/youtube/YouTubePlayerPanel";
import { useAze } from "./hooks/useAze";
import { applyMotionDocument, subscribeMotionPrefs } from "./lib/motion";
import { DEFAULT_ACCENT, applyAccent } from "./lib/theme";
import { cn } from "./lib/utils";
import type { ModuleInfo } from "./types";

function BootScreen({
  error,
  onRetry,
}: {
  error?: string | null;
  onRetry?: () => void;
}) {
  return (
    <div className="flex min-h-[100dvh] items-center justify-center bg-[var(--void)] px-6">
      <div className="w-full max-w-md text-center">
        <HexMark className="mx-auto h-16 w-16 text-[var(--accent)]" />
        <h1 className="mt-6 font-mono text-2xl font-semibold uppercase tracking-[0.34em] text-[var(--accent)]">
          J.A.R.V.I.S.
        </h1>
        <p className="mt-2 font-mono text-[9px] uppercase tracking-[0.24em] text-[var(--dim)]">
          LOCAL NEURAL INTERFACE
        </p>
        {error ? (
          <>
            <p className="mt-8 border border-[var(--danger)]/40 bg-[var(--danger)]/[0.05] px-4 py-3 font-mono text-[10px] leading-5 text-[var(--danger)]">
              {error}
            </p>
            {onRetry ? (
              <Button variant="primary" size="md" className="mt-4" onClick={onRetry}>
                RETRY LOCAL LINK
              </Button>
            ) : null}
          </>
        ) : (
          <>
            <div className="mx-auto mt-10 h-px w-48 overflow-hidden bg-[var(--line)]">
              <div className="aze-loading-bar h-full w-1/3 bg-[var(--accent)]" />
            </div>
            <p className="mt-4 font-mono text-[8px] uppercase tracking-[0.2em] text-[var(--muted)]">
              ESTABLISHING LOCAL LINK…
            </p>
          </>
        )}
      </div>
    </div>
  );
}

function moduleFallback(id: string): ModuleInfo {
  const nav = SIDEBAR_NAV.find((item) => item.id === id);
  return {
    id,
    index: "00",
    name: nav?.label ?? id,
    description: nav?.description ?? "",
    glyph: "◇",
    status: "No module telemetry reported.",
    available: false,
    count: 0,
  };
}

export default function App() {
  const aze = useAze();
  const { state } = aze;

  const [setupOpen, setSetupOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(true);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [transcriptOpen, setTranscriptOpen] = useState(true);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [skillsOpen, setSkillsOpen] = useState(false);
  const [agentOpen, setAgentOpen] = useState(false);
  const [openBays, setOpenBays] = useState<string[]>([]);
  const [activeBay, setActiveBay] = useState<string | null>(null);
  const [dismissedResearch, setDismissedResearch] = useState<string | null>(null);
  const [entered, setEntered] = useState(() => {
    try {
      return window.sessionStorage.getItem("aze.initialized") === "1";
    } catch {
      return false;
    }
  });

  const configured = state.setup.configured;
  const requiredSetup = Boolean(state.boot) && !configured && state.setupRequired;
  const setupDialogOpen = setupOpen || requiredSetup;

  useEffect(() => {
    applyAccent(state.settings?.ui_color ?? DEFAULT_ACCENT);
  }, [state.settings?.ui_color]);

  useEffect(() => {
    document.documentElement.style.setProperty(
      "--aze-sidebar-width",
      sidebarCollapsed ? "64px" : "236px",
    );
  }, [sidebarCollapsed]);

  useEffect(() => {
    document.documentElement.dataset.youtubeOpen = state.youtube.open
      ? "1"
      : "0";
  }, [state.youtube.open]);

  useEffect(() => {
    applyMotionDocument();
    return subscribeMotionPrefs(applyMotionDocument);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = "dark";
    try {
      window.localStorage.setItem("aze.theme", "dark");
    } catch {
      // Theme still applies for this tab when storage is unavailable.
    }
  }, []);

  useEffect(() => {
    if (!drawerOpen) return;
    const onResize = () => {
      if (window.matchMedia("(min-width: 1280px)").matches) setDrawerOpen(false);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [drawerOpen]);

  const activeTools = useMemo(
    () => state.toolEvents.filter((event) => event.phase === "start").length,
    [state.toolEvents],
  );

  const awaitingReply = useMemo(() => {
    const last = state.messages.at(-1);
    return Boolean(last && last.role === "user" && !last.pending && !last.failed);
  }, [state.messages]);

  const streamingReply = useMemo(
    () => state.messages.some((message) => message.streaming),
    [state.messages],
  );

  const error = useMemo(() => {
    if (state.fatal) return state.fatal;
    if (state.socket === "offline") return "Local event link lost. Reconnecting to the backend.";
    if (state.state === "OFFLINE" && configured) return "Gemini Live is offline. Check the link in Settings.";
    if (requiredSetup) {
      const latest = [...state.logs].reverse().find((entry) => entry.level === "error");
      return latest?.text ?? "A Gemini API key is required to connect.";
    }
    return null;
  }, [state.fatal, state.socket, state.state, state.logs, configured, requiredSetup]);

  const retryBootstrap = useCallback(() => {
    void aze.retryBootstrap();
  }, [aze.retryBootstrap]);

  const enterInterface = useCallback(() => {
    try {
      window.sessionStorage.setItem("aze.initialized", "1");
    } catch {
      // Session storage may be unavailable; entering still works.
    }
    setEntered(true);
  }, []);

  const openSetup = useCallback(() => setSetupOpen(true), []);
  const openSettings = useCallback(() => {
    setSettingsOpen(true);
    // The bootstrap snapshot only carries basic settings; device lists,
    // volume and the full settings shape come from GET /api/settings.
    // Without this, a fresh page opens the dialog with no data and leaves
    // the user on the "could not be loaded" card.
    void aze.loadSettings();
  }, [aze.loadSettings]);

  const toggleSidebar = useCallback(() => {
    if (window.matchMedia("(min-width: 1280px)").matches) {
      setSidebarCollapsed((value) => !value);
    } else {
      setDrawerOpen((value) => !value);
    }
  }, []);

  const openBay = useCallback((id: string) => {
    setDrawerOpen(false);
    if (id === "agent_run") {
      setMemoryOpen(false);
      setSkillsOpen(false);
      setAgentOpen(true);
      return;
    }
    setAgentOpen(false);
    if (id === "memory") {
      setSkillsOpen(false);
      setMemoryOpen(true);
      return;
    }
    if (id === "skills") {
      setMemoryOpen(false);
      setSkillsOpen(true);
      return;
    }
    setMemoryOpen(false);
    setSkillsOpen(false);
    setOpenBays((current) =>
      current.includes(id) ? current : [...current, id],
    );
    setActiveBay(id);
  }, []);

  const closeBayTab = useCallback((id: string) => {
    setOpenBays((current) => {
      if (id === "__collapse__") {
        setActiveBay(null);
        return current;
      }
      const next = current.filter((item) => item !== id);
      setActiveBay((active) =>
        active === id ? next.at(-1) ?? null : active,
      );
      return next;
    });
  }, []);

  const activeBayModule = useMemo(() => {
    if (!activeBay) return null;
    return (
      state.modules.find((module) => module.id === activeBay) ??
      moduleFallback(activeBay)
    );
  }, [activeBay, state.modules]);

  const bayTabs = useMemo(
    () =>
      openBays.map((id) => ({
        id,
        label: SIDEBAR_NAV.find((item) => item.id === id)?.label ?? id,
      })),
    [openBays],
  );

  const activeNav = agentOpen
    ? "agent_run"
    : memoryOpen
      ? "memory"
      : skillsOpen
        ? "skills"
        : activeBay;

  const researchOpen = Boolean(
    state.research.open &&
      state.research.job_id &&
      state.research.job_id !== dismissedResearch,
  );

  if (!state.boot && (state.loading || state.fatal)) {
    return <BootScreen error={state.fatal} onRetry={retryBootstrap} />;
  }

  if (!entered) {
    return (
      <>
        <InitialiseGate
          setup={state.setup}
          onEnter={enterInterface}
          onOpenSetup={openSetup}
        />
        <SetupDialog
          open={setupDialogOpen}
          required={requiredSetup}
          setup={state.setup}
          platform={state.boot?.app.platform ?? ""}
          onClose={() => setSetupOpen(false)}
          onSave={aze.saveApiKey}
        />
        <Toasts
          toasts={state.toasts}
          onDismiss={aze.dismissToast}
          onSnooze={aze.snoozeNotification}
        />
      </>
    );
  }

  return (
    <>
      <div className="aze-app">
        <Header
          assistantName={state.assistantName}
          state={state.state}
          socket={state.socket}
          sidebarCollapsed={sidebarCollapsed}
          drawerOpen={drawerOpen}
          onToggleSidebar={toggleSidebar}
          onOpenSettings={openSettings}
        />

        {state.fatal ? (
          <div className="aze-fatal">
            <p>{state.fatal}</p>
            <Button variant="quiet" size="sm" onClick={retryBootstrap}>
              RETRY
            </Button>
          </div>
        ) : null}

        <main className="aze-main">
          <Sidebar
            modules={state.modules}
            collapsed={sidebarCollapsed}
            onToggleCollapsed={() => setSidebarCollapsed((value) => !value)}
            drawer={false}
            activeBay={activeNav}
            onOpenBay={openBay}
            onOpenSettings={openSettings}
          />

          <div className="aze-center">
            <div
              className="aze-stage"
              data-youtube-open={state.youtube.open ? "true" : "false"}
            >
              <HaloStage
                state={state.state}
                subscribeAudio={aze.subscribeAudio}
                activeTools={activeTools}
                muted={state.muted}
                configured={configured}
                setupRequired={requiredSetup}
                awaitingReply={awaitingReply}
                streamingReply={streamingReply}
                error={error}
                accent={state.settings?.ui_color ?? DEFAULT_ACCENT}
                glowAlways
                suspended={state.youtube.open}
              />

              <YouTubePlayerPanel
                open={state.youtube.open}
                closing={state.youtube.closing}
                video={state.youtube.video}
                autoplay={state.youtube.autoplay}
                startSeconds={state.youtube.startSeconds}
                loadKey={state.youtube.loadKey}
                loadRequestId={state.youtube.loadRequestId}
                closeRequestId={state.youtube.closeRequestId}
                command={state.youtube.command}
                socketOnline={state.socket === "online"}
                onState={aze.youtubeSendState}
                onReady={aze.youtubeReady}
                onError={aze.youtubeError}
                onResult={aze.youtubeCommandResult}
                onCommandSettled={aze.youtubeCommandSettled}
                onLoadSettled={aze.youtubeLoadSettled}
                onRequestClose={aze.youtubeRequestClose}
                onCloseSettled={aze.youtubeSettleClose}
              />
            </div>

            <div
              className={cn(
                "aze-transcript",
                transcriptOpen && "aze-transcript--open",
              )}
            >
              <ConversationPanel
                messages={state.messages}
                logs={state.logs}
                assistantName={state.assistantName}
                awaitingReply={awaitingReply}
                dockOpen={transcriptOpen}
                onToggleDock={() => setTranscriptOpen((value) => !value)}
                footer={
                  <Composer
                    state={state.state}
                    muted={state.muted}
                    pttEnabled={state.settings?.push_to_talk_enabled ?? false}
                    onSend={aze.sendText}
                    onInterrupt={aze.interrupt}
                    onToggleMute={aze.toggleMute}
                    onPttHold={aze.setPttHeld}
                  />
                }
              />
            </div>
          </div>
        </main>
      </div>

      <ScreenObservationIndicator observation={state.observation} />

      {drawerOpen ? (
        <div className="aze-drawer" role="dialog" aria-label="Modules">
          <button
            type="button"
            aria-label="Close module rail"
            className="aze-drawer-backdrop"
            onClick={() => setDrawerOpen(false)}
          />
          <div className="aze-drawer-panel aze-drawer-panel--left">
            <Sidebar
              modules={state.modules}
              collapsed={false}
              onToggleCollapsed={() => setSidebarCollapsed((value) => !value)}
              drawer
              activeBay={activeNav}
              onOpenBay={openBay}
              onOpenSettings={openSettings}
              onClose={() => setDrawerOpen(false)}
            />
          </div>
        </div>
      ) : null}

      {activeBay && activeBayModule ? (
        <BayView
          module={activeBayModule}
          label={activeBayModule.name}
          description={activeBayModule.description}
          tools={state.tools}
          categories={state.toolCategories}
          onSelectTools={aze.setToolSelection}
          tabs={bayTabs}
          activeTab={activeBay}
          onSwitch={setActiveBay}
          onCloseTab={closeBayTab}
          onScan={aze.scanModule}
        />
      ) : null}

      {agentOpen ? (
        <AgentPanel
          run={state.agentRun}
          toolEvents={state.toolEvents}
          onClose={() => setAgentOpen(false)}
        />
      ) : null}

      {memoryOpen ? <MemoryView onClose={() => setMemoryOpen(false)} /> : null}

      {skillsOpen ? (
        <SkillsView aze={aze} onClose={() => setSkillsOpen(false)} />
      ) : null}

      {researchOpen ? (
        <ResearchPanel
          research={state.research}
          onClose={() => setDismissedResearch(state.research.job_id)}
        />
      ) : null}

      <SettingsDialog
        open={settingsOpen}
        settings={state.fullSettings}
        loading={state.settingsLoading}
        setup={state.setup}
        platform={state.boot?.app.platform ?? ""}
        audioTestLevel={state.audioTestLevel}
        audioTestKind={state.audioTestKind}
        onClose={() => setSettingsOpen(false)}
        onLoad={aze.loadSettings}
        onSave={aze.saveSettings}
        onOpenSetup={openSetup}
        onReconnect={aze.reconnect}
        onTestAudio={aze.testAudio}
        onSetVolume={aze.setVolume}
        onGetVolume={aze.getVolume}
        onRefreshAudio={aze.refreshAudioDevices}
        onPreviewAccent={applyAccent}
      />

      <SetupDialog
        open={setupDialogOpen}
        required={requiredSetup}
        setup={state.setup}
        platform={state.boot?.app.platform ?? ""}
        onClose={() => setSetupOpen(false)}
        onSave={aze.saveApiKey}
      />

      <ApprovalDialog
        approvals={state.approvals}
        onApprove={aze.approveAction}
        onReject={aze.rejectAction}
      />

      <Toasts
        toasts={state.toasts}
        onDismiss={aze.dismissToast}
        onSnooze={aze.snoozeNotification}
      />

      {state.shuttingDown ? (
        <ShutdownScreen socket={state.socket} reason={state.shutdownReason} />
      ) : null}
    </>
  );
}
