# Jarvis v1.0.0

A local, voice-first AI assistant. One conversation, two ways in: speak into the
microphone, or type in the command bar. The reply is spoken through the existing
Python audio pipeline and shown in the interface.

The desktop shell has been migrated to a **local web interface** built with
React, TypeScript, Tailwind CSS and Vite. The Python application behind it is
unchanged in the ways that matter: **Gemini Live remains the only conversation
engine**, the tool registry and execution system are the same, and the system
prompt, settings, voice pipeline and desktop integrations still live in Python.

```
Browser
  React + TypeScript + Tailwind + Canvas halo
        │  HTTP /api  +  WebSocket /ws  (127.0.0.1 only)
        ▼
FastAPI boundary  (web/server.py)
        │  WebUI adapter  (web/ui.py)
        ▼
main.JarvisLive  ──  Gemini Live
        │
        ├── actions/tool_calling.py  (the one tool registry + discovery)
        ├── actions/                 (all tool modules live directly here)
        ├── actions/research_tool.py (deep multi-source research + report)
        ├── Skills/Engine/           (persistent Skill discovery + activation)
        ├── Skills/Builtin|Installed (Skill packages on disk)
        ├── actions/youtube_player_tool.py  (YouTube discovery + in-app player)
        ├── core/audio_devices.py    (microphone, speakers, diagnostics)
        ├── config/__init__.py       (the one settings store)
        └── core/prompt.txt          (unchanged system prompt)
```


There is no second conversation engine, no duplicate tool registry, and no
second settings store. The browser never receives provider secrets.

---

## Install

Requirements: **Python 3.11+**. Windows 11 is the primary target; macOS and
Linux use the same Python code paths. Node.js is **not** required for normal
installation because the production frontend assets are included in the wheel.

```powershell
python -m pip install .
jarvis
```

A prebuilt release wheel is also produced at
`dist/jarvis-1.0.0-py3-none-any.whl`; it can be installed with
`python -m pip install .\dist\jarvis-1.0.0-py3-none-any.whl`.

`pip install .` installs all runtime dependencies from `requirements.txt` and
registers the `jarvis` console command. The command works from any directory;
the process working directory is never used to locate configuration, databases,
Skills or frontend assets.

### If `jarvis` is not found

Python installs console scripts into its **Scripts** directory, which may not be
on PATH yet. Find the exact directory:

```powershell
python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
```

Then add it to your **user** PATH (replace the example with the printed path):

```powershell
$scripts = "C:\Users\<you>\AppData\Local\Programs\Python\Python313\Scripts"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
[Environment]::SetEnvironmentVariable("Path", "$userPath;$scripts", "User")
```

Open a **new** terminal and run `jarvis`. If you installed into a virtual
environment, activate it first; its `Scripts` directory is already on PATH.

### Upgrade

```powershell
python -m pip install --upgrade .
```

Existing configuration, API keys, databases, notifications and installed
Skills are preserved. The installer never overwrites user data.

### Source development

```powershell
python -m pip install -e ".[dev]"
cd frontend
npm install
npm run build
cd ..
python main.py
```

Development mode with Vite hot reload:

```powershell
python main.py --dev
```

`python main.py` and `jarvis` use the same `web/launch.py` startup path. If the
preferred port `8765` is occupied, the launcher selects the next free loopback
port and prints the actual address. The server binds to `127.0.0.1` only.

### First launch and data location

On first run, paste a **Gemini API key** into the browser setup screen. A free key
is available from Google AI Studio. That is the only required credential.

| Install type | Configuration / databases / installed Skills |
| --- | --- |
| Source checkout | `./config/` and `./Skills/` in the repository |
| `pip install .` on Windows | `%LOCALAPPDATA%\Jarvis\config` and `%LOCALAPPDATA%\Jarvis\Skills` |
| `pip install .` on macOS | `~/Library/Application Support/Jarvis/` |
| `pip install .` on Linux | `$XDG_DATA_HOME/jarvis` or `~/.local/share/jarvis` |

`JARVIS_DATA_DIR` overrides the user-data root for portable/deployed installs.
SIP credentials may live in that data directory's `.env`; see `.env.example`.

References below to `config/api_keys.json` mean that configuration file in the
applicable location from the table above — the repository copy in a source
checkout, or the user-data directory when installed.

