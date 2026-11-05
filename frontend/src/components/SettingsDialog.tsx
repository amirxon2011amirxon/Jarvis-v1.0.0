import { memo, useEffect, useRef, useState, type ReactNode } from "react";

import type {
  AudioTestResult,
  FullSettings,
  SettingsPatch,
  SetupStatus,
} from "../types";
import { setMotionPrefs, useMotionPrefs, type MotionQuality } from "../lib/motion";
import { cn, isValidAccent } from "../lib/utils";
import { Button } from "./ui/Button";
import { Modal } from "./ui/Modal";
import { Segmented } from "./ui/Segmented";
import { Toggle } from "./ui/Toggle";

interface SettingsDialogProps {
  open: boolean;
  settings: FullSettings | null;
  loading: boolean;
  setup: SetupStatus;
  platform: string;
  audioTestLevel: number;
  audioTestKind: "input" | "output" | null;
  onClose: () => void;
  onLoad: () => Promise<void>;
  onSave: (patch: SettingsPatch) => Promise<FullSettings>;
  onOpenSetup: () => void;
  onReconnect: () => Promise<void>;
  onTestAudio: (kind: "input" | "output", device: string) => Promise<AudioTestResult>;
  onSetVolume: (volume: number, device: string) => Promise<boolean>;
  onGetVolume: (device: string) => Promise<number | null>;
  onRefreshAudio: () => Promise<void>;
  onPreviewAccent: (color: string) => void;
}

type Tab =
  | "identity"
  | "theme"
  | "audio"
  | "behavior"
  | "motion"
  | "advanced"
  | "link";

export type SettingsTab = Tab;

const SETTINGS_TABS: Tab[] = [
  "identity",
  "theme",
  "audio",
  "behavior",
  "motion",
  "advanced",
  "link",
];

export function isSettingsTab(value: unknown): value is SettingsTab {
  return SETTINGS_TABS.includes(String(value) as Tab);
}

const TABS: Array<{ id: Tab; label: string; code: string }> = [
  { id: "identity", label: "IDENTITY", code: "01" },
  { id: "theme", label: "APPEARANCE", code: "02" },
  { id: "audio", label: "AUDIO I/O", code: "03" },
  { id: "behavior", label: "BEHAVIOR", code: "04" },
  { id: "motion", label: "MOTION", code: "05" },
  { id: "advanced", label: "LINK PROFILE", code: "06" },
  { id: "link", label: "GEMINI LINK", code: "07" },
];

const QUALITY_OPTIONS = [
  { value: "low" as MotionQuality, label: "LOW" },
  { value: "balanced" as MotionQuality, label: "BALANCED" },
  { value: "high" as MotionQuality, label: "HIGH" },
];

const INPUT =
  "h-9 w-full rounded-[3px] border border-[var(--line-strong)] bg-[var(--void)] px-2.5 font-mono text-[12px] text-[var(--text)] outline-none transition-colors placeholder:text-[var(--dim)] focus:border-[var(--accent)]";
const LABEL =
  "font-mono text-[8px] font-semibold uppercase tracking-[0.16em] text-[var(--muted)]";

function Card({
  code,
  title,
  description,
  children,
}: {
  code: string;
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <section className="border border-[var(--line)] border-l-2 border-l-[var(--accent)]/50 bg-[var(--panel-2)]/55 p-4">
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-[8px] tracking-[0.14em] text-[var(--accent)]/70">
          {code}
        </span>
        <h3 className="font-mono text-[11px] font-semibold uppercase tracking-[0.16em] text-[var(--text)]">
          {title}
        </h3>
      </div>
      <p className="mt-1 text-[11px] leading-5 text-[var(--muted)]">{description}</p>
      <div className="mt-4 space-y-3">{children}</div>
    </section>
  );
}

