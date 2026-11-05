# Jarvis Skills Engine v1.0.0

The Skills Engine gives Jarvis persistent, specialised instruction packages that
the configured LLM can discover, inspect and activate when a task would benefit
from deeper expertise. A Skill is an instruction package, not a plugin that
grants permissions and not a separate model.

## Layout

```
Skills/
├── Engine/                 runtime implementation
│   ├── engine.py           lifecycle + public API
│   ├── discovery.py        Builtin/ and Installed/ scan
│   ├── registry.py         runtime catalog + enabled state
│   ├── loader.py           safe, cached instruction/reference reads
│   ├── activation.py       activation policy
│   ├── context_manager.py  conversation-scoped active instructions
│   ├── validator.py        manifest/package validation
│   ├── tools.py            LLM-facing tools
│   └── models.py           shared data structures
├── Builtin/                seven maintained packages
├── Installed/              user-installed packages
├── .state.json             disabled ids only (created on first change)
└── README.md
```

`Builtin/` and `Installed/` are scanned on startup. The filesystem is the source
of truth: nothing in the conversation, the browser or process memory is required
for a Skill to survive a restart.

## Package format

Every package is a directory directly under `Builtin/` or `Installed/`:

```
my-skill/
├── manifest.yaml
├── SKILL.md
├── references/             optional
├── resources/              optional
└── scripts/                optional, never executed automatically
```

`manifest.yaml` is required. It is parsed with `yaml.safe_load` and validated
before the package enters the registry.

```yaml
id: my-skill
name: My Skill
version: 1.0.0
description: What the Skill is for and when it helps.
author: Your Name
license: MIT

metadata:
  category: development
  tags:
    - example
    - workflow

requirements:
  tools:
    - file_control
    - web_fetch

entrypoint: SKILL.md

# Optional declarations. Paths must stay inside references/ and resources/.
references:
  - references/checklist.md
resources:
  - resources/template.css
```

`requirements.tools` is **advisory metadata**. It never grants a tool, bypasses
a permission, enables a disabled action or executes anything. The engine checks
actual tool availability and reports missing tools as warnings.

`SKILL.md` is the Level 2 instruction document. Keep it focused on specialised
expertise, not a copy of Jarvis's general system prompt. Optional references are
read only when the model asks for them.

## Progressive disclosure

| Level | When | What is read |
| --- | --- | --- |
| 1 — discovery | engine startup / refresh | `manifest.yaml` only |
| 2 — activation | model calls `activate_skill` | `SKILL.md` |
| 3 — resources | model calls `read_skill_reference` | one requested reference/resource |

Discovery never loads every `SKILL.md`. Activation never loads every reference.
The model decides which Skills and references are relevant.

## LLM tools

The engine registers three tools through the existing tool registry:

- `list_skills` — ids, names, descriptions, versions, categories, tags,
  availability, enabled and active state; no instructions.
- `activate_skill(skill_id)` — validates the id, enabled state and package,
  loads `SKILL.md`, marks it active for the current conversation and returns the
  instructions. Re-activating an already active Skill does not duplicate them.
- `read_skill_reference(skill_id, path)` — reads one listed reference/resource
  for an already active Skill.

There is no keyword router, regex classifier, mandatory activation or separate
Skill-selection model. The configured LLM decides whether a Skill would
materially improve the task and may decide that none is needed.

Multiple Skills can be active at once. Activation during an ongoing task is
supported. Active state is conversation-scoped: it is cleared when the
conversation boundary changes (for example a voice change that starts a fresh
conversation), and it is never persisted across process restarts.

## Persistence and updates

- Built-in and installed packages live on disk and are rediscovered at startup.
- Disabling a Skill writes only its id to `Skills/.state.json`; the package is
  untouched.
- `Builtin/` is maintained independently of `Installed/`. User packages are
  never rewritten by built-in updates.
- Uninstall is available only for packages whose source is `installed`, requires
  an explicit confirmation, and refuses paths outside `Installed/`.
- To install manually, place a validated package directory in `Installed/` and
  refresh discovery from the Skills widget. Importers and marketplaces are out
  of scope for v1.0.0.

## Security model

- Manifest shape, required fields, id format, semver, entrypoint and declared
  asset paths are validated.
- Directory traversal, absolute paths, drive-qualified paths and symlink escapes
  are rejected by the loader.
- Only text references/resources can be read. Scripts are listed as package
  assets but are never executed by the engine.
- Skill instructions are injected as subordinate guidance. They cannot override
  Jarvis's core system instructions, safety rules or permission system.
- A malformed Skill is reported and skipped; it cannot crash discovery or the
  application.
- The Skills Widget can enable/disable/refresh/inspect packages. It never
  receives a shell, arbitrary filesystem access or provider secrets.

## Widget

The existing Skills widget is connected to this engine through the local HTTP
API and WebSocket event hub. It shows installed Skills, source, version,
category, enabled state, active Skills with activation time, manifest metadata,
main instructions and available references/resources. Enable, disable, refresh
and inspect use the same backend state; updates arrive as `skills.updated`
events.

## Tests

```powershell
python -m unittest discover -s tests -v
```

Skills-specific coverage lives in `tests/test_skills_engine.py`.