---

## What is in it

| Part | What it does |
| --- | --- |
| **Web UI** | A minimal dashboard around one docked reactor: module rail, conversation, monitors, tool bus, Settings |
| **Voice in** | The existing Python microphone stream, optional push-to-talk |
| **Voice out** | The existing Gemini Live PCM playback, with the reactor following real output levels |
| **Engine** | Gemini Live — audio in, audio out, transcriptions, one session |
| **Tools** | The existing registry: apps, files, folders, websites, browser tabs, screen understanding, YouTube, and SIP telephone calls |
| **Telephony** | TLS SIP registration, incoming/outgoing calls, G.711 RTP, call controls, call history, and caller audio routed through the existing Gemini conversation |
| **YouTube** | Autonomous YouTube search and an in-app IFrame player panel (no external tab, no search UI) |
| **Settings** | Identity, accent, voice, audio devices, behavior, turn detection, API key |

The interface is a deliberately small dashboard built around one fixed central
reactor. A single top bar carries the name, local clock, link state and settings.
A narrow icon rail opens the module sheets; the centre holds the reactor, the
conversation and the composer. Nothing else competes for attention.

Navigation is direct: click a module icon in the rail, or use the mobile drawer
on narrow screens. **Memory**, **Skills** and the action bays open as sheets over
the dashboard, and the rail stays available so switching modules never requires
closing the current one. The reactor stays docked in the centre; it is not
draggable, resizable, detachable or moved automatically.

All built-in tools live directly in `actions/` and appear together under
**Core Tools** in the **Actions** bay. Click the folder heading to enable or
disable every tool, or click an individual tool card to toggle just that tool.
Selections are saved in `config/api_keys.json` and applied on the reconnection
they trigger. Discovery, imports, registered actions and saved selections follow
the tool's stable file-stem name, so moving a tool between a category folder and
the root keeps working without editing code. The Skills sheet is backed by a real
Skills Engine. The Agents, Plugins, Connectors, MCP Servers and GitHub
Repositories bays remain honest placeholders because those external packages do
not exist in this build.

### Surface inventory

- **Memory** — search, inspect, edit, delete, import/export and maintenance.
- **Actions** — tool discovery, search and folder/tool toggles.
- **Conversation** — the full transcript and system log, with the composer docked
  beneath it.
- **Settings** — the same settings sections and backend writes as before.
- **Agents**, **Plugins**, **Connectors**, **MCP Servers**,
  **GitHub Repositories** — the existing module bays and their real scan state.
- **Skills** — the real Skills Engine: installed packages, source/version/
  category, enabled state, live active Skills with activation time, manifest and
  instruction inspection, references and package details.
- **YouTube** — the existing IFrame player, opened automatically when a video
  starts and closed when playback stops.
- **Research** — a dedicated, scrollable deep-research panel with live progress,
  key findings, supporting evidence, statistics, conflicts, uncertainty,
  conclusions and clickable cited sources. It opens automatically when the
  `research` tool starts.

Panels use direct UI actions only. The assistant does not simulate clicks or
move windows; it supplies real data and media state.

### Actions

The 14 built-in tool modules live directly in `actions/` and are exposed to the
model without category subfolders:

- `list_apps`, `open_app`, `list_running_apps`, `close_app`,
  `search_files`, `open_file`, `open_folder`, `open_containing_folder`,
  `close_file`, `file_control` (create, rename, move, copy, delete, find, list,
  plus approval-gated write/append/replace_content/remove_content/
  clear_content editing), `read_document` (local, bounded reading of text,
  markup, Office, OpenDocument, PDF and EPUB files),
  `system_control` (system info, processes, brightness, volume, settings,
  workstation lock and sleep)
- `reminders` — persistent reminders and timers with built-in notifications;
  no dedicated panel, managed through natural language and the existing
  notification area
- `open_web`, `close_web`, `web_fetch` (HTTP web search and page reading — no browser)
- `research` — deep multi-source research with cross-referencing, credibility
  scoring, contradiction analysis and a live Research panel
- `screen_understanding`
- `youtube_player` (autonomous search, ranking and the in-app player panel).

The model also has core subsystem commands that are never shown as Actions-bay
tool cards: long-term memory, the Skills Engine, and `shutdown_jarvis` (process
shutdown).