function IdentitySection({
  settings,
  onSave,
}: {
  settings: FullSettings;
  onSave: (patch: SettingsPatch) => Promise<FullSettings>;
}) {
  const [name, setName] = useState(settings.assistant_name);
  const [user, setUser] = useState(settings.user_name);
  const [voice, setVoice] = useState(settings.voice_name);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setName(settings.assistant_name);
    setUser(settings.user_name);
    setVoice(settings.voice_name);
  }, [settings.assistant_name, settings.user_name, settings.voice_name]);

  const commit = async () => {
    setSaving(true);
    setError("");
    try {
      await onSave({
        assistant_name: name.trim() || "Jarvis",
        user_name: user.trim(),
        voice_name: voice,
      });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save identity.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card
      code="01"
      title="Identity profile"
      description="The assistant name and operator name are read by the existing system prompt on the next connection. A voice change reconnects with a fresh conversation, exactly as the desktop build did."
    >
      <div>
        <label className={LABEL} htmlFor="assistant-name">
          Assistant name
        </label>
        <input
          id="assistant-name"
          className={cn(INPUT, "mt-1.5")}
          value={name}
          maxLength={24}
          onChange={(event) => setName(event.target.value)}
        />
      </div>
      <div>
        <label className={LABEL} htmlFor="operator-name">
          Operator name
        </label>
        <input
          id="operator-name"
          className={cn(INPUT, "mt-1.5")}
          value={user}
          maxLength={32}
          placeholder="Optional"
          onChange={(event) => setUser(event.target.value)}
        />
      </div>
      <div>
        <label className={LABEL} htmlFor="voice-name">
          Gemini voice
        </label>
        <select
          id="voice-name"
          className={cn(INPUT, "mt-1.5")}
          value={voice}
          onChange={(event) => setVoice(event.target.value)}
        >
          {settings.voices.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </div>
      {error ? (
        <p className="font-mono text-[9px] leading-5 text-[var(--danger)]" role="alert">
          {error}
        </p>
      ) : null}
      <Button variant="primary" size="md" onClick={() => void commit()} disabled={saving}>
        {saving ? "COMMITTING…" : "COMMIT IDENTITY"}
      </Button>
    </Card>
  );
}

function AppearanceSection({
  settings,
  onSave,
  onPreviewAccent,
}: {
  settings: FullSettings;
  onSave: (patch: SettingsPatch) => Promise<FullSettings>;
  onPreviewAccent: (color: string) => void;
}) {
  const [color, setColor] = useState(settings.ui_color);
  const [location, setLocation] = useState(settings.weather_location ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setColor(settings.ui_color);
    setLocation(settings.weather_location ?? "");
  }, [settings.ui_color, settings.weather_location]);

  const preview = (value: string) => {
    setColor(value);
    if (isValidAccent(value)) onPreviewAccent(value);
  };

  const commit = async () => {
    if (!isValidAccent(color)) {
      setError("Use a #RRGGBB colour value.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      await onSave({
        ui_color: color.toLowerCase(),
        weather_location: location.trim(),
      });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save theme.");
    } finally {
      setSaving(false);
    }
  };

  const presets = [
    "#d8a63a",
    "#f1c15a",
    "#b98224",
    "#e0b85c",
    "#ffd77a",
    "#8f6a20",
    "#fff1c6",
  ];

  return (
    <Card
      code="02"
      title="Appearance matrix"
      description="The saved accent is applied to the interface immediately — panels, halo particles and indicators all follow it."
    >
      <div>
        <p className={LABEL}>Free color picker</p>
        <div className="mt-2 flex items-center gap-2">
          <input
            type="color"
            value={isValidAccent(color) ? color : "#d8a63a"}
            onChange={(event) => preview(event.target.value)}
            className="h-10 w-16 cursor-pointer rounded-[4px] border border-[var(--line-strong)] bg-[var(--void)] p-1"
            aria-label="Free accent colour picker"
          />
          <input
            value={color}
            onChange={(event) => preview(event.target.value)}
            maxLength={7}
            spellCheck={false}
            className={cn(INPUT, "flex-1")}
            aria-label="Accent hex value"
            placeholder="#RRGGBB"
          />
        </div>
        <p className="mt-1 font-mono text-[8px] uppercase tracking-[0.1em] text-[var(--dim)]">
          ANY COLOUR — APPLIED LIVE AS YOU PICK
        </p>
      </div>
      <div>
        <p className={LABEL}>Or pick a preset</p>
        <div className="mt-2 flex flex-wrap gap-2">
          {presets.map((preset) => (
            <button
              key={preset}
              type="button"
              onClick={() => preview(preset)}
              aria-label={`Use accent ${preset}`}
              aria-pressed={color.toLowerCase() === preset}
              className={cn(
                "h-7 w-7 rounded-[2px] border-2 transition-transform hover:scale-110 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]",
                color.toLowerCase() === preset
                  ? "border-[var(--text)]"
                  : "border-[var(--line-strong)]",
              )}
              style={{ background: preset }}
            />
          ))}
        </div>
      </div>
      <div>
        <label className={LABEL} htmlFor="weather-location">
          Weather location
        </label>
        <input
          id="weather-location"
          className={cn(INPUT, "mt-1.5")}
          value={location}
          maxLength={80}
          placeholder="Empty = Tashkent (default feed)"
          onChange={(event) => setLocation(event.target.value)}
        />
        <p className="mt-1 font-mono text-[8px] uppercase tracking-[0.1em] text-[var(--dim)]">
          A CITY NAME; RESOLVED THROUGH THE OPEN-METEO GEOCODER
        </p>
      </div>
      {error ? (
        <p className="font-mono text-[9px] leading-5 text-[var(--danger)]" role="alert">
          {error}
        </p>
      ) : null}
      <Button variant="primary" size="md" onClick={() => void commit()} disabled={saving}>
        {saving ? "APPLYING…" : "APPLY THEME"}
      </Button>
    </Card>
  );
}