Opening is app-first. Files and folders are searched locally. Executables,
installers, scripts and shortcuts require an explicit approval in the Jarvis
interface, not a model-supplied confirmation flag. Tool progress and failures
are reported from the real tool events; the interface never marks an operation
successful because a request was sent.

### Skills

`Skills/` is a first-class part of Jarvis, not a markdown loader. It contains a
filesystem-backed engine and seven maintained packages:

- `frontend-design`
- `web-development`
- `threejs-development`
- `web-research`
- `python-development`
- `code-review`
- `ui-ux-design`

Each package has a validated `manifest.yaml` and a `SKILL.md`; optional
`references/` and `resources/` are loaded only when the model asks for a specific
document. Discovery reads manifests only, activation loads `SKILL.md`, and
reference reads are individual and on demand.

The model decides whether a Skill is useful. Jarvis exposes `list_skills`,
`activate_skill`, and `read_skill_reference` through the existing tool registry;
there is no keyword router, regex classification or mandatory activation. One,
several or no Skills may be active for a task, and another Skill can be activated
mid-task. Activation is conversation-scoped: it is cleared at the conversation
boundary and never persisted across restarts.

Installed packages live in `Skills/Installed/`; built-ins live in
`Skills/Builtin/`. Enable/disable state is stored separately in
`Skills/.state.json` and never deletes a package. Built-in Skills cannot be
uninstalled. The Skills sheet can enable, disable, rescan, inspect manifests and
instructions, preview references and remove only user-installed packages.

Skill instructions are subordinate guidance. They never override the core system
prompt, permission rules or tool confirmation requirements, and manifest tool
lists grant nothing. See `Skills/README.md` for the package format and security
model.

### Deep research

Ask for a comparison, landscape scan, evidence review or fact-check and Jarvis
can call the `research` tool instead of a simple web lookup. It plans several
search angles, ranks candidates, reads multiple independent pages and builds a
structured report from extracted text only.

The report separates cross-referenced findings from single-source material,
surfaces possible contradictions, includes numeric evidence and explains source
credibility with a transparent heuristic. It never invents a statistic, citation
or source: every finding carries the fetched source ids and URLs it came from.
Failed searches, inaccessible pages, duplicate/near-duplicate pages and thin
evidence bases are recorded in the report's limitations.

The **Research** panel opens automatically when a job starts, shows live
phase/progress updates, and keeps the full report in its own scrollable panel so
the conversation remains readable. Source entries are clickable links with
domain, publication date where supplied, credibility label and score.
`web_fetch` remains the quick, ordinary lookup path; `research` is the
orchestrated multi-source workflow.

When the user asks to save a report, pass a new Markdown path in `save_path` on
the `research` call. The completed report is written directly from memory and
verified byte-for-byte; it is never copied through the model context. An
existing file is not overwritten, and the result reports `saved` or
`save_error` explicitly.

### Documents

`read_document` reads a document locally and returns bounded text plus
structure and metadata. It never uploads the file, executes macros or opens a
browser. Format detection uses both the extension and the file signature.

| Category | Formats |
| --- | --- |
| Text and markup | TXT, MD, LOG, RTF, HTML, XML, JSON, YAML |
| Office | DOCX, XLSX, PPTX, XLS; legacy DOC/PPT with an installed LibreOffice converter |
| PDF and ebooks | PDF, EPUB |
| OpenDocument | ODT, ODS, ODP |
| Tabular | CSV, TSV |

PDF text is returned page by page and empty pages are reported; scanned PDFs
are not pretended to be read because OCR is not installed. Spreadsheets report
sheet names and rows, presentations preserve slide order, and EPUB follows the
spine's chapter order. Limits (`max_chars`, `max_pages`, `max_rows`) and an
extraction deadline keep a large or malformed document from flooding the
conversation.

`file_control` can edit existing content only after a single-use approval in
the Jarvis interface. Plain-text edits preserve the existing encoding and line
endings. Structured editing is format-aware: DOCX paragraphs/tables, XLSX
cells, PPTX text, CSV/TSV rows and cells, and valid JSON/YAML. PDF, EPUB, RTF,
legacy Office and OpenDocument files are read-only in this build. Binary Office
files are never written as plain text, and unsupported operations return a
clear error instead of corrupting the file.

The parsers are ordinary entries in `requirements.txt`: `python-docx`,
`openpyxl`, `python-pptx`, `pypdf`, `ebooklib`, `odfpy`, `striprtf`, `xlrd` and
`charset-normalizer`. A missing parser produces a `missing_dependency` error
with installation guidance rather than empty content. Legacy DOC/PPT reading
uses an installed LibreOffice `soffice` converter; it is not bundled.

### Reminders and timers

Reminders and timers are background capabilities exposed through the
`reminders` tool. There is no separate Reminders sidebar entry or panel:
create and manage them by speaking to Jarvis, and due events appear in the
existing dashboard notification area.

The scheduler persists reminders in SQLite and restores pending work after a
restart. It uses UTC for storage and the system IANA timezone for daily,
weekly, monthly and custom recurrence, including daylight-saving transitions.
Monthly rules for missing dates (for example day 31 in February) use the last
valid day. A central scheduler thread handles all due work; it does not start a
sleeping coroutine per reminder.

Notifications are also persisted, with unread/read/dismissed status and
delivery results. The dashboard notification is authoritative and always
emitted; a desktop notification is attempted where the OS supports it and is
recorded as sent, unsupported or failed. Closing Jarvis does not promise an
immediate notification while it is closed: missed reminders and timers are
reported when Jarvis starts again. Notifications never execute tools,
commands or file operations.

### Docked reactor

The central arc reactor is a real-time Three.js scene: a gold mechanical
assembly with segmented armour rings, bronze spokes, bolt rings and a glowing
blue spherical core, rendered over a warm dark/gold interface. The whole assembly
hovers gently, throws a soft warm glow, and is surrounded by gold and blue
particle stars; the particles use fixed buffers and a single draw call per layer,
and rendering pauses while the tab is inactive, so the animation stays smooth. An
audio envelope makes the core glow and expand while Jarvis speaks.

The reactor is fixed in the centre of the stage. It does not follow the pointer,
cannot be dragged or resized, is never moved automatically to avoid a panel, and
has no detached desktop window. When the YouTube panel opens, the reactor is
suspended and returns when playback closes. This keeps the dashboard predictable
and keeps the assistant out of window management.

### Telephone calls over SIP

Jarvis has a real SIP/VoIP endpoint in `core/telephony.py`. It registers over
TLS to the Linphone Free SIP Service, listens for incoming calls, places
outgoing calls, answers/rejects/hangs up, mutes outbound audio, tracks call
state and duration, reconnects after network loss, and keeps a bounded call
history. The signalling implementation is a small RFC 3261 user agent with
digest authentication (MD5/SHA-256, `qop=auth`), RFC 5626 outbound contact
parameters and CRLF keepalives, SDP negotiation and G.711 RTP (PCMU/PCMA).
Linphone mobile answers with `RTP/SAVPF` and SDES crypto, so media is protected
with SRTP `AES_CM_128_HMAC_SHA1_80` through `pylibsrtp`; without SRTP the phone
rejects a plain RTP offer with `488 Not Acceptable Here`. It does not use
Docker and has no SIP daemon dependency.

Credentials live in the git-ignored project-root `.env` (copy
`.env.example` once). The non-secret defaults are already filled in; only the
password is blank:

```dotenv
SIP_USERNAME=jarvis2011
SIP_PASSWORD=
SIP_DOMAIN=sip.linphone.org
SIP_TRANSPORT=tls
SIP_DISPLAY_NAME=Jarvis
SIP_AUTO_ANSWER=true
```

The loader is built into `config/__init__.py`, runs once at import, never
prints values, and lets real process environment variables override the file.
`SIP_PASSWORD` is also accepted from `JARVIS_SIP_PASSWORD`; the rest have
`JARVIS_SIP_*` overrides for deployment. The password is never written to
`api_keys.json`, logs, status output or tool results. Registration refuses to
start until required settings validate, and a missing password produces an
actionable message naming `.env` and `SIP_PASSWORD` without echoing anything.

```powershell
# after filling SIP_PASSWORD in .env
python tools/telephony_check.py register
```