function AudioSection({
  settings,
  audioTestLevel,
  audioTestKind,
  onSave,
  onTestAudio,
  onSetVolume,
  onGetVolume,
  onRefreshAudio,
}: {
  settings: FullSettings;
  audioTestLevel: number;
  audioTestKind: "input" | "output" | null;
  onSave: (patch: SettingsPatch) => Promise<FullSettings>;
  onTestAudio: (kind: "input" | "output", device: string) => Promise<AudioTestResult>;
  onSetVolume: (volume: number, device: string) => Promise<boolean>;
  onGetVolume: (device: string) => Promise<number | null>;
  onRefreshAudio: () => Promise<void>;
}) {
  const [input, setInput] = useState(settings.input_device);
  const [output, setOutput] = useState(settings.output_device);
  const outputRef = useRef(settings.output_device);
  const [volume, setVolume] = useState(settings.output_volume ?? 50);
  const [volumeAvailable, setVolumeAvailable] = useState(
    settings.output_volume !== null,
  );
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState<"input" | "output" | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    setInput(settings.input_device);
    setOutput(settings.output_device);
    outputRef.current = settings.output_device;
    setVolume(settings.output_volume ?? 50);
    setVolumeAvailable(settings.output_volume !== null);
  }, [settings.input_device, settings.output_device, settings.output_volume]);

  const inputOptions =
    input && !settings.input_devices.includes(input)
      ? [...settings.input_devices, input]
      : settings.input_devices;
  const outputOptions =
    output && !settings.output_devices.includes(output)
      ? [...settings.output_devices, output]
      : settings.output_devices;

  const apply = async () => {
    setSaving(true);
    setError("");
    try {
      await onSave({ input_device: input, output_device: output });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not apply routing.");
    } finally {
      setSaving(false);
    }
  };

  const test = async (kind: "input" | "output") => {
    setTesting(kind);
    setResult(null);
    setError("");
    try {
      const response = await onTestAudio(
        kind,
        kind === "input" ? input : output,
      );
      setResult({ ok: response.ok, text: response.verdict });
    } catch (cause) {
      setResult({
        ok: false,
        text: cause instanceof Error ? cause.message : "The test failed.",
      });
    } finally {
      setTesting(null);
    }
  };

  const commitVolume = async () => {
    const ok = await onSetVolume(volume, output);
    if (!ok) setError("Output level could not be set.");
  };

  return (
    <Card
      code="03"
      title="Audio I/O"
      description="Devices are stored by name, not index, so a USB headset can come and go without silently pointing the setting at different hardware. Applying a device reconnects and keeps the conversation."
    >
      <div>
        <label className={LABEL} htmlFor="input-device">
          Microphone
        </label>
        <select
          id="input-device"
          className={cn(INPUT, "mt-1.5")}
          value={input}
          onChange={(event) => setInput(event.target.value)}
        >
          <option value="">System default</option>
          {inputOptions.map((name) => (
            <option key={name} value={name}>
              {name}
              {!settings.input_devices.includes(name) ? " (not connected)" : ""}
            </option>
          ))}
        </select>
        <div className="mt-2 flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => void test("input")}
            disabled={testing !== null}
          >
            {testing === "input" ? "RECORDING…" : "TEST INPUT"}
          </Button>
          <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-[var(--void)]">
            <div
              className="h-full bg-[var(--accent)] transition-[width] duration-100"
              style={{
                width: `${Math.round((audioTestKind === "input" ? audioTestLevel : 0) * 100)}%`,
              }}
            />
          </div>
        </div>
      </div>

      <div>
        <label className={LABEL} htmlFor="output-device">
          Speakers
        </label>
        <select
          id="output-device"
          className={cn(INPUT, "mt-1.5")}
          value={output}
          onChange={(event) => {
            const next = event.target.value;
            setOutput(next);
            // A volume read for the previous device may still be in flight.
            // Only the response for the current selection may move this slider.
            outputRef.current = next;
            setError("");
            void onGetVolume(next).then((value) => {
              if (outputRef.current !== next) return;
              if (value === null) {
                setVolumeAvailable(false);
              } else {
                setVolumeAvailable(true);
                setVolume(value);
              }
            });
          }}
        >
          <option value="">System default</option>
          {outputOptions.map((name) => (
            <option key={name} value={name}>
              {name}
              {!settings.output_devices.includes(name) ? " (not connected)" : ""}
            </option>
          ))}
        </select>
        <div className="mt-2 flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => void test("output")}
            disabled={testing !== null}
          >
            {testing === "output" ? "PLAYING…" : "TEST OUTPUT"}
          </Button>
          <Button variant="quiet" size="sm" onClick={() => void onRefreshAudio()}>
            RESCAN DEVICES
          </Button>
        </div>
      </div>

      <div>
        <div className="flex items-center justify-between">
          <label className={LABEL} htmlFor="output-volume">
            Output level
          </label>
          <span className="font-mono text-[9px] text-[var(--accent)]">
            {volumeAvailable ? `${volume}%` : "N/A"}
          </span>
        </div>
        <input
          id="output-volume"
          type="range"
          min={0}
          max={100}
          step={1}
          value={volume}
          disabled={!volumeAvailable}
          onChange={(event) => setVolume(Number(event.target.value))}
          onPointerUp={() => void commitVolume()}
          onKeyUp={() => void commitVolume()}
          onBlur={() => void commitVolume()}
          className="mt-2 h-1.5 w-full cursor-pointer appearance-none rounded-full bg-[var(--line)] accent-[var(--accent)] disabled:cursor-not-allowed disabled:opacity-40"
        />
        {!volumeAvailable ? (
          <p className="mt-1 font-mono text-[8px] uppercase tracking-[0.1em] text-[var(--dim)]">
            The host does not expose a per-device volume here.
          </p>
        ) : null}
      </div>

      {result ? (
        <p
          className={cn(
            "border px-2.5 py-2 font-mono text-[9px] leading-5",
            result.ok
              ? "border-[var(--ok)]/35 bg-[var(--ok)]/[0.04] text-[var(--ok)]"
              : "border-[var(--warn)]/35 bg-[var(--warn)]/[0.04] text-[var(--warn)]",
          )}
          role="status"
        >
          {result.text}
        </p>
      ) : null}
      {error ? (
        <p className="font-mono text-[9px] leading-5 text-[var(--danger)]" role="alert">
          {error}
        </p>
      ) : null}

      <Button variant="primary" size="md" onClick={() => void apply()} disabled={saving}>
        {saving ? "APPLYING…" : "APPLY ROUTING"}
      </Button>
    </Card>
  );
}