Optional environment overrides: `JARVIS_SIP_PORT`, `JARVIS_SIP_SRTP`
(default on), `SIP_AUTO_ANSWER`/`JARVIS_SIP_AUTO_ANSWER`, `JARVIS_SIP_STUN`,
`JARVIS_ENV_FILE`. With auto-answer on, an incoming call is answered only
while the Gemini voice session is connected; otherwise it keeps ringing. Install `requirements.txt` so the `pylibsrtp` Windows
wheel is present.

Diagnostics are opt-in and never print audio or credentials:
`JARVIS_SIP_TRACE=1` logs SIP method/status lines, SDP, RTP targets and first
packet counters; `JARVIS_PHONE_TRACE=1` logs the Gemini bridge's caller-RX and
Jarvis-TX batch counts, levels and queue depth.

`config/contacts.json` maps spoken names to SIP addresses; it ships with
`Amirxon` → `sip:amirxon2011@sip.linphone.org` and `Jarvis` →
`sip:jarvis2011@sip.linphone.org`. The model can resolve those names, a full
`sip:user@domain` URI, or a bare number on the configured domain. Unknown names
are an error rather than an invented call.

The action surface registered in `actions/tool_calling.py` is `call`,
`answer_call`, `reject_call`, `hangup`, `mute_call` (with `muted=false`),
`unmute_call`, `get_call_status` and `get_call_history`. Incoming calls publish
`telephony.incoming` / `telephony.call` events to the existing UI event hub and
write a `SYS:` notice. Auto-answer is controlled by `SIP_AUTO_ANSWER`: when on,
the call is answered automatically as soon as the inbound RTP endpoint is ready
and the Gemini voice session is connected. No outgoing call is ever started
without an explicit user request in that turn. Saying “hang up” on the call
(or asking through the typed composer) invokes the existing `hangup` action.

When a call becomes active, the RTP receive path feeds 16 kHz caller PCM into
the same `out_queue` used by the microphone; Gemini's existing VAD, STT,
memory and interruption logic therefore apply unchanged. Gemini's 24 kHz reply
PCM is sent to the call through a paced, bounded RTP queue; the local room
microphone and speaker are bypassed for the duration of the call. The phone
path has its own `EchoGuard`: while Jarvis is speaking, earpiece audio picked up
by the caller's microphone is not forwarded to Gemini, so it cannot interrupt
long replies, while genuine caller speech still triggers the barge-in path.
The turn is not marked finished until the paced RTP sender has drained. A
caller speaking over a reply follows the same barge-in path: queued Gemini
audio is discarded, the RTP sender is flushed immediately, and the pre-roll is
forwarded to Gemini. `tools/telephony_check.py` is the standalone real-network check
(`register`, `call`, `serve`) and has an optional local microphone/speaker
loopback for testing before the Gemini bridge is involved.

Linphone's infrastructure was verified directly: `sip.linphone.org:5061` accepts
TLS 1.3 with a valid certificate, answers `OPTIONS` with `200 OK`, and answers
an unauthenticated `REGISTER` with a Flexisip `401` digest challenge
(`algorithm=MD5`, `qop=auth`). With the account credentials in `.env`, the real
`REGISTER` completes with `200 Registration successful`; the standalone
`register` check prints `REGISTRATION OK`. Outgoing calls now ring Linphone
mobile after the SRTP offer fix; two-way audio should be checked on the first
live answered call. Incoming and outgoing signalling plus SRTP RTP are covered
by offline protocol/loopback tests. The password stays in `.env` (git-ignored)
or `JARVIS_SIP_PASSWORD`; the repository never contains it.

### YouTube in Jarvis

Ask for a video and Jarvis does the rest: it searches the YouTube Data API,
validates that candidates are public and embeddable, ranks them by query
relevance, title/description match, preferred channel, duration, popularity and
quality signals, then loads the winner into its own player panel. There is no
search bar and no result list, and no external YouTube tab is opened.

The player lives in a **YouTube panel** inside the stage. It appears when
playback starts, shows the real title/channel/state, and supports voice control
of pause, resume, seeking, volume, mute, fullscreen and closing. "Play another
video about X" reuses the same player and switches the video. The backend and
browser correlate every command with a unique request id and a bounded timeout; a
dispatched command is never reported as successful until the real IFrame player
answers.

The IFrame API script and player are created lazily: nothing YouTube-related is
downloaded until a video has actually been selected.

Configure the Data API key in `config/api_keys.json` (or the environment):

```json
{
  "youtube_api_key": "YOUR_YOUTUBE_DATA_API_V3_KEY",
  "youtube_region_code": "US"
}
```

`youtube_region_code` is optional and only affects region-restriction metadata.
The key stays on the Python side; the browser never receives it. If the key is
missing, searching reports an actionable configuration error. Direct video IDs
or URLs can still be played without a key (title/channel are then read from the
player itself).


---

## Talking to it

- **Just speak.** The microphone is live as soon as the Gemini Live session
  connects.
- **Push-to-talk (optional).** Turn on `PTT` in Settings and hold
  **Ctrl+Space**, or hold the **HOLD PTT** button in the command bar.
- **Stop it mid-reply** with `STOP`, or start speaking — the microphone is
  reopened when the reply ends.
- **Typing** works at any time. Press Enter to send, Shift+Enter for a newline.
- **Shutting down Jarvis itself** — say “Jarvis, shut down”, “close yourself” or
  “turn yourself off”. The browser shows a power-down screen, Jarvis closes its
  own Brave tab when it can identify it, and the Python server exits. When the
  `jarvis` command owns the cmd window, that console closes too. This is a
  core lifecycle command, not an Actions-bay tool. “Close YouTube” and “close
  Brave” are intentionally different requests.

The state on the reactor is the real backend state: `LISTENING`, `THINKING`,
`SPEAKING`, `MUTED`, or `OFFLINE`. The reactor status also reports how many
tools are executing. Tool output and system events remain available in the
conversation panel's LOG tab.

The conversation is kept in memory for the current backend process, just like
the desktop activity log. A browser refresh restores it from the running
backend; restarting Python starts a fresh conversation. The original project had
no persistent memory store, so this migration does not add one.

---

## Settings

Open Settings from the gear at the bottom of the module rail. You can also say
“open settings” or “open settings to the audio section”.

- **Identity** — assistant name, operator name and Gemini prebuilt voice.
  Changing the voice reconnects with a fresh conversation, because the voice is
  fixed when a Live session connects.
- **Appearance** — accent presets and a custom `#RRGGBB` colour. The saved
  accent is written through `config` and applied immediately. The particle core
  remains Jarvis gold.
- **Audio I/O** — microphone and speaker selection by name (names survive USB
  changes), output volume where the host exposes it, and the existing input test
  / output tone diagnostics. Applying a device reconnects and keeps the
  conversation.
- **Behavior** — proactive audio, thinking mode and push-to-talk. These use the
  existing config switches and rebuild the Live link when required.
- **Link profile** — server VAD silence/prefix windows and start/end
  sensitivity.
- **Gemini link** — masked key status, key update and a manual reconnect.

The full API key is never returned to the browser. Settings responses only
include a four-character masked tail. The key is stored in the existing
`config/api_keys.json`, which is git-ignored.

---

## Files