function BehaviorSection({
  settings,
  onSave,
}: {
  settings: FullSettings;
  onSave: (patch: SettingsPatch) => Promise<FullSettings>;
}) {
  const [proactive, setProactive] = useState(settings.proactive_audio_enabled);
  const [thinking, setThinking] = useState(settings.thinking_enabled);
  const [ptt, setPtt] = useState(settings.push_to_talk_enabled);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setProactive(settings.proactive_audio_enabled);
    setThinking(settings.thinking_enabled);
    setPtt(settings.push_to_talk_enabled);
  }, [
    settings.proactive_audio_enabled,
    settings.thinking_enabled,
    settings.push_to_talk_enabled,
  ]);

  const saveBehavior = async (nextProactive: boolean, nextThinking: boolean) => {
    setBusy(true);
    setError("");
    try {
      await onSave({
        proactive_audio_enabled: nextProactive,
        thinking_enabled: nextThinking,
      });
    } catch (cause) {
      setProactive(settings.proactive_audio_enabled);
      setThinking(settings.thinking_enabled);
      setError(cause instanceof Error ? cause.message : "Could not save behavior.");
    } finally {
      setBusy(false);
    }
  };

  const savePtt = async (next: boolean) => {
    setBusy(true);
    setError("");
    const previous = ptt;
    setPtt(next);
    try {
      await onSave({ push_to_talk_enabled: next });
    } catch (cause) {
      setPtt(previous);
      setError(cause instanceof Error ? cause.message : "Could not change push-to-talk.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card
      code="04"
      title="Behavior"
      description="These switches rebuild the Gemini Live link when changed. Push-to-talk also works from the footer button while the web interface is focused; the global Ctrl+Space hotkey remains available."
    >
      <Toggle
        label="Proactive audio"
        description="Let the model decide when an utterance is meant for it."
        checked={proactive}
        disabled={busy}
        onChange={(value) => {
          setProactive(value);
          void saveBehavior(value, thinking);
        }}
      />
      <Toggle
        label="Thinking mode"
        description="Extended reasoning before speech."
        checked={thinking}
        disabled={busy}
        onChange={(value) => {
          setThinking(value);
          void saveBehavior(proactive, value);
        }}
      />
      <Toggle
        label="Push to talk"
        description="Hold Ctrl+Space, or the HOLD PTT button, to transmit."
        checked={ptt}
        disabled={busy}
        onChange={(value) => void savePtt(value)}
      />
      {error ? (
        <p className="font-mono text-[9px] leading-5 text-[var(--danger)]" role="alert">
          {error}
        </p>
      ) : null}
      <p className="font-mono text-[8px] uppercase tracking-[0.12em] text-[var(--dim)]">
        BEHAVIOR CHANGES REBUILD THE LIVE LINK
      </p>
    </Card>
  );
}

function MotionSection() {
  const prefs = useMotionPrefs();

  return (
    <Card
      code="05"
      title="Motion design"
      description="How the interface moves. Quality scales decorative detail in the particle halo and panels; reduced motion keeps state changes instant and legible. These preferences apply to this browser only and never touch backend settings."
    >
      <div>
        <p className={LABEL}>Animation quality</p>
        <Segmented
          className="mt-2"
          ariaLabel="Animation quality"
          value={prefs.quality}
          options={QUALITY_OPTIONS}
          onChange={(quality) => setMotionPrefs({ quality })}
        />
        <p className="mt-1.5 font-mono text-[8px] uppercase tracking-[0.1em] text-[var(--dim)]">
          LOW TRIMS LOOPS AND DUST · HIGH ADDS DEPTH
        </p>
      </div>
      <Toggle
        label="Reduce motion"
        description={
          prefs.reducedActive && !prefs.reduced
            ? "Currently on via your operating system accessibility setting."
            : "Minimise movement; state changes resolve without animation. The OS reduced-motion preference is always honoured."
        }
        checked={prefs.reduced}
        onChange={(reduced) => setMotionPrefs({ reduced })}
      />
      <Toggle
        label="Particle effects"
        description="Dust rings, orbit motes and tracer glints around the halo."
        checked={prefs.particles}
        onChange={(particles) => setMotionPrefs({ particles })}
      />
      <Toggle
        label="Background ambience"
        description="Slow page glow and idle float vignettes."
        checked={prefs.ambience}
        onChange={(ambience) => setMotionPrefs({ ambience })}
      />
      <p className="font-mono text-[8px] uppercase tracking-[0.12em] text-[var(--dim)]">
        SAVED PER DEVICE IN THIS BROWSER // NOT SENT TO THE SERVER
      </p>
    </Card>
  );
}

function AdvancedSection({
  settings,
  onSave,
}: {
  settings: FullSettings;
  onSave: (patch: SettingsPatch) => Promise<FullSettings>;
}) {
  const tuning = settings.turn_tuning;
  const [enabled, setEnabled] = useState(tuning.enabled);
  const [silence, setSilence] = useState(tuning.silence_ms);
  const [prefix, setPrefix] = useState(tuning.prefix_ms);
  const [startSens, setStartSens] = useState(tuning.start_sensitivity);
  const [endSens, setEndSens] = useState(tuning.end_sensitivity);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setEnabled(settings.turn_tuning.enabled);
    setSilence(settings.turn_tuning.silence_ms);
    setPrefix(settings.turn_tuning.prefix_ms);
    setStartSens(settings.turn_tuning.start_sensitivity);
    setEndSens(settings.turn_tuning.end_sensitivity);
  }, [settings.turn_tuning]);

  const commit = async () => {
    setSaving(true);
    setError("");
    try {
      await onSave({
        turn_tuning: {
          enabled,
          silence_ms: Math.max(200, Math.min(3000, silence)),
          prefix_ms: Math.max(0, Math.min(1000, prefix)),
          start_sensitivity: startSens,
          end_sensitivity: endSens,
        },
      });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save the link profile.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card
      code="05"
      title="Advanced link profile"
      description="Server-side voice activity detection settings. These are passed to the existing Live session configuration on reconnect."
    >
      <Toggle
        label="Smart turn detection"
        description="Server VAD with a tuned silence window."
        checked={enabled}
        onChange={setEnabled}
      />
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className={LABEL} htmlFor="silence-ms">
            Silence window
          </label>
          <div className="relative mt-1.5">
            <input
              id="silence-ms"
              type="number"
              min={200}
              max={3000}
              step={50}
              value={silence}
              disabled={!enabled}
              onChange={(event) => setSilence(Number(event.target.value))}
              className={cn(INPUT, "pr-10 disabled:opacity-40")}
            />
            <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 font-mono text-[9px] text-[var(--dim)]">
              MS
            </span>
          </div>
        </div>
        <div>
          <label className={LABEL} htmlFor="prefix-ms">
            Prefix padding
          </label>
          <div className="relative mt-1.5">
            <input
              id="prefix-ms"
              type="number"
              min={0}
              max={1000}
              step={25}
              value={prefix}
              disabled={!enabled}
              onChange={(event) => setPrefix(Number(event.target.value))}
              className={cn(INPUT, "pr-10 disabled:opacity-40")}
            />
            <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 font-mono text-[9px] text-[var(--dim)]">
              MS
            </span>
          </div>
        </div>
        <div>
          <label className={LABEL} htmlFor="start-sens">
            Start sensitivity
          </label>
          <select
            id="start-sens"
            value={startSens}
            disabled={!enabled}
            onChange={(event) =>
              setStartSens(event.target.value as typeof startSens)
            }
            className={cn(INPUT, "mt-1.5 disabled:opacity-40")}
          >
            <option value="low">LOW</option>
            <option value="default">DEFAULT</option>
            <option value="high">HIGH</option>
          </select>
        </div>
        <div>
          <label className={LABEL} htmlFor="end-sens">
            End sensitivity
          </label>
          <select
            id="end-sens"
            value={endSens}
            disabled={!enabled}
            onChange={(event) => setEndSens(event.target.value as typeof endSens)}
            className={cn(INPUT, "mt-1.5 disabled:opacity-40")}
          >
            <option value="low">LOW</option>
            <option value="default">DEFAULT</option>
            <option value="high">HIGH</option>
          </select>
        </div>
      </div>
      {error ? (
        <p className="font-mono text-[9px] leading-5 text-[var(--danger)]" role="alert">
          {error}
        </p>
      ) : null}
      <Button variant="primary" size="md" onClick={() => void commit()} disabled={saving}>
        {saving ? "COMMITTING…" : "COMMIT LINK PROFILE"}
      </Button>
    </Card>
  );
}