```
setup.py                   setuptools metadata + jarvis console entry point
pyproject.toml             build backend declaration
MANIFEST.in                source/wheel resource inclusion rules
requirements.txt           runtime dependencies (also used by setup.py)
requirements-dev.txt       pytest, Ruff and Playwright only
CHANGELOG.md               release history
CONTRIBUTING.md            development, testing and contribution rules
LICENSE                    MIT license
THIRD_PARTY_LICENSES.md    direct dependency and bundled-asset license audit
main.py                    Gemini Live session loop + entrypoint
web/                       local HTTP/WebSocket boundary
  server.py                FastAPI routes, static assets, security checks
  runtime.py               settings/tools/session facade
  ui.py                    JarvisLive-compatible WebUI adapter
  events.py                thread-safe event fan-out
  launch.py                cross-platform launcher used by `jarvis`
  weather.py               cached Tashkent weather (Open-Meteo)
  metrics.py               CPU/memory/network/GPU telemetry
frontend/                  React + TypeScript + Tailwind (Vite)
  dist/                    packaged production assets
  src/App.tsx              dashboard shell: rail, stage, conversation, monitors
  src/components/          reactor stage, sidebar, conversation, dialogs,
                            memory, skills, research and monitor panels
  src/components/youtube/  lazy IFrame player, controller, stage panel, types
  src/components/ShutdownScreen.tsx  power-down overlay
  src/hooks/useAze.ts      typed state + backend client orchestration
  src/lib/api.ts           HTTP client
  src/lib/ws.ts            reconnecting WebSocket client
  src/lib/theme.ts         accent token application
  src/types.ts             explicit communication protocol types
  src/test/setup.ts        jsdom test cleanup
config/__init__.py         every setting, read and written in one place
config/api_keys.json       created on first run (git-ignored)
config/contacts.json       spoken-name to SIP-address contact map
.env                       local SIP credentials (git-ignored)
.env.example               placeholder-only credential templatecore/gemini.py             Live-model ladder and quota fallback
core/lifecycle.py          core shutdown command and process handlers
core/audio_devices.py      device discovery, testing, volume
core/telephony.py          TLS SIP registration, calls, RTP, call history
core/echo.py               keeps the speaker out of the microphone
core/hotkey.py             global/windowed push-to-talk
actions/                   tool registry + all built-in tool modules (flat)
  tool_calling.py          declarations, execution, discovery, selection
  open_app_tool.py         app launch and close
  open_file_tool.py        file/folder search, open, reveal, close
  file_control_tool.py     create, rename, move, copy, delete, find, list,
                            approval-gated content editing
  read_document_tool.py    local document reading (text, Office, PDF, EPUB)
  reminders_tool.py        reminders, timers and notifications tool surface
  system_control_tool.py   system info, processes, brightness, volume,
                            workstation lock and sleep
  open_web_tool.py         websites and Brave tabs
  web_fetch_tool.py        direct HTTP search and reading
  research_tool.py         deep multi-source research orchestration + report
  screen_understanding_tool.py
  screenshot_tool.py       explicit one-shot PNG screenshots
  zip_tool.py              safe ZIP create/inspect/extract/modify tool
  youtube_player_tool.py   YouTube search, ranking, player bridge + tool
  phone_call_tool.py       call/answer/reject/hangup/mute/status/history actions
tools/telephony_check.py   standalone real-network SIP register/call/serve check
Skills/                    Skills Engine + persistent packages
  Engine/                  discovery, registry, validation, loader,
                            activation, context, models, LLM tools
  Builtin/                 seven maintained Skill packages
  Installed/               user-installed Skill packages
  README.md                package format and security model
tests/                     existing tests + web boundary/browser tests
  test_skills_engine.py    discovery, validation, activation, persistence,
                            context lifecycle, tool integration, panel sync
  test_youtube_player_tool.py  discovery, ranking, dispatch, timeout, errors
  test_youtube_web.py          WebSocket correlation, disconnect, bootstrap, CSP
```

---

## Tests

Existing suite plus the new backend boundary tests:

```powershell
python -m unittest discover -s tests -v
```

Frontend typecheck, unit tests and production build:

```powershell
cd frontend
npm test
npm run build
cd ..
```

The YouTube tests mock the Data API and the browser, so they need no real
YouTube credentials. They cover discovery and ranking, argument validation,
request/response correlation, command timeouts, WebSocket disconnects, API
quota/auth errors, invalid video IDs, embeddability fallback, panel open/close,
video switching, player controls, fullscreen fallback, tab close and shutdown
handling, and component cleanup.

The Skills tests use temporary package directories and a temporary enabled-state
file. They cover manifest parsing, malformed and duplicate packages, path
traversal, discovery without reading `SKILL.md`, activation, multiple and dynamic
activation, duplicate prevention, reference loading, disabled Skills,
persistence across engine restarts, conversation-scoped context cleanup, the
three LLM tools through the existing registry, built-in uninstall protection, and
HTTP/WebSocket panel synchronisation.

The research tests mock search and page fetching at the tool boundary. They
cover multi-angle planning, real-text report construction, cross-source
corroboration, credibility scoring, statistics, contradictions, uncertainty,
duplicate URL/page exclusion, partial fetch failures, honest search failures,
tool registration, HTTP state and WebSocket progress events. The frontend suite
covers the running and completed report panels.

The browser smoke test is opt-in because it starts a local server and Chromium.
It uses a fake engine, so it never contacts Gemini or opens a microphone:

```powershell
$env:AZE_UI_TESTS="1"
python -m unittest tests.test_web_ui_smoke -v
```

It covers the halo/clock/weather layout, a message round trip, streaming
assistant text, tool activity, the system log, browser refresh, the responsive
mobile layout and drawer, the settings dialog, and the first-run setup overlay.
The launcher test starts the real production server with an isolated keyless
config and verifies that the built UI is served.

---

## Troubleshooting

**“FRONTEND ASSETS NOT FOUND.”** The Python backend is running but
`frontend/dist/index.html` is missing. Run:

```powershell
cd frontend
npm install
npm run build
cd ..
python main.py
```

**The browser opens on a different port.** That is intentional: the launcher
uses the first free loopback port and prints it. Nothing is hardcoded.

**“LINK LOST / RECONNECTING.”** The WebSocket dropped. The client reconnects
with backoff; a browser refresh asks the backend for a fresh snapshot. The
Python session is unaffected.

**“Jarvis is not connected yet.”** The Gemini Live session is still connecting,
offline, or waiting for a key. Check the system log and the `LINK` chip.

**“I can’t hear you.”** Open Settings → Audio I/O, pick the microphone, press
`TEST INPUT`. A flat meter means the OS is muting the device or the wrong input
was chosen. `TEST OUTPUT` plays a real tone through the selected speakers.

**“Brave could not be opened.”** Install Brave, or add
`"brave_path": "C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"`
to `config/api_keys.json` (or set the `BRAVE_PATH` environment variable). Jarvis
reports the launch error instead of silently using a different browser.

**“API key invalid.”** The setup screen reappears; paste a valid key. A VPN or
regional block can also surface as a connection error — the log names it and
retries with backoff.

**“YouTube search needs a YouTube Data API v3 key.”** Add
`"youtube_api_key": "..."` to `config/api_keys.json` or set the
`YOUTUBE_API_KEY` environment variable, then restart Jarvis. Enable the
**YouTube Data API v3** for that Google Cloud project. Quota exhaustion is
reported separately; the key is never sent to the browser.

**The YouTube panel does not go OS-fullscreen from a voice command.** Browsers
only allow native fullscreen from a direct click. Jarvis automatically switches
to its own fullscreen theater mode in that case; the panel's fullscreen button
still attempts native fullscreen first.

**`--dev` says npm or node_modules is missing.** Install Node.js, then run
`cd frontend && npm install`.

---

## Security / local boundary

- The server binds to `127.0.0.1` by default, never to every interface.
- Host and Origin are checked on HTTP and WebSocket requests. Cross-site
  browser requests are rejected; mutations require `application/json`.
- The API exposes settings, conversation state and tool metadata, but never the
  Gemini key, Brave path or arbitrary filesystem access.
- Skills use validated relative paths inside their own package. Directory
  traversal, absolute paths, drive-qualified paths and symlink escapes are
  rejected; scripts are never executed automatically.
- Skill instructions are subordinate to the core system prompt and existing
  permission rules. Manifest tool requirements are advisory and grant nothing.
- The browser cannot invoke shell commands or arbitrary filesystem operations.
  The only action path is the existing Gemini tool loop in Python.
- Destructive and high-impact actions require a single-use approval granted by
  the trusted dashboard; model arguments, tokens and `confirm` flags cannot
  authorize an operation. An approval resumes the exact blocked call once; the
  model is not asked to reconstruct it, and a rejected, expired, revoked or
  stale request never executes.
- The Content-Security-Policy allows only same-origin assets and local
  WebSockets, plus the official YouTube IFrame API/player origins needed for the
  in-app video panel. Third-party HTML is never placed in Jarvis's trusted
  document.
- Do not expose the port through a tunnel or reverse proxy. It is a single-user
  local control plane.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, tests, tool
conventions, security requirements and the pull-request checklist. Contributions
are accepted under the repository's MIT license.

---

## License

Jarvis is distributed under the MIT License; see [LICENSE](LICENSE).

Copyright (c) 2026 Amirxon Gandjayev.

The MIT license covers original Jarvis code only. Third-party Python packages,
frontend libraries, bundled assets and external services retain their own
licenses and terms. See [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) for
the audit, including redistribution concerns requiring an owner/legal decision.