function LinkSection({
  setup,
  platform,
  onOpenSetup,
  onReconnect,
}: {
  setup: SetupStatus;
  platform: string;
  onOpenSetup: () => void;
  onReconnect: () => Promise<void>;
}) {
  return (
    <Card
      code="06"
      title="Gemini link"
      description="Gemini Live is the only conversation engine in this build. The API key is stored in config/api_keys.json and is never sent back to the browser."
    >
      <div className="grid grid-cols-2 gap-3">
        <div className="border border-[var(--line)] bg-[var(--panel)]/70 p-3">
          <p className={LABEL}>Key</p>
          <p className="mt-1 font-mono text-[10px] text-[var(--text)]">
            {setup.key_masked || "NOT CONFIGURED"}
          </p>
        </div>
        <div className="border border-[var(--line)] bg-[var(--panel)]/70 p-3">
          <p className={LABEL}>State</p>
          <p
            className={cn(
              "mt-1 font-mono text-[10px]",
              setup.configured ? "text-[var(--ok)]" : "text-[var(--danger)]",
            )}
          >
            {setup.configured ? "KEY PRESENT // GEMINI LIVE" : "OFFLINE // KEY REQUIRED"}
          </p>
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" size="md" onClick={onOpenSetup}>
          UPDATE KEY
        </Button>
        <Button variant="ghost" size="md" onClick={() => void onReconnect()}>
          RECONNECT
        </Button>
      </div>
      <p className="font-mono text-[8px] uppercase tracking-[0.12em] text-[var(--dim)]">
        PLATFORM {platform || "UNKNOWN"} // LOCAL CONTROL PLANE
      </p>
    </Card>
  );
}

export function SettingsContent({
  settings,
  loading,
  setup,
  platform,
  audioTestLevel,
  audioTestKind,
  onLoad,
  onSave,
  onOpenSetup,
  onReconnect,
  onTestAudio,
  onSetVolume,
  onGetVolume,
  onRefreshAudio,
  onPreviewAccent,
  initialTab = "identity",
  onTabChange,
}: Omit<SettingsDialogProps, "open" | "onClose"> & {
  initialTab?: Tab;
  onTabChange?: (tab: Tab) => void;
}) {
  const [tab, setTab] = useState<Tab>(initialTab);

  if (loading && !settings) {
    return (
      <div className="space-y-3" aria-label="Loading settings">
        {[1, 2, 3].map((item) => (
          <div
            key={item}
            className="h-24 animate-pulse border border-[var(--line)] bg-[var(--panel-2)]/50"
          />
        ))}
      </div>
    );
  }

  if (!settings) {
    return (
      <div className="border border-[var(--danger)]/40 bg-[var(--danger)]/[0.05] p-4">
        <p className="font-mono text-[10px] uppercase tracking-[0.12em] text-[var(--danger)]">
          SETTINGS COULD NOT BE LOADED
        </p>
        <Button variant="ghost" size="sm" className="mt-3" onClick={() => void onLoad()}>
          RETRY
        </Button>
      </div>
    );
  }

  return (
    <div>
      <div className="grid gap-4 md:grid-cols-[178px_minmax(0,1fr)]">
        <nav
          className="flex gap-1 overflow-x-auto md:flex-col md:overflow-visible"
          aria-label="Settings sections"
        >
          {TABS.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => {
                setTab(item.id);
                onTabChange?.(item.id);
              }}
              aria-current={tab === item.id ? "page" : undefined}
              className={cn(
                "flex shrink-0 items-center gap-2 whitespace-nowrap border px-2.5 py-2 text-left font-mono text-[9px] font-semibold uppercase tracking-[0.1em] transition-colors",
                "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]",
                tab === item.id
                  ? "border-[var(--accent)]/60 bg-[var(--accent)]/[0.07] text-[var(--accent)]"
                  : "border-transparent text-[var(--muted)] hover:border-[var(--line)] hover:text-[var(--text)]",
              )}
            >
              <span className="text-[7px] text-[var(--dim)]">{item.code}</span>
              {item.label}
            </button>
          ))}
        </nav>

        <div className="min-w-0">
          {tab === "identity" ? (
            <IdentitySection settings={settings} onSave={onSave} />
          ) : null}
          {tab === "theme" ? (
            <AppearanceSection
              settings={settings}
              onSave={onSave}
              onPreviewAccent={onPreviewAccent}
            />
          ) : null}
          {tab === "audio" ? (
            <AudioSection
              settings={settings}
              audioTestLevel={audioTestLevel}
              audioTestKind={audioTestKind}
              onSave={onSave}
              onTestAudio={onTestAudio}
              onSetVolume={onSetVolume}
              onGetVolume={onGetVolume}
              onRefreshAudio={onRefreshAudio}
            />
          ) : null}
          {tab === "behavior" ? (
            <BehaviorSection settings={settings} onSave={onSave} />
          ) : null}
          {tab === "motion" ? <MotionSection /> : null}
          {tab === "advanced" ? (
            <AdvancedSection settings={settings} onSave={onSave} />
          ) : null}
          {tab === "link" ? (
            <LinkSection
              setup={setup}
              platform={platform}
              onOpenSetup={onOpenSetup}
              onReconnect={onReconnect}
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}

export const SettingsDialog = memo(function SettingsDialog({
  open,
  onClose,
  ...contentProps
}: SettingsDialogProps) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="System configuration"
      eyebrow="J.A.R.V.I.S. // LOCAL CONTROL PLANE"
      wide
      footer={
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <p className="font-mono text-[8px] uppercase tracking-[0.12em] text-[var(--dim)]">
            WRITES GO THROUGH config/api_keys.json · RECONNECTS ONLY WHEN REQUIRED
          </p>
          <p className="font-mono text-[8px] uppercase tracking-[0.12em] text-[var(--muted)]">
            {contentProps.setup.configured ? "GEMINI KEY PRESENT" : "GEMINI NOT CONFIGURED"}
          </p>
        </div>
      }
    >
      <SettingsContent {...contentProps} />
    </Modal>
  );
});

